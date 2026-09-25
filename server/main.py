"""AgentDeck 入口：FastAPI + REST + MCP(/mcp) + 静态观测窗。

启动：uvicorn server.main:app --host 127.0.0.1 --port 8765
管理端点（confirm/abort/p0/goal）鉴权：设置 AGENT_DECK_ADMIN_TOKEN 后须带
Authorization: Bearer <token>；未设置时仅允许本机调用。
"""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .bus import bus_registry
from .core.config import settings
from .core.message import Message
from .mcp_api import mount_mcp
from .memory_hub import hub
from .orchestrator import OrchestratorRegistry, reclaim_loop
from .registry import router as registry_router

_WEB = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(reclaim_loop())
    yield
    task.cancel()


app = FastAPI(title="AgentDeck", lifespan=lifespan)

# 观测窗嵌入 IDE webview 时允许跨源读取（嵌 webview 场景需要）；
# 管理端点另有 admin token 把守，放开 CORS 不放大写权限。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(registry_router)


# ---------- REST ----------

@app.get("/api/health")
def health():
    return {"ok": True, "service": "agent-deck", "version": "0.1.0"}


def require_admin(request: Request | None) -> None:
    """管理端点鉴权：设置 AGENT_DECK_ADMIN_TOKEN 后须带 Bearer 头；未设置仅限本机。"""
    expected = settings.admin_token
    if not expected:
        host = request.client.host if (request and request.client) else "127.0.0.1"
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(403, "本服务未启用远程管理（未设置 AGENT_DECK_ADMIN_TOKEN）")
        return
    auth = request.headers.get("authorization", "") if request else ""
    if auth != f"Bearer {expected}":
        raise HTTPException(401, "管理令牌无效")


class GoalIn(BaseModel):
    room_id: str = "default"


@app.post("/api/tasks/{task_id}/confirm")
async def confirm_task(task_id: str, body: GoalIn, request: Request):
    require_admin(request)
    orch = OrchestratorRegistry.get(body.room_id)
    result = await orch.confirm_task("human", task_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "确认失败"))
    return result


@app.post("/api/tasks/{task_id}/abort")
async def abort_task(task_id: str, body: GoalIn, request: Request):
    require_admin(request)
    orch = OrchestratorRegistry.get(body.room_id)
    result = await orch.abort_task(task_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "作废失败"))
    return result


class P0In(BaseModel):
    room_id: str = "default"
    text: str = "P0：立即停止当前动作。"


@app.post("/api/p0")
async def p0_interrupt(body: P0In, request: Request):
    """人类 P0 紧急打断（与 leader broadcast_p0 同通道）。"""
    require_admin(request)
    orch = OrchestratorRegistry.get(body.room_id)
    return await orch.broadcast_p0("human", body.text)


class GoalMsgIn(BaseModel):
    room_id: str = "default"
    text: str


@app.post("/api/goal")
async def human_goal(body: GoalMsgIn, request: Request):
    """人类经观测窗下达目标：落房间消息流，领导 Agent poll 到后按群规拆解。"""
    require_admin(request)
    if not body.text.strip():
        raise HTTPException(400, "目标不能为空")
    bus = bus_registry.get(body.room_id)
    m = await bus.publish(Message(
        room_id=body.room_id, type="chat", priority=1,
        sender_kind="system", sender_id="human",
        payload_text=f"【人类目标】{body.text.strip()}"))
    return {"ok": True, "seq": m.seq,
            "hint": "领导 Agent poll 到该消息后按群规 plan_draft 拆解"}


