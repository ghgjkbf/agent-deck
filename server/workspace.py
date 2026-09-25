"""文件工作区：任务目录隔离 + 乐观锁（自 Agent Room 精简）。

路径固定在 workspace/tasks/{task_id}/{subtask_id}/ 下，path 允许相对子路径。
files 表存最新内容与版本号；磁盘仅作归档镜像。
"""
import os
import re
import uuid

from .core.db import db
from .core.message import now_cst

_BASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "workspace")

# 仅允许普通文件名与子目录（不含 ..），防路径逃逸
_SAFE_RE = re.compile(r"^[\w\-一-鿿]+(?:/[\w\-一-鿿]+)*\.[\w]{1,8}$")
# task/subtask id 白名单（拼进磁盘路径前必须过）
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _disk_path(task_id: str, subtask_id: str, path: str) -> str:
    if not _ID_RE.match(task_id) or not _ID_RE.match(subtask_id):
        raise ValueError("非法 id")
    parts = path.split("/")
    d = os.path.join(_BASE, "tasks", task_id, subtask_id, *parts[:-1])
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, parts[-1])


def fs_list(task_id: str, subtask_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT path, version, author, updated_at FROM files"
            " WHERE task_id=? AND subtask_id=? ORDER BY path",
            (task_id, subtask_id)).fetchall()
    return [dict(r) for r in rows]


def read_file(task_id: str, subtask_id: str, path: str) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT path, version, content, author, updated_at FROM files"
            " WHERE task_id=? AND subtask_id=? AND path=?",
            (task_id, subtask_id, path)).fetchone()
    if not row:
        raise FileNotFoundError(f"{path} 不存在")
    return dict(row)


def write_file(task_id: str, subtask_id: str, path: str, content: str,
               author: str, base_version: int | None = None) -> dict:
    """乐观锁：base_version 不匹配当前版本 → 409 + 最新版本号。"""
    if not _SAFE_RE.match(path):
        raise ValueError("路径仅允许 字母数字/中文 与一层子目录，如 docs/plan.md")
    with db() as conn:
        row = conn.execute(
            "SELECT version FROM files WHERE task_id=? AND subtask_id=? AND path=?",
            (task_id, subtask_id, path)).fetchone()
        cur_version = row["version"] if row else 0
        if base_version is not None and base_version != cur_version:
            raise PermissionError(
                f"版本冲突：当前 v{cur_version}，你基于 v{base_version}。请重读后凭新版本重写。")
        new_version = cur_version + 1
        conn.execute(
            "INSERT INTO files (id, task_id, subtask_id, path, version, content,"
            " author, updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(task_id, subtask_id, path) DO UPDATE SET"
            " version=excluded.version, content=excluded.content,"
            " author=excluded.author, updated_at=excluded.updated_at",
            (uuid.uuid4().hex, task_id, subtask_id, path, new_version,
             content, author, now_cst()))
    dp = _disk_path(task_id, subtask_id, path)
    with open(dp, "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "path": path, "version": new_version}
