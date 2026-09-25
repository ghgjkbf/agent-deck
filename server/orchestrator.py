"""任务编排状态机（CEO 逻辑，无内置 LLM）。

状态流转（子任务）：
  pending → claimed → submitted → verified(=完成) 
                     ↘ rejected（打回，绑原执行者重交）→ submitted ...
  claimed 超 15 分钟未交付 → released → pending（重新可领）
  retry_count > 2 → escalated（升级领导处置）
任务：awaiting_confirm → running → done / aborted
"""
import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from .bus import bus_registry
from .core.config import settings
from .core.db import db
from .core.message import Message, now_cst

_CST = timezone(timedelta(hours=8))

# 交付文本中「声称写过的文件路径」
_CLAIM_RE = re.compile(
    r"[\w\-一-鿿]+(?:/[\w\-.一-鿿]+)*\.(?:md|txt|json|csv|log|yaml|yml|py|js|ts|html|css)\b",
    re.IGNORECASE)

_SUB_ST = {"pending": "待领", "claimed": "进行中", "submitted": "已交付",
           "verified": "已验收", "rejected": "已打回", "released": "已释放",
           "escalated": "已升级"}
_TASK_ST = {"awaiting_confirm": "待确认", "running": "执行中",
            "done": "已完成", "aborted": "已作废"}


def extract_claimed_paths(text: str) -> list[str]:
    seen, out = set(), []
    for m in _CLAIM_RE.finditer(text or ""):
        p = m.group(0).rstrip(".,;、）)】\"'`，。")
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ---------- 数据访问 ----------

