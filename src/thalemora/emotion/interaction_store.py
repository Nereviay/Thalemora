"""Durable, fail-open interaction-affect state for normal chat.

Only deterministic observations extracted from an already-persisted assistant reply
may be written.  The current row is mutable for lifecycle bookkeeping, while the
audit table keeps every transition.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from .interaction import (
    MAX_UNRESOLVED_AGE_SECONDS,
    decay_interaction_affect,
    extract_explicit_interaction_affect,
)


BUSY_TIMEOUT_MS = 250


def _utc(value: datetime | None = None) -> datetime:
    now = value or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def normalize_dependency_refs(refs: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Keep unique Boot Context card revision refs for Memory validation."""
    out = []
    seen = set()
    for raw in refs or ():
        if not isinstance(raw, Mapping):
            continue
        card_id = _positive_int(raw.get("card_id"))
        revision_id = _positive_int(raw.get("revision_id"))
        kind = str(raw.get("kind") or "").strip()
        if kind == "timeline":
            kind = "boot_context"
        if kind != "boot_context":
            continue
        key = (card_id, revision_id, kind)
        if card_id is None or revision_id is None or key in seen:
            continue
        seen.add(key)
        out.append({
            "card_id": card_id,
            "revision_id": revision_id,
            "kind": kind,
        })
    return out


