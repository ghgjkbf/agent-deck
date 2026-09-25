"""消息总线：publish → 落库 → 扇出 → listeners（沿用 Agent Room RoomBus 模式）。

- 落库取回自增 seq，后续 poll 增量按 cursor 拉。
- listeners: async (msg) -> None，异常互不影响。
- 广播通道保留（web 事件流用）；无 WS 依赖，纯 asyncio 队列。
"""
import asyncio
import json

from .core.db import db
from .core.message import Message, now_cst

_INSERT = ("INSERT INTO messages (msg_id, room_id, type, priority, sender_kind,"
           " sender_id, payload_text, mentions, parent_task_id, created_at)"
           " VALUES (?,?,?,?,?,?,?,?,?,?)")


class RoomBus:
    def __init__(self, room_id: str):
        self.room_id = room_id
        self.listeners: list = []
        self._subscribers: dict[str, asyncio.Queue] = {}

    # ---------- 发布 ----------
    async def publish(self, msg: Message) -> Message:
        with db() as conn:
            cur = conn.execute(_INSERT, (
                msg.msg_id, msg.room_id, msg.type, msg.priority,
                msg.sender_kind, msg.sender_id, msg.payload_text,
                json.dumps(msg.mentions), msg.parent_task_id, msg.created_at))
            msg.seq = cur.lastrowid
        await self.broadcast_raw(json.dumps(msg.to_dict(), ensure_ascii=False))
        for cb in list(self.listeners):
            try:
                await cb(self, msg)
            except Exception:
                pass
        return msg

    # ---------- 扇出 ----------
    def subscribe(self, client_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers[client_id] = q
        return q

    def unsubscribe(self, client_id: str) -> None:
        self._subscribers.pop(client_id, None)

    async def broadcast_raw(self, data: str) -> None:
        for q in list(self._subscribers.values()):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass

    # ---------- 增量拉取 ----------
    def poll(self, cursor: int = 0, limit: int = 50) -> dict:
        """拉取 seq > cursor 的消息，返回 next_cursor。"""
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE room_id=? AND seq>?"
                " ORDER BY seq LIMIT ?", (self.room_id, cursor, limit)).fetchall()
            latest = conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS m FROM messages WHERE room_id=?",
                (self.room_id,)).fetchone()["m"]
        msgs = [Message.from_row(r).to_dict() for r in rows]
        next_cursor = msgs[-1]["seq"] if msgs else cursor
        return {"messages": msgs, "next_cursor": next_cursor,
                "room_latest": latest, "has_p0": any(
                    m["priority"] == 0 for m in msgs[-20:])}


class BusRegistry:
    def __init__(self):
        self._rooms: dict[str, RoomBus] = {}

    def get(self, room_id: str) -> RoomBus:
        if room_id not in self._rooms:
            self._rooms[room_id] = RoomBus(room_id)
        return self._rooms[room_id]


bus_registry = BusRegistry()
