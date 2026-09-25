# AgentDeck

**MCP 多 Agent 纯编排插件**：单领导 Agent 指挥，多执行 Agent 协同。插件只做基础设施——任务黑板、消息总线、状态流转、文件共享——不内置 LLM，不替 Agent 做决策。

一个「领导 Agent」（如 ZCode）经 MCP 接入后负责任务拆解、派发、验收、汇总；任意数量的异构执行 Agent（Codex / DeepSeek Harness / Hermes / TRAE…）经 MCP 认领任务、独立执行、交付产物。人类通过内嵌小观测窗（可嵌 IDE 侧边栏 webview）实时旁观全部状态并可 P0 打断。

源自 [Agent Room](../agent-room) 的编排闭环提炼：一切皆消息（append-only 事件流）、编排者不执行、执行者不编排、交付真实性硬核验（声称写了文件而工作区没有 → 直接打回）。

## 核心机制

- **角色隔离**：`leader` 专责 `plan_draft / confirm_task / review_deliverable / task_status / broadcast_p0`；`executor` 专责 `claim_subtask / submit_deliverable`；越权调用直接拒绝
- **依赖拓扑**：子任务 `depends_on` 前置全部验收通过才解锁派发
- **乐观锁领单**：`claim_subtask` 先到先得，双人抢同一单只有一个成功
- **超时回收**：认领 15 分钟未交付自动释放回待领（`AGENT_DECK_CLAIM_TIMEOUT_S` 可调）
- **打回重做**：领导验收打回计 retry；重试超 2 次升级领导处置（`AGENT_DECK_SUBTASK_MAX_RETRIES` 可调）
- **交付硬核验**：交付文本声称的文件必须真的在工作区，否则无视验收直接打回（不计 retry）
- **技能定向派工**：成员注册时声明 `skills`；领导 `list_members` 查名册，按擅长给子任务指定 `assignee`（定向单仅指派者可领），也可留空自由认领
- **过程上报**：执行者 `report_progress` 一句话播报进度，领导 `task_status` 可见——轻量自愿，不做全程监控
- **状态闭环**：`pending → claimed → submitted → verified / rejected → done`，外加 `released`（超时释放）与 `escalated`（重试超限升级，领导用 `handle_escalated` requeue 退回重派或 force_verify 强验收闭环）；任务 abort 兜底清空全部未完成子任务，无永久卡死

## 快速开始

```bash
# 1. 启动（首次自动 uv sync 装依赖）
scripts\dev-start.bat
# 或：uv sync && uv run uvicorn server.main:app --host 127.0.0.1 --port 8765

# 2. 打开观测窗
#    浏览器访问 http://127.0.0.1:8765/ （320px 侧边栏 / 400×500 悬浮窗两种模式）

# 3. 注册成员（POST /api/agents，token 只显示一次）
curl -X POST http://127.0.0.1:8765/api/agents -H "Content-Type: application/json" ^
  -d "{\"name\":\"zcode-leader\",\"role\":\"leader\"}"
curl -X POST http://127.0.0.1:8765/api/agents -H "Content-Type: application/json" ^
  -d "{\"name\":\"codex-exec\",\"role\":\"executor\",\"skills\":[\"python\",\"爬虫\"]}"

# 4. 各 Agent 按对应配置接入 MCP（见 scripts/agent-access.md）
```

## MCP 端点

`http://127.0.0.1:8765/mcp`（streamable-http，stateless）

