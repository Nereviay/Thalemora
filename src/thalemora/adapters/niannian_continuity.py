from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import partial
import json
import logging
from pathlib import Path
import sqlite3

from hashlib import sha256
from thalemora.memory.event_state import read_current
from thalemora.continuity.facts import ContinuityFact, estimate_tokens, render_primer
from thalemora.continuity.recent import build_recent_summary, _filtered_text


RECENT_STATUS_MAX_AGE = timedelta(days=7)

def _normal_metadata(metadata, principal_id):
    return (isinstance(metadata, dict)
            and metadata.get("principal_id", principal_id) == principal_id
            and metadata.get("channel", "main") == "main"
            and metadata.get("visibility", "normal") == "normal"
            and metadata.get("sensitivity", "normal") == "normal"
            and metadata.get("request_kind", "chat") == "chat"
            and not any(metadata.get(flag) for flag in ("revoked", "private", "sensitive")))

def _read_only(db_path):
    conn = sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=0.25)
    conn.row_factory = sqlite3.Row
    return closing(conn)

def _public_user_event(conversation, raw, principal_id, raw_id):
    rows = conversation.execute(
        """SELECT m.event_id, m.client_event_id, m.content, m.metadata_json
           FROM message_records m JOIN context_epochs e ON e.epoch_id=m.epoch_id
           WHERE m.raw_event_id=? AND e.thread_id='main' AND e.status IN ('active','closed')
             AND e.deleted_at IS NULL AND e.purged_at IS NULL
             AND m.source IN ('canonical_submit','chat_submit','edit_resend')
             AND m.role='user' AND m.kind='message' AND m.status='active' AND m.deleted_at IS NULL
             AND m.superseded_by_event_id IS NULL AND m.raw_projection_status='linked'
             AND m.client_event_id IS NOT NULL AND m.event_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM message_records other
                             WHERE other.raw_event_id=m.raw_event_id AND other.id!=m.id)
             AND (SELECT COUNT(*) FROM projection_slots p
                  WHERE p.message_record_id=m.id AND p.epoch_id=m.epoch_id)=1
           LIMIT 2""", (raw_id,),
    ).fetchall()
    if len(rows) != 1:
        return None
    canonical = rows[0]
    meta = json.loads(canonical["metadata_json"] or "{}")
    if not _normal_metadata(meta, principal_id):
        return None
    authored = meta.get("authored_text", canonical["content"])
    row = raw.execute(
        """SELECT source, source_event_id, created_at, event_hash, metadata_json FROM raw_events
           WHERE id=? AND source='app' AND source_event_id=? AND role='user'
             AND conversation_id='main' AND deleted=0""",
        (raw_id, "chat:" + canonical["client_event_id"]),
    ).fetchone()
    if row is None or not isinstance(authored, str) or not authored:
        return None
    metadata = json.loads(row["metadata_json"])
    if not _normal_metadata(metadata, principal_id) or metadata.get("authored_text") != authored:
        return None
    return authored, canonical["event_id"], row

def _current_public_user_text(conversation, raw, principal_id, raw_id, source, source_event_id):
    try:
        event = _public_user_event(conversation, raw, principal_id, raw_id)
    except (sqlite3.Error, TypeError, ValueError):
        return None
    if event and (event[2]["source"], event[2]["source_event_id"]) == (source, source_event_id):
        return event[0]
    return None

def _verified_user_quote(item, principal_id, conversation, raw):
    raw_id = item.get("event_id")
    if (type(raw_id) is not int or not 0 < raw_id < 2**63
            or item.get("role") != "user" or not isinstance(item.get("quote"), str) or not item["quote"]
            or not item.get("event_hash") or not isinstance(item.get("created_at"), str)):
        return None
    occurred = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
    if occurred.tzinfo is None:
        return None

    event = _public_user_event(conversation, raw, principal_id, raw_id)
    if event is None:
        return None
    authored, event_id, row = event
    if row["event_hash"] != item["event_hash"] or item["quote"] not in authored:
        return None
    # Writer 也把 RawEvent 的 SQLite 无偏移时间按 UTC 读取。
    raw_time = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    if raw_time.tzinfo is None:
        raw_time = raw_time.replace(tzinfo=timezone.utc)
    if raw_time != occurred:
        return None
    return authored, occurred, {"raw_event_id": raw_id, "event_id": event_id,
                               "event_hash": item["event_hash"], "created_at": item["created_at"]}

