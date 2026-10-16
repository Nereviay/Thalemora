"""只读 Memory 的可重建检索派生层；提取自 retrieval_shadow.py。
显式路径、模型与 embedder；encode_many / encode_one 返回同维度归一化向量。
调用方在修订、状态或 lineage 变更后显式刷新索引；不加载模型。
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Mapping, Sequence
import numpy as np
SCHEMA_VERSION = 1

class RetrievalShadowError(RuntimeError):
    pass

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')

def _stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def _sha(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

@contextmanager
def _ro(path: str):
    conn = sqlite3.connect(f'file:{os.path.abspath(path)}?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=10000')
    try:
        yield conn
    finally:
        conn.close()


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS retrieval_shadow_schema (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    schema_version INTEGER NOT NULL,
    authority_note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('memory_card','raw_event')),
    source_key TEXT NOT NULL UNIQUE,
    source_id INTEGER NOT NULL,
    raw_event_start_id INTEGER,
    raw_event_end_id INTEGER,
    epoch_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    event_time TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    importance REAL,
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    vector BLOB,
    vector_dim INTEGER,
    embedding_model TEXT,
    embedded_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_retrieval_chunks_kind
ON chunks(source_kind, source_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_chunks_epoch
ON chunks(epoch_id, source_kind);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
USING fts5(source_key UNINDEXED, text, tokenize='trigram');
CREATE TABLE IF NOT EXISTS build_receipts (
    build_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_count INTEGER NOT NULL,
    embedded_count INTEGER NOT NULL,
    embedding_model TEXT,
    source_max_id INTEGER,
    content_bundle_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

class RetrievalIndex:

    def __init__(self, db_path: str, *, memory_db_path: str, embedding_model: str, embedder):
        self.db_path = os.path.abspath(os.path.expanduser(db_path))
        self.memory_db_path = os.path.abspath(os.path.expanduser(memory_db_path))
        if self.db_path == self.memory_db_path or (os.path.exists(self.db_path) and os.path.exists(self.memory_db_path) and os.path.samefile(self.db_path, self.memory_db_path)):
            raise ValueError('索引路径必须与 Memory 来源分离')
        if not embedding_model.strip():
            raise ValueError('必须显式传入 embedding_model')
        self.embedding_model = embedding_model
        self.embedder = embedder
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=10000')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(SCHEMA)
            row = conn.execute('SELECT schema_version FROM retrieval_shadow_schema WHERE singleton=1').fetchone()
            if row is None:
                conn.execute('INSERT INTO retrieval_shadow_schema(singleton,schema_version,authority_note) VALUES(1,?,?)', (SCHEMA_VERSION, '仅可重建派生索引；Memory 与原始证据保持唯一权威'))
            elif int(row['schema_version']) != SCHEMA_VERSION:
                raise RetrievalShadowError('retrieval shadow schema mismatch')

    @staticmethod
    def _source_bundle_hash(rows: Sequence[Mapping]) -> str:
        payload = [(row['source_key'], row['content_hash'], row.get('epoch_id', ''), row.get('status', '')) for row in rows]
        return _sha(_stable_json(payload))

    def _replace_lane(
        self,
        source_kind: str,
        rows: Sequence[Mapping],
        *,
        source_path: str,
        embedder=None,
        batch_size: int = 16,
    ) -> dict:
        if source_kind not in {"memory_card", "raw_event"}:
            raise RetrievalShadowError("invalid source_kind")
        rows = list(rows)
        preserved_vectors = {}
        with self._connect() as conn:
            existing_rows = conn.execute(
                "SELECT source_key,content_hash,vector,vector_dim,embedding_model,embedded_at "
                "FROM chunks WHERE source_kind=?",
                (source_kind,),
            ).fetchall()
            existing = [row["source_key"] for row in existing_rows]
            preserved_vectors = {
                row["source_key"]: {
                    "content_hash": row["content_hash"],
                    "vector": row["vector"],
                    "vector_dim": row["vector_dim"],
                    "embedding_model": row["embedding_model"],
                    "embedded_at": row["embedded_at"],
                }
                for row in existing_rows
                if row["vector"] is not None
            }
            if existing:
                conn.executemany(
                    "DELETE FROM chunks_fts WHERE source_key=?",
                    [(key,) for key in existing],
                )
            conn.execute("DELETE FROM chunks WHERE source_kind=?", (source_kind,))

            now = _now()
            for row in rows:
                conn.execute(
                    "INSERT INTO chunks("
                    "source_kind,source_key,source_id,raw_event_start_id,raw_event_end_id,"
                    "epoch_id,role,event_time,status,importance,title,text,content_hash,"
                    "provenance_json,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source_kind,
                        row["source_key"],
                        int(row["source_id"]),
                        row.get("raw_event_start_id"),
                        row.get("raw_event_end_id"),
                        row.get("epoch_id", ""),
                        row.get("role", ""),
                        row.get("event_time", ""),
                        row.get("status", ""),
                        row.get("importance"),
                        row.get("title", ""),
                        row["text"],
                        row["content_hash"],
                        _stable_json(row.get("provenance", {})),
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO chunks_fts(source_key,text) VALUES(?,?)",
                    (row["source_key"], row["text"]),
                )
                previous = preserved_vectors.get(row["source_key"])
                if (
                    previous
                    and previous.get("content_hash") == row["content_hash"]
                    and previous.get("vector") is not None
                    and previous.get("embedding_model") == self.embedding_model
                ):
                    conn.execute(
                        "UPDATE chunks SET vector=?,vector_dim=?,embedding_model=?,embedded_at=? "
                        "WHERE source_key=?",
                        (
                            previous.get("vector"),
                            previous.get("vector_dim"),
                            previous.get("embedding_model"),
                            previous.get("embedded_at"),
                            row["source_key"],
                        ),
                    )

        embedded = 0
        model_name = None
        rows_to_embed = [
            row for row in rows
            if not (
                (preserved_vectors.get(row["source_key"]) or {}).get("content_hash")
                == row["content_hash"]
                and (preserved_vectors.get(row["source_key"]) or {}).get("vector") is not None
                and (preserved_vectors.get(row["source_key"]) or {}).get("embedding_model") == self.embedding_model
            )
        ]
        if embedder is not None and rows_to_embed:
            texts = [row["text"] for row in rows_to_embed]
            vectors = embedder.encode_many(texts, batch_size=batch_size)
            if len(vectors) != len(rows_to_embed):
                raise RetrievalShadowError("embedder returned wrong vector count")
            model_name = self.embedding_model
            embedded_at = _now()
            with self._connect() as conn:
                for row, vector in zip(rows_to_embed, vectors):
                    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
                    conn.execute(
                        "UPDATE chunks SET vector=?,vector_dim=?,embedding_model=?,embedded_at=? "
                        "WHERE source_key=?",
                        (
                            arr.tobytes(),
                            int(arr.shape[0]),
                            model_name,
                            embedded_at,
                            row["source_key"],
                        ),
                    )
                    embedded += 1

        max_id = max((int(row["source_id"]) for row in rows), default=None)
        bundle_hash = self._source_bundle_hash(rows)
        build_id = f"{source_kind}:{bundle_hash[:20]}"
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO build_receipts("
                "build_id,source_kind,source_path,source_count,embedded_count,"
                "embedding_model,source_max_id,content_bundle_hash,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    build_id,
                    source_kind,
                    os.path.abspath(source_path),
                    len(rows),
                    embedded,
                    model_name,
                    max_id,
                    bundle_hash,
                    _now(),
                ),
            )
        return {
            "build_id": build_id,
            "source_kind": source_kind,
            "source_count": len(rows),
            "embedded_count": embedded,
            "embedding_model": model_name,
            "source_max_id": max_id,
            "content_bundle_hash": bundle_hash,
        }

    @staticmethod
    def _memory_rows(memory_db: str) -> list[dict]:
        with _ro(memory_db) as conn:
            card_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(cards)").fetchall()
            }
            optional = {
                "valence": "valence" if "valence" in card_columns else "NULL AS valence",
                "arousal": "arousal" if "arousal" in card_columns else "NULL AS arousal",
                "hits": "hits" if "hits" in card_columns else "0 AS hits",
                "last_hit_at": (
                    "last_hit_at" if "last_hit_at" in card_columns else "NULL AS last_hit_at"
                ),
                "created_at": (
                    "created_at" if "created_at" in card_columns else "NULL AS created_at"
                ),
                "pinned": "pinned" if "pinned" in card_columns else "0 AS pinned",
            }
            has_lineage = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='memory_card_lineage'"
            ).fetchone()
            lineage_filter = (
                "AND NOT EXISTS (SELECT 1 FROM memory_card_lineage l "
                "WHERE l.from_card_id=cards.id AND l.retired_at IS NULL) "
                if has_lineage else ""
            )
            rows = conn.execute(
                "SELECT id,title,content,status,importance,happened_at,current_revision_id,"
                "keywords,memory_kind,"
                + ",".join(optional.values())
                + " FROM cards WHERE status!='trashed' "
                "AND memory_kind='episodic' " + lineage_filter + "ORDER BY id"
            ).fetchall()
            has_evidence = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='card_evidence'"
            ).fetchone()
            evidence_rows = (
                conn.execute(
                    "SELECT revision_id,source_kind,source_ref,raw_event_start_id,"
                    "raw_event_end_id,event_hash FROM card_evidence ORDER BY id"
                ).fetchall()
                if has_evidence
                else []
            )
            has_organized = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='memory_organized_content'"
            ).fetchone()
            organized_rows = (
                conn.execute(
                    "SELECT card_id,based_on_revision_id,content "
                    "FROM memory_organized_content"
                ).fetchall()
                if has_organized else []
            )
        organized_by_card = {int(item["card_id"]): item for item in organized_rows}
        evidence_by_revision = {}
        for evidence in evidence_rows:
            evidence_by_revision.setdefault(int(evidence["revision_id"]), []).append({
                "source_kind": str(evidence["source_kind"] or ""),
                "source_ref": str(evidence["source_ref"] or ""),
                "raw_event_start_id": evidence["raw_event_start_id"],
                "raw_event_end_id": evidence["raw_event_end_id"],
                "event_hash": str(evidence["event_hash"] or ""),
            })
        out = []
        for row in rows:
            projection = organized_by_card.get(int(row["id"]))
            use_organized = (
                projection is not None
                and int(projection["based_on_revision_id"]) == int(row["current_revision_id"] or 0)
            )
            body = str(projection["content"] if use_organized else row["content"] or "").strip()
            text = (str(row["title"] or "").strip() + "\n" + body).strip()
            if not text:
                continue
            out.append(
                {
                    "source_key": f"memory:{int(row['id'])}",
                    "source_id": int(row["id"]),
                    "status": str(row["status"] or ""),
                    "importance": row["importance"],
                    "title": str(row["title"] or ""),
                    "event_time": str(row["happened_at"] or ""),
                    "text": text,
                    "content_hash": _sha(text),
                    "provenance": {
                        "card_id": int(row["id"]),
                        "revision_id": int(row["current_revision_id"] or 0),
                        "text_projection": "organized_first_person" if use_organized else "canonical",
                        "keywords": str(row["keywords"] or ""),
                        "valence": row["valence"],
                        "arousal": row["arousal"],
                        "hits": int(row["hits"] or 0),
                        "last_hit_at": str(row["last_hit_at"] or ""),
                        "created_at": str(row["created_at"] or ""),
                        "pinned": bool(row["pinned"]),
                        "evidence_refs": evidence_by_revision.get(
                            int(row["current_revision_id"] or 0), []
                        ),
                    },
                }
            )
        return out

    def refresh_memory_card(
        self,
        card_id: int,
        *,
        embedder=None,
    ) -> dict:
        """Refresh exactly one derived Memory chunk after a canonical revision."""
        memory_db = self.memory_db_path
        embedder = self.embedder if embedder is None else embedder
        cid = int(card_id)
        rows = [row for row in self._memory_rows(memory_db) if int(row["source_id"]) == cid]
        source_key = f"memory:{cid}"
        if not rows:
            with self._connect() as conn:
                conn.execute("DELETE FROM chunks_fts WHERE source_key=?", (source_key,))
                conn.execute("DELETE FROM chunks WHERE source_key=?", (source_key,))
            return {"card_id": cid, "present": False, "embedded": False}

        row = rows[0]
        vector = None
        model_name = None
        embedded_at = None
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT content_hash,vector,vector_dim,embedding_model,embedded_at "
                "FROM chunks WHERE source_key=?", (source_key,)
            ).fetchone()
        if (
            existing is not None
            and existing["content_hash"] == row["content_hash"]
            and existing["vector"] is not None
            and existing["embedding_model"] == self.embedding_model
        ):
            vector = np.frombuffer(existing["vector"], dtype=np.float32).copy()
            model_name = existing["embedding_model"]
            embedded_at = existing["embedded_at"]
        elif embedder is not None:
            vector = np.asarray(embedder.encode_one(row["text"]), dtype=np.float32).reshape(-1)
            model_name = self.embedding_model
            embedded_at = _now()
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks_fts WHERE source_key=?", (source_key,))
            conn.execute("DELETE FROM chunks WHERE source_key=?", (source_key,))
            conn.execute(
                "INSERT INTO chunks("
                "source_kind,source_key,source_id,raw_event_start_id,raw_event_end_id,"
                "epoch_id,role,event_time,status,importance,title,text,content_hash,"
                "provenance_json,vector,vector_dim,embedding_model,embedded_at,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "memory_card",
                    row["source_key"],
                    int(row["source_id"]),
                    row.get("raw_event_start_id"),
                    row.get("raw_event_end_id"),
                    row.get("epoch_id", ""),
                    row.get("role", ""),
                    row.get("event_time", ""),
                    row.get("status", ""),
                    row.get("importance"),
                    row.get("title", ""),
                    row["text"],
                    row["content_hash"],
                    _stable_json(row.get("provenance", {})),
                    None if vector is None else vector.tobytes(),
                    None if vector is None else int(vector.shape[0]),
                    model_name,
                    embedded_at,
                    _now(),
                ),
            )
            conn.execute(
                "INSERT INTO chunks_fts(source_key,text) VALUES(?,?)",
                (source_key, row["text"]),
            )
        return {
            "card_id": cid,
            "present": True,
            "embedded": vector is not None,
            "embedding_model": model_name,
        }

    def rebuild_memory(
        self,
        *,
        batch_size: int = 16,
    ) -> dict:
        memory_db, embedder = self.memory_db_path, self.embedder
        rows = self._memory_rows(memory_db)
        return self._replace_lane(
            "memory_card",
            rows,
            source_path=memory_db,
            embedder=embedder,
            batch_size=batch_size,
        )

    def _lexical_hits(self, query: str, source_kind: str, limit: int, *, literal=False) -> list[tuple[str, float]]:
        query = str(query or "").strip()
        if not query:
            return []
        with self._connect() as conn:
            hits = []
            rows = conn.execute(
                "SELECT c.source_key,bm25(chunks_fts) AS score "
                "FROM chunks_fts JOIN chunks c ON c.source_key=chunks_fts.source_key "
                "WHERE chunks_fts MATCH ? AND c.source_kind=? "
                "ORDER BY score LIMIT ?",
                ('"' + query.replace('"', '""') + '"' if literal else query, source_kind, int(limit)),
            ).fetchall()
            hits.extend((row["source_key"], float(row["score"])) for row in rows)
            if len(query) >= 2 or literal:
                like_rows = conn.execute(
                    "SELECT source_key FROM chunks WHERE source_kind=? AND text LIKE ? LIMIT ?",
                    (source_kind, f"%{query}%", int(limit)),
                ).fetchall()
                existing = {key for key, _ in hits}
                for row in like_rows:
                    if row["source_key"] not in existing:
                        hits.append((row["source_key"], 0.0))
        return hits[:limit]

    def _vector_hits(
        self,
        query_vector: np.ndarray,
        source_kind: str,
        limit: int,
    ) -> list[tuple[str, float]]:
        q = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        norm = np.linalg.norm(q)
        if norm <= 0:
            return []
        q = q / norm
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source_key,vector,vector_dim,embedding_model FROM chunks "
                "WHERE source_kind=? AND vector IS NOT NULL",
                (source_kind,),
            ).fetchall()
        keys = []
        vectors = []
        for row in rows:
            if row["embedding_model"] != self.embedding_model:
                raise RetrievalShadowError("索引模型已变化，请先更新或重建")
            dim = int(row["vector_dim"] or 0)
            vec = np.frombuffer(row["vector"], dtype=np.float32)
            if dim <= 0 or vec.size != dim or vec.size != q.size:
                raise RetrievalShadowError("索引向量维度损坏或与查询模型不匹配")
            keys.append(row["source_key"])
            vectors.append(vec)
        if not vectors:
            return []
        matrix = np.vstack(vectors)
        scores = matrix @ q
        n = min(int(limit), len(keys))
        if n <= 0:
            return []
        if n == len(keys):
            order = np.argsort(-scores)
        else:
            idx = np.argpartition(-scores, n - 1)[:n]
            order = idx[np.argsort(-scores[idx])]
        return [(keys[int(i)], float(scores[int(i)])) for i in order[:n]]

    def search(self, query: str, *, top_k: int=8, pool: int=40, lexical_queries: Sequence[str] | None=None) -> list[dict]:
        source_kind, embedder = 'memory_card', self.embedder
        if lexical_queries is None:
            lexical = self._lexical_hits(query, source_kind, pool)
        else:
            lexical = list(dict.fromkeys(key for term in lexical_queries
                                        for key, _ in self._lexical_hits(term, source_kind, pool, literal=True)))[:pool]
            lexical = [(key, 0.0) for key in lexical]
        vector = []
        if embedder is not None:
            vector = self._vector_hits(embedder.encode_one(query), source_kind, pool)
        scores: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        for (rank, (key, _score)) in enumerate(lexical, 1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (60.0 + rank)
            reasons.setdefault(key, []).append(f'lexical_rank:{rank}')
        for (rank, (key, cosine)) in enumerate(vector, 1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (60.0 + rank)
            reasons.setdefault(key, []).append(f'vector_rank:{rank}:{cosine:.4f}')
        if not scores:
            return []
        keys = list(scores)
        placeholders = ','.join(('?' for _ in keys))
        with self._connect() as conn:
            rows = conn.execute(f'SELECT * FROM chunks WHERE source_key IN ({placeholders})', keys).fetchall()
        by_key = {row['source_key']: row for row in rows}
        ordered = sorted(scores, key=lambda key: (-scores[key], key))[:int(top_k)]
        result = []
        for key in ordered:
            row = by_key.get(key)
            if row is None:
                continue
            item = dict(row)
            item.pop('vector', None)
            item['provenance'] = json.loads(item.pop('provenance_json'))
            item['retrieval_score'] = scores[key]
            item['retrieval_reasons'] = reasons.get(key, [])
            item['source_scope'] = 'long_term_memory'
            result.append(item)
        return result

    def list_memory_chunks(self, *, statuses: Sequence[str] | None=None) -> list[dict]:
        params = []
        where = ["source_kind='memory_card'"]
        if statuses:
            placeholders = ','.join(('?' for _ in statuses))
            where.append(f'status IN ({placeholders})')
            params.extend((str(x) for x in statuses))
        with self._connect() as conn:
            rows = conn.execute('SELECT * FROM chunks WHERE ' + ' AND '.join(where) + ' ORDER BY source_id', params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item.pop('vector', None)
            item['provenance'] = json.loads(item.pop('provenance_json'))
            out.append(item)
        return out
