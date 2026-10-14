"""Memory 正式修订写入。

调用方在事务前准备 embedding 与模型标识；事务内接口不提交或回滚。
沿用原有事实、错误与事务算法，不加载 Provider。
"""

import datetime

import hashlib

import json

import sqlite3

from . import field_sources as memory_field_sources

class CardNotFound(Exception):
    pass

class RevisionConflict(Exception):
    def __init__(self, expected_revision_id, current_revision_id):
        self.expected_revision_id = expected_revision_id
        self.current_revision_id = current_revision_id
        super().__init__(
            f"expected revision {expected_revision_id}, current revision "
            f"is {current_revision_id}"
        )

class InvalidRevisionState(Exception):
    pass

class ExtensionStorageUnavailable(Exception):
    pass

UNCHANGED = object()

_SNAPSHOT_FIELDS = (
    "content",
    "title",
    "tags",
    "keywords",
    "importance",
    "valence",
    "arousal",
    "happened_at",
)

def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _content_hash(content):
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()

def _connect(db_path):
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def _table_exists(conn, table_name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None

def _extension_storage_available(conn):
    return all(
        _table_exists(conn, table_name)
        for table_name in ("memory_extensions", "memory_extension_evidence")
    )

def extension_storage_available(db_path):
    """Return whether Extension V2 storage is fully installed, without mutating it."""
    conn = _connect(db_path)
    try:
        return _extension_storage_available(conn)
    finally:
        conn.close()

def _embedding_values(content, embedding, embedding_model=None):
    if not embedding:
        return None, None, None, None
    if embedding_model is None:
        raise ValueError("embedding_model is required with embedding")
    return (
        json.dumps(embedding).encode(),
        _content_hash(content),
        embedding_model,
        _now(),
    )

def _insert_revision(
    conn,
    *,
    card_id,
    revision_no,
    previous_revision_id,
    merged_from_revision_id,
    snapshot,
    change_type,
    origin,
    candidate_id=None,
):
    cursor = conn.execute(
        """
        INSERT INTO card_revisions (
            card_id, revision_no, previous_revision_id,
            merged_from_revision_id, content, title, tags, keywords,
            importance, valence, arousal, happened_at, content_hash,
            change_type, origin, candidate_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card_id,
            revision_no,
            previous_revision_id,
            merged_from_revision_id,
            snapshot["content"],
            snapshot["title"],
            snapshot["tags"],
            snapshot["keywords"],
            snapshot["importance"],
            snapshot["valence"],
            snapshot["arousal"],
            snapshot["happened_at"],
            _content_hash(snapshot["content"]),
            change_type,
            origin,
            candidate_id,
        ),
    )
    return int(cursor.lastrowid)

def _insert_evidence(conn, revision_id, source_kind, source_ref):
    conn.execute(
        "INSERT INTO card_evidence (revision_id, source_kind, source_ref) "
        "VALUES (?, ?, ?)",
        (revision_id, source_kind, source_ref),
    )

def _insert_evidence_rows(conn, revision_id, evidence_rows):
    for evidence in evidence_rows:
        conn.execute(
            """
            INSERT INTO card_evidence (
                revision_id, source_kind, source_ref,
                raw_event_start_id, raw_event_end_id, event_hash,
                char_start, char_end, quote_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                evidence["source_kind"],
                evidence.get("source_ref"),
                evidence.get("raw_event_start_id"),
                evidence.get("raw_event_end_id"),
                evidence.get("event_hash"),
                evidence.get("char_start"),
                evidence.get("char_end"),
                evidence.get("quote_hash"),
            ),
        )

def _insert_revision_evidence(
    conn, revision_id, evidence_source_kind, evidence_source_ref, evidence_rows
):
    if evidence_rows is not None:
        _insert_evidence_rows(conn, revision_id, evidence_rows)
    else:
        _insert_evidence(
            conn, revision_id, evidence_source_kind, evidence_source_ref
        )

def _existing_candidate_result(conn, candidate_id):
    if not candidate_id:
        return None
    row = conn.execute(
        "SELECT id, card_id FROM card_revisions WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "card_id": int(row["card_id"]),
        "current_revision_id": int(row["id"]),
        "changed": False,
    }

def create_card_in_transaction(
    conn,
    *,
    content,
    title="",
    tags="",
    keywords="",
    importance=5.0,
    valence=None,
    arousal=None,
    happened_at=None,
    embedding=None,
    embedding_model=None,
    pinned=0,
    source="auto",
    memory_kind="episodic",
    origin,
    evidence_source_kind=None,
    evidence_source_ref=None,
    evidence_rows=None,
    candidate_id=None,
    ring_content=None,
    ring_by="auto",
    field_sources=None,
):
    """Create a card, revision and evidence using the caller's transaction."""
    existing = _existing_candidate_result(conn, candidate_id)
    if existing:
        return existing
    embedding_values = _embedding_values(content, embedding, embedding_model)
    cursor = conn.execute(
        """
        INSERT INTO cards (
            content, happened_at, importance, tags, embedding, pinned, title,
            keywords, source, valence, arousal, embedding_content_hash,
            embedding_model, embedded_at, memory_kind
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            content,
            happened_at,
            importance,
            tags,
            embedding_values[0],
            pinned,
            title,
            keywords,
            source,
            valence,
            arousal,
            embedding_values[1],
            embedding_values[2],
            embedding_values[3],
            memory_kind,
        ),
    )
    card_id = int(cursor.lastrowid)
    snapshot = {
        "content": content,
        "title": title,
        "tags": tags,
        "keywords": keywords,
        "importance": importance,
        "valence": valence,
        "arousal": arousal,
        "happened_at": happened_at,
    }
    revision_id = _insert_revision(
        conn,
        card_id=card_id,
        revision_no=1,
        previous_revision_id=None,
        merged_from_revision_id=None,
        snapshot=snapshot,
        change_type="create",
        origin=origin,
        candidate_id=candidate_id,
    )
    _insert_revision_evidence(
        conn, revision_id, evidence_source_kind, evidence_source_ref,
        evidence_rows,
    )
    # ``rings`` is legacy display history. Canonical P3 history lives only in
    # card_revisions; keep accepting the old kwargs while callers migrate.
    cursor = conn.execute(
        "UPDATE cards SET current_revision_id=? WHERE id=?",
        (revision_id, card_id),
    )
    if cursor.rowcount != 1:
        raise InvalidRevisionState(f"card {card_id} projection update failed")
    memory_field_sources.mark(conn, card_id, field_sources or {})
    return {
        "card_id": card_id,
        "current_revision_id": revision_id,
        "changed": True,
    }

def create_card(db_path, **kwargs):
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = create_card_in_transaction(conn, **kwargs)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def update_card_in_transaction(
    conn,
    *,
    card_id,
    expected_current_revision_id,
    content=UNCHANGED,
    title=UNCHANGED,
    tags=UNCHANGED,
    keywords=UNCHANGED,
    importance=UNCHANGED,
    valence=UNCHANGED,
    arousal=UNCHANGED,
    happened_at=UNCHANGED,
    embedding=UNCHANGED,
    embedding_model=None,
    change_type="manual_edit",
    origin="manual_ui",
    evidence_source_kind="manual",
    evidence_source_ref="app_memory_ui",
    evidence_rows=None,
    merged_from_revision_id=None,
    candidate_id=None,
    ring_by="manual",
    ring_content=UNCHANGED,
    forbidden_memory_kind=None,
    force_revision=False,
    field_sources=None,
    maintenance_fields=(),
):
    """CAS-update a card projection and append its canonical revision."""
    existing = _existing_candidate_result(conn, candidate_id)
    if existing:
        return existing
    current = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if current is None:
        raise CardNotFound(card_id)
    is_metadata_maintenance = origin == "migration" and change_type == "derived_refresh" and bool(maintenance_fields)
    if current["source"] == "manual" and origin != "manual_ui" and not is_metadata_maintenance:
        raise InvalidRevisionState(
            f"manual-source card {card_id} can only be changed by manual_ui"
        )
    if forbidden_memory_kind and current["memory_kind"] == forbidden_memory_kind:
        raise InvalidRevisionState(
            f"{forbidden_memory_kind} card {card_id} cannot be updated here"
        )
    current_revision_id = current["current_revision_id"]
    if current_revision_id != expected_current_revision_id:
        raise RevisionConflict(expected_current_revision_id, current_revision_id)

    current_revision = conn.execute(
        "SELECT revision_no, card_id, origin FROM card_revisions WHERE id=?",
        (current_revision_id,),
    ).fetchone()
    if current_revision is None or current_revision["card_id"] != card_id:
        raise InvalidRevisionState(f"card {card_id} has an invalid current revision")
    if (
        current["memory_kind"] == "episodic"
        and current_revision["origin"] == "manual_ui"
        and origin != "manual_ui"
        and not is_metadata_maintenance
    ):
        raise InvalidRevisionState(
            f"manual-ui current revision on episodic card {card_id} can only be changed by manual_ui"
        )
    if merged_from_revision_id is not None:
        merged = conn.execute(
            "SELECT card_id FROM card_revisions WHERE id=?",
            (merged_from_revision_id,),
        ).fetchone()
        if merged is None:
            raise InvalidRevisionState(
                f"merged revision {merged_from_revision_id} does not exist"
            )

    supplied = {
        "content": content,
        "title": title,
        "tags": tags,
        "keywords": keywords,
        "importance": importance,
        "valence": valence,
        "arousal": arousal,
        "happened_at": happened_at,
    }
    updated = {
        field: current[field] if supplied[field] is UNCHANGED else supplied[field]
        for field in _SNAPSHOT_FIELDS
    }
    if is_metadata_maintenance:
        allowed = set(maintenance_fields) & {"title","keywords","importance","tags"}
        if any(field not in allowed and current[field] != updated[field] for field in _SNAPSHOT_FIELDS):
            raise InvalidRevisionState("maintenance can only fill approved metadata")
        provenance = memory_field_sources.get(conn, card_id)
        if any(
            provenance.get(field) not in {"pending","derived"}
            and (current[field] is not None if field == "importance" else bool(str(current[field] or "").strip()))
            for field in allowed
        ):
            raise InvalidRevisionState("maintenance requires a pending or derived field")
    if (
        not force_revision
        and merged_from_revision_id is None
        and all(current[field] == updated[field] for field in _SNAPSHOT_FIELDS)
    ):
        memory_field_sources.mark(conn, card_id, field_sources or {})
        return {
            "card_id": card_id,
            "current_revision_id": current_revision_id,
            "changed": False,
        }

    content_changed = current["content"] != updated["content"]
    if content_changed:
        new_embedding = None if embedding is UNCHANGED else embedding
        embedding_values = _embedding_values(
            updated["content"], new_embedding, embedding_model
        )
    else:
        embedding_values = (
            current["embedding"],
            current["embedding_content_hash"],
            current["embedding_model"],
            current["embedded_at"],
        )

    revision_id = _insert_revision(
        conn,
        card_id=card_id,
        revision_no=int(current_revision["revision_no"]) + 1,
        previous_revision_id=current_revision_id,
        merged_from_revision_id=merged_from_revision_id,
        snapshot=updated,
        change_type=change_type,
        origin=origin,
        candidate_id=candidate_id,
    )
    _insert_revision_evidence(
        conn, revision_id, evidence_source_kind, evidence_source_ref,
        evidence_rows,
    )
    cursor = conn.execute(
        """
        UPDATE cards SET
            content=?, title=?, tags=?, keywords=?, importance=?, valence=?,
            arousal=?, happened_at=?, embedding=?, embedding_content_hash=?,
            embedding_model=?, embedded_at=?, current_revision_id=?
        WHERE id=? AND current_revision_id=?
        """,
        (
            updated["content"],
            updated["title"],
            updated["tags"],
            updated["keywords"],
            updated["importance"],
            updated["valence"],
            updated["arousal"],
            updated["happened_at"],
            embedding_values[0],
            embedding_values[1],
            embedding_values[2],
            embedding_values[3],
            revision_id,
            card_id,
            current_revision_id,
        ),
    )
    if cursor.rowcount != 1:
        raise RevisionConflict(expected_current_revision_id, None)
    memory_field_sources.mark(conn, card_id, field_sources or {})
    return {
        "card_id": card_id,
        "current_revision_id": revision_id,
        "changed": True,
    }

def update_card(db_path, **kwargs):
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = update_card_in_transaction(conn, **kwargs)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _existing_extension_result(conn, candidate_id):
    if not candidate_id:
        return None
    if not _extension_storage_available(conn):
        return None
    row = conn.execute(
        "SELECT id, card_id, base_revision_id FROM memory_extensions "
        "WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "card_id": int(row["card_id"]),
        "base_revision_id": int(row["base_revision_id"]),
        "current_revision_id": int(row["base_revision_id"]),
        "extension_id": int(row["id"]),
        "changed": False,
    }

def _insert_extension_evidence(
    conn,
    extension_id,
    evidence_source_kind,
    evidence_source_ref,
    evidence_rows,
):
    rows = evidence_rows
    if rows is None:
        rows = [{
            "source_kind": evidence_source_kind,
            "source_ref": evidence_source_ref,
        }]
    for evidence in rows:
        conn.execute(
            """
            INSERT INTO memory_extension_evidence (
                extension_id, source_kind, source_ref,
                raw_event_start_id, raw_event_end_id, event_hash,
                char_start, char_end, quote_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                extension_id,
                evidence["source_kind"],
                evidence.get("source_ref"),
                evidence.get("raw_event_start_id"),
                evidence.get("raw_event_end_id"),
                evidence.get("event_hash"),
                evidence.get("char_start"),
                evidence.get("char_end"),
                evidence.get("quote_hash"),
            ),
        )

def create_extension_in_transaction(
    conn,
    *,
    card_id,
    expected_base_revision_id,
    content,
    keywords="",
    importance=5.0,
    happened_at=None,
    origin,
    source,
    same_memory_confidence,
    evidence_source_kind=None,
    evidence_source_ref=None,
    evidence_rows=None,
    candidate_id=None,
):
    """Append a same-memory delta without changing the canonical card."""
    if not _extension_storage_available(conn):
        raise ExtensionStorageUnavailable("extension_storage_unavailable")
    existing = _existing_extension_result(conn, candidate_id)
    if existing:
        return existing
    if candidate_id and conn.execute(
        "SELECT 1 FROM card_revisions WHERE candidate_id=?", (candidate_id,)
    ).fetchone():
        raise InvalidRevisionState(
            f"candidate {candidate_id} already committed as a card revision"
        )

    card = conn.execute(
        "SELECT current_revision_id, status, memory_kind, source "
        "FROM cards WHERE id=?",
        (card_id,),
    ).fetchone()
    if card is None:
        raise CardNotFound(card_id)
    current_revision_id = card["current_revision_id"]
    if current_revision_id != expected_base_revision_id:
        raise RevisionConflict(expected_base_revision_id, current_revision_id)
    if card["status"] != "fresh":
        raise InvalidRevisionState(f"card {card_id} is not fresh")
    if card["memory_kind"] != "episodic":
        raise InvalidRevisionState(
            f"{card['memory_kind']} card {card_id} cannot receive an extension"
        )
    if card["source"] == "manual":
        raise InvalidRevisionState(
            f"manual-source card {card_id} cannot receive an extension"
        )
    base = conn.execute(
        "SELECT card_id, origin, change_type FROM card_revisions WHERE id=?",
        (expected_base_revision_id,),
    ).fetchone()
    if base is None or int(base["card_id"]) != int(card_id):
        raise InvalidRevisionState(
            f"card {card_id} has an invalid current revision"
        )
    allowed_base_origins = {"auto_writer", "agent_mcp", "bottle_answer"}
    # Full-history cutover rewrites canonical auto cards as migration/derived_refresh.
    # Those revisions are authoritative current projections, not legacy snapshots, and
    # must remain extensible after cutover. Manual/manual_ui authority is still blocked.
    migration_refresh = (
        base["origin"] == "migration" and base["change_type"] == "derived_refresh"
    )
    if base["origin"] not in allowed_base_origins and not migration_refresh:
        raise InvalidRevisionState(
            f"{base['origin']} revision {expected_base_revision_id} "
            "cannot receive an extension"
        )

    cursor = conn.execute(
        """
        INSERT INTO memory_extensions (
            card_id, base_revision_id, content, keywords, importance,
            happened_at, origin, source, candidate_id,
            same_memory_confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card_id,
            expected_base_revision_id,
            content,
            keywords,
            importance,
            happened_at,
            origin,
            source,
            candidate_id,
            same_memory_confidence,
        ),
    )
    extension_id = int(cursor.lastrowid)
    _insert_extension_evidence(
        conn,
        extension_id,
        evidence_source_kind,
        evidence_source_ref,
        evidence_rows,
    )
    return {
        "card_id": int(card_id),
        "base_revision_id": int(current_revision_id),
        "current_revision_id": int(current_revision_id),
        "extension_id": extension_id,
        "changed": True,
    }

def create_extension(db_path, **kwargs):
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = create_extension_in_transaction(conn, **kwargs)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def find_candidate_revision(db_path, candidate_id):
    """Return an already committed candidate mutation for crash recovery."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, card_id FROM card_revisions WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        return {"revision_id": int(row["id"]), "card_id": int(row["card_id"])}
    finally:
        conn.close()

def find_candidate_extension(db_path, candidate_id):
    """Return an already committed extension for idempotency/recovery."""
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "memory_extensions"):
            return None
        row = conn.execute(
            "SELECT id, card_id, base_revision_id FROM memory_extensions "
            "WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "extension_id": int(row["id"]),
            "card_id": int(row["card_id"]),
            "base_revision_id": int(row["base_revision_id"]),
            "current_revision_id": int(row["base_revision_id"]),
        }
    finally:
        conn.close()

def find_candidate_mutation(db_path, candidate_id):
    """Find either canonical revision or extension committed by a candidate."""
    revision = find_candidate_revision(db_path, candidate_id)
    extension = find_candidate_extension(db_path, candidate_id)
    if revision and extension:
        raise InvalidRevisionState(
            f"candidate {candidate_id} is attached to multiple mutations"
        )
    if extension:
        return {"kind": "extension", **extension}
    if revision:
        return {
            "kind": "revision",
            "card_id": revision["card_id"],
            "current_revision_id": revision["revision_id"],
            "revision_id": revision["revision_id"],
        }
    return None
