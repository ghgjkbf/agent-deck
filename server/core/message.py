"""消息协议：一切皆消息（append-only 事件流），沿用 Agent Room 设计。

type ∈ {chat, deliver, dispatch, receipt, system, interrupt, task}
priority: 0 = P0（打断），1 = 任务事件，3 = 普通消息
sender_kind ∈ {agent, leader, system}
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

_CST = timezone(timedelta(hours=8))


def now_cst() -> str:
    return datetime.now(_CST).isoformat(timespec="milliseconds")


class Message:
    __slots__ = ("msg_id", "room_id", "type", "priority", "sender_kind",
                 "sender_id", "payload_text", "mentions", "parent_task_id",
                 "created_at", "seq")

    def __init__(self, *, room_id: str, type: str, sender_kind: str,
                 sender_id: str, payload_text: str = "",
                 mentions: list[str] | None = None,
                 parent_task_id: str | None = None,
                 priority: int = 3, msg_id: str | None = None,
                 created_at: str | None = None, seq: int | None = None):
        self.msg_id = msg_id or uuid.uuid4().hex
        self.room_id = room_id
        self.type = type
        self.priority = priority
        self.sender_kind = sender_kind
        self.sender_id = sender_id
        self.payload_text = payload_text
        self.mentions = mentions or []
        self.parent_task_id = parent_task_id
        self.created_at = created_at or now_cst()
        self.seq = seq

    def to_dict(self) -> dict:
        return {
            "seq": self.seq, "msg_id": self.msg_id, "room_id": self.room_id,
            "type": self.type, "priority": self.priority,
            "sender": {"kind": self.sender_kind, "id": self.sender_id},
            "text": self.payload_text, "mentions": self.mentions,
            "parent_task_id": self.parent_task_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, r) -> "Message":
        return cls(
            room_id=r["room_id"], type=r["type"], priority=r["priority"],
            sender_kind=r["sender_kind"], sender_id=r["sender_id"],
            payload_text=r["payload_text"] or "",
            mentions=json.loads(r["mentions"] or "[]"),
            parent_task_id=r["parent_task_id"],
            msg_id=r["msg_id"], created_at=r["created_at"], seq=r["seq"],
        )
