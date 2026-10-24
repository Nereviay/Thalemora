"""原话与 Memory 联合检索；复用公共 Memory/词法/向量核心，不加载模型。"""

from __future__ import annotations

import json

import os

from thalemora.retrieval.core import RetrievalIndex, RetrievalShadowError, _ro, _sha

RAW_SOURCES = ("app", "rikkahub", "rikkahub_import", "gateway", "heartbeat")

class NiannianRetrievalIndex(RetrievalIndex):

    @staticmethod
    def _runtime_epoch_map(runtime_db: str) -> dict[int, str]:
        if not runtime_db or not os.path.exists(runtime_db):
            return {}
        with _ro(runtime_db) as conn:
            rows = conn.execute("SELECT raw_event_id,epoch_id FROM message_records WHERE raw_event_id IS NOT NULL AND status='active' ORDER BY id").fetchall()
        result = {}
        for row in rows:
            result[int(row['raw_event_id'])] = str(row['epoch_id'] or '')
        return result

    @staticmethod
    def _delivered_proactive_raw_ids(runtime_db: str | None) -> set[int]:
        if not runtime_db or not os.path.exists(runtime_db):
            return set()
        with _ro(runtime_db) as conn:
            rows = conn.execute("SELECT raw_event_id FROM message_records WHERE kind='proactive_message' AND status='active' AND json_extract(metadata_json,'$.proactive_delivery.status')='delivered' AND raw_event_id IS NOT NULL").fetchall()
        return {int(row['raw_event_id']) for row in rows}

    @staticmethod
    def _raw_rows(raw_db: str, runtime_db: str | None, *, max_events: int | None=None, raw_event_id: int | None=None) -> list[dict]:
        epoch_map = NiannianRetrievalIndex._runtime_epoch_map(runtime_db or '')
        delivered_proactive_ids = NiannianRetrievalIndex._delivered_proactive_raw_ids(runtime_db)
        placeholders = ','.join(('?' for _ in RAW_SOURCES))
        params: list = list(RAW_SOURCES)
        sql = 'SELECT id,source,source_event_id,event_hash,role,text,created_at,metadata_json FROM raw_events WHERE source IN (' + placeholders + ") AND role IN ('user','assistant') AND COALESCE(deleted,0)!=1 AND length(trim(COALESCE(text,'')))>0 AND COALESCE(json_extract(NULLIF(metadata_json,''),'$.channel'),'main')='main'"
        if raw_event_id is not None:
            sql += ' AND id=?'
            params.append(int(raw_event_id))
        sql += ' ORDER BY id'
        with _ro(raw_db) as conn:
            if max_events is None:
                db_rows = conn.execute(sql, params).fetchall()
            else:
                limited = 'SELECT * FROM (' + sql.replace(' ORDER BY id', ' ORDER BY id DESC') + ' LIMIT ?) ORDER BY id'
                db_rows = conn.execute(limited, params + [int(max_events) * len(RAW_SOURCES)]).fetchall()
        seen_hashes: dict[tuple, set[str]] = {}
        out = []
        for row in db_rows:
            if row['source'] == 'heartbeat' and int(row['id']) not in delivered_proactive_ids:
                continue
            metadata = {}
            try:
                metadata = json.loads(row['metadata_json'] or '{}')
                if not isinstance(metadata, dict):
                    metadata = {}
            except Exception:
                metadata = {}
            text = str(row['text'] or '')
            if row['role'] == 'user':
                authored = metadata.get('authored_text')
                if isinstance(authored, str) and authored.strip():
                    text = authored
            text = text.strip()
            if not text:
                continue
            event_hash = str(row['event_hash'] or '').strip()
            dedupe_key = (event_hash, row['role'], text, row['created_at']) if event_hash else None
            if dedupe_key is not None:
                sources = seen_hashes.setdefault(dedupe_key, set())
                if sources and row['source'] not in sources:
                    sources.add(row['source'])
                    continue
                sources.add(row['source'])
            raw_id = int(row['id'])
            out.append({'source_key': f'raw:{raw_id}', 'source_id': raw_id, 'raw_event_start_id': raw_id, 'raw_event_end_id': raw_id, 'epoch_id': epoch_map.get(raw_id, ''), 'role': str(row['role'] or ''), 'event_time': str(row['created_at'] or ''), 'text': text, 'content_hash': _sha(text), 'provenance': {'raw_event_id': raw_id, 'source': str(row['source'] or ''), 'source_event_id': str(row['source_event_id'] or ''), 'event_hash': event_hash}})
        if max_events is not None and len(out) > int(max_events):
            out = out[-int(max_events):]
        return out

    def rebuild_raw(self, raw_db: str, runtime_db: str | None, *, max_events: int | None=None, embedder=None, batch_size: int=16) -> dict:
        return self._replace_lane('raw_event',
            lambda: self._raw_rows(raw_db, runtime_db, max_events=max_events),
            source_path=raw_db, embedder=embedder, batch_size=batch_size)

    def sync_delivered_proactive(self, raw_db: str, runtime_db: str, *, embedder=None, batch_size: int=16) -> dict:
        """Replace only delivered heartbeat units, using the shared lane fence."""
        expected = self._generation('raw_event')
        rows = [r for r in self._raw_rows(raw_db, runtime_db) if r['provenance']['source'] == 'heartbeat']
        with self._connect() as conn:
            existing = {r['source_key']: r for r in conn.execute(
                "SELECT * FROM chunks WHERE source_kind='raw_event' "
                "AND json_extract(provenance_json,'$.source')='heartbeat'")}
        updated = sum(r['source_key'] not in existing
                      or existing[r['source_key']]['content_hash'] != r['content_hash']
                      or json.loads(existing[r['source_key']]['provenance_json']) != r['provenance']
                      or (embedder is not None and not self._vector_bound(existing[r['source_key']], self.embedding_model))
                      for r in rows)
        stale = len(set(existing) - {r['source_key'] for r in rows})
        result = self._replace_lane('raw_event', rows, source_path=raw_db,
            embedder=embedder, batch_size=batch_size, expected_generation=expected,
            selector=("json_extract(provenance_json,'$.source')='heartbeat'", ()))
        return dict(eligible=len(rows), updated=0 if result['discarded'] else updated,
                    removed=0 if result['discarded'] else stale,
                    new_embeddings=result['embedded_count'], discarded=result['discarded'])

    def refresh_delivered_proactive(self, raw_db: str, runtime_db: str, raw_event_id: int, *, embedder=None) -> dict:
        """Refresh one heartbeat; inference must never hold SQLite's write lock."""
        raw_id = int(raw_event_id)
        if raw_id <= 0:
            raise ValueError('positive raw_event_id required')
        expected = self._generation('raw_event')
        rows = [r for r in self._raw_rows(raw_db, runtime_db, raw_event_id=raw_id)
                if r['provenance']['source'] == 'heartbeat']
        with self._connect() as conn:
            previous = conn.execute("SELECT * FROM chunks WHERE source_key=?", (f'raw:{raw_id}',)).fetchone()
        if previous and json.loads(previous['provenance_json']).get('source') != 'heartbeat':
            raise RetrievalShadowError('raw_event key belongs to another source')
        result = self._replace_lane('raw_event', rows, source_path=raw_db, embedder=embedder,
            expected_generation=expected, selector=('source_key=?', (f'raw:{raw_id}',)), single=True)
        return dict(raw_event_id=raw_id, present=bool(rows), embedded=bool(result['embedded_count']),
                    unchanged=bool(rows and previous and previous['content_hash'] == rows[0]['content_hash']
                                   and self._vector_bound(previous, self.embedding_model)),
                    discarded=result['discarded'])

    def search_lane(self, query: str, source_kind: str, *, embedder=None, current_epoch_id: str='', top_k: int=8, pool: int=40) -> list[dict]:
        if source_kind not in {'memory_card', 'raw_event'}:
            raise RetrievalShadowError('invalid source_kind')
        lexical = self._lexical_hits(query, source_kind, pool)
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
        for (key, row) in by_key.items():
            if source_kind == 'raw_event' and current_epoch_id and (row['epoch_id'] == current_epoch_id):
                scores[key] += 0.0001
                reasons.setdefault(key, []).append('current_epoch_prior')
        ordered = sorted(scores, key=lambda key: (-scores[key], key))[:int(top_k)]
        result = []
        for key in ordered:
            row = by_key.get(key)
            if row is None:
                continue
            item = dict(row)
            item.pop('vector', None)
            try:
                item['provenance'] = json.loads(item.pop('provenance_json'))
            except Exception:
                item['provenance'] = {}
            item['retrieval_score'] = scores[key]
            item['retrieval_reasons'] = reasons.get(key, [])
            item['source_scope'] = 'current_epoch' if source_kind == 'raw_event' and current_epoch_id and (row['epoch_id'] == current_epoch_id) else 'history' if source_kind == 'raw_event' else 'long_term_memory'
            result.append(item)
        return result

    def search_parallel(self, query: str, *, embedder=None, current_epoch_id: str='', top_k: int=6) -> dict:
        return {'query': query, 'memory_hits': self.search_lane(query, 'memory_card', embedder=embedder, current_epoch_id=current_epoch_id, top_k=top_k), 'raw_hits': self.search_lane(query, 'raw_event', embedder=embedder, current_epoch_id=current_epoch_id, top_k=top_k)}
