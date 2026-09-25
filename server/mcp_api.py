"""MCP 工具层：streamable-http 挂载 /mcp，17 个工具按角色校验。

挂载模式沿用 Agent Room mount_gateway（已验证）：
srv.streamable_http_app(stateless_http=True) → parent_app.mount，
session_manager.run() 包进父 lifespan。
"""
import contextlib
import json

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from .bus import bus_registry
from .core.db import db
from .core.message import Message
from .memory_hub import hub
from .orchestrator import (OrchestratorRegistry, _room_guard as _room_guard_err,
                           _subtask, _task, _subtasks)
from .registry import authenticate, authenticate_role, touch
from .workspace import fs_list, read_file, write_file

HOUSE_RULES = """【AgentDeck 房间使用约定】
1. 角色与工具：leader 专责 plan_draft / confirm_task / review_deliverable / task_status / list_members / broadcast_p0；executor 专责 claim_subtask / submit_deliverable / report_progress。通用：join_room / poll_messages / send_message / declare_status。
2. leader 流程：list_members 看成员技能 → plan_draft(goal, subtasks[{title,guidance,depends_on,assignee}], auto_confirm=true) 直接派发 → 收到交付后 review_deliverable 通过/打回 → 全部通过后 task_status 汇总。仅高风险任务才 auto_confirm=false 留人确认。
3. executor 流程：poll_messages 看到 dispatch 排产单 → claim_subtask 认领 → 干活，阶段进展用 report_progress 一句话上报（正在做什么/卡在哪）→ 产物 fs_write 落工作区 → submit_deliverable 交付。收到 priority=0 interrupt 立即停止并回确认。
4. 交付纪律：交付文本里声称写过的文件必须真的已 fs_write 落工作区，否则硬核验直接打回；claimed 超 15 分钟未交付自动释放。
5. 打回后凭原认领身份重交（retry 超 2 次升级领导处置）。
6. 工作区路径：workspace/tasks/{task_id}/{subtask_id}/，写文件必须带 base_version 乐观锁，冲突时重读重写。
7. offline 前 declare_status(status="offline")。"""


def _err(e: Exception) -> str:
    return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def _ok(**kw) -> str:
    return json.dumps({"ok": True, **kw}, ensure_ascii=False)


def _auth_from_args_or_headers(agent_id: str | None, token: str | None,
                               ctx) -> tuple[str, str]:
    """双因子取值：优先工具参数；缺省时回退 HTTP 头（X-Agent-Id / Authorization Bearer）。

    支持把 agent_id/token 放进 MCP 客户端 headers（config.json 的 headers 配置），
    Agent 调工具时就不必每次传身份参数。
    """
    if (not agent_id or not token) and ctx is not None:
        headers = ctx.headers or {}
        agent_id = agent_id or headers.get("x-agent-id")
        authz = headers.get("authorization", "")
        token = token or (authz[7:] if authz.lower().startswith("bearer ") else None)
    if not agent_id or not token:
        raise ValueError("缺少 agent_id/token（参数或 X-Agent-Id/Authorization 头）")
    return agent_id, token