@app.get("/api/state")
def state(room_id: str = "default"):
    """观测窗一次拉全量轻数据：任务/子任务/成员。"""
    from .core.db import db
    with db() as conn:
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE room_id=? ORDER BY created_at DESC LIMIT 5",
            (room_id,)).fetchall()
        agents = conn.execute(
            "SELECT agent_id, name, role, status, last_seen, skills FROM agents"
            " WHERE room_id=? ORDER BY role, agent_id", (room_id,)).fetchall()
    out_tasks = []
    for t in tasks:
        d = dict(t)
        with db() as conn:
            subs = conn.execute(
                "SELECT subtask_id, seq, title, status, claimant_id, retry_count,"
                " escalated, assignee FROM subtasks WHERE task_id=? ORDER BY seq",
                (t["task_id"],)).fetchall()
            prog = conn.execute(
                "SELECT p.subtask_id, p.agent_id, p.text, p.created_at FROM"
                " progress p JOIN subtasks s ON s.subtask_id=p.subtask_id"
                " WHERE s.task_id=? ORDER BY p.id DESC LIMIT 10",
                (t["task_id"],)).fetchall()
        d["subtasks"] = [dict(s) for s in subs]
        d["progress"] = [dict(p) for p in prog]
        out_tasks.append(d)
    return {"ok": True, "tasks": out_tasks,
            "agents": [dict(a) for a in agents]}


@app.get("/api/events")
def events(cursor: int = 0, room_id: str = "default"):
    """观测窗轮询增量事件。"""
    bus = bus_registry.get(room_id)
    return {"ok": True, **bus.poll(cursor, limit=30)}


@app.get("/api/memory")
def memory_query(room_id: str = "default", q: str = "", k: int = 3):
    return {"ok": True, "hits": hub.search_public(room_id, q, k)}


# ---------- 智能体工厂（AgentDeck → EvoFlow） ----------

_EVOFLOW_GATEWAY = "http://127.0.0.1:8012"


@app.get("/api/factory/members")
def factory_members(room_id: str = "default", request: Request = None):
    """智能体工厂：房间执行者名册（含技能），供勾选为 EvoFlow 智能体的外部执行者。"""
    require_admin(request)
    from .core.db import db
    with db() as conn:
        rows = conn.execute(
            "SELECT agent_id, name, role, status, skills FROM agents"
            " WHERE room_id=? AND role='executor' ORDER BY name", (room_id,)).fetchall()
    return {"ok": True, "members": [{
        "agent_id": r["agent_id"], "name": r["name"], "status": r["status"],
        "skills": json.loads(r["skills"] or "[]")} for r in rows]}


class DraftAgentIn(BaseModel):
    agent_code: str
    agent_name: str
    goal: str
    selected_members: list[str] = []      # agent_id 列表
    room_id: str = "default"


def _draft_prompt(goal: str, members: list[dict]) -> tuple[str, str]:
    """把勾选的外部执行者梳理成 EvoFlow 智能体的 system_prompt + soul_md。"""
    lines = [f"#角色\n你是「{goal}」方向的团队领导，通过 AgentDeck 房间（MCP: agent-deck）"
             "指挥外部执行者完成工作。你自己不执行具体任务，只做拆解、派发、监督、验收与汇总。"]
    exec_lines = []
    for m in members:
        skills = "、".join(m["skills"]) or "通用"
        exec_lines.append(f"- {m['name']}（agent_id={m['agent_id']}，当前{m['status']}）：擅长 {skills}")
    if exec_lines:
        lines.append("#团队外部执行者（按擅长定向派工）\n" + "\n".join(exec_lines))
    lines.append(
        "#工作流程\n"
        "1. 接到目标后先 list_members 确认成员在线与技能，再 plan_draft 拆解（≤5 个子任务，"
        "按技能 assignee 定向指派，有依赖的写 depends_on），auto_confirm=true 直接派发。\n"
        "2. 用 poll_messages 盯房间：执行者 report_progress 的进度会出现在消息流；"
        "交付后用 review_deliverable 验收——不合格打回并写清原因（重试超 2 次会升级，"
        "用 handle_escalated requeue 退回重派或 force_verify 强验收）。\n"
        "3. 全部子任务 verified 后用 task_status 核对 done，向用户汇总结果。\n"
        "4. 任务整体失败或目标作废时用 abort_task 清场。紧急情况 broadcast_p0 全局打断。")
    lines.append(
        "#输出规范\n"
        "- 给用户的汇报按「结果 → 关键路径 → 遗留问题」三段式，引用子任务编号。\n"
        "- 验收理由必须具体到交付物内容，不写「不错」「可以」这类空评语。")
    lines.append(
        "#禁止\n"
        "- 禁止跳过验收直接宣称任务完成。\n"
        "- 禁止在验收前把执行者的交付原文当自己的成果汇报。\n"
        "- 禁止派发与成员技能无关的任务（无合适人选时明确告知用户，不要硬派）。")
    soul = (f"你是务实的团队领导：先想清楚拆解，再严格验收。相信外部执行者的专业能力，"
            f"但交付必须经你核验才算数。你的团队在 AgentDeck 房间里，使命：{goal}。")
    return "\n\n".join(lines), soul


