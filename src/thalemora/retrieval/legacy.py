"""存量卡片、私语和沉思的进程内检索。缓存按实例复用，传输与来源由接入方提供。"""
from array import array
from collections import OrderedDict
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


RRF_K = 10


_VECTOR_CACHE_MAX = max(1, int(os.environ.get("RECALL_VECTOR_CACHE_MAX", "900")))

def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0

def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))

def _importance_factor(value):
    importance = _safe_float(value, 5.0)
    if importance <= 0:
        importance = 5.0
    importance = _clamp(importance, 1.0, 10.0)
    return 0.6 + 0.4 * (importance / 10.0)

def _elapsed_ms(start):
    return round((time.time() - start) * 1000, 3)

def _as_dict(row):
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:
        return {}

def _content_hash(content):
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()

def _split_tags(tags):
    return [t.strip() for t in str(tags or "").split(",") if t.strip()]

def _day_like(card):
    title = str(card.get("title") or "")
    happened_at = str(card.get("happened_at") or "")[:10]
    return (
        _DAY_RE.match(title) is not None
        and happened_at == title
        and "日子" in _split_tags(card.get("tags"))
    )

def _card_shadow_meta(card, fallback_content=None):
    data = _as_dict(card)
    return {
        "id": data.get("id"),
        "status": data.get("status"),
        "day_like": _day_like(data),
        "content_hash": _content_hash(data.get("content") if data else fallback_content),
    }

def _shadow_item(cid, rank, meta_by_id, fallback_content=None, **scores):
    item = dict(meta_by_id.get(cid) or _card_shadow_meta(
        {"id": cid, "content": fallback_content, "status": None}
    ))
    item["rank"] = rank
    item.update(scores)
    return item

def _ids_scores(items):
    out = []
    for rank, item in enumerate(items or [], 1):
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            out.append({"id": item[0], "rank": rank, "score": item[2]})
    return out

def _stable_card_id_key(card_id):
    try:
        return (0, int(card_id))
    except (TypeError, ValueError):
        return (1, str(card_id))

def _rrf_fuse(lanes, k=RRF_K):
    """Fuse uniformly weighted lane ranks into (card_id, content, RRF score)."""
    scores, contents = {}, {}
    for lane in lanes:
        seen_in_lane = set()
        for rank, item in enumerate(lane, 1):
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            card_id, content = item[0], item[1]
            if card_id in seen_in_lane:
                continue
            seen_in_lane.add(card_id)
            contents.setdefault(card_id, content)
            scores[card_id] = scores.get(card_id, 0.0) + 1.0 / (k + rank)
    return sorted(
        ((card_id, contents[card_id], score) for card_id, score in scores.items()),
        key=lambda item: (-item[2], _stable_card_id_key(item[0])),
    )

