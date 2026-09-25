"""stdio → streamable-http MCP 桥：给只支持 stdio 的 Agent 用。

stdin 读 JSON-RPC 帧 → POST 转发到 AgentDeck /mcp → stdout 回写。
SSE 响应取 data: 行 JSON；透传 mcp-session-id。
若设置 AGENT_DECK_ID / AGENT_DECK_TOKEN 环境变量，自动注入 tools/call 参数
（agent_id/token 缺省时），Agent 无需在参数里传令牌。
"""
import json
import os
import sys

import httpx

GATEWAY_URL = os.environ.get("AGENT_DECK_URL", "http://127.0.0.1:8765/mcp")
AGENT_ID = os.environ.get("AGENT_DECK_ID", "")
AGENT_TOKEN = os.environ.get("AGENT_DECK_TOKEN", "")


def _inject_auth(payload: dict) -> dict:
    if (AGENT_ID and AGENT_TOKEN
            and payload.get("method") == "tools/call"):
        args = payload.get("params", {}).setdefault("arguments", {})
        args.setdefault("agent_id", AGENT_ID)
        args.setdefault("token", AGENT_TOKEN)
    return payload


def main() -> None:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if AGENT_ID and AGENT_TOKEN:
        headers["X-Agent-Id"] = AGENT_ID
        headers["Authorization"] = f"Bearer {AGENT_TOKEN}"
    session_id = None
    with httpx.Client(trust_env=False, timeout=60) as client:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                payload = _inject_auth(json.loads(line))
            except json.JSONDecodeError:
                continue
            h = dict(headers)
            if session_id:
                h["mcp-session-id"] = session_id
            resp = client.post(GATEWAY_URL, json=payload, headers=h)
            sid = resp.headers.get("mcp-session-id")
            if sid:
                session_id = sid
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                for sline in resp.text.splitlines():
                    if sline.startswith("data: "):
                        print(sline[6:], flush=True)
            elif resp.text:
                print(resp.text, flush=True)


if __name__ == "__main__":
    main()
