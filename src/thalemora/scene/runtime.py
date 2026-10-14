"""Scene 分段与进度核心；来源 scene_runtime.py，历史库仅显式只读。"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Mapping, Sequence

import numpy as np

from .store import SceneShadowStore

RAW_SOURCES = ("app", "rikkahub", "rikkahub_import", "gateway", "heartbeat")


STATE_VERSION = 1

_SYNC_LOCKS_GUARD = threading.Lock()
_SYNC_LOCKS: dict[str, threading.RLock] = {}


def _sync_lock_for(path: str) -> threading.RLock:
    key = os.path.realpath(os.path.abspath(path))
    with _SYNC_LOCKS_GUARD:
        lock = _SYNC_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _SYNC_LOCKS[key] = lock
        return lock


_TRANSITION_RE = re.compile(
    r"^(?:对了|说起来|换个话题|顺便|另外|还有个事|还有一件事|题外话|我突然想到)"
)
_CONTINUE_RE = re.compile(
    r"(?:刚才|之前那个|接着|继续|然后呢|昨天那个|上次那个|那件事|前面说的)"
)

RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS scene_runtime_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL,
    last_processed_raw_event_id INTEGER NOT NULL,
    initialized_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scene_runtime_considered (
    scene_id TEXT PRIMARY KEY,
    scene_revision_no INTEGER NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    considered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scene_boundary_reviews (
    scene_id TEXT PRIMARY KEY,
    scene_revision_no INTEGER NOT NULL,
    decision TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
);
"""


class SceneRuntimeError(RuntimeError):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_time(value) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


@contextmanager
def _ro(path: str):
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
    finally:
        conn.close()


