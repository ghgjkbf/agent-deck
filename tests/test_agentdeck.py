"""进程内单元测试：状态机 / 依赖拓扑 / 超时回收 / 越权 / 工作区乐观锁。"""
import asyncio
import os
import tempfile

import pytest

# 每个测试进程一个独立 DB（import 前设置）
os.environ["AGENT_DECK_DB"] = tempfile.mktemp(suffix=".db")

import server.core.db as cdb  # noqa: E402
cdb.DB_PATH = os.environ["AGENT_DECK_DB"]
cdb.init_db()

from server.orchestrator import Orchestrator  # noqa: E402
from server.registry import authenticate, authenticate_role  # noqa: E402
from server.workspace import read_file, write_file  # noqa: E402


def mk_room(room_id="r"):
    return Orchestrator(room_id)


def test_full_loop_plan_confirm_claim_submit_review():
    """拆解→确认→领单→交付→验收→任务 done 全链路。"""
    async def run():
        o = mk_room("t1")
        r = await o.plan_draft("L", "目标X", [{"title": "A"}, {"title": "B", "depends_on": [1]}])
        assert r["ok"]
        tid, s1 = r["task_id"], r["subtasks"][0]["subtask_id"]
        s2 = r["subtasks"][1]["subtask_id"]
        assert (await o.confirm_task("L", tid))["ok"]
        assert (await o.claim("E1", s1))["ok"]
        assert (await o.submit("E1", s1, "完成（无文件声明）"))["status"] == "submitted"
        assert (await o.review(s1, accept=True, reason="ok", reviewer="L"))["status"] == "verified"
        # #1 verified 后 #2 才可领
        assert (await o.claim("E1", s2))["ok"]
        await o.submit("E1", s2, "完成 B")
        await o.review(s2, accept=True, reason="ok", reviewer="L")
        from server.orchestrator import _task
        assert _task(tid)["status"] == "done"
    asyncio.run(run())


def test_deps_block_and_unlock():
    """依赖拓扑：未解锁不可领；前置通过后解锁。"""
    async def run():
        o = mk_room("t2")
        r = await o.plan_draft("L", "目标", [{"title": "A"}, {"title": "B", "depends_on": [1]}])
        s2 = r["subtasks"][1]["subtask_id"]
        await o.confirm_task("L", r["task_id"])
        r = await o.claim("E1", s2)
        assert not r["ok"] and "依赖" in r["error"]
    asyncio.run(run())


def test_optimistic_lock_on_claim():
    """乐观锁：同一子任务只能被认领一次。"""
    async def run():
        o = mk_room("t3")
        r = await o.plan_draft("L", "目标", [{"title": "A"}])
        s1 = r["subtasks"][0]["subtask_id"]
        await o.confirm_task("L", r["task_id"])
        assert (await o.claim("E1", s1))["ok"]
        r = await o.claim("E2", s1)
        assert not r["ok"] and "认领" in r["error"]
    asyncio.run(run())


def test_verify_claims_hard_reject():
    """空口交付（声称文件不存在）被硬核验直接打回。"""
    async def run():
        o = mk_room("t4")
        r = await o.plan_draft("L", "目标", [{"title": "A"}])
        s1 = r["subtasks"][0]["subtask_id"]
        await o.confirm_task("L", r["task_id"])
        await o.claim("E1", s1)
        r = await o.submit("E1", s1, "见 docs/outline.md")
        assert r["status"] == "rejected"
    asyncio.run(run())


def test_retry_and_escalation():
    """领导打回计 retry；超过 2 次升级（escalated）。"""
    async def run():
        o = mk_room("t5")
        r = await o.plan_draft("L", "目标", [{"title": "A"}])
        s1 = r["subtasks"][0]["subtask_id"]
        await o.confirm_task("L", r["task_id"])
        await o.claim("E1", s1)
        for i in range(1, 4):
            await o.submit("E1", s1, f"第{i}次交付（无文件声明）")
            r = await o.review(s1, accept=False, reason="不合格", reviewer="L")
        assert r["status"] == "escalated" and r["retries"] == 3
    asyncio.run(run())


def test_claim_timeout_reclaim():
    """超时回收：claimed 超时子任务被释放回 pending。"""
    async def run():
        o = mk_room("t6")
        from server.core import config
        old = config.settings.claim_timeout_s
        config.settings.claim_timeout_s = -1  # 立即超时
        try:
            r = await o.plan_draft("L", "目标", [{"title": "A"}])
            s1 = r["subtasks"][0]["subtask_id"]
            await o.confirm_task("L", r["task_id"])
            assert (await o.claim("E1", s1))["ok"]
            n = await o.reclaim_timeouts()
            assert n == 1
            from server.orchestrator import _subtask
            assert _subtask(s1)["status"] == "released"
            # 释放后可重新认领
            assert (await o.claim("E2", s1))["ok"]
        finally:
            config.settings.claim_timeout_s = old
    asyncio.run(run())