def _task(task_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def _subtasks(task_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM subtasks WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()
    return [dict(r) for r in rows]


def _subtask(subtask_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM subtasks WHERE subtask_id=?",
                           (subtask_id,)).fetchone()
    return dict(row) if row else None


def _deps_met(sub: dict) -> bool:
    done = {s["seq"] for s in _subtasks(sub["task_id"])
            if s["status"] in ("verified",)}
    return all(d in done for d in json.loads(sub["depends_on"] or "[]"))


# ---------- 编排器 ----------

def _room_guard(sub: dict, room_id: str) -> str | None:
    """跨房间防护：返回错误信息或 None。"""
    task = _task(sub["task_id"])
    if not task or task["room_id"] != room_id:
        return "子任务不属于你所在房间"
    return None


class Orchestrator:
    """每房间一个实例；挂到 RoomBus.listeners 处理 deliver/claim 消息。"""

    def __init__(self, room_id: str):
        self.room_id = room_id

    @property
    def bus(self):
        return bus_registry.get(self.room_id)

    # -- 任务创建（领导 plan_draft 落库） --
    async def plan_draft(self, leader_id: str, goal: str,
                         subtasks: list[dict], *,
                         auto_confirm: bool = False) -> dict:
        with db() as conn:
            active = conn.execute(
                "SELECT task_id FROM tasks WHERE room_id=? AND status IN"
                " ('awaiting_confirm','running') LIMIT 1", (self.room_id,)).fetchone()
        if active:
            return {"ok": False, "error": "已有进行中的任务，请先完成或 abort"}
        if not goal.strip() or not subtasks:
            return {"ok": False, "error": "goal 与 subtasks 不能为空"}
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        ts = now_cst()
        with db() as conn:
            conn.execute(
                "INSERT INTO tasks (task_id, room_id, goal, status, leader_id,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (task_id, self.room_id, goal.strip(), "awaiting_confirm",
                 leader_id, ts, ts))
            for i, st in enumerate(subtasks, 1):
                conn.execute(
                    "INSERT INTO subtasks (subtask_id, task_id, seq, title,"
                    " guidance, depends_on, assignee, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"sub_{uuid.uuid4().hex[:8]}", task_id, i,
                     st.get("title", f"子任务{i}"), st.get("guidance", ""),
                     json.dumps(st.get("depends_on", [])),
                     st.get("assignee"), ts, ts))
        # 定向派工标注 + plan 落库完成
        await self.bus.publish(Message(
            room_id=self.room_id, type="task", priority=1,
            sender_kind="leader", sender_id=leader_id,
            payload_text=f"任务拆解{'已自动确认' if auto_confirm else '待确认'}：{goal.strip()}",
            parent_task_id=task_id))
        if auto_confirm:
            await self.confirm_task(leader_id, task_id)
        return {"ok": True, "task_id": task_id,
                "auto_confirmed": auto_confirm,
                "subtasks": [{"subtask_id": s["subtask_id"], "seq": s["seq"],
                              "title": s["title"], "status": s["status"],
                              "depends_on": json.loads(s["depends_on"])}
                             for s in _subtasks(task_id)]}

    async def confirm_task(self, leader_id: str, task_id: str) -> dict:
        task = _task(task_id)
        if not task or task["room_id"] != self.room_id:
            return {"ok": False, "error": "任务不存在"}
        if task["status"] != "awaiting_confirm":
            return {"ok": False, "error": f"任务状态为 {task['status']}，不可确认"}
        with db() as conn:
            conn.execute("UPDATE tasks SET status='running', updated_at=?"
                         " WHERE task_id=?", (now_cst(), task_id))
        await self.dispatch_ready(task_id)
        return {"ok": True}

    async def abort_task(self, task_id: str) -> dict:
        task = _task(task_id)
        if not task or task["status"] in ("done", "aborted"):
            return {"ok": False, "error": "任务不存在或已是终态"}
        with db() as conn:
            conn.execute("UPDATE tasks SET status='aborted', updated_at=?"
                         " WHERE task_id=?", (now_cst(), task_id))
            conn.execute(
                "UPDATE subtasks SET status='released', updated_at=?"
                " WHERE task_id=? AND status NOT IN ('verified','released')",
                (now_cst(), task_id))
        await self.bus.publish(Message(
            room_id=self.room_id, type="system", priority=1,
            sender_kind="system", sender_id="deck",
            payload_text=f"任务 {task_id} 已作废，全部未完成子任务释放。",
            parent_task_id=task_id))
        return {"ok": True}

    # -- 派发：依赖满足的子任务置 pending 并广播 --
    async def dispatch_ready(self, task_id: str) -> int:
        task = _task(task_id)
        if not task or task["status"] != "running":
            return 0
        n = 0
        for sub in _subtasks(task_id):
            if sub["status"] not in ("pending", "released"):
                continue
            if not _deps_met(sub):
                continue
            n += 1
            mentions = [sub["assignee"]] if sub.get("assignee") else []
            head = f"【定向排产单 #{sub['seq']}】" if mentions else f"【排产单 #{sub['seq']}】"
            await self.bus.publish(Message(
                room_id=self.room_id, type="dispatch", priority=1,
                sender_kind="system", sender_id="deck",
                payload_text=(f"{head}{sub['title']}"
                              f"\n目标：{task['goal']}\n要求：{sub['guidance'] or '按能力完成'}"
                              f"\n用 claim_subtask(subtask_id=\"{sub['subtask_id']}\") 认领。"),
                parent_task_id=task_id, mentions=mentions))
        return n

    # -- 领单（乐观锁） --
    async def claim(self, executor_id: str, subtask_id: str) -> dict:
        sub = _subtask(subtask_id)
        if not sub:
            return {"ok": False, "error": "子任务不存在"}
        if err := _room_guard(sub, self.room_id):
            return {"ok": False, "error": err}
        task = _task(sub["task_id"])
        if not task or task["status"] != "running":
            return {"ok": False, "error": "所属任务不在执行中"}
        if sub["status"] not in ("pending", "released"):
            return {"ok": False, "error": f"子任务状态为 {_SUB_ST.get(sub['status'], sub['status'])}，不可认领"}
        if not _deps_met(sub):
            return {"ok": False, "error": "依赖未满足"}
        if sub.get("assignee") and sub["assignee"] != executor_id:
            return {"ok": False, "error": f"该单定向指派给 {sub['assignee']}"}
        with db() as conn:
            cur = conn.execute(
                "UPDATE subtasks SET status='claimed', claimant_id=?, claimed_at=?,"
                " updated_at=? WHERE subtask_id=? AND status IN ('pending','released')",
                (executor_id, now_cst(), now_cst(), subtask_id))
            if cur.rowcount != 1:
                return {"ok": False, "error": "已被他人认领"}
        await self.bus.publish(Message(
            room_id=self.room_id, type="system", priority=2,
            sender_kind="agent", sender_id=executor_id,
            payload_text=f"已认领 #{sub['seq']} {sub['title']}。",
            parent_task_id=sub["task_id"]))
        return {"ok": True, "subtask": _subtask(subtask_id)}

    # -- 交付（乐观锁校验 claimant；触发验收） --
    async def submit(self, executor_id: str, subtask_id: str,
                     deliverable: str) -> dict:
        sub = _subtask(subtask_id)
        if not sub:
            return {"ok": False, "error": "子任务不存在"}
        if err := _room_guard(sub, self.room_id):
            return {"ok": False, "error": err}
        if sub["status"] not in ("claimed", "rejected"):
            return {"ok": False, "error": f"子任务状态为 {sub['status']}，不可交付"}
        if sub["claimant_id"] != executor_id:
            return {"ok": False, "error": "你不是该子任务的认领者"}
        with db() as conn:
            conn.execute(
                "UPDATE subtasks SET status='submitted', deliverable=?, updated_at=?"
                " WHERE subtask_id=?", (deliverable, now_cst(), subtask_id))
        await self.bus.publish(Message(
            room_id=self.room_id, type="deliver", priority=1,
            sender_kind="agent", sender_id=executor_id,
            payload_text=deliverable, parent_task_id=sub["task_id"]))
        # 硬核验：交付真实性（声称的文件必须存在于工作区）
        checks = self._verify_claims(sub, deliverable)
        if checks and all(c.startswith("✗") for c in checks):
            return await self.review(subtask_id, accept=False,
                                     reason="交付真实性核验失败：声称的文件均不存在于工作区",
                                     reviewer="deck", count_retry=False)
        return {"ok": True, "status": "submitted",
                "message": "已交付，等待领导验收（review_deliverable）"}

    # -- 验收（领导；deck 硬核验也走这里） --
    async def review(self, subtask_id: str, *, accept: bool, reason: str,
                     reviewer: str, count_retry: bool = True) -> dict:
        sub = _subtask(subtask_id)
        if not sub:
            return {"ok": False, "error": "子任务不存在"}
        if sub["status"] != "submitted":
            return {"ok": False, "error": f"子任务状态为 {sub['status']}，不可验收"}
        return await self._review_locked(sub, accept=accept, reason=reason,
                                         reviewer=reviewer,
                                         count_retry=count_retry)

    # -- 升级处置（领导对 escalated 子任务的后繼动作） --
    async def handle_escalated(self, subtask_id: str, action: str,
                               reason: str, reviewer: str) -> dict:
        """escalated 子任务的闭环：requeue（退回重派）或 force_verify（强验收）。"""
        sub = _subtask(subtask_id)
        if not sub:
            return {"ok": False, "error": "子任务不存在"}
        if err := _room_guard(sub, self.room_id):
            return {"ok": False, "error": err}
        if sub["status"] != "escalated":
            return {"ok": False, "error": f"子任务状态为 {sub['status']}，非升级态"}
        if action == "requeue":
            with db() as conn:
                cur = conn.execute(
                    "UPDATE subtasks SET status='pending', claimant_id=NULL,"
                    " claimed_at=NULL, last_receipt=?, updated_at=?"
                    " WHERE subtask_id=? AND status='escalated'",
                    (reason, now_cst(), subtask_id))
                if cur.rowcount != 1:
                    return {"ok": False, "error": "状态已变化，请重试"}
            await self.bus.publish(Message(
                room_id=self.room_id, type="system", priority=1,
                sender_kind="system", sender_id="deck",
                payload_text=f"升级处置：#{sub['seq']} {sub['title']} 退回待领（{reason}）",
                parent_task_id=sub["task_id"]))
            await self.dispatch_ready(sub["task_id"])
            return {"ok": True, "status": "pending"}
        if action == "force_verify":
            with db() as conn:
                cur = conn.execute(
                    "UPDATE subtasks SET status='verified', last_receipt=?, updated_at=?"
                    " WHERE subtask_id=? AND status='escalated'",
                    (f"[强验收] {reason}", now_cst(), subtask_id))
                if cur.rowcount != 1:
                    return {"ok": False, "error": "状态已变化，请重试"}
            await self.bus.publish(Message(
                room_id=self.room_id, type="receipt", priority=1,
                sender_kind="system", sender_id="deck",
                payload_text=f"升级强验收：#{sub['seq']} {sub['title']}（{reason}）",
                parent_task_id=sub["task_id"]))
            return await self._maybe_finalize(sub["task_id"])
        return {"ok": False, "error": "action 仅支持 requeue | force_verify"}

    async def _review_locked(self, sub: dict, *, accept: bool, reason: str,
                             reviewer: str, count_retry: bool) -> dict:
        task_id = sub["task_id"]
        if accept:
            with db() as conn:
                cur = conn.execute(
                    "UPDATE subtasks SET status='verified', last_receipt=?, updated_at=?"
                    " WHERE subtask_id=? AND status='submitted'",
                    (reason, now_cst(), sub["subtask_id"]))
                if cur.rowcount != 1:
                    return {"ok": False, "error": "状态已变化，请重试"}
            return await self._maybe_finalize(task_id, subtask_id=sub["subtask_id"])
        # 打回重做：绑原执行者；领导验收打回计 retry，deck 硬核验拦截不计数
        retries = sub["retry_count"] + (1 if count_retry else 0)
        escalated = retries > settings.subtask_max_retries
        new_status = "escalated" if escalated else "rejected"
        with db() as conn:
            conn.execute(
                "UPDATE subtasks SET status=?, retry_count=?, last_receipt=?,"
                " updated_at=? WHERE subtask_id=? AND status='submitted'",
                (new_status, retries, reason, now_cst(), sub["subtask_id"]))
        await self.bus.publish(Message(
            room_id=self.room_id, type="receipt", priority=1,
            sender_kind="system", sender_id="deck",
            payload_text=(f"验收打回：#{sub['seq']} {sub['title']}——{reason}"
                          f"（第 {retries} 次）" +
                          ("已升级领导处置（用 handle_escalated：requeue 退回重派或 force_verify 强验收）。"
                           if escalated else "请重做后重新交付。")),
            parent_task_id=task_id))
        if escalated:
            leader = _task(task_id)["leader_id"]
            await self.bus.publish(Message(
                room_id=self.room_id, type="system", priority=1,
                sender_kind="system", sender_id="deck",
                payload_text=(f"@{leader} 子任务 #{sub['seq']} 重试超限，请处置："
                              f"handle_escalated(action=\"requeue\"|\"force_verify\")。"),
                parent_task_id=task_id, mentions=[leader]))
        return {"ok": True, "status": new_status, "retries": retries}

    async def _maybe_finalize(self, task_id: str, subtask_id: str | None = None) -> dict:
        """验收通过后的收尾：全 verified 则任务 done，否则解锁下一环。"""
        remaining = [s for s in _subtasks(task_id)
                     if s["status"] not in ("verified",)]
        if not remaining:
            with db() as conn:
                conn.execute("UPDATE tasks SET status='done', updated_at=?"
                             " WHERE task_id=? AND status='running'",
                             (now_cst(), task_id))
            await self.bus.publish(Message(
                room_id=self.room_id, type="system", priority=1,
                sender_kind="system", sender_id="deck",
                payload_text=f"任务 {task_id} 全部完成。", parent_task_id=task_id))
            return {"ok": True, "status": "verified"}
        await self.dispatch_ready(task_id)
        return {"ok": True, "status": "verified"}

    # -- 交付真实性核验 --
    def _verify_claims(self, sub: dict, delivery_text: str) -> list[str]:
        from .workspace import read_file
        checks = []
        for p in extract_claimed_paths(delivery_text):
            try:
                f = read_file(sub["task_id"], sub["subtask_id"], p)
                checks.append(f"✓ {p}（v{f['version']}，{len(f['content'])} 字符）")
            except Exception:
                checks.append(f"✗ {p} 未在工作区找到")
        return checks

    # -- 超时回收 --
    async def reclaim_timeouts(self) -> int:
        """claimed 超时未交付 → released → pending 可再领。"""
        cutoff = (datetime.now(_CST) - timedelta(seconds=settings.claim_timeout_s)
                  ).isoformat(timespec="milliseconds")
        n = 0
        with db() as conn:
            rows = conn.execute(
                "SELECT s.subtask_id, s.seq, s.title, s.task_id FROM subtasks s"
                " JOIN tasks t ON t.task_id=s.task_id"
                " WHERE t.room_id=? AND s.status='claimed' AND s.claimed_at<?",
                (self.room_id, cutoff)).fetchall()
        for r in rows:
            with db() as conn:
                conn.execute(
                    "UPDATE subtasks SET status='released', updated_at=?"
                    " WHERE subtask_id=? AND status='claimed'",
                    (now_cst(), r["subtask_id"]))
            n += 1
            await self.bus.publish(Message(
                room_id=self.room_id, type="system", priority=2,
                sender_kind="system", sender_id="deck",
                payload_text=(f"超时回收：#{r['seq']} {r['title']} 认领后"
                              f"{settings.claim_timeout_s // 60} 分钟未交付，已释放待领。"),
                parent_task_id=r["task_id"]))
        return n

    # -- P0 全局打断 --
    async def broadcast_p0(self, leader_id: str, text: str) -> dict:
        await self.bus.publish(Message(
            room_id=self.room_id, type="interrupt", priority=0,
            sender_kind="leader", sender_id=leader_id,
            payload_text=text or "P0：立即停止当前动作。"))
        return {"ok": True}

    # -- 过程级交互：进度上报（轻量、自愿，不做全程监控） --
    async def report_progress(self, agent_id: str, subtask_id: str,
                              text: str) -> dict:
        sub = _subtask(subtask_id)
        if not sub or sub["claimant_id"] != agent_id:
            return {"ok": False, "error": "你不是该子任务的认领者"}
        if err := _room_guard(sub, self.room_id):
            return {"ok": False, "error": err}
        with db() as conn:
            conn.execute(
                "INSERT INTO progress (subtask_id, agent_id, text, created_at)"
                " VALUES (?,?,?,?)", (subtask_id, agent_id, text, now_cst()))
        await self.bus.publish(Message(
            room_id=self.room_id, type="chat", priority=2,
            sender_kind="agent", sender_id=agent_id,
            payload_text=f"[进度] #{sub['seq']} {sub['title']}：{text}",
            parent_task_id=sub["task_id"]))
        return {"ok": True}

    def progress_of(self, task_id: str) -> list[dict]:
        with db() as conn:
            rows = conn.execute(
                "SELECT p.subtask_id, p.agent_id, p.text, p.created_at"
                " FROM progress p JOIN subtasks s ON s.subtask_id=p.subtask_id"
                " WHERE s.task_id=? ORDER BY p.id DESC LIMIT 50",
                (task_id,)).fetchall()
        return [dict(r) for r in rows]

    # -- 总线入口 --
    async def on_message(self, bus, msg: Message):
        # 全部动作（claim/submit/review/p0）走 MCP 工具或 REST 直接调用，
        # 总线上的消息只作广播与留痕，不做二次处理
        return


class OrchestratorRegistry:
    _insts: dict[str, Orchestrator] = {}

    @classmethod
    def get(cls, room_id: str) -> Orchestrator:
        if room_id not in cls._insts:
            orch = Orchestrator(room_id)
            bus_registry.get(room_id).listeners.append(orch.on_message)
            cls._insts[room_id] = orch
        return cls._insts[room_id]


async def reclaim_loop():
    """后台协程：每 60 秒扫描各房间超时认领。"""
    while True:
        await asyncio.sleep(60)
        for room_id in list(OrchestratorRegistry._insts):
            try:
                await OrchestratorRegistry.get(room_id).reclaim_timeouts()
            except Exception:
                pass