class SceneRuntime:
    def __init__(
        self,
        *,
        db_path: str,
        raw_db: str,
        runtime_db: str,
        embedder,
        initial_backfill_events: int = 120,
        cooling_minutes: float = 20.0,
        finalize_minutes: float = 120.0,
        finalized_rescan_min_events: int = 24,
        finalized_window_events: int = 4,
    ):
        self.db_path = os.path.abspath(db_path)
        self.raw_db = os.path.abspath(raw_db)
        self.runtime_db = os.path.abspath(runtime_db)
        self.embedder = embedder
        self.initial_backfill_events = max(0, int(initial_backfill_events))
        self.cooling_minutes = max(1.0, float(cooling_minutes))
        self.finalize_minutes = max(self.cooling_minutes + 1.0, float(finalize_minutes))
        self.finalized_rescan_min_events = max(6, int(finalized_rescan_min_events))
        self.finalized_window_events = max(2, min(8, int(finalized_window_events)))
        self.store = SceneShadowStore(self.db_path)
        self._sync_lock = _sync_lock_for(self.db_path)
        self._initialize_runtime_tables()

    def _connect(self):
        return self.store._connect()

    def _initialize_runtime_tables(self):
        with self._connect() as conn:
            conn.executescript(RUNTIME_SCHEMA)
            row = conn.execute(
                "SELECT version FROM scene_runtime_state WHERE singleton=1"
            ).fetchone()
            if row is not None and int(row["version"]) != STATE_VERSION:
                raise SceneRuntimeError("scene runtime state version mismatch")

    def _max_raw_id(self) -> int:
        with _ro(self.raw_db) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id),0) AS max_id FROM raw_events"
            ).fetchone()
        return int(row["max_id"] or 0)

    def _ensure_state(self, now: dt.datetime) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_processed_raw_event_id FROM scene_runtime_state "
                "WHERE singleton=1"
            ).fetchone()
            if row is not None:
                return int(row["last_processed_raw_event_id"])
        max_id = self._max_raw_id()
        start = max(0, max_id - self.initial_backfill_events)
        timestamp = _iso(now)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO scene_runtime_state("
                "singleton,version,last_processed_raw_event_id,initialized_at,updated_at"
                ") VALUES(1,?,?,?,?)",
                (STATE_VERSION, start, timestamp, timestamp),
            )
        return start

    def _set_last_processed(self, raw_id: int, now: dt.datetime):
        with self._connect() as conn:
            conn.execute(
                "UPDATE scene_runtime_state SET last_processed_raw_event_id=?,updated_at=? "
                "WHERE singleton=1",
                (int(raw_id), _iso(now)),
            )

    def _epoch_map(self, ids: Sequence[int]) -> dict[int, str]:
        ids = [int(x) for x in ids if int(x) > 0]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with _ro(self.runtime_db) as conn:
            rows = conn.execute(
                f"SELECT raw_event_id,epoch_id FROM message_records "
                f"WHERE raw_event_id IN ({placeholders}) AND status='active'",
                ids,
            ).fetchall()
        return {int(row["raw_event_id"]): str(row["epoch_id"] or "") for row in rows}

    def _fetch_new_events(self, after_id: int, limit: int = 1000) -> tuple[list[dict], int]:
        placeholders = ",".join("?" for _ in RAW_SOURCES)
        params = [int(after_id), *RAW_SOURCES, int(limit)]
        with _ro(self.raw_db) as conn:
            rows = conn.execute(
                "SELECT id,source,source_event_id,event_hash,role,text,created_at,metadata_json "
                "FROM raw_events WHERE id>? AND source IN (" + placeholders + ") "
                "AND role IN ('user','assistant') AND COALESCE(deleted,0)!=1 "
                "AND length(trim(COALESCE(text,'')))>0 ORDER BY id LIMIT ?",
                params,
            ).fetchall()
        if not rows:
            return [], after_id
        max_seen = max(int(row["id"]) for row in rows)
        epoch_map = self._epoch_map([int(row["id"]) for row in rows])

        # Different ingestion surfaces may mirror the same canonical event.
        seen = set()
        events = []
        for row in rows:
            metadata = {}
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
                if not isinstance(metadata, dict):
                    metadata = {}
            except Exception:
                metadata = {}
            text = str(row["text"] or "")
            if row["role"] == "user":
                authored = metadata.get("authored_text")
                if isinstance(authored, str) and authored.strip():
                    text = authored
            text = text.strip()
            if not text:
                continue
            event_hash = str(row["event_hash"] or "").strip()
            dedupe_key = (
                event_hash,
                str(row["role"] or ""),
                text,
                str(row["created_at"] or ""),
            )
            if event_hash and dedupe_key in seen:
                continue
            if event_hash:
                seen.add(dedupe_key)
            raw_id = int(row["id"])
            events.append({
                "raw_event_id": raw_id,
                "epoch_id": epoch_map.get(raw_id, ""),
                "role": str(row["role"] or ""),
                "event_time": str(row["created_at"] or ""),
                "event_hash": event_hash,
                "text": text,
            })
        return events, max_seen

    def _fetch_raw_text(self, raw_event_id: int) -> str:
        with _ro(self.raw_db) as conn:
            row = conn.execute(
                "SELECT role,text,metadata_json FROM raw_events WHERE id=?",
                (int(raw_event_id),),
            ).fetchone()
        if row is None:
            return ""
        text = str(row["text"] or "")
        if str(row["role"] or "") == "user":
            try:
                meta = json.loads(row["metadata_json"] or "{}")
            except Exception:
                meta = {}
            authored = meta.get("authored_text") if isinstance(meta, dict) else None
            if isinstance(authored, str) and authored.strip():
                text = authored
        return text.strip()

    def _active_scene(self) -> dict | None:
        rows = self.store.list_scenes(states=["open", "cooling"])
        if not rows:
            return None
        # A healthy runtime has exactly one. Prefer newest and finalize stale
        # duplicates conservatively rather than letting two scenes own future events.
        rows.sort(key=lambda item: (str(item.get("updated_at") or ""), item["scene_id"]), reverse=True)
        chosen = rows[0]
        for stale in rows[1:]:
            self.store.finalize(stale["scene_id"], reason="runtime_recovery_multiple_active")
        return self.store.get_scene(chosen["scene_id"])

    def _cosine(self, a: str, b: str) -> float | None:
        if self.embedder is None or not a.strip() or not b.strip():
            return None
        vectors = self.embedder.encode_many([a[:1200], b[:1200]], batch_size=2)
        if len(vectors) != 2:
            return None
        left = np.asarray(vectors[0], dtype=np.float32).reshape(-1)
        right = np.asarray(vectors[1], dtype=np.float32).reshape(-1)
        if left.size != right.size or left.size == 0:
            return None
        return float(left @ right)

    def _boundary_decision(self, previous: dict, current: dict) -> dict:
        prev_time = _parse_time(previous.get("event_time"))
        cur_time = _parse_time(current.get("event_time"))
        gap_minutes = 0.0
        if prev_time is not None and cur_time is not None:
            gap_minutes = max(0.0, (cur_time - prev_time).total_seconds() / 60.0)

        previous_text = str(previous.get("text") or "")
        current_text = str(current.get("text") or "")
        cosine = self._cosine(previous_text, current_text)
        explicit_transition = bool(
            current.get("role") == "user" and _TRANSITION_RE.search(current_text)
        )
        explicit_continue = bool(
            current.get("role") == "user" and _CONTINUE_RE.search(current_text)
        )

        if explicit_continue:
            return {
                "boundary": False,
                "ambiguous": False,
                "reason": "explicit_continuation",
                "gap_minutes": gap_minutes,
                "cosine": cosine,
            }
        if gap_minutes >= 180:
            return {
                "boundary": True,
                "ambiguous": False,
                "reason": "long_idle",
                "gap_minutes": gap_minutes,
                "cosine": cosine,
            }
        if gap_minutes >= 45 and (cosine is None or cosine < 0.60):
            return {
                "boundary": True,
                "ambiguous": False,
                "reason": "idle_plus_topic_shift",
                "gap_minutes": gap_minutes,
                "cosine": cosine,
            }
        if gap_minutes >= 20 and cosine is not None and cosine < 0.42:
            return {
                "boundary": True,
                "ambiguous": False,
                "reason": "cooling_plus_low_similarity",
                "gap_minutes": gap_minutes,
                "cosine": cosine,
            }
        if explicit_transition and cosine is not None and cosine < 0.50:
            return {
                "boundary": True,
                "ambiguous": False,
                "reason": "explicit_transition_plus_shift",
                "gap_minutes": gap_minutes,
                "cosine": cosine,
            }
        ambiguous = (
            cosine is not None
            and cosine < 0.36
            and gap_minutes >= 2
            and not explicit_continue
        )
        return {
            "boundary": False,
            "ambiguous": ambiguous,
            "reason": "ambiguous_shift" if ambiguous else "continuous",
            "gap_minutes": gap_minutes,
            "cosine": cosine,
        }

    def _event_ref(self, event: Mapping) -> dict:
        return {
            "raw_event_id": int(event["raw_event_id"]),
            "epoch_id": str(event.get("epoch_id") or ""),
            "role": str(event.get("role") or ""),
            "event_time": str(event.get("event_time") or ""),
            "event_hash": str(event.get("event_hash") or ""),
        }

    def _scene_tail_event(self, scene: Mapping) -> dict | None:
        events = list(scene.get("events") or ())
        if not events:
            return None
        last = dict(events[-1])
        last["text"] = self._fetch_raw_text(int(last["raw_event_id"]))
        return last

    def _append_to_scene(self, scene: dict, event: dict, *, reason: str):
        refs = list(scene.get("events") or ())
        refs.append(self._event_ref(event))
        self.store.revise_sources(scene["scene_id"], refs, reason=reason)

    def _apply_idle_state(self, now: dt.datetime) -> list[dict]:
        changes = []
        scene = self._active_scene()
        if scene is None:
            return changes
        tail = self._scene_tail_event(scene)
        when = _parse_time((tail or {}).get("event_time"))
        if when is None:
            return changes
        idle_minutes = max(0.0, (now - when).total_seconds() / 60.0)
        if scene["state"] == "open" and idle_minutes >= self.cooling_minutes:
            self.store.mark_cooling(scene["scene_id"], reason="idle_cooling")
            changes.append({"scene_id": scene["scene_id"], "to": "cooling"})
            scene = self.store.get_scene(scene["scene_id"])
        if scene["state"] == "cooling" and idle_minutes >= self.finalize_minutes:
            self.store.finalize(scene["scene_id"], reason="idle_finalized")
            changes.append({"scene_id": scene["scene_id"], "to": "finalized"})
        return changes

    @staticmethod
    def _boundary_reason_values(raw_reasons: str) -> dict:
        try:
            values = json.loads(raw_reasons or "[]")
        except Exception:
            values = []
        result = {}
        for value in values if isinstance(values, list) else ():
            text = str(value or "")
            if "=" not in text:
                continue
            key, raw = text.split("=", 1)
            try:
                result[key] = float(raw)
            except ValueError:
                result[key] = raw
        return result

    @staticmethod
    def _boundary_reason_list(raw_reasons: str) -> list[str]:
        try:
            values = json.loads(raw_reasons or "[]")
        except Exception:
            values = []
        if not isinstance(values, list):
            return []
        return [str(value or "") for value in values]

    def _boundary_reviewed(self, scene_id: str, revision_no: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT scene_revision_no FROM scene_boundary_reviews "
                "WHERE scene_id=?",
                (str(scene_id),),
            ).fetchone()
        return row is not None and int(row["scene_revision_no"]) == int(revision_no)

    def _mark_boundary_reviewed(
        self,
        scene_id: str,
        revision_no: int,
        decision: str,
        detail: Mapping | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO scene_boundary_reviews("
                "scene_id,scene_revision_no,decision,detail_json,reviewed_at"
                ") VALUES(?,?,?,?,?) "
                "ON CONFLICT(scene_id) DO UPDATE SET "
                "scene_revision_no=excluded.scene_revision_no,"
                "decision=excluded.decision,"
                "detail_json=excluded.detail_json,"
                "reviewed_at=excluded.reviewed_at",
                (
                    str(scene_id),
                    int(revision_no),
                    str(decision or ""),
                    json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                    _iso(_now()),
                ),
            )

    def _scene_events_with_text(self, scene: Mapping) -> list[dict]:
        result = []
        for ref in scene.get("events") or ():
            item = dict(ref)
            item["text"] = self._fetch_raw_text(int(item["raw_event_id"]))
            result.append(item)
        return result

    @staticmethod
    def _window_text(events: Sequence[Mapping]) -> str:
        parts = []
        for event in events:
            text = re.sub(r"\s+", " ", str(event.get("text") or "")).strip()
            if text:
                role = str(event.get("role") or "")
                parts.append(f"{role}:{text[:320]}")
        return "\n".join(parts)[:2200]

    def _finalized_rescan_candidates(self, scene: Mapping) -> list[dict]:
        """Re-scan a finalized Scene for strong topic/activity boundaries.

        Realtime segmentation intentionally under-cuts.  Finalization is allowed
        to review broader local windows so a gradual topic shift that never
        produced one strong adjacent-pair signal can still split later.

        ContextEpoch metadata is intentionally ignored here.
        """
        events = self._scene_events_with_text(scene)
        total = len(events)
        if self.embedder is None or total < 4:
            return []

        first_time = _parse_time(events[0].get("event_time"))
        last_time = _parse_time(events[-1].get("event_time"))
        span_minutes = 0.0
        if first_time is not None and last_time is not None:
            span_minutes = max(
                0.0, (last_time - first_time).total_seconds() / 60.0
            )
        if (
            total < self.finalized_rescan_min_events
            and span_minutes < self.finalize_minutes
        ):
            return []

        window = self.finalized_window_events
        candidates = []
        for split_index in range(2, total - 1):
            current = events[split_index]
            previous = events[split_index - 1]

            # Start the right-hand Scene on a user-authored turn so a user
            # question and its assistant answer are never separated.
            if str(current.get("role") or "") != "user":
                continue

            current_text = str(current.get("text") or "")
            if _CONTINUE_RE.search(current_text):
                continue

            left_window = events[max(0, split_index - window):split_index]
            right_window = events[split_index:min(total, split_index + window)]
            if len(left_window) < 2 or len(right_window) < 2:
                continue

            left_text = self._window_text(left_window)
            right_text = self._window_text(right_window)
            if not left_text or not right_text:
                continue

            window_cosine = self._cosine(left_text, right_text)
            adjacent_cosine = self._cosine(
                str(previous.get("text") or ""),
                current_text,
            )
            prev_time = _parse_time(previous.get("event_time"))
            cur_time = _parse_time(current.get("event_time"))
            gap_minutes = 0.0
            if prev_time is not None and cur_time is not None:
                gap_minutes = max(
                    0.0,
                    (cur_time - prev_time).total_seconds() / 60.0,
                )

            explicit_transition = bool(_TRANSITION_RE.search(current_text))
            strong = False
            strong_reason = ""

            # Time + topic reset. These are conservative and are intended to
            # catch boundaries missed by the live adjacent-pair path.
            if gap_minutes >= 45 and (
                window_cosine is None or window_cosine <= 0.66
            ):
                strong = True
                strong_reason = "finalized_long_idle_topic_reset"
            elif (
                gap_minutes >= 20
                and window_cosine is not None
                and window_cosine <= 0.58
            ):
                strong = True
                strong_reason = "finalized_cooling_topic_reset"
            elif (
                explicit_transition
                and window_cosine is not None
                and window_cosine <= 0.58
            ):
                strong = True
                strong_reason = "finalized_explicit_transition"
            elif (
                total >= 12
                and gap_minutes >= 0.5
                and window_cosine is not None
                and adjacent_cosine is not None
                and window_cosine <= 0.30
                and adjacent_cosine <= 0.42
            ):
                # Oversized Scene with an abrupt semantic reset even though the
                # user kept talking without an idle gap.
                strong = True
                strong_reason = "finalized_oversized_topic_reset"
            elif (
                total >= 24
                and gap_minutes >= 2.0
                and window_cosine is not None
                and adjacent_cosine is not None
                and window_cosine <= 0.40
                and adjacent_cosine <= 0.48
            ):
                strong = True
                strong_reason = "finalized_large_scene_topic_reset"

            if not strong:
                continue

            semantic_distance = (
                0.0 if window_cosine is None else max(0.0, 1.0 - window_cosine)
            )
            adjacent_distance = (
                0.0 if adjacent_cosine is None else max(0.0, 1.0 - adjacent_cosine)
            )
            gap_score = min(1.0, gap_minutes / 30.0)
            score = min(
                1.0,
                (0.58 * semantic_distance)
                + (0.22 * adjacent_distance)
                + (0.20 * gap_score)
                + (0.08 if explicit_transition else 0.0),
            )
            candidates.append({
                "split_index": split_index,
                "before_event_id": int(previous["raw_event_id"]),
                "after_event_id": int(current["raw_event_id"]),
                "score": score,
                "gap_minutes": gap_minutes,
                "window_cosine": window_cosine,
                "adjacent_cosine": adjacent_cosine,
                "explicit_transition": explicit_transition,
                "reason": strong_reason,
            })

        candidates.sort(
            key=lambda item: (
                int(item["split_index"]),
                -float(item["score"]),
            )
        )
        return candidates

    def _candidate_is_strong_finalized(
        self,
        candidate: Mapping,
        *,
        scene_event_count: int,
    ) -> tuple[bool, dict]:
        reasons = self._boundary_reason_list(candidate.get("reasons_json"))
        values = self._boundary_reason_values(candidate.get("reasons_json"))
        score = float(candidate.get("score") or 0.0)
        gap = float(values.get("gap_minutes") or 0.0)
        if "finalized_rescan" not in reasons:
            return (score >= 0.72 and gap >= 8.0), {
                "score": score,
                "gap_minutes": gap,
                "source": "realtime_candidate",
            }

        window_cosine = values.get("window_cosine")
        adjacent_cosine = values.get("adjacent_cosine")
        if not isinstance(window_cosine, (int, float)):
            window_cosine = None
        if not isinstance(adjacent_cosine, (int, float)):
            adjacent_cosine = None
        explicit_transition = bool(int(values.get("explicit_transition") or 0))
        strong = (
            (gap >= 45 and (window_cosine is None or window_cosine <= 0.66))
            or (
                gap >= 20
                and window_cosine is not None
                and window_cosine <= 0.58
            )
            or (
                explicit_transition
                and window_cosine is not None
                and window_cosine <= 0.58
            )
            or (
                scene_event_count >= 12
                and gap >= 0.5
                and window_cosine is not None
                and adjacent_cosine is not None
                and window_cosine <= 0.30
                and adjacent_cosine <= 0.42
            )
            or (
                scene_event_count >= 24
                and gap >= 2.0
                and window_cosine is not None
                and adjacent_cosine is not None
                and window_cosine <= 0.40
                and adjacent_cosine <= 0.48
            )
        )
        return strong, {
            "score": score,
            "gap_minutes": gap,
            "window_cosine": window_cosine,
            "adjacent_cosine": adjacent_cosine,
            "explicit_transition": explicit_transition,
            "source": "finalized_rescan",
        }

    def repair_finalized_boundaries(self, *, limit: int = 4) -> list[dict]:
        with self._sync_lock, self.store.transaction():
            return self._repair_finalized_boundaries(limit=limit)

    def _repair_finalized_boundaries(self, *, limit: int = 4) -> list[dict]:
        """Repair finalized Scene boundaries using persisted + retrospective signals.

        Realtime remains conservative. Finalization may re-scan local windows so
        a topic reset that was too gradual for the adjacent-pair path can still
        split later. Raw events never move or change; only derived Scene
        membership/revision lineage changes.
        """
        max_repairs = max(0, int(limit))
        repairs: list[dict] = []
        queue = [
            str(row["scene_id"])
            for row in self.store.list_scenes(states=["finalized"])
        ]

        while queue and len(repairs) < max_repairs:
            scene_id = queue.pop(0)
            scene = self.store.get_scene(scene_id)
            if (
                scene.get("state") != "finalized"
                or scene.get("projection_status") != "current"
            ):
                continue

            revision_no = int(scene["current_revision_no"])
            events = list(scene.get("events") or ())
            pending = self.store.list_boundary_candidates(
                scene_id, status="pending"
            )

            # If this exact finalized projection has not yet had a retrospective
            # review, synthesize strong local-window candidates. This fixes
            # oversized Scenes that never emitted a realtime boundary candidate.
            if not self._boundary_reviewed(scene_id, revision_no):
                existing_pairs = {
                    (int(item["before_event_id"]), int(item["after_event_id"]))
                    for item in pending
                }
                for candidate in self._finalized_rescan_candidates(scene)[:8]:
                    pair = (
                        int(candidate["before_event_id"]),
                        int(candidate["after_event_id"]),
                    )
                    if pair in existing_pairs:
                        continue
                    reasons = [
                        "finalized_rescan",
                        str(candidate["reason"]),
                        f"gap_minutes={float(candidate['gap_minutes']):.3f}",
                        (
                            "window_cosine=none"
                            if candidate["window_cosine"] is None
                            else f"window_cosine={float(candidate['window_cosine']):.4f}"
                        ),
                        (
                            "adjacent_cosine=none"
                            if candidate["adjacent_cosine"] is None
                            else f"adjacent_cosine={float(candidate['adjacent_cosine']):.4f}"
                        ),
                        (
                            "explicit_transition=1"
                            if candidate["explicit_transition"]
                            else "explicit_transition=0"
                        ),
                    ]
                    candidate_id = self.store.add_boundary_candidate(
                        scene_id,
                        int(candidate["before_event_id"]),
                        int(candidate["after_event_id"]),
                        score=float(candidate["score"]),
                        reasons=reasons,
                    )
                    pending.append({
                        "candidate_id": candidate_id,
                        "scene_id": scene_id,
                        "before_event_id": int(candidate["before_event_id"]),
                        "after_event_id": int(candidate["after_event_id"]),
                        "score": float(candidate["score"]),
                        "reasons_json": json.dumps(
                            reasons, ensure_ascii=False, sort_keys=True
                        ),
                        "status": "pending",
                    })
                    existing_pairs.add(pair)

            # Pick the earliest strong boundary in narrative order. Splitting
            # there lets the right child be reviewed again in the same pass,
            # so a three-topic oversized Scene can become three Scenes instead
            # of permanently stopping after one split.
            indexed_candidates = []
            for candidate in pending:
                strong, meta = self._candidate_is_strong_finalized(
                    candidate,
                    scene_event_count=len(events),
                )
                if not strong:
                    continue
                after_id = int(candidate["after_event_id"])
                split_index = next(
                    (
                        index
                        for index, event in enumerate(events)
                        if int(event["raw_event_id"]) == after_id
                    ),
                    None,
                )
                if (
                    split_index is None
                    or split_index < 2
                    or len(events) - split_index < 2
                ):
                    continue
                indexed_candidates.append(
                    (int(split_index), candidate, meta)
                )

            indexed_candidates.sort(
                key=lambda item: (
                    item[0],
                    -float(item[1].get("score") or 0.0),
                )
            )

            if not indexed_candidates:
                # Finalization is the decision point: weak/invalid pending
                # candidates are resolved so this immutable projection is not
                # rescored every five minutes.
                for candidate in pending:
                    self.store.resolve_boundary_candidate(
                        candidate["candidate_id"], "rejected"
                    )
                self._mark_boundary_reviewed(
                    scene_id,
                    revision_no,
                    "clean",
                    {
                        "event_count": len(events),
                        "pending_candidates": len(pending),
                    },
                )
                continue

            split_index, selected, selected_meta = indexed_candidates[0]
            left = events[:split_index]
            right = events[split_index:]
            score = float(selected.get("score") or 0.0)
            gap = float((selected_meta or {}).get("gap_minutes") or 0.0)
            source = str((selected_meta or {}).get("source") or "candidate")
            before_event_id = int(selected["before_event_id"])
            after_event_id = int(selected["after_event_id"])

            new_scene_id = self.store.split_scene(
                scene_id,
                left,
                right,
                reason=(
                    f"finalized_boundary source={source} "
                    f"score={score:.3f} gap={gap:.3f}"
                ),
            )
            self.store.resolve_boundary_candidate(
                selected["candidate_id"], "accepted"
            )

            # Remaining candidates were scored against the pre-split
            # projection. Never silently carry them across new lineage.
            for candidate in self.store.list_boundary_candidates(
                scene_id, status="pending"
            ):
                self.store.resolve_boundary_candidate(
                    candidate["candidate_id"], "rejected"
                )

            right_text = self._fetch_raw_text(int(right[0]["raw_event_id"]))
            if right_text:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE scenes SET title_hint=?,topic_hint=? "
                        "WHERE scene_id=?",
                        (right_text[:80], right_text[:120], new_scene_id),
                    )

            repairs.append({
                "scene_id": scene_id,
                "scene_revision_no": revision_no,
                "new_scene_id": new_scene_id,
                "candidate_id": selected["candidate_id"],
                "source": source,
                "score": score,
                "gap_minutes": gap,
                "window_cosine": (selected_meta or {}).get("window_cosine"),
                "adjacent_cosine": (selected_meta or {}).get("adjacent_cosine"),
                "before_event_id": before_event_id,
                "after_event_id": after_event_id,
                "left_events": len(left),
                "right_events": len(right),
            })

            # Both projections have new revision lineage after split. Re-check
            # them rather than declaring the left child clean by assumption.
            # Right goes first because multi-topic oversize tends to accumulate
            # later topics there; left remains eligible if a newly-local window
            # exposes another earlier cut.
            queue.insert(0, scene_id)
            queue.insert(0, new_scene_id)

        return repairs

    def sync(self, *, now: dt.datetime | None = None, limit: int = 1000) -> dict:
        with self._sync_lock, self.store.transaction():
            return self._sync_locked(now=now, limit=limit)

    def _sync_locked(self, *, now: dt.datetime | None = None, limit: int = 1000) -> dict:
        now = now or _now()
        last_processed = self._ensure_state(now)
        events, max_seen = self._fetch_new_events(last_processed, limit=limit)
        processed = 0
        created = 0
        finalized = 0
        boundaries = []

        # 同步中的 membership、边界与水位共同提交；仍兼容先前已落 membership
        # 而水位滞后的输入，终结 Scene 中的 ownership 同样是持久回执。
        with self._connect() as conn:
            committed_ids = {
                int(row["raw_event_id"])
                for row in conn.execute(
                    "SELECT raw_event_id FROM scene_current_events "
                    "WHERE raw_event_id>? AND raw_event_id<=?",
                    (last_processed, max_seen),
                )
            }

        for event in events:
            if int(event["raw_event_id"]) in committed_ids:
                continue
            scene = self._active_scene()
            if scene is None:
                self.store.create_scene(
                    [self._event_ref(event)],
                    title_hint=event["text"][:80],
                    topic_hint=event["text"][:120],
                    reason="runtime_seed",
                )
                created += 1
                processed += 1
                continue

            previous = self._scene_tail_event(scene)
            if previous is None:
                self._append_to_scene(scene, event, reason="runtime_append_empty_tail")
                processed += 1
                continue
            decision = self._boundary_decision(previous, event)
            if decision["boundary"]:
                self.store.finalize(
                    scene["scene_id"],
                    reason="runtime_boundary:" + decision["reason"],
                )
                finalized += 1
                self.store.create_scene(
                    [self._event_ref(event)],
                    title_hint=event["text"][:80],
                    topic_hint=event["text"][:120],
                    reason="runtime_boundary_new_scene",
                )
                created += 1
            else:
                if scene["state"] == "cooling":
                    self.store.resume(scene["scene_id"], reason="runtime_continuity_resume")
                    scene = self.store.get_scene(scene["scene_id"])
                if decision["ambiguous"]:
                    candidate_id = self.store.add_boundary_candidate(
                        scene["scene_id"],
                        before_event_id=int(previous["raw_event_id"]),
                        after_event_id=int(event["raw_event_id"]),
                        score=max(
                            0.0,
                            min(1.0, 1.0 - float(decision["cosine"] or 0.0)),
                        ),
                        reasons=[
                            str(decision["reason"]),
                            f"gap_minutes={decision['gap_minutes']:.3f}",
                            (
                                "cosine=none"
                                if decision["cosine"] is None
                                else f"cosine={float(decision['cosine']):.4f}"
                            ),
                        ],
                    )
                    boundaries.append(candidate_id)
                self._append_to_scene(scene, event, reason="runtime_append")
            processed += 1

        if max_seen > last_processed:
            self._set_last_processed(max_seen, now)
        idle_changes = self._apply_idle_state(now)
        finalized += sum(1 for item in idle_changes if item["to"] == "finalized")
        boundary_repairs = self.repair_finalized_boundaries()
        active = self._active_scene()
        return {
            "ok": True,
            "processed_events": processed,
            "last_processed_raw_event_id": max(max_seen, last_processed),
            "created_scenes": created,
            "finalized_scenes": finalized,
            "boundary_candidates": len(boundaries),
            "boundary_repairs": boundary_repairs,
            "idle_changes": idle_changes,
            "active_scene": (
                {
                    "scene_id": active["scene_id"],
                    "state": active["state"],
                    "revision_no": active["current_revision_no"],
                    "event_count": len(active.get("events") or ()),
                }
                if active is not None
                else None
            ),
        }

    def boundary_review(self, scene_id: str) -> dict | None:
        """Return safe review metadata for the current derived Scene revision."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT scene_revision_no,decision,detail_json,reviewed_at "
                "FROM scene_boundary_reviews WHERE scene_id=?",
                (str(scene_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except Exception:
            detail = {}
        return {
            "scene_revision_no": int(row["scene_revision_no"]),
            "decision": str(row["decision"] or ""),
            "detail": detail if isinstance(detail, dict) else {},
            "reviewed_at": str(row["reviewed_at"] or ""),
        }

    def boundary_trace(self, scene: Mapping) -> dict:
        """Return content-free boundary lineage suitable for observability."""
        scene_id = str(scene.get("scene_id") or "")
        revision_no = int(scene.get("current_revision_no") or 0)
        events = list(scene.get("events") or ())
        first_event_id = (
            int(events[0]["raw_event_id"]) if events else None
        )
        accepted = []
        seen_candidate_ids = set()

        def add_candidate(candidate: Mapping, *, source_scene_id: str) -> None:
            candidate_id = str(candidate.get("candidate_id") or "")
            if not candidate_id or candidate_id in seen_candidate_ids:
                return
            try:
                reasons = json.loads(candidate.get("reasons_json") or "[]")
            except Exception:
                reasons = []
            accepted.append({
                "candidate_id": candidate_id,
                "source_scene_id": str(source_scene_id or ""),
                "before_event_id": int(candidate.get("before_event_id") or 0),
                "after_event_id": int(candidate.get("after_event_id") or 0),
                "score": float(candidate.get("score") or 0.0),
                "reasons": [
                    str(value) for value in reasons
                ] if isinstance(reasons, list) else [],
            })
            seen_candidate_ids.add(candidate_id)

        for candidate in self.store.list_boundary_candidates(
            scene_id, status="accepted"
        ):
            add_candidate(candidate, source_scene_id=scene_id)

        relations = []
        for relation in scene.get("relations") or ():
            if not isinstance(relation, Mapping):
                continue
            relation_out = {
                "relation": str(relation.get("relation") or ""),
                "from_scene_id": str(relation.get("from_scene_id") or ""),
                "to_scene_id": str(relation.get("to_scene_id") or ""),
                "reason": str(relation.get("reason") or ""),
            }
            relations.append(relation_out)
            if (
                relation_out["relation"] == "split_from"
                and relation_out["from_scene_id"] == scene_id
                and first_event_id is not None
            ):
                parent_id = relation_out["to_scene_id"]
                parent_candidates = self.store.list_boundary_candidates(
                    parent_id, status="accepted"
                )
                for candidate in parent_candidates:
                    if int(candidate.get("after_event_id") or 0) == first_event_id:
                        add_candidate(candidate, source_scene_id=parent_id)

        return {
            "scene_id": scene_id,
            "revision_no": revision_no,
            "state": str(scene.get("state") or ""),
            "event_count": len(events),
            "boundary_review": self.boundary_review(scene_id),
            "accepted_boundaries": accepted,
            "relations": relations,
        }

    def finalized_unconsidered(self, *, limit: int = 12) -> list[dict]:
        scenes = self.store.list_scenes(states=["finalized"])
        with self._connect() as conn:
            considered = {
                row["scene_id"]: int(row["scene_revision_no"])
                for row in conn.execute(
                    "SELECT scene_id,scene_revision_no FROM scene_runtime_considered"
                ).fetchall()
            }
        result = []
        for row in scenes:
            revision = int(row["current_revision_no"])
            if considered.get(row["scene_id"]) == revision:
                continue
            scene = self.store.get_scene(row["scene_id"])
            result.append(scene)
            if len(result) >= int(limit):
                break
        return result

    def mark_considered(self, scene_id: str, revision_no: int, decision: str, reason: str):
        with self._sync_lock, self.store.transaction() as conn:
            scene = self.store.get_scene(scene_id)
            if scene["current_revision_no"] != revision_no:
                raise SceneRuntimeError("scene revision conflict")
            if scene["state"] != "finalized" or scene["projection_status"] != "current":
                raise SceneRuntimeError("considered receipt requires current finalized scene")
            conn.execute(
                "INSERT OR REPLACE INTO scene_runtime_considered("
                "scene_id,scene_revision_no,decision,reason,considered_at"
                ") VALUES(?,?,?,?,?)",
                (
                    str(scene_id),
                    int(revision_no),
                    str(decision or ""),
                    str(reason or ""),
                    _iso(_now()),
                ),
            )

    def scene_evidence(self, scene: Mapping) -> list[dict]:
        result = []
        for item in scene.get("events") or ():
            raw_id = int(item["raw_event_id"])
            text = self._fetch_raw_text(raw_id)
            if not text:
                continue
            role = str(item.get("role") or "")
            result.append({
                "event_id": raw_id,
                "role": role,
                "beijing_time": str(item.get("event_time") or ""),
                "quotable_text": text,
            })
        return result
