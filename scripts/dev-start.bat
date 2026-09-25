@echo off
rem AgentDeck 一键启动：uv 环境 + uvicorn 127.0.0.1:8765
cd /d "%~dp0.."
if not exist .venv (
    echo [agent-deck] creating venv with uv ...
    uv sync
)
uv run uvicorn server.main:app --host 127.0.0.1 --port 8765
