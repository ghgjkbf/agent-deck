"""AgentDeck 入口：FastAPI + REST + MCP(/mcp) + 静态观测窗。

启动：uvicorn server.main:app --host 127.0.0.1 --port 8765
管理端点（confirm/abort/p0/goal）鉴权：设置 AGENT_DECK_ADMIN_TOKEN 后须带
Authorization: Bearer <token>；未设置时仅允许本机调用。
"""
import asyncio
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


def require_admin(request: Request) -> None:
    """管理端点鉴权：设置 AGENT_DECK_ADMIN_TOKEN 后须带 Bearer 头；未设置仅限本机。"""
    expected = settings.admin_token
    if not expected:
        if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(403, "本服务未启用远程管理（未设置 AGENT_DECK_ADMIN_TOKEN）")
        return
    auth = request.headers.get("authorization", "")
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


# ---------- 观测窗 ----------

@app.get("/")
def index():
    return FileResponse(_WEB / "index.html")

# MCP 子 app 挂根路径（内部路由 /mcp）——必须最后注册，避免遮蔽 /api 路由
mount_mcp(app)
