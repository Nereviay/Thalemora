"""Independent durable authority for shadow Self Model maintenance runs.

The store is separate from canonical conversation storage. It keeps only
runtime-owned provenance, lifecycle facts, and validated tool calls; provider
reasoning and visible response text are never persisted.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterator, Mapping, Sequence

from .contract import namespace_for_principal, validate_model_args


STORE_VERSION = "p3d-5b-maintenance-authority-v1"
RUN_STATES = frozenset({"accepted", "completed", "failed", "cancelled", "superseded"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class MaintenanceStoreError(ValueError):
    """Stable failure raised for authority conflicts or corrupt lineage."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(name: str, value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise MaintenanceStoreError(f"invalid_{name}")
    return value


def _timestamp(name: str, value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise MaintenanceStoreError(f"invalid_{name}") from exc
    if parsed.tzinfo is None:
        raise MaintenanceStoreError(f"invalid_{name}")
    return value


def _sha(name: str, value: str | None) -> str | None:
    if value is not None and not _SHA_RE.fullmatch(value):
        raise MaintenanceStoreError(f"invalid_{name}")
    return value


def _source_refs_json(refs: Sequence[Mapping[str, str]]) -> str:
    normalized: list[dict[str, str]] = []
    for item in refs:
        if set(item) != {"durable_ref", "source_hash"}:
            raise MaintenanceStoreError("invalid_context_source_ref")
        durable_ref = str(item["durable_ref"])
        if not durable_ref or ":" not in durable_ref or len(durable_ref) > 512:
            raise MaintenanceStoreError("invalid_context_source_ref")
        source_hash = str(item["source_hash"])
        _sha("context_source_hash", source_hash)
        normalized.append({"durable_ref": durable_ref, "source_hash": source_hash})
    return _canonical_json(normalized)


@dataclass(frozen=True)
class MaintenanceRun:
    run_id: str
    principal_id: str
    namespace: str
    context_epoch_id: str
    trigger_kind: str
    trigger_key: str
    normal_context_sha256: str
    normal_message_count: int
    context_source_refs_json: str
    started_at: str
    store_version: str
    header_fingerprint: str

    @property
    def context_source_refs(self) -> tuple[dict[str, str], ...]:
        return tuple(json.loads(self.context_source_refs_json))


@dataclass(frozen=True)
class MaintenanceLifecycleEvent:
    event_seq: int
    event_id: str
    run_id: str
    state: str
    provider_visible_text_sha256: str | None
    recorded_at: str
    reason_code: str | None
    event_fingerprint: str


@dataclass(frozen=True)
class MaintenanceToolCall:
    tool_seq: int
    run_id: str
    tool_call_id: str
    tool_name: str
    args_json: str
    tool_call_root_id: str
    result_status: str
    result_code: str | None
    recorded_at: str
    tool_fingerprint: str

    @property
    def args(self) -> dict[str, Any]:
        return dict(json.loads(self.args_json))

    @property
    def durable_ref(self) -> str:
        return f"maintenance-tool:{self.run_id}:{self.tool_call_id}"


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS maintenance_runs (
    run_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    context_epoch_id TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    trigger_key TEXT NOT NULL,
    normal_context_sha256 TEXT NOT NULL,
    normal_message_count INTEGER NOT NULL CHECK (normal_message_count >= 0),
    context_source_refs_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    store_version TEXT NOT NULL,
    header_fingerprint TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS maintenance_lifecycle_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('accepted','completed','failed','cancelled','superseded')),
    provider_visible_text_sha256 TEXT,
    recorded_at TEXT NOT NULL,
    reason_code TEXT,
    event_fingerprint TEXT NOT NULL UNIQUE,
    UNIQUE (run_id, state),
    FOREIGN KEY (run_id) REFERENCES maintenance_runs(run_id)
);
CREATE TABLE IF NOT EXISTS maintenance_tool_calls (
    tool_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    tool_call_root_id TEXT NOT NULL UNIQUE,
    result_status TEXT NOT NULL,
    result_code TEXT,
    recorded_at TEXT NOT NULL,
    tool_fingerprint TEXT NOT NULL UNIQUE,
    UNIQUE (run_id, tool_call_id),
    FOREIGN KEY (run_id) REFERENCES maintenance_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_maintenance_events_run
    ON maintenance_lifecycle_events(run_id, event_seq);
CREATE INDEX IF NOT EXISTS idx_maintenance_tools_run
    ON maintenance_tool_calls(run_id, tool_seq);
CREATE TRIGGER IF NOT EXISTS maintenance_runs_no_update
BEFORE UPDATE ON maintenance_runs BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER IF NOT EXISTS maintenance_runs_no_delete
BEFORE DELETE ON maintenance_runs BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER IF NOT EXISTS maintenance_events_no_update
BEFORE UPDATE ON maintenance_lifecycle_events BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER IF NOT EXISTS maintenance_events_no_delete
BEFORE DELETE ON maintenance_lifecycle_events BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER IF NOT EXISTS maintenance_tools_no_update
BEFORE UPDATE ON maintenance_tool_calls BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER IF NOT EXISTS maintenance_tools_no_delete
BEFORE DELETE ON maintenance_tool_calls BEGIN SELECT RAISE(ABORT, 'append_only'); END;
"""


class SQLiteSelfModelMaintenanceStore:
    """Append-only, idempotent authority log with verified reads."""

    def __init__(self, path: str | Path, *, readonly: bool = False):
        if path is None or not str(path).strip():
            raise ValueError("an explicit maintenance SQLite path is required")
        self.path = str(path)
        target = Path(path).resolve().as_uri() + "?mode=ro" if readonly else self.path
        self._connection = sqlite3.connect(target, uri=readonly, isolation_level=None, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        if readonly:
            return
        self._connection.executescript(_SCHEMA)
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @staticmethod
    def _header_payload(**values: Any) -> dict[str, Any]:
        return {key: values[key] for key in (
            "run_id", "principal_id", "namespace", "context_epoch_id",
            "trigger_kind", "trigger_key", "normal_context_sha256",
            "normal_message_count", "context_source_refs_json", "started_at",
            "store_version",
        )}

    def accept_run(
        self,
        *,
        run_id: str,
        principal_id: str,
        context_epoch_id: str,
        trigger_kind: str,
        trigger_key: str,
        normal_context_sha256: str,
        normal_message_count: int,
        context_source_refs: Sequence[Mapping[str, str]],
        started_at: str,
    ) -> MaintenanceRun:
        run_id = _stable_id("run_id", run_id)
        principal_id = _stable_id("principal_id", principal_id)
        context_epoch_id = _stable_id("context_epoch_id", context_epoch_id)
        trigger_kind = _stable_id("trigger_kind", trigger_kind)
        trigger_key = _stable_id("trigger_key", trigger_key)
        _sha("normal_context_sha256", normal_context_sha256)
        if isinstance(normal_message_count, bool) or not isinstance(normal_message_count, int) or normal_message_count < 0:
            raise MaintenanceStoreError("invalid_normal_message_count")
        started_at = _timestamp("started_at", started_at)
        namespace = namespace_for_principal(principal_id)
        refs_json = _source_refs_json(context_source_refs)
        payload = self._header_payload(
            run_id=run_id, principal_id=principal_id, namespace=namespace,
            context_epoch_id=context_epoch_id, trigger_kind=trigger_kind,
            trigger_key=trigger_key, normal_context_sha256=normal_context_sha256,
            normal_message_count=normal_message_count,
            context_source_refs_json=refs_json, started_at=started_at,
            store_version=STORE_VERSION,
        )
        fingerprint = _sha256(_canonical_json(payload))
        with self._transaction():
            existing = self._connection.execute(
                "SELECT * FROM maintenance_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is not None:
                canonical = self._run_from_row(existing)
                if canonical.header_fingerprint != fingerprint:
                    raise MaintenanceStoreError("run_id_conflict")
                return canonical
            self._connection.execute(
                """INSERT INTO maintenance_runs (
                    run_id, principal_id, namespace, context_epoch_id,
                    trigger_kind, trigger_key, normal_context_sha256,
                    normal_message_count, context_source_refs_json, started_at,
                    store_version, header_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*payload.values(), fingerprint),
            )
            self._append_event_in_transaction(
                run_id=run_id, state="accepted",
                provider_visible_text_sha256=None, recorded_at=started_at,
                reason_code=None,
            )
        result = self.run(run_id)
        assert result is not None
        return result

    def _append_event_in_transaction(
        self, *, run_id: str, state: str,
        provider_visible_text_sha256: str | None, recorded_at: str,
        reason_code: str | None,
    ) -> MaintenanceLifecycleEvent:
        payload = {
            "run_id": run_id, "state": state,
            "provider_visible_text_sha256": provider_visible_text_sha256,
            "recorded_at": recorded_at, "reason_code": reason_code,
        }
        fingerprint = _sha256(_canonical_json(payload))
        event_id = "maintenance-event:" + fingerprint.removeprefix("sha256:")[:32]
        existing = self._connection.execute(
            "SELECT * FROM maintenance_lifecycle_events WHERE run_id = ? AND state = ?",
            (run_id, state),
        ).fetchone()
        if existing is not None:
            canonical = self._event_from_row(existing)
            if canonical.event_fingerprint != fingerprint:
                raise MaintenanceStoreError("lifecycle_event_conflict")
            return canonical
        self._connection.execute(
            """INSERT INTO maintenance_lifecycle_events (
                event_id, run_id, state, provider_visible_text_sha256,
                recorded_at, reason_code, event_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, run_id, state, provider_visible_text_sha256,
             recorded_at, reason_code, fingerprint),
        )
        row = self._connection.execute(
            "SELECT * FROM maintenance_lifecycle_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return self._event_from_row(row)

    def append_lifecycle_event(
        self, *, run_id: str, state: str, recorded_at: str,
        provider_visible_text_sha256: str | None = None,
        reason_code: str | None = None,
    ) -> MaintenanceLifecycleEvent:
        if state not in RUN_STATES or state == "accepted":
            raise MaintenanceStoreError("invalid_lifecycle_state")
        _timestamp("recorded_at", recorded_at)
        _sha("provider_visible_text_sha256", provider_visible_text_sha256)
        if reason_code is not None:
            _stable_id("reason_code", reason_code)
        with self._transaction():
            if self._connection.execute(
                "SELECT 1 FROM maintenance_runs WHERE run_id = ?", (run_id,)
            ).fetchone() is None:
                raise MaintenanceStoreError("run_not_found")
            rows = self._connection.execute(
                "SELECT * FROM maintenance_lifecycle_events WHERE run_id = ? ORDER BY event_seq",
                (run_id,),
            ).fetchall()
            events = tuple(self._event_from_row(row) for row in rows)
            current = events[-1].state
            existing = next((item for item in events if item.state == state), None)
            if existing is not None:
                payload = {
                    "run_id": run_id, "state": state,
                    "provider_visible_text_sha256": provider_visible_text_sha256,
                    "recorded_at": recorded_at, "reason_code": reason_code,
                }
                if existing.event_fingerprint != _sha256(_canonical_json(payload)):
                    raise MaintenanceStoreError("lifecycle_event_conflict")
                return existing
            if current != "accepted":
                raise MaintenanceStoreError("terminal_state_conflict")
            return self._append_event_in_transaction(
                run_id=run_id, state=state,
                provider_visible_text_sha256=provider_visible_text_sha256,
                recorded_at=recorded_at, reason_code=reason_code,
            )

    def append_tool_call(
        self, *, run_id: str, tool_call_id: str, tool_name: str,
        args: Mapping[str, Any], result_status: str, recorded_at: str,
        result_code: str | None = None,
    ) -> MaintenanceToolCall:
        _stable_id("run_id", run_id)
        tool_call_id = _stable_id("tool_call_id", tool_call_id)
        tool_name = _stable_id("tool_name", tool_name)
        result_status = _stable_id("result_status", result_status)
        if result_code is not None:
            _stable_id("result_code", result_code)
        _timestamp("recorded_at", recorded_at)
        validated_args = validate_model_args(tool_name, args)
        expected_status = (
            "read_only" if tool_name == "self_model_review" else "validated"
        )
        if result_status != expected_status:
            raise MaintenanceStoreError("invalid_tool_result_status")
        args_json = _canonical_json(validated_args)
        root_id = f"maintenance-tool-call:{run_id}:{tool_call_id}"
        payload = {
            "run_id": run_id, "tool_call_id": tool_call_id,
            "tool_name": tool_name, "args_json": args_json,
            "tool_call_root_id": root_id, "result_status": result_status,
            "result_code": result_code, "recorded_at": recorded_at,
        }
        fingerprint = _sha256(_canonical_json(payload))
        with self._transaction():
            if self._connection.execute(
                "SELECT 1 FROM maintenance_runs WHERE run_id = ?", (run_id,)
            ).fetchone() is None:
                raise MaintenanceStoreError("run_not_found")
            existing = self._connection.execute(
                "SELECT * FROM maintenance_tool_calls WHERE run_id = ? AND tool_call_id = ?",
                (run_id, tool_call_id),
            ).fetchone()
            if existing is not None:
                canonical = self._tool_from_row(existing)
                if canonical.tool_fingerprint != fingerprint:
                    raise MaintenanceStoreError("tool_call_id_conflict")
                return canonical
            state_row = self._connection.execute(
                "SELECT state FROM maintenance_lifecycle_events WHERE run_id = ? ORDER BY event_seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if state_row is None or state_row["state"] != "accepted":
                raise MaintenanceStoreError("run_not_accepted")
            self._connection.execute(
                """INSERT INTO maintenance_tool_calls (
                    run_id, tool_call_id, tool_name, args_json,
                    tool_call_root_id, result_status, result_code, recorded_at,
                    tool_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, tool_call_id, tool_name, args_json, root_id,
                 result_status, result_code, recorded_at, fingerprint),
            )
        result = self.tool_call(run_id, tool_call_id)
        assert result is not None
        return result

    def run(self, run_id: str) -> MaintenanceRun | None:
        row = self._connection.execute(
            "SELECT * FROM maintenance_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._run_from_row(row) if row is not None else None

    def run_ids(self) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT * FROM maintenance_runs ORDER BY rowid"
        ).fetchall()
        # Reading every header makes enumeration fail closed on tampering too.
        return tuple(self._run_from_row(row).run_id for row in rows)

    def lifecycle_events(self, run_id: str) -> tuple[MaintenanceLifecycleEvent, ...]:
        rows = self._connection.execute(
            "SELECT * FROM maintenance_lifecycle_events WHERE run_id = ? ORDER BY event_seq",
            (run_id,),
        ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def current_state(self, run_id: str) -> str | None:
        events = self.lifecycle_events(run_id)
        return events[-1].state if events else None

    def tool_call(self, run_id: str, tool_call_id: str) -> MaintenanceToolCall | None:
        row = self._connection.execute(
            "SELECT * FROM maintenance_tool_calls WHERE run_id = ? AND tool_call_id = ?",
            (run_id, tool_call_id),
        ).fetchone()
        return self._tool_from_row(row) if row is not None else None

    def tool_calls(self, run_id: str) -> tuple[MaintenanceToolCall, ...]:
        rows = self._connection.execute(
            "SELECT * FROM maintenance_tool_calls WHERE run_id = ? ORDER BY tool_seq",
            (run_id,),
        ).fetchall()
        return tuple(self._tool_from_row(row) for row in rows)

    def tool_call_by_durable_ref(self, durable_ref: str) -> MaintenanceToolCall | None:
        row = self._connection.execute(
            """SELECT * FROM maintenance_tool_calls
               WHERE ('maintenance-tool:' || run_id || ':' || tool_call_id) = ?""",
            (durable_ref,),
        ).fetchone()
        return self._tool_from_row(row) if row is not None else None

    def _run_from_row(self, row: sqlite3.Row) -> MaintenanceRun:
        result = MaintenanceRun(**dict(row))
        payload = self._header_payload(**asdict(result))
        if result.store_version != STORE_VERSION or result.header_fingerprint != _sha256(_canonical_json(payload)):
            raise MaintenanceStoreError("authority_integrity_failure")
        _source_refs_json(result.context_source_refs)
        return result

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> MaintenanceLifecycleEvent:
        result = MaintenanceLifecycleEvent(**dict(row))
        payload = {key: getattr(result, key) for key in (
            "run_id", "state", "provider_visible_text_sha256", "recorded_at", "reason_code"
        )}
        if result.event_fingerprint != _sha256(_canonical_json(payload)):
            raise MaintenanceStoreError("authority_integrity_failure")
        return result

    @staticmethod
    def _tool_from_row(row: sqlite3.Row) -> MaintenanceToolCall:
        result = MaintenanceToolCall(**dict(row))
        payload = {key: getattr(result, key) for key in (
            "run_id", "tool_call_id", "tool_name", "args_json", "tool_call_root_id",
            "result_status", "result_code", "recorded_at"
        )}
        if result.tool_fingerprint != _sha256(_canonical_json(payload)):
            raise MaintenanceStoreError("authority_integrity_failure")
        try:
            parsed = json.loads(result.args_json)
        except json.JSONDecodeError as exc:
            raise MaintenanceStoreError("authority_integrity_failure") from exc
        if not isinstance(parsed, dict) or result.args_json != _canonical_json(parsed):
            raise MaintenanceStoreError("authority_integrity_failure")
        return result
