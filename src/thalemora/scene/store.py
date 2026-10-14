"""Scene 派生经历存储；来源 scene_shadow.py，Memory 唯一拥有卡片。"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


SCHEMA_VERSION = 1

SCENE_STATES = {"open", "cooling", "finalized"}
PROJECTION_STATES = {"current", "retired"}
RELATIONS = {
    "revisit",
    "continue",
    "correct",
    "resolve",
    "split_from",
    "merged_into",
}
BOUNDARY_STATES = {"pending", "accepted", "rejected"}


class SceneShadowError(RuntimeError):
    pass


class SceneNotFound(SceneShadowError):
    pass


class InvalidTransition(SceneShadowError):
    pass


class SourceConflict(SceneShadowError):
    pass


class SceneContractError(SceneShadowError):
    pass


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class SceneEventRef:
    raw_event_id: int
    epoch_id: str = ""
    role: str = ""
    event_time: str = ""
    event_hash: str = ""

    @classmethod
    def from_value(cls, value: Mapping | int) -> "SceneEventRef":
        if isinstance(value, int):
            value = {"raw_event_id": value}
        if not isinstance(value, Mapping):
            raise SceneContractError("event ref must be int or mapping")
        try:
            raw_event_id = int(value.get("raw_event_id"))
        except (TypeError, ValueError) as exc:
            raise SceneContractError("raw_event_id must be an integer") from exc
        if raw_event_id <= 0:
            raise SceneContractError("raw_event_id must be > 0")
        return cls(
            raw_event_id=raw_event_id,
            epoch_id=str(value.get("epoch_id") or ""),
            role=str(value.get("role") or ""),
            event_time=str(value.get("event_time") or ""),
            event_hash=str(value.get("event_hash") or ""),
        )

    def as_dict(self) -> dict:
        return {
            "raw_event_id": self.raw_event_id,
            "epoch_id": self.epoch_id,
            "role": self.role,
            "event_time": self.event_time,
            "event_hash": self.event_hash,
        }


SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS scene_shadow_schema (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    schema_version INTEGER NOT NULL,
    authority_note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenes (
    scene_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('open','cooling','finalized')),
    projection_status TEXT NOT NULL DEFAULT 'current'
        CHECK(projection_status IN ('current','retired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    cooling_at TEXT,
    finalized_at TEXT,
    current_revision_no INTEGER NOT NULL CHECK(current_revision_no >= 1),
    title_hint TEXT NOT NULL DEFAULT '',
    topic_hint TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS scene_current_events (
    raw_event_id INTEGER PRIMARY KEY,
    scene_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    epoch_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    event_time TEXT NOT NULL DEFAULT '',
    event_hash TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL,
    FOREIGN KEY(scene_id) REFERENCES scenes(scene_id) ON DELETE RESTRICT,
    UNIQUE(scene_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_scene_current_events_scene
ON scene_current_events(scene_id, ordinal);

CREATE TABLE IF NOT EXISTS scene_revisions (
    scene_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    state_snapshot TEXT NOT NULL,
    projection_status_snapshot TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_events_json TEXT NOT NULL,
    epoch_ids_json TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scene_id, revision_no),
    FOREIGN KEY(scene_id) REFERENCES scenes(scene_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS scene_transitions (
    transition_id TEXT PRIMARY KEY,
    scene_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(scene_id) REFERENCES scenes(scene_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_scene_transitions_scene
ON scene_transitions(scene_id, created_at);

CREATE TABLE IF NOT EXISTS scene_relations (
    relation_id TEXT PRIMARY KEY,
    from_scene_id TEXT NOT NULL,
    to_scene_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN (
        'revisit','continue','correct','resolve','split_from','merged_into'
    )),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(from_scene_id) REFERENCES scenes(scene_id) ON DELETE RESTRICT,
    FOREIGN KEY(to_scene_id) REFERENCES scenes(scene_id) ON DELETE RESTRICT,
    UNIQUE(from_scene_id, to_scene_id, relation)
);

CREATE TABLE IF NOT EXISTS boundary_candidates (
    candidate_id TEXT PRIMARY KEY,
    scene_id TEXT NOT NULL,
    before_event_id INTEGER NOT NULL,
    after_event_id INTEGER NOT NULL,
    score REAL NOT NULL,
    reasons_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(scene_id) REFERENCES scenes(scene_id) ON DELETE RESTRICT,
    UNIQUE(scene_id, before_event_id, after_event_id)
);
"""