def test_single_active_task():
    """单活跃任务：有待确认/执行中任务时不可再拆解。"""
    async def run():
        o = mk_room("t7")
        assert (await o.plan_draft("L", "目标1", [{"title": "A"}]))["ok"]
        r = await o.plan_draft("L", "目标2", [{"title": "B"}])
        assert not r["ok"] and "abort" in r["error"]
    asyncio.run(run())


def test_abort_releases_subtasks():
    """abort 兜底：任务作废，未完成子任务全部释放，无卡死。"""
    async def run():
        o = mk_room("t8")
        r = await o.plan_draft("L", "目标", [{"title": "A"}])
        tid = r["task_id"]
        await o.confirm_task("L", tid)
        r = await o.abort_task(tid)
        assert r["ok"]
        r2 = await o.plan_draft("L", "新目标", [{"title": "B"}])
        assert r2["ok"]  # 互斥解除
    asyncio.run(run())


def test_role_guard():
    """角色隔离：executor 不能验收/拆解，leader 不能领单。"""
    from fastapi import HTTPException
    from server.registry import register_agent_for_test
    L = register_agent_for_test("t9", "L", "leader")
    E = register_agent_for_test("t9", "E", "executor")
    with pytest.raises(HTTPException) as e:
        authenticate_role(E["agent_id"], E["token"], "leader")
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        authenticate_role(L["agent_id"], L["token"], "executor")
    assert e.value.status_code == 403
    a = authenticate(L["agent_id"], L["token"])
    assert a["role"] == "leader"
    with pytest.raises(HTTPException):
        authenticate(L["agent_id"], "wrong-token")


def test_workspace_optimistic_lock_and_isolation(tmp_path, monkeypatch):
    """工作区：乐观锁冲突 409；子任务目录互相隔离。"""
    import server.workspace as ws
    monkeypatch.setattr(ws, "_BASE", str(tmp_path))
    tid, sa, sb = "tk", "s1", "s2"
    r = write_file(tid, sa, "docs/plan.md", "v1", "E1")
    assert r["version"] == 1
    with pytest.raises(PermissionError):
        write_file(tid, sa, "docs/plan.md", "v2-bad", "E2", base_version=0)
    r = write_file(tid, sa, "docs/plan.md", "v2", "E2", base_version=1)
    assert r["version"] == 2
    # 路径逃逸拦截
    with pytest.raises(ValueError):
        write_file(tid, sa, "../evil.txt", "x", "E")
    # 隔离：s2 看不到 s1 的文件
    with pytest.raises(FileNotFoundError):
        read_file(tid, sb, "docs/plan.md")
    assert read_file(tid, sa, "docs/plan.md")["content"] == "v2"


def test_memory_public_query():
    """公共记忆：写入后可检索命中。"""
    from server.memory_hub import hub
    hub.write_public("tm", "部署脚本在 deploy/windows.ps1", {"k": 1})
    hits = hub.search_public("tm", "部署脚本 windows")
    assert hits and "deploy" in hits[0]["text"]


def test_skills_and_directed_dispatch():
    """技能登记 + 定向派工：list_members 查技能，assignee 子任务仅指定者可领。"""
    from server.registry import register_agent_for_test
    from server.orchestrator import _subtask
    register_agent_for_test("td1", "codex", "executor")   # id 无所谓
    L = register_agent_for_test("td1", "L", "leader")
    E = register_agent_for_test("td1", "pybot", "executor")

    async def run():
        o = Orchestrator("td1")
        r = await o.plan_draft("L", "写爬虫", [
            {"title": "抓数据", "assignee": E["agent_id"]},
            {"title": "自由任务"}], auto_confirm=True)
        assert r["ok"] and r["auto_confirmed"]
        from server.orchestrator import _task
        assert _task(r["task_id"])["status"] == "running"  # 已自动确认
        s1, s2 = (r["subtasks"][0]["subtask_id"], r["subtasks"][1]["subtask_id"])
        # 定向单：他人不可领，指定者可领
        other = register_agent_for_test("td1", "other", "executor")
        r = await o.claim(other["agent_id"], s1)
        assert not r["ok"] and "定向" in r["error"]
        r = await o.claim(E["agent_id"], s1)
        assert r["ok"]
        # 自由单：任何人可领
        assert (await o.claim(other["agent_id"], s2))["ok"]

        # 进度上报
        r = await o.report_progress(E["agent_id"], s1, "已抓取 80%")
        assert r["ok"]
        r = await o.report_progress(other["agent_id"], s1, "越权上报")
        assert not r["ok"]
        assert any("80%" in p["text"] for p in o.progress_of(r and s1 and _subtask(s1)["task_id"]))

    asyncio.run(run())


def test_list_members_shows_skills():
    """REST 注册带 skills；list_members 数据源（agents.skills 列）可见。"""
    from server.registry import register_agent_for_test, hash_token
    from server.core.db import db
    a = register_agent_for_test("td2", "writer", "executor")
    with db() as conn:
        conn.execute("UPDATE agents SET skills=? WHERE agent_id=?",
                     ('["写作","调研"]', a["agent_id"]))
        row = conn.execute("SELECT skills FROM agents WHERE agent_id=?",
                           (a["agent_id"],)).fetchone()
    import json
    assert json.loads(row["skills"]) == ["写作", "调研"]
