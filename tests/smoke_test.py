"""AgentDeck 增强冒烟：1 领导 + 2 技能执行者经真实 MCP 端点（/mcp）全链路。

覆盖：技能登记 → list_members → plan_draft(auto_confirm+assignee 定向) →
越权领单拒绝 → report_progress 过程上报 → 交付 → 验收 → 依赖解锁 → 任务闭环 → P0。
前置：服务已启动（scripts/dev-start.bat），DB 为空（重跑请删 server/agentdeck.db 后重启）。
"""
import json
import os
import urllib.request

BASE = os.environ.get("AGENT_DECK_URL", "http://127.0.0.1:8765")
ID = [0]


def rest(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def mcp_call(name, args, headers=None):
    ID[0] += 1
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(BASE + "/mcp", method="POST",
        data=json.dumps({"jsonrpc": "2.0", "id": ID[0], "method": "tools/call",
                         "params": {"name": name, "arguments": args}}).encode(),
        headers=h)
    raw = urllib.request.urlopen(req).read().decode()
    for line in raw.splitlines():
        if line.startswith("data: "):
            d = json.loads(line[6:])
            if "error" in d:
                raise RuntimeError(f"MCP error: {d['error']}")
            return json.loads(d["result"]["content"][0]["text"])
    raise RuntimeError(f"unexpected response: {raw[:200]}")


def expect(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    assert cond, label


def main():
    # 1 领导 + 2 技能执行者（headers 鉴权，工具调用不传身份参数）
    L = rest("POST", "/api/agents", {"name": "zcode-leader", "role": "leader"})
    E1 = rest("POST", "/api/agents", {"name": "codex-exec", "role": "executor",
                                      "skills": ["python", "爬虫"]})
    E2 = rest("POST", "/api/agents", {"name": "hermes-exec", "role": "executor",
                                      "skills": ["写作", "调研"]})
    H1 = {"X-Agent-Id": L["agent_id"], "Authorization": "Bearer " + L["token"]}
    H2 = {"X-Agent-Id": E1["agent_id"], "Authorization": "Bearer " + E1["token"]}
    H3 = {"X-Agent-Id": E2["agent_id"], "Authorization": "Bearer " + E2["token"]}

    r = mcp_call("join_room", {}, H2)
    expect(r["ok"] and r["skills"] == ["python", "爬虫"], "join_room 返回技能卡")
    expect(mcp_call("join_room", {}, H3)["status"] == "online", "join_room executor2")
    expect(mcp_call("join_room", {"agent_id": L["agent_id"], "token": "bad"})["ok"] is False, "错误令牌被拒")

    r = mcp_call("list_members", {}, H1)
    names = {m["name"] for m in r["members"]}
    expect(r["ok"] and {"zcode-leader", "codex-exec", "hermes-exec"} <= names,
           "list_members 成员名册（含本次注册 3 员）")
    expect(any("爬虫" in m["skills"] for m in r["members"]), "名册含技能标签")
    r = mcp_call("list_members", {}, H2)
    expect(r["ok"] is False, "executor 越权 list_members 被拒")

    # 领导按技能定向派工 + auto_confirm 免人确认
    r = mcp_call("plan_draft", {
        "goal": "出一份竞品爬虫调研报告", "auto_confirm": True,
        "subtasks": [
            {"title": "抓取竞品数据", "assignee": E1["agent_id"],
             "guidance": "用 python 爬取竞品列表"},
            {"title": "撰写报告", "assignee": E2["agent_id"], "depends_on": [1]}]},
        H1)
    expect(r["ok"] and r["auto_confirmed"], "plan_draft 自动确认派发")
    tid, s1, s2 = r["task_id"], r["subtasks"][0]["subtask_id"], r["subtasks"][1]["subtask_id"]

    # 定向单越权被拒；指定者可领
    r = mcp_call("claim_subtask", {"subtask_id": s1}, H3)
    expect(r["ok"] is False and "定向" in r["error"], "非指定者领定向单被拒")
    expect(mcp_call("claim_subtask", {"subtask_id": s1}, H2)["ok"], "codex 领定向爬虫单")

    # 过程上报 + 领导可见
    expect(mcp_call("report_progress", {"subtask_id": s1, "text": "已抓取 3/5 个竞品"}, H2)["ok"], "report_progress 上报")
    r = mcp_call("task_status", {"task_id": tid}, H1)
    expect(r["ok"] and len(r["progress"]) >= 1, "task_status 含进度流")

    # 交付（无文件声明，不触发核验）→ 验收 → 依赖解锁
    mcp_call("submit_deliverable", {"subtask_id": s1, "deliverable": "爬取完成，结果已更新全文"}, H2)
    mcp_call("review_deliverable", {"subtask_id": s1, "accept": True, "reason": "数据完整"}, H1)
    r = mcp_call("claim_subtask", {"subtask_id": s2}, H3)
    expect(r["ok"], "#1 验收后 hermes 领报告单")
    mcp_call("submit_deliverable", {"subtask_id": s2, "deliverable": "报告完成，已更新全文"}, H3)
    mcp_call("review_deliverable", {"subtask_id": s2, "accept": True, "reason": "报告合格"}, H1)
    r = mcp_call("task_status", {"task_id": tid}, H1)
    expect(r["task"]["status"] == "done", "任务闭环 done")

    # 硬核验仍生效：交付声称文件但没写 → 打回
    r = mcp_call("plan_draft", {"goal": "任务B", "auto_confirm": True,
                                "subtasks": [{"title": "写文件", "assignee": E1["agent_id"]}]}, H1)
    s3 = r["subtasks"][0]["subtask_id"]
    mcp_call("claim_subtask", {"subtask_id": s3}, H2)
    r = mcp_call("submit_deliverable", {"subtask_id": s3, "deliverable": "见 docs/result.md"}, H2)
    expect(r["status"] == "rejected", "硬核验：空口交付仍被打回")

    # P0
    expect(mcp_call("broadcast_p0", {"text": "P0 测试"}, H1)["ok"], "broadcast_p0")
    r = mcp_call("poll_messages", {"cursor": 0}, H2)
    expect(r["has_p0"] is True, "P0 标记可见")

    print("\nALL SMOKE PASS")


if __name__ == "__main__":
    main()
