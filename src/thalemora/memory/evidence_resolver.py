# -*- coding: utf-8 -*-
"""从显式指定的授权档案只读解析证据，不使用默认数据库路径。"""
import hashlib
import json
import os
import sqlite3
from urllib.parse import quote


EXACT_SOURCE_KINDS = {"user_text_exact", "assistant_text_exact"}


def _readonly_connection(path):
    absolute_path = os.path.abspath(os.fspath(path))
    connection = sqlite3.connect(
        f"file:{quote(absolute_path)}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def _range(start_id, end_id):
    if start_id is None and end_id is None:
        return None
    return {"start_id": start_id, "end_id": end_id}


def _optional_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _base_result(evidence):
    start_id = _optional_int(evidence["raw_event_start_id"])
    end_id = _optional_int(evidence["raw_event_end_id"])
    same_event = (
        evidence["source_kind"] in EXACT_SOURCE_KINDS
        and start_id is not None
        and end_id in (None, start_id)
    )
    return {
        "source_kind": evidence["source_kind"],
        "source_ref": evidence["source_ref"],
        "raw_event_id": start_id if same_event else None,
        "raw_event_range": (
            None
            if same_event
            else _range(
                start_id,
                end_id,
            )
        ),
        "role": None,
        "created_at": None,
        "resolution": evidence["source_kind"],
        "verified": False,
    }


def _evidence_key(row):
    return tuple(
        row[column]
        for column in (
            "source_kind",
            "source_ref",
            "raw_event_start_id",
            "raw_event_end_id",
            "event_hash",
            "char_start",
            "char_end",
            "quote_hash",
        )
    )


def _load_current_evidence(memory_db_path, card_id):
    connection = _readonly_connection(memory_db_path)
    try:
        rows = connection.execute(
            """
            SELECT e.source_kind, e.source_ref,
                   e.raw_event_start_id, e.raw_event_end_id, e.event_hash,
                   e.char_start, e.char_end, e.quote_hash
            FROM cards c
            JOIN card_evidence e ON e.revision_id=c.current_revision_id
            WHERE c.id=?
            ORDER BY e.id
            """,
            (card_id,),
        ).fetchall()
    finally:
        connection.close()

    unique_rows = []
    seen = set()
    for row in rows:
        key = _evidence_key(row)
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)
    return unique_rows