class SceneShadowStore:
    def __init__(self, db_path: str, *, clock=None):
        self.db_path = os.path.abspath(os.path.expanduser(db_path))
        self._clock = clock or _now
        self._transaction_connection = None
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        if self._transaction_connection is not None:
            yield self._transaction_connection
            return
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        """同一调用内协调 Scene、边界和水位；失败整段回滚。"""
        with self._connect() as connection:
            if self._transaction_connection is not None:
                yield connection
                return
            connection.execute("BEGIN IMMEDIATE")
            self._transaction_connection = connection
            try:
                yield connection
            finally:
                self._transaction_connection = None

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            row = connection.execute(
                "SELECT schema_version FROM scene_shadow_schema WHERE singleton=1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO scene_shadow_schema(singleton,schema_version,authority_note) "
                    "VALUES(1,?,?)",
                    (
                        SCHEMA_VERSION,
                        "derived/rebuildable Scene projection; raw_events remains source truth",
                    ),
                )
            elif int(row["schema_version"]) != SCHEMA_VERSION:
                raise SceneContractError(
                    f"scene shadow schema mismatch: {row['schema_version']} != {SCHEMA_VERSION}"
                )

    @staticmethod
    def _normalize_events(events: Iterable[Mapping | int]) -> list[SceneEventRef]:
        refs = [SceneEventRef.from_value(item) for item in events]
        if not refs:
            raise SceneContractError("Scene must contain at least one source event")
        seen = set()
        for ref in refs:
            if ref.raw_event_id in seen:
                raise SceneContractError(f"duplicate raw_event_id {ref.raw_event_id}")
            seen.add(ref.raw_event_id)
        return refs

    def _scene_row(self, connection: sqlite3.Connection, scene_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM scenes WHERE scene_id=?", (scene_id,)
        ).fetchone()
        if row is None:
            raise SceneNotFound(scene_id)
        return row

    def _current_events(
        self, connection: sqlite3.Connection, scene_id: str
    ) -> list[SceneEventRef]:
        rows = connection.execute(
            "SELECT raw_event_id,epoch_id,role,event_time,event_hash "
            "FROM scene_current_events WHERE scene_id=? ORDER BY ordinal",
            (scene_id,),
        ).fetchall()
        return [
            SceneEventRef(
                raw_event_id=int(row["raw_event_id"]),
                epoch_id=row["epoch_id"],
                role=row["role"],
                event_time=row["event_time"],
                event_hash=row["event_hash"],
            )
            for row in rows
        ]

    def _assert_no_source_conflicts(
        self,
        connection: sqlite3.Connection,
        events: Sequence[SceneEventRef],
        *,
        allowed_scene_ids: set[str] | None = None,
    ) -> None:
        allowed = allowed_scene_ids or set()
        ids = [item.raw_event_id for item in events]
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT raw_event_id,scene_id FROM scene_current_events "
            f"WHERE raw_event_id IN ({placeholders})",
            ids,
        ).fetchall()
        conflicts = [
            (int(row["raw_event_id"]), row["scene_id"])
            for row in rows
            if row["scene_id"] not in allowed
        ]
        if conflicts:
            raise SourceConflict(f"source events already owned by current Scene: {conflicts}")

    def _write_membership(
        self,
        connection: sqlite3.Connection,
        scene_id: str,
        events: Sequence[SceneEventRef],
        *,
        allowed_scene_ids: set[str] | None = None,
    ) -> None:
        self._assert_no_source_conflicts(
            connection, events, allowed_scene_ids=allowed_scene_ids
        )
        connection.execute(
            "DELETE FROM scene_current_events WHERE scene_id=?", (scene_id,)
        )
        ts = self._clock()
        connection.executemany(
            "INSERT INTO scene_current_events("
            "raw_event_id,scene_id,ordinal,epoch_id,role,event_time,event_hash,added_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    ref.raw_event_id,
                    scene_id,
                    index,
                    ref.epoch_id,
                    ref.role,
                    ref.event_time,
                    ref.event_hash,
                    ts,
                )
                for index, ref in enumerate(events)
            ],
        )

    def _append_revision(
        self,
        connection: sqlite3.Connection,
        scene_id: str,
        *,
        reason: str,
    ) -> int:
        row = self._scene_row(connection, scene_id)
        events = self._current_events(connection, scene_id)
        if not events:
            raise SceneContractError("current Scene projection cannot have zero events")
        revision_no = int(row["current_revision_no"])
        payload = [item.as_dict() for item in events]
        epoch_ids = []
        for item in events:
            if item.epoch_id and item.epoch_id not in epoch_ids:
                epoch_ids.append(item.epoch_id)
        connection.execute(
            "INSERT INTO scene_revisions("
            "scene_id,revision_no,state_snapshot,projection_status_snapshot,reason,"
            "source_events_json,epoch_ids_json,source_fingerprint,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                scene_id,
                revision_no,
                row["state"],
                row["projection_status"],
                str(reason or ""),
                _stable_json(payload),
                _stable_json(epoch_ids),
                _hash(payload),
                self._clock(),
            ),
        )
        return revision_no

    def create_scene(
        self,
        events: Iterable[Mapping | int],
        *,
        scene_id: str | None = None,
        title_hint: str = "",
        topic_hint: str = "",
        reason: str = "create",
    ) -> str:
        refs = self._normalize_events(events)
        sid = scene_id or _new_id("scene")
        ts = self._clock()
        with self._connect() as connection:
            self._assert_no_source_conflicts(connection, refs)
            connection.execute(
                "INSERT INTO scenes("
                "scene_id,state,projection_status,created_at,updated_at,opened_at,"
                "current_revision_no,title_hint,topic_hint"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    "open",
                    "current",
                    ts,
                    ts,
                    ts,
                    1,
                    str(title_hint or ""),
                    str(topic_hint or ""),
                ),
            )
            self._write_membership(connection, sid, refs)
            self._append_revision(connection, sid, reason=reason)
            connection.execute(
                "INSERT INTO scene_transitions("
                "transition_id,scene_id,from_state,to_state,reason,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (_new_id("scene-transition"), sid, None, "open", reason, ts),
            )
        return sid

    def get_scene(self, scene_id: str) -> dict:
        with self._connect() as connection:
            row = self._scene_row(connection, scene_id)
            result = dict(row)
            result["events"] = [
                item.as_dict() for item in self._current_events(connection, scene_id)
            ]
            result["relations"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT * FROM scene_relations "
                    "WHERE from_scene_id=? OR to_scene_id=? ORDER BY created_at,relation_id",
                    (scene_id, scene_id),
                ).fetchall()
            ]
            return result

    def list_scenes(
        self,
        *,
        states: Sequence[str] | None = None,
        current_only: bool = True,
    ) -> list[dict]:
        params: list = []
        where = []
        if current_only:
            where.append("projection_status='current'")
        if states:
            invalid = set(states) - SCENE_STATES
            if invalid:
                raise SceneContractError(f"invalid states: {sorted(invalid)}")
            placeholders = ",".join("?" for _ in states)
            where.append(f"state IN ({placeholders})")
            params.extend(states)
        sql = "SELECT * FROM scenes"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, scene_id"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def _transition(self, scene_id: str, to_state: str, *, reason: str) -> None:
        if to_state not in SCENE_STATES:
            raise SceneContractError(f"invalid Scene state {to_state}")
        allowed = {
            "open": {"cooling", "finalized"},
            "cooling": {"open", "finalized"},
            "finalized": set(),
        }
        with self._connect() as connection:
            row = self._scene_row(connection, scene_id)
            if row["projection_status"] != "current":
                raise InvalidTransition("retired Scene cannot transition")
            current = row["state"]
            if to_state == current:
                return
            if to_state not in allowed[current]:
                raise InvalidTransition(f"{current} -> {to_state} is not allowed")
            ts = self._clock()
            assignments = ["state=?", "updated_at=?"]
            values: list = [to_state, ts]
            if to_state == "cooling":
                assignments.append("cooling_at=?")
                values.append(ts)
            elif to_state == "open":
                assignments.extend(["opened_at=?", "cooling_at=NULL"])
                values.append(ts)
            elif to_state == "finalized":
                assignments.append("finalized_at=?")
                values.append(ts)
            values.append(scene_id)
            connection.execute(
                f"UPDATE scenes SET {','.join(assignments)} WHERE scene_id=?",
                values,
            )
            connection.execute(
                "INSERT INTO scene_transitions("
                "transition_id,scene_id,from_state,to_state,reason,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (
                    _new_id("scene-transition"),
                    scene_id,
                    current,
                    to_state,
                    str(reason or ""),
                    ts,
                ),
            )

    def mark_cooling(self, scene_id: str, *, reason: str = "topic_shift") -> None:
        self._transition(scene_id, "cooling", reason=reason)

    def resume(self, scene_id: str, *, reason: str = "topic_resumed") -> None:
        self._transition(scene_id, "open", reason=reason)

    def finalize(self, scene_id: str, *, reason: str = "scene_complete") -> None:
        self._transition(scene_id, "finalized", reason=reason)

    def revise_sources(
        self,
        scene_id: str,
        events: Iterable[Mapping | int],
        *,
        reason: str,
        expected_revision_no: int | None = None,
    ) -> int:
        refs = self._normalize_events(events)
        with self._connect() as connection:
            row = self._scene_row(connection, scene_id)
            if row["projection_status"] != "current":
                raise SceneContractError("retired Scene cannot receive source revisions")
            if expected_revision_no is not None and int(row["current_revision_no"]) != expected_revision_no:
                raise SourceConflict("scene revision conflict")
            self._assert_no_source_conflicts(
                connection, refs, allowed_scene_ids={scene_id}
            )
            next_revision = int(row["current_revision_no"]) + 1
            connection.execute(
                "UPDATE scenes SET current_revision_no=?,updated_at=? WHERE scene_id=?",
                (next_revision, self._clock(), scene_id),
            )
            self._write_membership(
                connection,
                scene_id,
                refs,
                allowed_scene_ids={scene_id},
            )
            self._append_revision(connection, scene_id, reason=reason)
            return next_revision

    def split_scene(
        self,
        scene_id: str,
        left_events: Iterable[Mapping | int],
        right_events: Iterable[Mapping | int],
        *,
        reason: str,
        new_scene_id: str | None = None,
    ) -> str:
        left = self._normalize_events(left_events)
        right = self._normalize_events(right_events)
        left_ids = {item.raw_event_id for item in left}
        right_ids = {item.raw_event_id for item in right}
        if left_ids & right_ids:
            raise SceneContractError("split halves overlap")
        with self._connect() as connection:
            row = self._scene_row(connection, scene_id)
            current = self._current_events(connection, scene_id)
            current_ids = [item.raw_event_id for item in current]
            left_ordered_ids = [item.raw_event_id for item in left]
            right_ordered_ids = [item.raw_event_id for item in right]
            if left_ordered_ids + right_ordered_ids != current_ids:
                raise SceneContractError(
                    "split must preserve one contiguous ordered partition of current Scene events"
                )
            sid2 = new_scene_id or _new_id("scene")
            ts = self._clock()
            next_revision = int(row["current_revision_no"]) + 1

            connection.execute(
                "DELETE FROM scene_current_events WHERE scene_id=?", (scene_id,)
            )
            connection.execute(
                "UPDATE scenes SET current_revision_no=?,updated_at=? WHERE scene_id=?",
                (next_revision, ts, scene_id),
            )
            self._write_membership(connection, scene_id, left)
            self._append_revision(connection, scene_id, reason=f"split:left:{reason}")

            connection.execute(
                "INSERT INTO scenes("
                "scene_id,state,projection_status,created_at,updated_at,opened_at,"
                "cooling_at,finalized_at,current_revision_no,title_hint,topic_hint"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid2,
                    row["state"],
                    "current",
                    ts,
                    ts,
                    ts,
                    ts if row["state"] == "cooling" else None,
                    ts if row["state"] == "finalized" else None,
                    1,
                    "",
                    "",
                ),
            )
            self._write_membership(connection, sid2, right)
            self._append_revision(connection, sid2, reason=f"split:right:{reason}")
            connection.execute(
                "INSERT INTO scene_relations("
                "relation_id,from_scene_id,to_scene_id,relation,reason,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (
                    _new_id("scene-relation"),
                    sid2,
                    scene_id,
                    "split_from",
                    reason,
                    ts,
                ),
            )
            return sid2

    def merge_scenes(
        self,
        target_scene_id: str,
        source_scene_id: str,
        *,
        reason: str,
    ) -> int:
        if target_scene_id == source_scene_id:
            raise SceneContractError("cannot merge Scene into itself")
        with self._connect() as connection:
            target = self._scene_row(connection, target_scene_id)
            source = self._scene_row(connection, source_scene_id)
            if target["projection_status"] != "current" or source["projection_status"] != "current":
                raise SceneContractError("merge requires current Scene projections")
            if target["state"] != "finalized" or source["state"] != "finalized":
                raise SceneContractError("merge currently requires finalized Scenes")
            target_events = self._current_events(connection, target_scene_id)
            source_events = self._current_events(connection, source_scene_id)
            all_events = {item.raw_event_id: item for item in target_events + source_events}
            merged = [all_events[key] for key in sorted(all_events)]
            ts = self._clock()

            connection.execute(
                "DELETE FROM scene_current_events WHERE scene_id IN (?,?)",
                (target_scene_id, source_scene_id),
            )
            next_revision = int(target["current_revision_no"]) + 1
            connection.execute(
                "UPDATE scenes SET current_revision_no=?,updated_at=? WHERE scene_id=?",
                (next_revision, ts, target_scene_id),
            )
            connection.execute(
                "UPDATE scenes SET projection_status='retired',updated_at=? WHERE scene_id=?",
                (ts, source_scene_id),
            )
            self._write_membership(connection, target_scene_id, merged)
            self._append_revision(
                connection, target_scene_id, reason=f"merge:{source_scene_id}:{reason}"
            )
            connection.execute(
                "INSERT INTO scene_relations("
                "relation_id,from_scene_id,to_scene_id,relation,reason,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (
                    _new_id("scene-relation"),
                    source_scene_id,
                    target_scene_id,
                    "merged_into",
                    reason,
                    ts,
                ),
            )
            return next_revision

    def add_relation(
        self,
        from_scene_id: str,
        to_scene_id: str,
        relation: str,
        *,
        reason: str,
    ) -> str:
        if relation not in RELATIONS:
            raise SceneContractError(f"invalid relation {relation}")
        if from_scene_id == to_scene_id:
            raise SceneContractError("self relation is not allowed")
        relation_id = _new_id("scene-relation")
        with self._connect() as connection:
            self._scene_row(connection, from_scene_id)
            self._scene_row(connection, to_scene_id)
            connection.execute(
                "INSERT INTO scene_relations("
                "relation_id,from_scene_id,to_scene_id,relation,reason,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (
                    relation_id,
                    from_scene_id,
                    to_scene_id,
                    relation,
                    str(reason or ""),
                    self._clock(),
                ),
            )
        return relation_id

    def list_boundary_candidates(
        self,
        scene_id: str,
        *,
        status: str | None = None,
    ) -> list[dict]:
        with self._connect() as connection:
            self._scene_row(connection, scene_id)
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM boundary_candidates WHERE scene_id=? "
                    "ORDER BY score DESC,created_at,candidate_id",
                    (scene_id,),
                ).fetchall()
            else:
                clean = str(status or "").strip()
                if clean not in {"pending", "accepted", "rejected"}:
                    raise SceneContractError("invalid boundary candidate status")
                rows = connection.execute(
                    "SELECT * FROM boundary_candidates "
                    "WHERE scene_id=? AND status=? "
                    "ORDER BY score DESC,created_at,candidate_id",
                    (scene_id, clean),
                ).fetchall()
        return [dict(row) for row in rows]

    def add_boundary_candidate(
        self,
        scene_id: str,
        before_event_id: int,
        after_event_id: int,
        *,
        score: float,
        reasons: Sequence[str],
    ) -> str:
        before = int(before_event_id)
        after = int(after_event_id)
        if before <= 0 or after <= 0 or before == after:
            raise SceneContractError("invalid boundary event ids")
        candidate_id = _new_id("scene-boundary")
        ts = self._clock()
        with self._connect() as connection:
            self._scene_row(connection, scene_id)
            connection.execute(
                "INSERT INTO boundary_candidates("
                "candidate_id,scene_id,before_event_id,after_event_id,score,reasons_json,"
                "status,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    scene_id,
                    before,
                    after,
                    float(score),
                    _stable_json(list(reasons)),
                    "pending",
                    ts,
                    ts,
                ),
            )
        return candidate_id

    def resolve_boundary_candidate(self, candidate_id: str, status: str) -> None:
        if status not in {"accepted", "rejected"}:
            raise SceneContractError("boundary resolution must be accepted or rejected")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM boundary_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise SceneContractError(f"unknown boundary candidate {candidate_id}")
            if row["status"] != "pending":
                raise SceneContractError("boundary candidate already resolved")
            connection.execute(
                "UPDATE boundary_candidates SET status=?,updated_at=? WHERE candidate_id=?",
                (status, self._clock(), candidate_id),
            )

    def revisions(self, scene_id: str) -> list[dict]:
        with self._connect() as connection:
            self._scene_row(connection, scene_id)
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM scene_revisions WHERE scene_id=? ORDER BY revision_no",
                    (scene_id,),
                ).fetchall()
            ]