def _recent_writer_source(fact, conversation, raw_events_db):
    evidence = json.loads(fact.evidence_json)
    if not 1 <= len(evidence) <= 3 or not all(isinstance(item, dict) for item in evidence):
        return None
    ids = ",".join(str(item.get("event_id")) for item in evidence)
    if fact.source_kind == "canonical_raw_event" and (
            fact.source_ref != "raw-events:" + ids or fact.source_hash != sha256(fact.evidence_json.encode("utf-8")).hexdigest()):
        return None
    refs = []
    matching_raw_ids = []
    now = datetime.now(timezone.utc)
    with _read_only(raw_events_db) as raw:
        raw.execute("BEGIN")
        for item in evidence:
            verified = _verified_user_quote(item, fact.principal_id, conversation, raw)
            if verified is None:
                return None
            authored, occurred, ref = verified
            if occurred > now:
                return None
            if item["quote"] == authored == fact.content and now - occurred <= RECENT_STATUS_MAX_AGE:
                matching_raw_ids.append(ref["raw_event_id"])
            refs.append(ref)
    if not matching_raw_ids:
        return None
    return {"ref": f"{fact.fact_id}:v{fact.version}", "version": fact.version, "kind": "memory",
            "text": fact.content, "principal_id": fact.principal_id, "visibility": "normal",
            "source_refs": refs, "matching_raw_ids": matching_raw_ids}

_MARKER_TABLES = (
    ("card_evidence", "card_revisions", "revision_id"),
    ("memory_extension_evidence", "memory_extensions", "extension_id"),
)

def _event_candidates(facts_db, status_raw_ids):
    """Bound both index walks before resolving card ownership. Never sort full history."""
    markers = []
    status_claimed = False
    with _read_only(facts_db) as facts:
        facts.execute("BEGIN")
        for table, parent, fk in _MARKER_TABLES:
            index = f"idx_{table}_event_raw"
            if not facts.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index,)).fetchone():
                return [], False
            markers.extend(facts.execute(
                f"""SELECT recent.raw_id, p.card_id FROM (
                    SELECT raw_event_start_id AS raw_id, {fk} FROM {table} INDEXED BY {index}
                    WHERE event_state IS NOT NULL ORDER BY raw_event_start_id DESC LIMIT 12
                ) recent JOIN {parent} p ON p.id=recent.{fk}"""
            ).fetchall())
            # Even an older Writer status must not revive a closed or superseded card.
            for raw_id in status_raw_ids:
                if facts.execute(f"SELECT 1 FROM {table} INDEXED BY {index} WHERE event_state IS NOT NULL "
                                 "AND raw_event_start_id=? LIMIT 1", (raw_id,)).fetchone():
                    status_claimed = True
    cards = dict.fromkeys(card for _, card in sorted(markers, key=lambda row: row[0], reverse=True))
    return list(cards)[:12], status_claimed

def _event_source(event, principal_id):
    key = f"memory-event:{event['card_id']}"
    evidence = {name: event[name] for name in ("card_id", "revision_id", "extension_id", "event_state",
                                               "evidence_id", "raw_event_id", "event_hash", "quote_hash")}
    ref = (f"{key}:r{event['revision_id']}:x{event['extension_id']}:raw{event['raw_event_id']}"
           f":{event['event_hash']}:{event['quote_hash']}")
    return {"ref": ref, "kind": "memory", "principal_id": principal_id, "visibility": "normal",
            "text": event["quote"], "source_refs": [evidence]}

def build_activation_primer(current_facts, *, principal_id, conversation, raw_events_db, facts_db=None):
    """Freeze Writer status and, for a new bound epoch, eligible Memory events."""
    profile = []
    updates = []
    sources = {}
    for fact in current_facts:
        if fact.fact_key != "user.recent_status" or fact.source_kind not in {"canonical_raw_event", "manual_ui"}:
            profile.append(fact)
            continue
        evidence = json.loads(fact.evidence_json)
        # 人工改值没有引文。锁操作原样保留来源，可能是 Prompt 迁移而非 Writer。
        if fact.source_kind == "manual_ui" and (not evidence or all(
                isinstance(ref, dict) and ref.get("source_kind") == "user_managed_prompt" for ref in evidence)):
            profile.append(fact)
            continue
        if (fact.principal_id != principal_id or fact.status != "active"
                or fact.visibility != "primer" or fact.sensitivity != "normal"):
            continue
        try:
            source = _recent_writer_source(fact, conversation, raw_events_db)
        except (sqlite3.Error, ValueError, TypeError) as exc:
            # An unavailable optional source must not block the archive/rollover.
            logging.getLogger(__name__).warning("Recent status evidence unavailable (%s)", type(exc).__name__)
            continue
        if source is not None:
            sources[source["ref"]] = source
            updates.append({"key": fact.fact_key, "state": "current",
                            "text": fact.content, "source_refs": [source["ref"]]})
    if facts_db:
        status_ids = [raw_id for source in sources.values() for raw_id in source["matching_raw_ids"]]
        try:
            cards, status_claimed = _event_candidates(facts_db, status_ids)
            if status_claimed:
                updates.clear()  # Only the Writer status has been added so far.
            if cards:
                with _read_only(raw_events_db) as raw:
                    reader = partial(_current_public_user_text, conversation, raw, principal_id)
                    for card in reversed(cards):
                        event = read_current(card, memory_db_path=facts_db, raw_db_path=raw_events_db,
                                             current_public_user_text=reader)
                        if event is None or event["event_state"] not in {"ongoing", "update"}:
                            continue
                        source = _event_source(event, principal_id)
                        sources[source["ref"]] = source
                        updates.append({"key": f"memory-event:{card}",
                                        "state": "open" if event["event_state"] == "ongoing" else "changed",
                                        "text": event["quote"], "source_refs": [source["ref"]]})
        except (sqlite3.Error, ValueError, TypeError) as exc:
            logging.getLogger(__name__).warning("Recent event evidence unavailable (%s)", type(exc).__name__)
    primer = render_primer(profile, principal_id=principal_id)
    primer["recent_summary"] = build_recent_summary(updates, sources=sources, principal_id=principal_id)
    return primer

