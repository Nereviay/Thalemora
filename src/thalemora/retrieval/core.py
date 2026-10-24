"""只读 Memory 的可重建检索派生层；提取自 retrieval_shadow.py。
显式路径、模型与 embedder；边界验证同维度有限非零向量并实施 L2 归一化。
调用方在修订、状态或 lineage 变更后显式刷新索引；不加载模型。
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import numbers
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from typing import Mapping, Sequence
import numpy as np
SCHEMA_VERSION = 3
VECTOR_CONTRACT = 1  # finite, nonzero, flat real vector, L2 unit float32

class RetrievalShadowError(RuntimeError):
    pass

class VectorContractError(RetrievalShadowError):
    """Invalid numerical/model contract, NOT an optional service outage."""


def unit_vector(value) -> np.ndarray:
    """Reject malformed shapes/types; scale before norm to avoid overflow/underflow."""
    try:
        raw = np.asarray(value, dtype=object)
        if raw.ndim != 1 or not raw.size or any(
                isinstance(x, (bool, np.bool_)) or not isinstance(x, numbers.Real) for x in raw):
            raise VectorContractError('vector must be a nonempty flat real numeric vector')
        vector = np.asarray(value, dtype=np.float64)
    except (ValueError, TypeError, OverflowError) as exc:
        raise VectorContractError('invalid numeric vector') from exc
    if not np.isfinite(vector).all():
        raise VectorContractError('vector must be finite')
    scale = np.max(np.abs(vector))
    if scale == 0:
        raise VectorContractError('zero vector is not a direction')
    scaled = vector / scale
    return np.asarray(scaled / np.linalg.norm(scaled), dtype=np.float32)


def cached_vector(row) -> np.ndarray:
    """A marked cache row must already satisfy the contract; never silently fix it."""
    dim = row['vector_dim']
    if type(dim) is not int or dim <= 0:
        raise VectorContractError('invalid cached vector dimension')
    try:
        vector = np.frombuffer(row['vector'], dtype=np.float32)
    except (ValueError, TypeError) as exc:
        raise VectorContractError('invalid cached vector bytes') from exc
    if (vector.size != dim or not np.isfinite(vector).all()
            or not np.isclose(np.linalg.norm(vector.astype(np.float64)), 1., rtol=1e-6, atol=1e-7)):
        raise VectorContractError('cached vector violates unit/dimension contract')
    return vector

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
            # Two independent initializers can both read the old journal mode
            # then fail its lock upgrade immediately, despite busy_timeout.
            # Retry ONLY SQLite BUSY here (<= ~2.1s); never retry model inference.
            deadline = time.monotonic() + 2.0
            conn.execute('PRAGMA busy_timeout=100')
            while True:
                try:
                    conn.execute('PRAGMA journal_mode=WAL')
                    break
                except sqlite3.OperationalError as exc:
                    if getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            conn.execute('PRAGMA busy_timeout=10000')
            conn.executescript(SCHEMA)
            # Only the disposable retrieval cache evolves. Old vectors have no
            # trustworthy binding, so keep lexical rows but rebuild vectors lazily.
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT schema_version FROM retrieval_shadow_schema WHERE singleton=1').fetchone()
            if row is not None and int(row['schema_version']) not in (1, 2, SCHEMA_VERSION):
                raise RetrievalShadowError('retrieval shadow schema mismatch')
            columns = {r['name'] for r in conn.execute('PRAGMA table_info(chunks)')}
            for name, definition in (
                ('generation', 'INTEGER NOT NULL DEFAULT 0'),
                ('target_model', 'TEXT'),
                ('vector_generation', 'INTEGER'),
                ('vector_content_hash', 'TEXT'),
                ('vector_contract', 'INTEGER NOT NULL DEFAULT 0'),
            ):
                if name not in columns:
                    conn.execute(f'ALTER TABLE chunks ADD COLUMN {name} {definition}')
            conn.execute('CREATE TABLE IF NOT EXISTS model_dimensions ('
                         'model TEXT PRIMARY KEY, dimension INTEGER NOT NULL CHECK(dimension>0))')
            conn.execute('CREATE TABLE IF NOT EXISTS lane_generations ('
                         'source_kind TEXT PRIMARY KEY, generation INTEGER NOT NULL)')
            conn.executemany('INSERT OR IGNORE INTO lane_generations VALUES(?,0)',
                             [('memory_card',), ('raw_event',)])
            conn.execute('INSERT OR REPLACE INTO retrieval_shadow_schema VALUES(1,?,?)',
                         (SCHEMA_VERSION, '仅可重建派生索引；Memory 与原始证据保持唯一权威'))

    def _generation(self, source_kind: str) -> int:
        with self._connect() as conn:
            row = conn.execute('SELECT generation FROM lane_generations WHERE source_kind=?',
                               (source_kind,)).fetchone()
        if row is None:
            raise RetrievalShadowError('invalid source_kind')
        return int(row[0])

    @staticmethod
    def _vector_bound(row, model: str) -> bool:
        bound = (row['vector'] is not None and row['generation'] > 0
                 and row['vector_generation'] == row['generation']
                 and row['vector_content_hash'] == row['content_hash']
                 and row['embedding_model'] == row['target_model'] == model
                 and row['vector_contract'] == VECTOR_CONTRACT)
        if bound:
            cached_vector(row)  # marked but corrupt is an error, not a dropped row
        return bound

    @staticmethod
    def _check_dimension(conn, model: str, dimension: int, *, publish=False):
        row = conn.execute('SELECT dimension FROM model_dimensions WHERE model=?', (model,)).fetchone()
        if row is not None and row[0] != dimension:
            raise VectorContractError('embedding model dimension changed; use a new model identity')
        if row is None and publish:
            conn.execute('INSERT INTO model_dimensions VALUES(?,?)', (model, dimension))

    @staticmethod
    def _source_bundle_hash(rows: Sequence[Mapping]) -> str:
        return _sha(_stable_json(list(rows)))

    def _replace_lane(
        self, source_kind: str, rows, *, source_path: str, embedder=None,
        batch_size: int = 16, expected_generation: int | None = None,
        selector: tuple[str, tuple] | None = None, single: bool = False,
    ) -> dict:
        """Publish metadata then CAS vector bindings, never infer under a write lock.

        Suppliers are evaluated AFTER the generation fence is read. A caller with
        an already materialized snapshot must capture/pass expected_generation
        before reading its sources. selector is an internal subset of this lane.
        Any concurrent lane/subset publication supersedes an in-flight batch;
        missing vectors are detectable and repaired by the next freshness pass.
        """
        if source_kind not in {'memory_card', 'raw_event'}:
            raise RetrievalShadowError('invalid source_kind')
        model = self.embedding_model  # snapshot identity before source/model work
        expected = self._generation(source_kind) if expected_generation is None else expected_generation
        rows = list(rows() if callable(rows) else rows)
        bundle_hash = self._source_bundle_hash(rows)
        generation = expected + 1
        build_id = f'{source_kind}:{generation}:{bundle_hash[:20]}'
        result = dict(build_id=build_id, source_kind=source_kind, source_count=len(rows),
                      embedded_count=0, reused_count=0, embedding_model=None,
                      source_max_id=max((int(r['source_id']) for r in rows), default=None),
                      content_bundle_hash=bundle_hash, generation=generation, discarded=False)
        where, params = selector or ('1=1', ())
        predicate = 'source_kind=? AND (' + where + ')'
        pending = []
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            updated = conn.execute('UPDATE lane_generations SET generation=generation+1 '
                                   'WHERE source_kind=? AND generation=?', (source_kind, expected))
            if updated.rowcount != 1:
                return dict(result, discarded=True)
            previous = {r['source_key']: r for r in conn.execute(
                'SELECT * FROM chunks WHERE ' + predicate, (source_kind, *params))}
            for key in previous:
                conn.execute('DELETE FROM chunks_fts WHERE source_key=?', (key,))
            conn.execute('DELETE FROM chunks WHERE ' + predicate, (source_kind, *params))
            now = _now()
            for row in rows:
                old = previous.get(row['source_key'])
                reuse = (old is not None and old['content_hash'] == row['content_hash']
                         and self._vector_bound(old, model))
                if reuse:
                    self._check_dimension(conn, model, old['vector_dim'])
                conn.execute(
                    'INSERT INTO chunks(source_kind,source_key,source_id,raw_event_start_id,'
                    'raw_event_end_id,epoch_id,role,event_time,status,importance,title,text,'
                    'content_hash,provenance_json,created_at,generation,target_model,'
                    'vector,vector_dim,embedding_model,embedded_at,vector_generation,vector_content_hash,vector_contract) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (source_kind, row['source_key'], int(row['source_id']),
                     row.get('raw_event_start_id'), row.get('raw_event_end_id'), row.get('epoch_id', ''),
                     row.get('role', ''), row.get('event_time', ''), row.get('status', ''),
                     row.get('importance'), row.get('title', ''), row['text'], row['content_hash'],
                     _stable_json(row.get('provenance', {})), now, generation, model,
                     old['vector'] if reuse else None, old['vector_dim'] if reuse else None,
                     model if reuse else None, old['embedded_at'] if reuse else None,
                     generation if reuse else None, row['content_hash'] if reuse else None,
                     VECTOR_CONTRACT if reuse else 0))
                conn.execute('INSERT INTO chunks_fts(source_key,text) VALUES(?,?)',
                             (row['source_key'], row['text']))
                if reuse:
                    result['reused_count'] += 1
                else:
                    pending.append(row)

        vectors = []
        if embedder is not None and pending:
            vectors = ([embedder.encode_one(r['text']) for r in pending] if single else
                       embedder.encode_many([r['text'] for r in pending], batch_size=batch_size))
            if not isinstance(vectors, (list, tuple, np.ndarray)):
                raise VectorContractError('embedder must return a batch of vectors')
            if isinstance(vectors, np.ndarray) and vectors.ndim == 0:
                raise VectorContractError('embedder must return a batch of vectors')
            if len(vectors) != len(pending):
                raise RetrievalShadowError('embedder returned wrong vector count')
            # Numerical validation and normalization also run outside the write transaction.
            vectors = [unit_vector(v) for v in vectors]
            if len({v.size for v in vectors}) != 1:
                raise VectorContractError('embedder returned inconsistent batch dimensions')
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT generation FROM lane_generations WHERE source_kind=?',
                                   (source_kind,)).fetchone()[0]
            if current != generation:
                return dict(result, discarded=True)
            if vectors:
                self._check_dimension(conn, model, int(vectors[0].size), publish=True)
            for row, vector in zip(pending, vectors):
                changed = conn.execute(
                    'UPDATE chunks SET vector=?,vector_dim=?,embedding_model=?,embedded_at=?,'
                    'vector_generation=?,vector_content_hash=?,vector_contract=? WHERE source_kind=? AND source_key=? '
                    'AND generation=? AND content_hash=? AND target_model=?',
                    (vector.tobytes(), int(vector.size), model, _now(), generation, row['content_hash'], VECTOR_CONTRACT,
                     source_kind, row['source_key'], generation, row['content_hash'], model))
                if changed.rowcount != 1:
                    # Never claim successful work when its destination vanished.
                    raise RetrievalShadowError('vector destination CAS lost')
                result['embedded_count'] += changed.rowcount
            result['embedding_model'] = model if vectors else None
            conn.execute(
                'INSERT INTO build_receipts(build_id,source_kind,source_path,source_count,'
                'embedded_count,embedding_model,source_max_id,content_bundle_hash,created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (build_id, source_kind, os.path.abspath(source_path), len(rows),
                 result['embedded_count'], result['embedding_model'], result['source_max_id'],
                 bundle_hash, _now()))
        return result

    @staticmethod
    def _memory_rows(memory_db: str, *, connection=None) -> list[dict]:
        with (nullcontext(connection) if connection is not None else _ro(memory_db)) as conn:
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
            from thalemora.memory.extensions import load_extensions
            extensions = load_extensions(conn)
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
        # Independent units reuse the existing memory_card lane and schema.
        # source_id remains the parent card ID; source_key is the unit identity.
        for item in extensions:
            if item['eligibility'] != 'current' or not item['content'].strip():
                continue
            text = item['content'].strip()
            out.append({
                'source_key': f"memory:{item['card_id']}:extension:{item['extension_id']}",
                'source_id': item['card_id'], 'status': item['card_status'],
                'importance': item['importance'], 'title': '',
                'event_time': item['happened_at'] or '', 'text': text,
                'content_hash': _sha(text),
                'provenance': {
                    'card_id': item['card_id'], 'revision_id': item['base_revision_id'],
                    'extension_id': item['extension_id'], 'base_revision_id': item['base_revision_id'],
                    'text_projection': 'extension', 'keywords': item['keywords'] or '',
                    'evidence_refs': item['evidence_refs'],
                },
            })
        return out

    def refresh_memory_card(self, card_id: int, *, embedder=None) -> dict:
        """Atomically replace this card's canonical and extension units."""
        embedder = self.embedder if embedder is None else embedder
        cid = int(card_id)
        result = self._replace_lane(
            'memory_card',
            lambda: [r for r in self._memory_rows(self.memory_db_path) if int(r['source_id']) == cid],
            source_path=self.memory_db_path, embedder=embedder,
            selector=('source_id=?', (cid,)), single=True)
        ready = not result['discarded'] and bool(result['embedded_count'] + result['reused_count'])
        return dict(card_id=cid, present=bool(result['source_count']), embedded=ready,
                    embedding_model=self.embedding_model if ready else None,
                    discarded=result['discarded'])

    def rebuild_memory(self, *, batch_size: int = 16) -> dict:
        return self._replace_lane(
            'memory_card', lambda: self._memory_rows(self.memory_db_path),
            source_path=self.memory_db_path, embedder=self.embedder, batch_size=batch_size)

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
        q = unit_vector(query_vector)
        with self._connect() as conn:
            self._check_dimension(conn, self.embedding_model, int(q.size))
            rows = conn.execute(
                "SELECT * FROM chunks "
                "WHERE source_kind=? AND vector IS NOT NULL",
                (source_kind,),
            ).fetchall()
        keys = []
        vectors = []
        for row in rows:
            if not self._vector_bound(row, row['embedding_model']):
                continue  # v1/unbound/unfinished cache vectors are never trusted
            if row["embedding_model"] != self.embedding_model:
                raise RetrievalShadowError("索引模型已变化，请先更新或重建")
            vec = cached_vector(row)
            if vec.size != q.size:
                raise VectorContractError("索引向量维度损坏或与查询模型不匹配")
            keys.append(row["source_key"])
            vectors.append(vec)
        if not vectors:
            return []
        matrix = np.vstack(vectors)
        scores = np.clip(matrix @ q, -1., 1.)
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
            reasons.setdefault(key, []).append(f'vector_rank:{rank}:{cosine!r}')
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