| 工具 | 角色 | 说明 |
|---|---|---|
| `join_room(agent_id, token)` | 通用 | 认证接入，返回身份卡、角色、群规 |
| `poll_messages(cursor)` | 通用 | 拉增量消息（排产单/验收/P0） |
| `send_message(text, mentions?)` | 通用 | 发日志、进度 |
| `declare_status(status)` | 通用 | online / busy / offline |
| `claim_subtask(subtask_id)` | executor | 认领子任务（乐观锁；定向单仅指派者可领） |
| `report_progress(subtask_id, text)` | executor | 过程进度一句话上报（领导可见） |
| `submit_deliverable(subtask_id, text)` | executor | 交付（触发硬核验 + 领导验收） |
| `fs_list(task_id, subtask_id)` | 通用 | 列子任务工作区文件 |
| `fs_read(task_id, subtask_id, path)` | 通用 | 读文件（含当前版本号） |
| `fs_write(task_id, subtask_id, path, content, base_version?)` | 通用 | 写文件（乐观锁） |
| `plan_draft(goal, subtasks[], auto_confirm)` | leader | 拆解并派发（assignee 定向、auto_confirm 免人确认） |
| `list_members()` | leader | 成员名册：角色、在线状态、技能 |
| `confirm_task(task_id)` | leader | 确认派发（人类也可经 REST 确认） |
| `review_deliverable(subtask_id, accept, reason)` | leader | 验收：通过 / 打回 |
| `task_status(task_id)` | leader | 查全链路状态 |
| `handle_escalated(subtask_id, action, reason)` | leader | 升级处置：requeue 退回重派 / force_verify 强验收 |
| `broadcast_p0(text)` | leader | 全局紧急打断（priority=0） |

## 典型协作流

```
人类 ──目标──▶ 领导Agent(ZCode)
                 │ plan_draft（拆解图，含依赖）
                 ├─▶ 人类/领导 confirm_task
                 │ dispatch 广播排产单
                 ▼
   执行Agent(Codex) claim_subtask #1 ── fs_write 产物 ──▶ submit_deliverable
                 │                      ▲ 硬核验：声称的文件必须存在
                 ▼                      └─ 不合格 → 打回重做（≤2 次，超限升级）
   领导Agent review_deliverable ──通过──▶ 依赖解锁 #2 → Hermes 认领 …
                 ▼
            全部 verified → task done → 人类在观测窗验收
```

## 观测窗

单文件 `web/index.html`，原生 HTML/JS 零依赖：
- **Tab1 任务看板**：主目标、子任务状态（待领/进行中/已交付/打回）、执行者、确认/作废按钮
- **Tab2 实时消息**：关键事件倒序（最近 20 条）
- **Tab3 成员状态**：角色、在线状态
- 底部：目标输入框 + P0 紧急打断按钮
- 320px 侧边栏模式（默认）/ 400×500 悬浮窗模式（右上角按钮切换），可嵌入 webview

## HTTP API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| POST | `/api/rooms`、`/api/agents` | 建房间 / 注册成员（发一次性令牌） |
| POST | `/api/tasks/{id}/confirm`、`/api/tasks/{id}/abort` | 人类确认 / 作废任务 |
| POST | `/api/p0` | 人类 P0 全局打断 |
| GET | `/api/state`、`/api/events?cursor=` | 观测窗轮询：全量状态 / 增量事件 |

## 测试

```bash
uv run pytest -q            # 13 个单元用例（状态机/依赖/超时/越权/定向/工作区）
uv run python -X utf8 tests/smoke_test.py   # 全链路冒烟（需服务已启动、DB 为空）
```

## 配置（环境变量，均可选）

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_DECK_PORT` | 8765 | 服务端口 |
| `AGENT_DECK_DB` | `server/agentdeck.db` | SQLite 路径 |
| `AGENT_DECK_CLAIM_TIMEOUT_S` | 900 | 认领超时（秒） |
| `AGENT_DECK_SUBTASK_MAX_RETRIES` | 2 | 打回重试上限（超限升级） |
| `AGENT_DECK_MEMORY_TOP_K` | 3 | 记忆检索条数 |

## 安全模型

- 默认监听 `127.0.0.1`，仅供本机使用；MCP 面双因子认证（agent_id + sha256 token），跨房间操作被拒
- 人类管理端点（confirm/abort/p0/goal）：未设 `AGENT_DECK_ADMIN_TOKEN` 时仅本机可调；对外暴露时必须设置该环境变量并以 `Authorization: Bearer <token>` 调用
- SQLite 全参数化查询；工作区路径白名单校验（防穿越）；令牌仅注册响应出现一次，不落日志

## License

MIT