def _status_visible(status, principal_id, facts_db, conversation_db, raw_events_db):
    try:
        ref, = status["source_refs"]
        with _read_only(facts_db) as facts:
            row = facts.execute(
                """SELECT l.principal_id,l.owner_scope,l.fact_key,v.fact_id,v.version,v.content,
                          v.status,v.visibility,v.sensitivity,v.source_kind,v.source_ref,v.source_hash,
                          v.created_at,v.evidence_json
                   FROM continuity_fact_lineages l JOIN continuity_fact_versions v ON v.fact_id=l.fact_id
                   JOIN continuity_fact_versions latest ON latest.fact_id=l.fact_id AND latest.version=(
                     SELECT MAX(version) FROM continuity_fact_versions WHERE fact_id=l.fact_id)
                   WHERE l.principal_id=? AND l.owner_scope='user' AND l.fact_key='user.recent_status'
                     AND v.version=? AND latest.status='active'
                     AND latest.visibility='primer' AND latest.sensitivity='normal'""",
                (principal_id, ref["version"]),
            ).fetchone()
        if row is not None and row["content"] == status["text"]:
            with _read_only(conversation_db) as conversation:
                conversation.execute("BEGIN")
                source = _recent_writer_source(ContinuityFact(**dict(row)), conversation, raw_events_db)
            if source and all(source[name] == ref.get(name) for name in ("ref", "kind", "version", "source_refs")):
                return True
    except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        logging.getLogger(__name__).warning("Frozen recent status evidence unavailable (%s)", type(exc).__name__)
    return False

def _event_visible(item, principal_id, facts_db, conversation, raw, raw_events_db):
    source, = item["source_refs"]
    evidence, = source["source_refs"]
    card = evidence["card_id"]
    if item["key"] != f"memory-event:{card}":
        return False
    event = read_current(card, memory_db_path=facts_db, raw_db_path=raw_events_db,
                         current_public_user_text=partial(_current_public_user_text, conversation, raw, principal_id))
    if event is None or event["event_state"] not in {"ongoing", "update"}:
        return False
    if event["raw_event_id"] == evidence["raw_event_id"]:
        return event["quote"] == item["text"] and all(event.get(k) == v for k, v in evidence.items())
    # Only a later update may coexist with the frozen version. Recheck its
    # original evidence too: deleting an old extension must not revive its quote.
    if event["raw_event_id"] < evidence["raw_event_id"]:
        return False
    if evidence["extension_id"] is None:
        owner = """card_evidence e JOIN card_revisions r ON r.id=e.revision_id
                   WHERE r.card_id=:card_id AND r.id=:revision_id"""
    else:
        owner = """memory_extension_evidence e JOIN memory_extensions x ON x.id=e.extension_id
                   WHERE x.card_id=:card_id AND x.id=:extension_id
                   AND x.base_revision_id=:revision_id AND x.status='active'"""
    with _read_only(facts_db) as facts:
        eligible = facts.execute(f"""SELECT 1 FROM {owner}
            AND e.id=:evidence_id AND e.event_state=:event_state AND e.source_kind='user_text_exact'
            AND e.raw_event_start_id=:raw_event_id AND e.raw_event_end_id=:raw_event_id
            AND e.event_hash=:event_hash AND e.quote_hash=:quote_hash
            AND e.char_start=0 AND e.char_end=:length""", {**evidence, "length": len(item["text"])}).fetchone()
    if eligible is None:
        return False
    original = _public_user_event(conversation, raw, principal_id, evidence["raw_event_id"])
    return bool(original and original[0] == item["text"] and original[2]["event_hash"] == evidence["event_hash"])

def visible_primer_text(content, recent, *, principal_id, facts_db, conversation_db, raw_events_db):
    """Check only frozen items at delivery. Never discover candidates or refresh the snapshot."""
    items = (recent or {}).get("items", ())
    hidden = set()
    events = [item for item in items if item["key"].startswith("memory-event:")]
    if events:
        try:
            with _read_only(conversation_db) as conversation, _read_only(raw_events_db) as raw:
                conversation.execute("BEGIN")
                raw.execute("BEGIN")
                for item in events:
                    if not _event_visible(item, principal_id, facts_db, conversation, raw, raw_events_db):
                        hidden.add(item["key"])
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            logging.getLogger(__name__).warning("Frozen event evidence unavailable (%s)", type(exc).__name__)
            hidden.update(item["key"] for item in events)
    for item in items:
        if item["key"] == "user.recent_status" and not _status_visible(
                item, principal_id, facts_db, conversation_db, raw_events_db):
            hidden.add(item["key"])
    return _filtered_text(content, recent, [item for item in items if item["key"] not in hidden])
