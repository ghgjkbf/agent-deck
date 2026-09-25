"""成员注册与认证：一次性令牌 + sha256 双因子 + 角色绑定 + 技能登记。"""
import hashlib
import json
import secrets
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .core.db import db
from .core.message import now_cst

router = APIRouter(prefix="/api")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RoomIn(BaseModel):
    name: str = "default"


class AgentIn(BaseModel):
    room_id: str = "default"
    name: str
    role: str = "executor"          # leader | executor
    skills: list[str] = []          # 擅长能力标签，如 ["python","爬虫","写作"]


def _ensure_room(room_id: str, name: str | None = None) -> None:
    with db() as conn:
        row = conn.execute("SELECT id FROM rooms WHERE id=?", (room_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO rooms (id, name, created_at) VALUES (?,?,?)",
                         (room_id, name or room_id, now_cst()))


@router.post("/rooms")
def create_room(body: RoomIn):
    room_id = body.name.strip() or "default"
    _ensure_room(room_id, body.name)
    return {"ok": True, "room_id": room_id}


@router.post("/agents")
def register_agent(body: AgentIn):
    """注册成员：返回 agent_id 与一次性明文 token（仅此一次）。"""
    if body.role not in ("leader", "executor"):
        raise HTTPException(400, "role 必须是 leader 或 executor")
    _ensure_room(body.room_id)
    agent_id = f"agt_{uuid.uuid4().hex[:8]}"
    token = "adeck_" + secrets.token_hex(20)
    ts = now_cst()
    with db() as conn:
        conn.execute(
            "INSERT INTO agents (agent_id, room_id, name, role, token_hash,"
            " status, last_seen, skills, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (agent_id, body.room_id, body.name, body.role,
             hash_token(token), "offline", ts,
             json.dumps(body.skills, ensure_ascii=False), ts))
    return {"ok": True, "agent_id": agent_id, "token": token,
            "role": body.role, "room_id": body.room_id}


def authenticate(agent_id: str, token: str) -> dict:
    """双因子：agent 存在 + sha256(token) 匹配。失败抛 HTTPException。"""
    if not agent_id or not token:
        raise HTTPException(401, "缺少 agent_id 或 token")
    with db() as conn:
        row = conn.execute(
            "SELECT agent_id, room_id, name, role, status, token_hash, skills"
            " FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
    if not row or row["token_hash"] != hash_token(token):
        raise HTTPException(401, "认证失败：agent_id 或 token 无效")
    return dict(row)


def authenticate_role(agent_id: str, token: str, role: str) -> dict:
    agent = authenticate(agent_id, token)
    if agent["role"] != role:
        raise HTTPException(403, f"需要 {role} 角色，当前为 {agent['role']}")
    return agent


def touch(agent_id: str, status: str | None = None) -> None:
    with db() as conn:
        if status:
            conn.execute("UPDATE agents SET last_seen=?, status=? WHERE agent_id=?",
                         (now_cst(), status, agent_id))
        else:
            conn.execute("UPDATE agents SET last_seen=? WHERE agent_id=?",
                         (now_cst(), agent_id))


# ---------- 测试辅助（进程内单测用，不经 HTTP） ----------

def register_agent_for_test(room_id: str, name: str, role: str) -> dict:
    import uuid as _uuid
    agent_id = f"agt_{_uuid.uuid4().hex[:8]}"
    token = "adeck_" + secrets.token_hex(20)
    ts = now_cst()
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO rooms (id, name, created_at) VALUES (?,?,?)",
            (room_id, room_id, ts))
        conn.execute(
            "INSERT INTO agents (agent_id, room_id, name, role, token_hash,"
            " status, last_seen, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (agent_id, room_id, name, role, hash_token(token), "offline", ts, ts))
    return {"agent_id": agent_id, "token": token, "role": role}
