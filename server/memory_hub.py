"""公共向量记忆：本地哈希向量，零外部依赖（自 Agent Room embeddings/hub 精简）。

- embed：256 维 unigram+bigram MD5 哈希向量（离线可用）
- Collection：JSON 文件落盘 + 线程锁，暴力余弦
"""
import json
import hashlib
import math
import os
import threading

from .core.message import now_cst

_DIM = 256
_BASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "workspace", "memory")


def embed(text: str) -> list[float]:
    vec = [0.0] * _DIM
    t = (text or "").lower()
    grams = [t[i:i + 1] for i in range(len(t))] + \
            [t[i:i + 2] for i in range(len(t) - 1)]
    for g in grams:
        h = hashlib.md5(g.encode("utf-8")).digest()
        idx = int.from_bytes(h[:2], "big") % _DIM
        vec[idx] += 1.0 if h[2] % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class Collection:
    def __init__(self, name: str):
        self.name = name
        self._lock = threading.Lock()
        self._path = os.path.join(_BASE, f"{name}.json")
        self._records: list[dict] = []
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            with open(self._path, encoding="utf-8") as f:
                self._records = json.load(f)

    def _save(self):
        os.makedirs(_BASE, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._records, f, ensure_ascii=False)

    def add(self, text: str, meta: dict | None = None) -> None:
        with self._lock:
            self._records.append({
                "text": text, "meta": meta or {},
                "vector": embed(text), "created_at": now_cst()})
            self._save()

    def search(self, query: str, k: int = 3) -> list[dict]:
        if not self._records:
            return []
        qv = embed(query)
        scored = [( _cosine(qv, r["vector"]), r) for r in self._records]
        scored.sort(key=lambda x: -x[0])
        return [{"text": r["text"], "meta": r["meta"],
                 "created_at": r["created_at"], "score": round(s, 4)}
                for s, r in scored[:k] if s > 0]

    def clear(self) -> None:
        with self._lock:
            self._records = []
            self._save()


class MemoryHub:
    """公共记忆按房间隔离：room_{room_id}_public。"""

    def __init__(self):
        self._cols: dict[str, Collection] = {}

    def _col(self, room_id: str) -> Collection:
        name = f"room_{room_id}_public"
        if name not in self._cols:
            self._cols[name] = Collection(name)
        return self._cols[name]

    def write_public(self, room_id: str, text: str, meta: dict | None = None):
        self._col(room_id).add(text, meta)

    def search_public(self, room_id: str, query: str, k: int = 3) -> list[dict]:
        return self._col(room_id).search(query, k)


hub = MemoryHub()