def build_mcp_server() -> MCPServer:
    srv = MCPServer(
        name="agent-deck",
        version="0.1.0",
        instructions="AgentDeck：MCP 多 Agent 编排插件。leader 拆解/派发/验收，executor 认领/交付。先 join_room 报到。",
    )

    # ---------- 通用 ----------

    @srv.tool()
    async def join_room(agent_id: str = None, token: str = None,
                        room_id: str = "default", ctx: Context = None) -> str:
        """认证接入：校验令牌、置在线，返回身份卡（角色）与群规。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate(agent_id, token)
            touch(agent_id, "online")
            return _ok(agent_id=agent_id, name=agent["name"],
                       role=agent["role"],
                       skills=json.loads(agent.get("skills") or "[]"),
                       room_id=agent["room_id"],
                       status="online", rules=HOUSE_RULES)
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def poll_messages(cursor: int = 0, limit: int = 50,
                            room_id: str = "default",
                            agent_id: str = None, token: str = None,
                            ctx: Context = None) -> str:
        """拉取增量消息（cursor 上次返回的 next_cursor，首次 0）。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate(agent_id, token)
            touch(agent_id)
            bus = bus_registry.get(agent["room_id"])
            return json.dumps({"ok": True, **bus.poll(cursor, limit)},
                              ensure_ascii=False)
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def send_message(text: str, mentions: list[str] | None = None,
                           msg_type: str = "chat",
                           agent_id: str = None, token: str = None,
                           ctx: Context = None) -> str:
        """发送日志/进度消息（msg_type: chat|deliver；deliver 触发所认领子任务的交付登记——正常交付请用 submit_deliverable）。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate(agent_id, token)
            touch(agent_id)
            if msg_type not in ("chat", "deliver"):
                return _err(ValueError("msg_type 仅支持 chat|deliver"))
            sender_kind = "leader" if agent["role"] == "leader" else "agent"
            bus = bus_registry.get(agent["room_id"])
            m = await bus.publish(Message(
                room_id=agent["room_id"], type=msg_type, sender_kind=sender_kind,
                sender_id=agent_id, payload_text=text,
                mentions=mentions or []))
            return _ok(msg_id=m.msg_id, seq=m.seq, type=msg_type)
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def declare_status(status: str, agent_id: str = None,
                             token: str = None, ctx: Context = None) -> str:
        """上报在线状态：online | busy | offline。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate(agent_id, token)
            if status not in ("online", "busy", "offline"):
                return _err(ValueError("status 仅支持 online|busy|offline"))
            touch(agent_id, status)
            return _ok(agent_id=agent_id, status=status)
        except Exception as e:
            return _err(e)

    # ---------- executor 专用 ----------

    @srv.tool()
    async def report_progress(subtask_id: str, text: str,
                              agent_id: str = None, token: str = None,
                              ctx: Context = None) -> str:
        """上报工作进度（一句话：正在做什么/卡在哪）。仅 executor，仅限自己认领的子任务。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate_role(agent_id, token, "executor")
            touch(agent_id)
            orch = OrchestratorRegistry.get(agent["room_id"])
            return json.dumps(await orch.report_progress(agent_id, subtask_id, text),
                              ensure_ascii=False)
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def fs_list(task_id: str, subtask_id: str, agent_id: str = None,
                      token: str = None, ctx: Context = None) -> str:
        """列出子任务工作区文件（路径/版本/作者）。通用。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate(agent_id, token)
            touch(agent_id)
            return _ok(files=fs_list(task_id, subtask_id))
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def fs_read(task_id: str, subtask_id: str, path: str,
                      agent_id: str = None, token: str = None,
                      ctx: Context = None) -> str:
        """读子任务工作区文件（返回内容与当前版本号）。通用。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate(agent_id, token)
            touch(agent_id)
            f = read_file(task_id, subtask_id, path)
            return _ok(path=f["path"], version=f["version"],
                       author=f["author"], content=f["content"])
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def fs_write(task_id: str, subtask_id: str, path: str, content: str,
                       base_version: int = None, agent_id: str = None,
                       token: str = None, ctx: Context = None) -> str:
        """写子任务工作区文件（乐观锁：base_version 不匹配返回最新版本号，重读重写）。

        交付纪律：交付文本里声称的文件必须先 fs_write 落工作区。通用。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate(agent_id, token)
            touch(agent_id)
            return json.dumps(write_file(task_id, subtask_id, path, content,
                                         agent_id, base_version),
                              ensure_ascii=False)
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def claim_subtask(subtask_id: str, agent_id: str = None,
                            token: str = None, ctx: Context = None) -> str:
        """认领子任务（乐观锁，先到先得）。仅 executor。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate_role(agent_id, token, "executor")
            touch(agent_id, "busy")
            orch = OrchestratorRegistry.get(agent["room_id"])
            return json.dumps(await orch.claim(agent_id, subtask_id),
                              ensure_ascii=False)
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def submit_deliverable(subtask_id: str, deliverable: str,
                                 agent_id: str = None, token: str = None,
                                 ctx: Context = None) -> str:
        """交付子任务产物说明（触发真实性核验与领导验收）。仅 executor。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate_role(agent_id, token, "executor")
            touch(agent_id, "online")
            orch = OrchestratorRegistry.get(agent["room_id"])
            return json.dumps(await orch.submit(agent_id, subtask_id, deliverable),
                              ensure_ascii=False)
        except Exception as e:
            return _err(e)

    # ---------- leader 专用 ----------

    @srv.tool()
    async def plan_draft(goal: str, subtasks: list[dict],
                         auto_confirm: bool = False,
                         agent_id: str = None, token: str = None,
                         ctx: Context = None) -> str:
        """提交任务拆解方案并派发。subtasks: [{title, guidance?, depends_on?([seq,...]), assignee?(定向 agent_id)}]。

        auto_confirm=true 时免人工确认直接派发（默认建议 true，仅高风险任务留 false 等人确认）。
        定向派工：先 task_status/list_members 看成员技能，把子任务 assignee 设为对应 agent_id。
        仅 leader。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate_role(agent_id, token, "leader")
            touch(agent_id)
            orch = OrchestratorRegistry.get(agent["room_id"])
            return json.dumps(await orch.plan_draft(agent_id, goal, subtasks,
                                                    auto_confirm=auto_confirm),
                              ensure_ascii=False)
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def confirm_task(task_id: str, agent_id: str = None,
                           token: str = None, ctx: Context = None) -> str:
        """确认任务（也可由人类经 REST /api/tasks/{id}/confirm），确认后按依赖派发。仅 leader。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate_role(agent_id, token, "leader")
            touch(agent_id)
            orch = OrchestratorRegistry.get(agent["room_id"])
            return json.dumps(await orch.confirm_task(agent_id, task_id),
                              ensure_ascii=False)
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def abort_task(task_id: str, agent_id: str = None,
                         token: str = None, ctx: Context = None) -> str:
        """作废任务：未完成子任务全部释放，房间可开新任务。仅 leader。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate_role(agent_id, token, "leader")
            touch(agent_id)
            orch = OrchestratorRegistry.get(agent["room_id"])
            return json.dumps(await orch.abort_task(task_id),
                              ensure_ascii=False)
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def review_deliverable(subtask_id: str, accept: bool, reason: str,
                                 agent_id: str = None, token: str = None,
                                 ctx: Context = None) -> str:
        """验收交付物：accept=true 验收通过 / false 打回重做。仅 leader。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate_role(agent_id, token, "leader")
            touch(agent_id)
            orch = OrchestratorRegistry.get(agent["room_id"])
            sub = _subtask(subtask_id)
            if not sub:
                return _err(KeyError("子任务不存在"))
            if err := _room_guard_err(sub, agent["room_id"]):
                return _err(PermissionError(err))
            return json.dumps(
                await orch.review(subtask_id, accept=accept, reason=reason,
                                  reviewer=agent_id),
                ensure_ascii=False)
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def handle_escalated(subtask_id: str, action: str, reason: str,
                               agent_id: str = None, token: str = None,
                               ctx: Context = None) -> str:
        """升级处置（重试超限的子任务）：action=requeue 退回待领重派 / force_verify 强验收。仅 leader。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate_role(agent_id, token, "leader")
            touch(agent_id)
            orch = OrchestratorRegistry.get(agent["room_id"])
            return json.dumps(
                await orch.handle_escalated(subtask_id, action, reason,
                                            reviewer=agent_id),
                ensure_ascii=False)
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def task_status(task_id: str, agent_id: str = None,
                          token: str = None, ctx: Context = None) -> str:
        """查询任务全链路状态（任务 + 全部子任务）。仅 leader。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate_role(agent_id, token, "leader")
            touch(agent_id)
            task = _task(task_id)
            if not task or task["room_id"] != agent["room_id"]:
                return _err(KeyError("任务不存在"))
            subs = _subtasks(task_id)
            orch = OrchestratorRegistry.get(agent["room_id"])
            return _ok(task={k: task[k] for k in
                             ("task_id", "goal", "status", "leader_id")},
                       subtasks=[{k: s[k] for k in
                                  ("subtask_id", "seq", "title", "status",
                                   "claimant_id", "retry_count", "escalated",
                                   "assignee")}
                                 for s in subs],
                       progress=orch.progress_of(task_id))
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def list_members(agent_id: str = None, token: str = None,
                           ctx: Context = None) -> str:
        """查询房间成员名册：角色、在线状态、各自技能。领导据此按擅长定向派工。仅 leader。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate_role(agent_id, token, "leader")
            touch(agent_id)
            from .core.db import db
            with db() as conn:
                rows = conn.execute(
                    "SELECT agent_id, name, role, status, skills FROM agents"
                    " WHERE room_id=? ORDER BY role DESC, agent_id",
                    (agent["room_id"],)).fetchall()
            return _ok(members=[{
                "agent_id": r["agent_id"], "name": r["name"],
                "role": r["role"], "status": r["status"],
                "skills": json.loads(r["skills"] or "[]")} for r in rows])
        except Exception as e:
            return _err(e)

    @srv.tool()
    async def broadcast_p0(text: str, agent_id: str = None,
                           token: str = None, ctx: Context = None) -> str:
        """P0 全局紧急打断：广播 priority=0 interrupt，所有成员立即停止当前动作。仅 leader。"""
        try:
            agent_id, token = _auth_from_args_or_headers(agent_id, token, ctx)
            agent = authenticate_role(agent_id, token, "leader")
            touch(agent_id)
            orch = OrchestratorRegistry.get(agent["room_id"])
            return json.dumps(await orch.broadcast_p0(agent_id, text),
                              ensure_ascii=False)
        except Exception as e:
            return _err(e)

    return srv


def mount_mcp(parent_app):
    """挂载 /mcp 端点（streamable-http, stateless）+ lifespan 包装。"""
    srv = build_mcp_server()
    sub = srv.streamable_http_app(stateless_http=True)

    parent_lifespan = parent_app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with srv.session_manager.run():
            async with parent_lifespan(app):
                yield

    parent_app.router.lifespan_context = lifespan
    # 子 app 内部路由就是 /mcp，挂到根下即得最终端点 /mcp
    parent_app.mount("", sub)
    return srv