class InteractionAffectStore:
    def __init__(self, db_path: str, *, extract=extract_explicit_interaction_affect):
        self.db_path = str(db_path)
        self.extract = extract

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._ensure_schema(conn)
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS interaction_affect_states (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              thread_id TEXT NOT NULL,
              status TEXT NOT NULL,
              label TEXT NOT NULL,
              target TEXT NOT NULL,
              intensity INTEGER NOT NULL,
              unresolved INTEGER NOT NULL,
              source TEXT NOT NULL,
              evidence_quote TEXT NOT NULL,
              source_user_raw_event_id INTEGER NOT NULL,
              source_assistant_raw_event_id INTEGER NOT NULL,
              dependency_refs_json TEXT NOT NULL DEFAULT '[]',
              generation_id TEXT,
              updated_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              terminal_reason TEXT,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_interaction_affect_current
              ON interaction_affect_states(thread_id, status, id DESC);
            CREATE TABLE IF NOT EXISTS interaction_affect_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              state_id INTEGER,
              thread_id TEXT NOT NULL,
              action TEXT NOT NULL,
              reason TEXT,
              details_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_interaction_affect_audit_state
              ON interaction_affect_audit(state_id, id);
            """
        )

    @staticmethod
    def _state(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        try:
            refs = json.loads(row["dependency_refs_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            refs = []
        return {
            "id": int(row["id"]),
            "thread_id": row["thread_id"],
            "status": row["status"],
            "label": row["label"],
            "target": row["target"],
            "intensity": int(row["intensity"]),
            "unresolved": bool(row["unresolved"]),
            "source": row["source"],
            "evidence_quote": row["evidence_quote"],
            "source_user_raw_event_id": int(row["source_user_raw_event_id"]),
            "source_assistant_raw_event_id": int(row["source_assistant_raw_event_id"]),
            "dependency_refs": normalize_dependency_refs(refs),
            "generation_id": row["generation_id"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _active_row(conn: sqlite3.Connection, thread_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM interaction_affect_states "
            "WHERE thread_id=? AND status='active' ORDER BY id DESC LIMIT 1",
            (thread_id,),
        ).fetchone()

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        *,
        state_id: int | None,
        thread_id: str,
        action: str,
        now: datetime,
        reason: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO interaction_affect_audit"
            "(state_id, thread_id, action, reason, details_json, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                state_id,
                thread_id,
                action,
                reason,
                json.dumps(dict(details or {}), ensure_ascii=False, separators=(",", ":")),
                _iso(now),
            ),
        )

    def record_completed_reply(
        self,
        assistant_text: str,
        *,
        source_user_raw_event_id: Any,
        source_assistant_raw_event_id: Any,
        dependency_refs: Iterable[Mapping[str, Any]] | None = None,
        generation_id: str | None = None,
        thread_id: str = "main",
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Apply an explicit-only observation after both raw events exist."""
        user_raw_id = _positive_int(source_user_raw_event_id)
        assistant_raw_id = _positive_int(source_assistant_raw_event_id)
        if user_raw_id is None or assistant_raw_id is None:
            return None
        observation = self.extract(assistant_text)
        if observation is None:
            return None
        now = _utc(now)
        thread = str(thread_id or "main").strip() or "main"
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = self._active_row(conn, thread)
            if observation["action"] == "clear":
                if current is not None:
                    conn.execute(
                        "UPDATE interaction_affect_states "
                        "SET status='cleared', terminal_reason=? WHERE id=?",
                        (observation["resolution"], int(current["id"])),
                    )
                    self._audit(
                        conn,
                        state_id=int(current["id"]),
                        thread_id=thread,
                        action="cleared",
                        reason=observation["resolution"],
                        now=now,
                        details={"evidence_quote": observation["evidence_quote"]},
                    )
                conn.execute("COMMIT")
                return None

            if current is not None:
                conn.execute(
                    "UPDATE interaction_affect_states "
                    "SET status='superseded', terminal_reason='new_explicit_state' WHERE id=?",
                    (int(current["id"]),),
                )
                self._audit(
                    conn,
                    state_id=int(current["id"]),
                    thread_id=thread,
                    action="superseded",
                    reason="new_explicit_state",
                    now=now,
                )
            refs = normalize_dependency_refs(dependency_refs)
            expires = now + timedelta(seconds=MAX_UNRESOLVED_AGE_SECONDS)
            cursor = conn.execute(
                "INSERT INTO interaction_affect_states("
                "thread_id,status,label,target,intensity,unresolved,source,evidence_quote,"
                "source_user_raw_event_id,source_assistant_raw_event_id,"
                "dependency_refs_json,generation_id,updated_at,expires_at,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    thread,
                    "active",
                    observation["label"],
                    observation["target"],
                    int(observation["intensity"]),
                    1 if observation["unresolved"] else 0,
                    observation["source"],
                    observation["evidence_quote"],
                    user_raw_id,
                    assistant_raw_id,
                    json.dumps(refs, ensure_ascii=False, separators=(",", ":")),
                    str(generation_id or "").strip() or None,
                    _iso(now),
                    _iso(expires),
                    _iso(now),
                ),
            )
            state_id = int(cursor.lastrowid)
            self._audit(
                conn,
                state_id=state_id,
                thread_id=thread,
                action="set",
                reason="assistant_explicit",
                now=now,
                details={"dependency_refs": refs},
            )
            conn.execute("COMMIT")
            return self.current(thread_id=thread)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def current(self, *, thread_id: str = "main") -> dict[str, Any] | None:
        conn = self._connect()
        try:
            return self._state(self._active_row(conn, thread_id))
        finally:
            conn.close()

    def load_for_injection(
        self,
        validate_refs: Callable[[list[dict[str, Any]]], str],
        *,
        thread_id: str = "main",
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Return a decayed state; unavailable validation suppresses this read only."""
        now = _utc(now)
        conn = self._connect()
        try:
            row = self._active_row(conn, thread_id)
            state = self._state(row)
            if state is None:
                return None
            try:
                expires_at = datetime.fromisoformat(state["expires_at"].replace("Z", "+00:00"))
            except (TypeError, ValueError):
                expires_at = now
            if now >= _utc(expires_at):
                self._terminate(
                    conn, state, status="expired", action="expired",
                    reason="ttl_elapsed", now=now,
                )
                return None
            refs = state["dependency_refs"]
            if refs:
                try:
                    validation = str(validate_refs(refs) or "unavailable")
                except Exception:
                    validation = "unavailable"
                if validation == "invalid":
                    self._terminate(
                        conn, state, status="invalidated", action="invalidated",
                        reason="dependency_invalid", now=now,
                        details={"dependency_refs": refs},
                    )
                    return None
                if validation != "valid":
                    return None
            return decay_interaction_affect(state, now=now)
        finally:
            conn.close()

    def _terminate(
        self,
        conn: sqlite3.Connection,
        state: Mapping[str, Any],
        *,
        status: str,
        action: str,
        reason: str,
        now: datetime,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE interaction_affect_states SET status=?, terminal_reason=? "
            "WHERE id=? AND status='active'",
            (status, reason, int(state["id"])),
        ).rowcount
        if changed:
            self._audit(
                conn,
                state_id=int(state["id"]),
                thread_id=str(state["thread_id"]),
                action=action,
                reason=reason,
                now=now,
                details=details,
            )
        conn.execute("COMMIT")

    def audit_rows(self, *, thread_id: str = "main") -> list[dict[str, Any]]:
        """Small test/ops read model; callers never need to parse live tables."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT state_id, action, reason, details_json, created_at "
                "FROM interaction_affect_audit WHERE thread_id=? ORDER BY id",
                (thread_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
