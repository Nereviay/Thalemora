"""既有卡表的 BM25 缓存。只在 TTL 到期且索引内容改变时重建。"""
import hashlib
import sqlite3
import threading
import time
from .bm25 import build_index

class CachedBM25:
    def __init__(self, path, ttl):
        self.path = path
        self.ttl = ttl
        self.cache = {"index": None, "built_at": 0, "fingerprint": None}
        self.lock = threading.Lock()

    def _source_snapshot(self):
        """Load the exact rows used by BM25 and fingerprint that same snapshot."""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(cards)").fetchall()}
            fields = ["id", "title", "content", "tags", "keywords", "status"]
            kind_filter = ""
            if "memory_kind" in columns:
                fields.append("memory_kind")
                kind_filter = " WHERE memory_kind='episodic'"
            rows = conn.execute(
                f"SELECT {','.join(fields)} FROM cards"
                + kind_filter + " ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

        digest = hashlib.sha256()
        for row in rows:
            for field in fields:
                value = row[field]
                if value is None:
                    payload = b"n"
                elif isinstance(value, bytes):
                    payload = b"b" + value
                else:
                    payload = b"s" + str(value).encode("utf-8")
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
        return rows, digest.digest()

    def _ensure_index(self, force=False):
        now = time.time()
        idx = self.cache["index"]
        if not force and idx is not None and now - self.cache["built_at"] < self.ttl:
            return idx
        with self.lock:
            now = time.time()
            idx = self.cache["index"]
            if force or idx is None:
                rows, fingerprint = self._source_snapshot()
                self.cache["index"] = build_index(rows)
                self.cache["fingerprint"] = fingerprint
                self.cache["built_at"] = time.time()
            elif now - self.cache["built_at"] >= self.ttl:
                rows, fingerprint = self._source_snapshot()
                if fingerprint != self.cache["fingerprint"]:
                    self.cache["index"] = build_index(rows)
                    self.cache["fingerprint"] = fingerprint
                self.cache["built_at"] = time.time()
            return self.cache["index"]

    def search(self, query, limit=10, include_sunk=False, include_archived=False):
        return self._ensure_index().search(query, limit, include_sunk, include_archived)
