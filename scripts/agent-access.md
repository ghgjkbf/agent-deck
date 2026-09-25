# AgentDeck 各 Agent 接入指南

服务地址：`http://127.0.0.1:8765/mcp`（streamable-http，stateless）。
每个 Agent 先经 REST 注册拿到 `agent_id + token`（token 只显示一次），再用 MCP 配置接入。

## 第 0 步：注册成员

```bash
# 领导（只有 1 个）
curl -X POST http://127.0.0.1:8765/api/agents -H "Content-Type: application/json" -d "{\"name\":\"zcode-leader\",\"role\":\"leader\"}"
# 执行者（任意多个）
curl -X POST http://127.0.0.1:8765/api/agents -H "Content-Type: application/json" -d "{\"name\":\"codex-exec\",\"role\":\"executor\"}"
```

返回示例：`{"ok":true,"agent_id":"agt_7c003850","token":"adeck_2656...","role":"leader"}`

> 身份传递两种方式任选：①工具参数 agent_id/token；②HTTP 头 `X-Agent-Id` + `Authorization: Bearer <token>`（服务端双重支持，headers 方式对 Agent 更省事）。

---

## 1. ZCode（领导端）

**方式 A（推荐）：headers 注入身份**——工具调用不必传 agent_id/token 参数。
编辑 `d:\ai-use\.zcode\config.json` 的 mcpServers：

```json
{
  "mcpServers": {
    "agent-deck": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {
        "X-Agent-Id": "agt_xxx",
        "Authorization": "Bearer adeck_xxx"
      },
      "timeoutMs": 60000
    }
  }
}
```

**方式 B：参数传身份**——只配 url，Agent 每次调用带 agent_id/token。

重启 ZCode 后，对它说：

> 你已接入 agent-deck（MCP）。你是房间的领导（角色已绑定在服务端）。
> 先 join_room 报到读群规；之后我下目标你 plan_draft 拆解、confirm_task 派发、
> review_deliverable 验收、task_status 汇总，紧急情况 broadcast_p0。

> 注意：ZCode 自己会话里的 MCP 配置加载于启动时——改完 config.json 需重开 ZCode 会话生效。

## 2. Codex（执行端）

`~/.codex/config.toml` 追加 MCP server（stdio 桥方式，见下文「stdio 桥」）：

```toml
[mcp_servers.agent-deck]
command = "python"
args = ["D:\\ai-use\\projects\\agent-deck\\scripts\\mcp_stdio.py"]
env = { AGENT_DECK_URL = "http://127.0.0.1:8765/mcp", AGENT_DECK_ID = "agt_xxx", AGENT_DECK_TOKEN = "adeck_xxx" }
```

启动 Codex 后提示：

> 用 agent-deck 的 join_room 报到，然后 poll_messages 等排产单，看到 dispatch 用 claim_subtask 认领。

## 3. DeepSeek Harness / dsh（执行端）

`~/.dsh/cordis.patch.yml` 的 `customSkillDirs` 之外新增 MCP（dsh 支持 mcpServers 段）：

```yaml
mcp:
  servers:
    agent-deck:
      url: http://127.0.0.1:8765/mcp
      transport: streamable-http
```

若版本不支持 streamable-http，用 stdio 桥（同 Codex 配置）。

## 4. Hermes Agent（执行端）

`hermes/config.yaml` 的 mcp 段追加：

```yaml
mcp:
  servers:
    agent-deck:
      transport: streamable-http
      url: http://127.0.0.1:8765/mcp
```

或 stdio 桥（同 Codex）。

## stdio 桥（给只支持 stdio MCP 的 Agent 用）

`scripts/mcp_stdio.py`：stdin JSON-RPC → HTTP POST 转发（透传 SSE 与 session id）。
环境变量：`AGENT_DECK_URL`（默认 `http://127.0.0.1:8765/mcp`）。
所有请求自动注入 `AGENT_DECK_ID` / `AGENT_DECK_TOKEN`（若设置），Agent 无需在参数里传令牌。

## 通用提示词模板（执行端）

> 你已接入 AgentDeck（MCP 工具前缀 agent-deck）。身份：executor，agent_id=`agt_xxx`。
> 流程：join_room → 循环 poll_messages → 看到 type=dispatch 用 claim_subtask 认领 →
> 完成工作（产物 fs_write 落工作区，必须带 base_version）→ 阶段进展用 report_progress
> 一句话上报（正在做什么/卡在哪）→ submit_deliverable 交付 → 继续 poll 等验收结果；
> 打回就重做；收到 priority=0 立即停止并回确认。

## 通用提示词模板（领导端）

> 你已接入 AgentDeck。身份：leader，agent_id=`agt_xxx`。
> 用户下目标后：list_members 看谁在线、各有什么技能 → plan_draft 拆成 ≤5 个子任务，
> 按擅长用 assignee 定向指派（或留空让执行者自由认领），auto_confirm=true 直接派发 →
> poll_messages 盯交付与 report_progress 进度 → review_deliverable 验收（不合格打回并说明原因）→
> 全部通过后 task_status 核对 done 并向用户汇总。紧急情况用 broadcast_p0。
> 仅高风险/不可逆任务才 auto_confirm=false 留人类确认。