class LegacyRecall:
    def __init__(self, *, store, lexical, embed, embedding_model, latest_mood, rerank, config, telemetry, logger):
        self.store = store
        self.lexical = lexical
        self.embed = embed
        self.embedding_model = embedding_model
        self.latest_mood = latest_mood
        self.request_rerank = rerank
        self.config = config
        self.telemetry = telemetry
        self.logger = logger
        self.vector_cache = OrderedDict()
        self.cache_lock = threading.Lock()
        self.rerank_failures = 0

    def _cached_embedding(self, card_id, embedding_blob):
        """Return a compact parsed embedding and its norm from the bounded LRU cache."""
        blob = bytes(embedding_blob)
        key = (card_id, hashlib.sha256(blob).digest())
        with self.cache_lock:
            cached = self.vector_cache.get(key)
            if cached is not None:
                self.vector_cache.move_to_end(key)
                return cached
            vec = array("d", json.loads(blob.decode()))
            cached = (vec, math.sqrt(sum(x * x for x in vec)))
            self.vector_cache[key] = cached
            self.vector_cache.move_to_end(key)
            while len(self.vector_cache) > _VECTOR_CACHE_MAX:
                self.vector_cache.popitem(last=False)
            return cached

    def _metadata_by_id(self, ids):
        clean_ids, seen = [], set()
        for cid in ids:
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                continue
            if cid not in seen:
                clean_ids.append(cid)
                seen.add(cid)
        if not clean_ids:
            return {}
        try:
            conn = sqlite3.connect(self.store.DB_PATH)
            conn.row_factory = sqlite3.Row
            marks = ",".join("?" for _ in clean_ids)
            rows = conn.execute(
                "SELECT id, content, status, title, tags, happened_at FROM cards "
                f"WHERE id IN ({marks})",
                clean_ids,
            ).fetchall()
            conn.close()
            return {r["id"]: _card_shadow_meta(r) for r in rows}
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return {}

    def _finish_shadow(self, shadow, start):
        try:
            lanes = shadow.get("lanes") or {}
            shadow["counts"] = {
                "vector": len(lanes.get("vector") or []),
                "bm25": len(lanes.get("bm25") or []),
                "like": len(lanes.get("like") or []),
                "merged": len(shadow.get("merged") or []),
                "rerank_input": len(shadow.get("rerank_input") or []),
                "rerank_output": len(shadow.get("rerank_output") or []),
                "output": len(shadow.get("output") or []),
            }
            shadow["latency_ms"] = _elapsed_ms(start)
            self.telemetry.log_recall_shadow(shadow)
        except Exception:
            pass

    def _embedding_freshness_state(self, card):
        keys = set(card.keys()) if hasattr(card, "keys") else set()
        content_hash = card["embedding_content_hash"] if "embedding_content_hash" in keys else None
        model = card["embedding_model"] if "embedding_model" in keys else None
        embedded_at = card["embedded_at"] if "embedded_at" in keys else None
        current_hash = _content_hash(card["content"])
        if content_hash not in (None, "") and content_hash != current_hash:
            return "stale"
        if model not in (None, "") and model != self.embedding_model:
            return "stale"
        if content_hash == current_hash and model == self.embedding_model and bool(embedded_at):
            return "fresh"
        return "unknown"

    def _vector_pool(self, query_vec, limit=None, *, shadow=None, pure_cosine_rank=False, include_sunk=False):
        """全量算余弦，捞浮着的卡里最像的（沉底/归档不参与自动召回）
        embedding 为 NULL 的卡跳过（历史遗留），由 BM25/关键词路兜底。"""
        limit = self.config["vector_pool_top"] if limit is None else limit
        threshold = self.config["sim_threshold"]
        conn = sqlite3.connect(self.store.DB_PATH)
        conn.row_factory = sqlite3.Row
        card_columns = {row[1] for row in conn.execute("PRAGMA table_info(cards)").fetchall()}
        kind_filter = " AND memory_kind='episodic'" if "memory_kind" in card_columns else ""
        status_filter = "status IN ('fresh','sunk')" if include_sunk else "status='fresh'"
        rows = conn.execute(
            "SELECT * FROM cards WHERE " + status_filter + kind_filter
            + self.store._canonical_filter(conn)
        ).fetchall()
        conn.close()
        query_norm = math.sqrt(sum(x * x for x in query_vec)) if query_vec else 0.0
        scored = []
        for r in rows:
            if not r["embedding"]:
                continue
            freshness = self._embedding_freshness_state(r)
            if pure_cosine_rank and freshness == "stale":
                continue
            vec, card_norm = self._cached_embedding(r["id"], r["embedding"])
            if not query_vec or len(query_vec) != len(vec):
                sim = 0.0
            else:
                dot = sum(x * y for x, y in zip(query_vec, vec))
                sim = dot / (query_norm * card_norm) if query_norm and card_norm else 0.0
            if sim > threshold:
                weighted_score = sim * _importance_factor(r["importance"])
                score = sim if pure_cosine_rank else weighted_score
                scored.append((score, r["id"], r["content"], sim, weighted_score, freshness, r))
        scored.sort(key=lambda x: (-x[0], _stable_card_id_key(x[1])))
        top = scored[:limit]
        if shadow is not None:
            shadow[:] = []
            for rank, (score, cid, content, sim, weighted_score, freshness, row) in enumerate(top, 1):
                item = _card_shadow_meta(row, fallback_content=content)
                item["rank"] = rank
                item["pure_cosine"] = sim
                item["weighted_score"] = weighted_score
                item["embedding_freshness"] = freshness
                shadow.append(item)
        return [
            (score, cid, content)
            for score, cid, content, _sim, _weighted, _freshness, _row in top
        ]

    def _mood_valence01(self):
        try:
            mood = self.latest_mood()
            if not mood:
                return None
            valence = _safe_float(mood.get("valence"), 0.0)
            return _clamp((valence + 10.0) / 20.0, 0.0, 1.0)
        except Exception:
            return None

    def _card_valences(self, card_ids):
        ids = [int(cid) for cid in card_ids if cid is not None]
        if not ids:
            return {}
        conn = sqlite3.connect(self.store.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(cards)").fetchall()]
            if "valence" not in cols:
                return {}
            marks = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT id, valence FROM cards WHERE id IN ({marks})", ids).fetchall()
            return {r["id"]: _safe_float(r["valence"], 0.5) for r in rows}
        finally:
            conn.close()

    def _apply_emotion_boost(self, candidates, enabled):
        """低落时给偏正向记忆轻量加分；只改候选排序，不强制过滤。"""
        if not enabled:
            return candidates
        mood_valence = self._mood_valence01()
        if mood_valence is None or mood_valence >= 0.4:
            return candidates
        valences = self._card_valences([c[0] for c in candidates])
        if not valences:
            return candidates
        boosted = []
        for cid, content, score in candidates:
            extra = 0.1 if valences.get(cid, 0.5) > 0.6 else 0.0
            boosted.append((cid, content, _safe_float(score, 0.0) + extra))
        boosted.sort(key=lambda it: it[2], reverse=True)
        return boosted

    def _rerank(self, query, candidates, top_n=None, *, trace_id=None, caller="unknown", shadow=None):
        """调本地 8811 rerank 精排，返回排序后的 (id, content, score)
        挂了降级为本轮 coarse candidate 顺序并写告警日志；连续失败达到阈值单独标记。"""
        if top_n is None:
            top_n = self.config["rerank_top_n"]
        if not candidates:
            return []
        docs = [c[1] for c in candidates]
        started = time.time()
        try:
            data = self.request_rerank(query, docs, min(top_n, len(docs)), caller=caller, trace_id=trace_id)
            ranked = []
            for item in data.get("results", []):
                idx = item["index"]
                if idx < len(candidates):
                    cid, content = candidates[idx][0], candidates[idx][1]
                    ranked.append((cid, content, item.get("relevance_score", 0)))
            self.rerank_failures = 0  # 恢复即清零
            ranked = ranked[:top_n]
            latency_ms = _elapsed_ms(started)
            if shadow is not None:
                shadow["rerank_latency_ms"] = latency_ms
                shadow["degraded"] = False
                shadow["rerank_output"] = _ids_scores(ranked)
            return ranked
        except Exception as e:
            self.rerank_failures += 1
            msg = (
                f"trace_id={trace_id or ''} caller={caller or 'unknown'} "
                f"candidates={len(candidates)} error_type={type(e).__name__} "
                "degraded_to_candidate_order"
            )
            if self.rerank_failures >= self.config["rerank_fail_threshold"]:
                self.logger.error(f"RERANK_DEGRADED consecutive_failures={self.rerank_failures} {msg}")
            else:
                self.logger.warning(f"RERANK_DEGRADED consecutive_failures={self.rerank_failures} {msg}")
            fallback = [(c[0], c[1], c[2]) for c in candidates[:top_n]]
            latency_ms = _elapsed_ms(started)
            if shadow is not None:
                shadow["rerank_latency_ms"] = latency_ms
                shadow["degraded"] = True
                shadow["rerank_output"] = _ids_scores(fallback)
            return fallback

    def recall(self, query, top_n=None, include_sunk=False, include_archived=False, use_rerank=True,
               emotion_match=None, *, trace_id=None, caller="unknown", trigger_raw_event_id=None):
        """主入口：BM25 + 关键词 LIKE + 向量粗捞 → 合并去重 → rerank 精排
        include_sunk=True 时三路召回都允许捞沉底卡，供“还记得/以前那次”等显式历史提及时使用；
        include_archived=True 仅供手动搜归档区。普通自动召回默认仍只用 fresh。"""
        started = time.time()
        if top_n is None:
            top_n = self.config["rerank_top_n"]
        if emotion_match is None:
            emotion_match = self.config["emotion_match"]
        caller = caller or "unknown"
        shadow = {
            "event_type": "recall_run",
            "route": "recall",
            "trace_id": trace_id,
            "caller": caller,
            "trigger_raw_event_id": trigger_raw_event_id,
            "top_n": top_n,
            "include_sunk": include_sunk,
            "include_archived": include_archived,
            "use_rerank": use_rerank,
            "emotion_match": emotion_match,
            "degraded": False,
            "rerank_latency_ms": None,
            "lanes": {"vector": [], "bm25": [], "like": []},
            "merged": [],
            "rerank_input": [],
            "rerank_output": [],
            "output": [],
        }
        vector_shadow = []
        if use_rerank:
            bm_hits = self.lexical.search(query, limit=self.config["bm25_top"],
                                  include_sunk=include_sunk, include_archived=include_archived)
            kw_hits = self.store.search_keyword(query, limit=self.config["kw_top"],
                                        include_sunk=include_sunk, include_archived=include_archived,
                                        episodic_only=True)
            try:
                q_vec = self.embed(query, trace_id=trace_id, caller=caller,
                              purpose="recall_query_embedding")
                vec_hits = self._vector_pool(
                    q_vec, shadow=vector_shadow, pure_cosine_rank=True, include_sunk=include_sunk
                )
            except Exception as exc:
                vec_hits = []
                self.logger.warning(
                    "EMBEDDING_DEGRADED trace_id=%s caller=%s error_type=%s lexical_lanes_continue",
                    trace_id or "", caller, type(exc).__name__,
                )
        else:
            # merge_sweep and other legacy callers keep the pre-P2 mixed-score behavior.
            q_vec = self.embed(query, trace_id=trace_id, caller=caller, purpose="recall_query_embedding")
            vec_hits = self._vector_pool(q_vec, shadow=vector_shadow, include_sunk=include_sunk)
            bm_hits = self.lexical.search(query, limit=self.config["bm25_top"],
                                  include_sunk=include_sunk, include_archived=include_archived)
            kw_hits = self.store.search_keyword(query, limit=self.config["kw_top"],
                                        include_sunk=include_sunk, include_archived=include_archived,
                                        episodic_only=True)
        shadow["lanes"]["vector"] = vector_shadow
        meta_ids = [cid for _score, cid, _content in vec_hits]
        meta_ids.extend(cid for cid, _content, _score in bm_hits)
        meta_ids.extend(r.get("id") for r in kw_hits if isinstance(r, dict))
        meta_by_id = self._metadata_by_id(meta_ids)
        shadow["lanes"]["bm25"] = [
            _shadow_item(cid, rank, meta_by_id, fallback_content=content, raw_score=score)
            for rank, (cid, content, score) in enumerate(bm_hits, 1)
        ]
        shadow["lanes"]["like"] = [
            _shadow_item(r.get("id"), rank, meta_by_id, fallback_content=r.get("content"), like_score=0.5)
            for rank, r in enumerate(kw_hits, 1)
            if isinstance(r, dict)
        ]
        if use_rerank:
            vector_lane = [(cid, content, score) for score, cid, content in vec_hits]
            like_lane = [
                (r["id"], r["content"], 0.5) for r in kw_hits if isinstance(r, dict)
            ]
            merged = _rrf_fuse((vector_lane, bm_hits, like_lane))
        else:
            # Legacy vector-first de-duplication and incomparable raw scores are retained
            # until P2C replaces merge_sweep admission in a separate gate.
            seen, merged = set(), []
            for sim, cid, content in vec_hits:
                if cid not in seen:
                    seen.add(cid)
                    merged.append((cid, content, sim))
            for cid, content, score in bm_hits:
                if cid not in seen:
                    seen.add(cid)
                    merged.append((cid, content, score))
            for r in kw_hits:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    merged.append((r["id"], r["content"], 0.5))
        shadow["merged"] = _ids_scores(merged)
        if not merged:
            self._finish_shadow(shadow, started)
            return []
        if use_rerank:
            merged = merged[:self.config["vector_pool_top"]]
            shadow["rerank_input"] = _ids_scores(merged)
            ranked = self._rerank(query, merged, top_n=top_n, trace_id=trace_id, caller=caller, shadow=shadow)
        else:
            ranked = sorted(merged, key=lambda it: it[2], reverse=True)
            shadow["rerank_output"] = _ids_scores(ranked)
        # v0.2.151：统一召回契约 (id, content, score)，防御历史版本混入多余字段
        # （如 (id, content, score, created_at)），避免调用方解包报错（merge_sweep 曾中招）。
        out = []
        for it in ranked:
            if isinstance(it, (list, tuple)) and len(it) >= 3:
                out.append((it[0], it[1], it[2]))
        out = out[:top_n]
        shadow["output"] = _ids_scores(out)
        if not shadow.get("rerank_output"):
            shadow["rerank_output"] = _ids_scores(out)
        self._finish_shadow(shadow, started)
        return out

    def recall_whispers(self, query, top_n=3, *, trace_id=None, caller="whisper_recall"):
        """想说的话专用轻召回：只搜非 Mood whisper，legacy consumed 不影响资格。

        返回 (id, content, cosine_score, created_at, pinned)，供调用方选择注入候选。
        """
        q = str(query or "").strip()
        if not q:
            return []
        try:
            top_n = max(1, min(int(top_n), 10))
        except (TypeError, ValueError):
            top_n = 3
        caller = caller or "whisper_recall"
        q_vec = self.embed(q, trace_id=trace_id, caller=caller, purpose="whisper_query_embedding")
        conn = sqlite3.connect(self.store.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, content, pinned, consumed, embedding, created_at FROM whispers "
                "WHERE content NOT LIKE '（情绪评估）%'"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()
        query_norm = math.sqrt(sum(x * x for x in q_vec)) if q_vec else 0.0
        scored = []
        for row in rows:
            if not row["embedding"]:
                continue
            try:
                vec, whisper_norm = self._cached_embedding(
                    ("whisper", int(row["id"])),
                    row["embedding"],
                )
            except Exception:
                continue
            if not q_vec or len(q_vec) != len(vec):
                sim = 0.0
            else:
                dot = sum(x * y for x, y in zip(q_vec, vec))
                sim = (
                    dot / (query_norm * whisper_norm)
                    if query_norm and whisper_norm else 0.0
                )
            if sim > self.config["sim_threshold"]:
                scored.append((sim, int(row["id"]), row["content"], row["created_at"], int(row["pinned"] or 0)))
        scored.sort(key=lambda item: (-item[0], -item[4], -item[1]))
        return [
            (wid, content, sim, created_at, pinned)
            for sim, wid, content, created_at, pinned in scored[:top_n]
        ]

    def recall_contemplations(self, query, top_n=2, *, trace_id=None, caller="contemplation_recall"):
        """沉思室专用召回：只搜 contemplations 表，结果格式同 recall（id, content, score）。
        返回 (id, content, score, created_at) 四元组。
        （_contemplation_recall_items 已按长度防御解析），不对外暴露。
        仅供宿主内部使用，对外只提供数量。"""
        if top_n is None:
            top_n = self.config["rerank_top_n"]
        caller = caller or "contemplation_recall"
        q_vec = self.embed(query, trace_id=trace_id, caller=caller, purpose="contemplation_query_embedding")
        conn = sqlite3.connect(self.store.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT id, content, embedding, created_at FROM contemplations").fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()
        scored = []
        for r in rows:
            if not r["embedding"]:
                continue
            vec = json.loads(r["embedding"].decode())
            sim = _cosine(q_vec, vec)
            if sim > self.config["sim_threshold"]:
                scored.append((sim, r["id"], r["content"], r["created_at"]))
        scored.sort(key=lambda x: -x[0])
        return [(cid, content, sim, created_at) for sim, cid, content, created_at in scored[:top_n]]