def _load_lineage_evidence(memory_db_path, card_id):
    """Load evidence that still belongs to the current revision identity, read-only.

    Extend/derived-refresh/consolidate revisions inherit prior evidence. A current
    manual_edit/correct/supersede revision is an identity reset: evidence from the
    replaced version must not leak into current-card matching. The same reset rule
    applies when walking backward and reaching the revision that established the
    active version.
    """
    connection = _readonly_connection(memory_db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(card_revisions)")}
        required = {"id", "previous_revision_id", "merged_from_revision_id", "change_type"}
        if not required <= columns:
            return [(None, row) for row in _load_current_evidence(memory_db_path, card_id)]
        row = connection.execute(
            "SELECT current_revision_id FROM cards WHERE id=?", (card_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return []
        queue = [int(row[0])]
        revision_ids = []
        seen_revisions = set()
        reset_types = {"manual_edit", "correct", "supersede"}
        while queue:
            revision_id = queue.pop(0)
            if revision_id in seen_revisions:
                continue
            seen_revisions.add(revision_id)
            revision = connection.execute(
                "SELECT id,previous_revision_id,merged_from_revision_id,change_type "
                "FROM card_revisions WHERE id=?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                continue
            revision_ids.append(int(revision["id"]))
            if str(revision["change_type"]) in reset_types:
                continue
            previous = revision["previous_revision_id"]
            merged = revision["merged_from_revision_id"]
            if previous is not None:
                queue.append(int(previous))
            if merged is not None:
                queue.append(int(merged))
        rows = []
        for revision_id in revision_ids:
            for evidence in connection.execute(
                """SELECT source_kind,source_ref,raw_event_start_id,raw_event_end_id,event_hash,
                          char_start,char_end,quote_hash
                     FROM card_evidence WHERE revision_id=? ORDER BY id""",
                (revision_id,),
            ):
                rows.append((revision_id, evidence))
    finally:
        connection.close()
    unique = []
    seen = set()
    for revision_id, evidence in rows:
        key = _evidence_key(evidence)
        if key in seen:
            continue
        seen.add(key)
        unique.append((revision_id, evidence))
    return unique


def _load_extension_evidence(memory_db_path, extension_id):
    connection = _readonly_connection(memory_db_path)
    try:
        rows = connection.execute(
            """
            SELECT source_kind, source_ref,
                   raw_event_start_id, raw_event_end_id, event_hash,
                   char_start, char_end, quote_hash
            FROM memory_extension_evidence
            WHERE extension_id=?
            ORDER BY id
            """,
            (extension_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        rows = []
    finally:
        connection.close()

    unique_rows = []
    seen = set()
    for row in rows:
        key = _evidence_key(row)
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)
    return unique_rows


def _load_raw_events(raw_db_path, event_ids):
    if not event_ids:
        return {}, None
    try:
        connection = _readonly_connection(raw_db_path)
    except sqlite3.Error:
        return {}, "raw_store_unavailable"
    try:
        placeholders = ",".join("?" for _ in event_ids)
        rows = connection.execute(
            "SELECT id, event_hash, role, text, created_at, metadata_json, deleted "
            f"FROM raw_events WHERE id IN ({placeholders})",
            event_ids,
        ).fetchall()
        return {int(row["id"]): row for row in rows}, None
    except sqlite3.Error:
        return {}, "raw_store_unavailable"
    finally:
        connection.close()


def _modern_quotable_text(raw_event):
    if raw_event["role"] != "user":
        return raw_event["text"]
    try:
        metadata = json.loads(raw_event["metadata_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    authored_text = metadata.get("authored_text") if isinstance(metadata, dict) else None
    return authored_text if isinstance(authored_text, str) else None


def _resolve_exact(evidence, raw_events, raw_error):
    result = _base_result(evidence)
    event_id = result["raw_event_id"]
    if event_id is None:
        result["resolution"] = "invalid_raw_event_range"
        return result
    if raw_error:
        result["resolution"] = raw_error
        return result

    raw_event = raw_events.get(event_id)
    if raw_event is None:
        result["resolution"] = "raw_event_missing"
        return result
    result["role"] = raw_event["role"]
    result["created_at"] = raw_event["created_at"]
    if int(raw_event["deleted"] or 0) == 1:
        result["resolution"] = "raw_event_deleted"
        return result

    raw_text = raw_event["text"]
    if not isinstance(raw_text, str):
        result["resolution"] = "invalid_raw_event_text"
        return result

    event_hash = evidence["event_hash"]
    char_start = evidence["char_start"]
    char_end = evidence["char_end"]
    quote_hash = evidence["quote_hash"]
    if event_hash and event_hash != raw_event["event_hash"]:
        result["resolution"] = "event_hash_mismatch"
        return result
    span_values = (char_start, char_end, quote_hash)
    if all(value in (None, "") for value in span_values):
        result["quote"] = raw_text
        result["resolution"] = "whole_event_legacy"
        return result
    if char_start is None or char_end is None or not quote_hash:
        result["resolution"] = "incomplete_exact_metadata"
        return result

    text = _modern_quotable_text(raw_event)
    if not isinstance(text, str):
        result["resolution"] = "quotable_text_unavailable"
        return result
    if (
        isinstance(char_start, bool)
        or isinstance(char_end, bool)
        or not isinstance(char_start, int)
        or not isinstance(char_end, int)
        or char_start < 0
        or char_end <= char_start
        or char_end > len(text)
    ):
        result["resolution"] = "invalid_span"
        return result

    resolved_quote = text[char_start:char_end]
    resolved_hash = hashlib.sha256(resolved_quote.encode("utf-8")).hexdigest()
    if resolved_hash != quote_hash:
        result["resolution"] = "quote_hash_mismatch"
        return result
    result["quote"] = resolved_quote
    result["resolution"] = "span_verified"
    result["verified"] = True
    return result


def _resolve_evidence_rows(evidence_rows, raw_db_path):
    exact_ids = []
    for row in evidence_rows:
        if row["source_kind"] not in EXACT_SOURCE_KINDS:
            continue
        start_id = _optional_int(row["raw_event_start_id"])
        end_id = _optional_int(row["raw_event_end_id"])
        if start_id is not None and end_id in (None, start_id):
            exact_ids.append(start_id)
    exact_ids = list(dict.fromkeys(exact_ids))
    raw_events, raw_error = _load_raw_events(raw_db_path, exact_ids)

    resolved = []
    for evidence in evidence_rows:
        source_kind = evidence["source_kind"]
        if source_kind in EXACT_SOURCE_KINDS:
            resolved.append(_resolve_exact(evidence, raw_events, raw_error))
            continue
        item = _base_result(evidence)
        if source_kind == "raw_range":
            item["raw_event_id"] = None
            item["raw_event_range"] = _range(
                _optional_int(evidence["raw_event_start_id"]),
                _optional_int(evidence["raw_event_end_id"]),
            )
        resolved.append(item)
    return resolved


def resolve_card_evidence(card_id, *, memory_db_path, raw_db_path):
    """Resolve evidence for a card's current revision without opening writable DBs."""
    evidence_rows = _load_current_evidence(memory_db_path, card_id)
    return _resolve_evidence_rows(evidence_rows, raw_db_path)


def resolve_card_lineage_evidence(card_id, *, memory_db_path, raw_db_path):
    """Resolve evidence across the current card revision ancestry without writes."""
    lineage_rows = _load_lineage_evidence(memory_db_path, card_id)
    if not lineage_rows:
        return []
    resolved = _resolve_evidence_rows([row for _revision_id, row in lineage_rows], raw_db_path)
    for item, (revision_id, _row) in zip(resolved, lineage_rows):
        item["lineage_revision_id"] = revision_id
    return resolved


def resolve_extension_evidence(
    extension_id, *, memory_db_path, raw_db_path
):
    """Resolve one extension's own evidence without consulting card evidence."""
    evidence_rows = _load_extension_evidence(memory_db_path, extension_id)
    return _resolve_evidence_rows(evidence_rows, raw_db_path)