@app.post("/api/factory/draft-agent")
async def factory_draft(body: DraftAgentIn, request: Request = None):
    """勾选外部执行者 + 使命 → 梳理成 EvoFlow 智能体提示词草稿（预览用，未创建）。"""
    require_admin(request)
    from .core.db import db
    if not body.goal.strip():
        raise HTTPException(400, "goal 不能为空")
    with db() as conn:
        picked = []
        for aid in body.selected_members:
            row = conn.execute(
                "SELECT agent_id, name, status, skills FROM agents"
                " WHERE agent_id=? AND room_id=?", (aid, body.room_id)).fetchone()
            if row:
                picked.append({"agent_id": row["agent_id"], "name": row["name"],
                               "status": row["status"],
                               "skills": json.loads(row["skills"] or "[]")})
    if not picked:
        raise HTTPException(400, "至少勾选一名外部执行者")
    system_prompt, soul_md = _draft_prompt(body.goal.strip(), picked)
    return {"ok": True,
            "draft": {"agent_code": body.agent_code, "agent_name": body.agent_name,
                      "goal": body.goal.strip(), "members": picked,
                      "system_prompt": system_prompt, "soul": soul_md,
                      "mcp_servers": ["agent-deck"]}}


@app.post("/api/factory/create-agent")
async def factory_create(body: DraftAgentIn, request: Request = None):
    """确认草稿后真创建：调 EvoFlow Gateway POST /api/agents 并勾选 agent-deck MCP。"""
    require_admin(request)
    draft = (await factory_draft(body, None))["draft"]
    payload = {"agent_code": draft["agent_code"], "agent_name": draft["agent_name"],
               "description": draft["goal"],
               "system_prompt": draft["system_prompt"], "soul": draft["soul"],
               "mcp_servers": draft["mcp_servers"], "tags": ["agent-deck", "领导"]}
    import urllib.request as _u
    req = _u.Request(_EVOFLOW_GATEWAY + "/api/agents", method="POST",
                     data=json.dumps(payload).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
    try:
        with _u.urlopen(req, timeout=30) as resp:
            created = json.loads(resp.read().decode())
    except Exception as e:
        raise HTTPException(502, f"EvoFlow Gateway 创建失败：{e}")
    # 兜底勾选 MCP 到新 agent（Gateway 已写则跳过）
    import sqlite3
    conn = sqlite3.connect(r"C:\Users\Administrator\.evoflow\data\app\evoflow.db")
    row = conn.execute(
        "SELECT 1 FROM evoflow_agent_list_items WHERE agent_code=? AND"
        " list_kind='mcp_servers' AND item_value='agent-deck'",
        (draft["agent_code"],)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO evoflow_agent_list_items (agent_code, list_kind,"
            " item_value, sort_order, updated_at) VALUES (?,?,?,?,datetime('now'))",
            (draft["agent_code"], "mcp_servers", "agent-deck", 0))
        conn.commit()
    conn.close()
    return {"ok": True, "created": created, "draft": draft,
            "mcp_bound": True}


# ---------- 观测窗 ----------

@app.get("/")
def index():
    return FileResponse(_WEB / "index.html")

# MCP 子 app 挂根路径（内部路由 /mcp）——必须最后注册，避免遮蔽 /api 路由
mount_mcp(app)
