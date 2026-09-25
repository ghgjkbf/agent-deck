"""SQLite 存储层：单文件 + 全局锁串行化（沿用 Agent Room db.py 模式）。

DB 路径：环境变量 AGENT_DECK_DB 优先，默认 server/agentdeck.db。
"""
import os
import sqlite3
import threading
from contextlib import contextmanager

from .config import settings

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agentdeck.db")
DB_PATH = settings.db_path or _DEFAULT_DB

_lock = threading.Lock()


@contextmanager
def db():
    """每次调用新开连接；row 访问用 dict 风格 row["col"]。"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with _lock:
            yield conn
            conn.commit()
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'executor',        -- leader | executor
  token_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'offline',       -- online | busy | offline
  last_seen TEXT NOT NULL,
  skills TEXT NOT NULL DEFAULT '[]',            -- JSON: 擅长能力标签
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL,
  goal TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'awaiting_confirm', -- awaiting_confirm|running|done|aborted
  leader_id TEXT NOT NULL,
  summary TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subtasks (
  subtask_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  title TEXT NOT NULL,
  guidance TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|claimed|submitted|verified|rejected|released|escalated
  claimant_id TEXT,
  claimed_at TEXT,
  depends_on TEXT NOT NULL DEFAULT '[]',   -- JSON: 依赖的 seq 列表
  assignee TEXT,                           -- 定向派工：指定执行者 agent_id（可空=自由认领）
  retry_count INTEGER NOT NULL DEFAULT 0,
  escalated INTEGER NOT NULL DEFAULT 0,
  deliverable TEXT,
  last_receipt TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  msg_id TEXT UNIQUE NOT NULL,
  room_id TEXT NOT NULL,
  type TEXT NOT NULL,               -- chat|deliver|dispatch|receipt|system|interrupt|task
  priority INTEGER NOT NULL DEFAULT 3,   -- 0 = P0
  sender_kind TEXT NOT NULL,        -- agent|leader|system
  sender_id TEXT NOT NULL,
  payload_text TEXT,
  mentions TEXT NOT NULL DEFAULT '[]',
  parent_task_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  subtask_id TEXT NOT NULL,
  path TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  content TEXT NOT NULL,
  author TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(task_id, subtask_id, path)
);
CREATE TABLE IF NOT EXISTS progress (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subtask_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

# 轻量增量迁移：老库补列
_MIGRATIONS = [
    ("agents", "skills", "ALTER TABLE agents ADD COLUMN skills TEXT NOT NULL DEFAULT '[]'"),
    ("subtasks", "assignee", "ALTER TABLE subtasks ADD COLUMN assignee TEXT"),
]


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        for table, col, ddl in _MIGRATIONS:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(ddl)


init_db()
