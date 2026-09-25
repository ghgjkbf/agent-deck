"""AgentDeck 配置：环境变量优先，默认值兜底。"""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Settings:
    host = "127.0.0.1"
    port = _int("AGENT_DECK_PORT", 8765)
    db_path = os.environ.get("AGENT_DECK_DB", "")
    claim_timeout_s = _int("AGENT_DECK_CLAIM_TIMEOUT_S", 900)   # 15 分钟
    subtask_max_retries = _int("AGENT_DECK_SUBTASK_MAX_RETRIES", 2)
    memory_top_k = _int("AGENT_DECK_MEMORY_TOP_K", 3)
    # 管理令牌（人类控制端点：confirm/abort/p0/goal）。设置后请求须带
    # Authorization: Bearer <token>；未设置仅限本机使用（默认监听 127.0.0.1）。
    admin_token = os.environ.get("AGENT_DECK_ADMIN_TOKEN", "")


settings = Settings()
