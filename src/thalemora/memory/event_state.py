"""现实事项的可选标记，仅属于一次 Writer 修订或扩展的原话证据。

current_public_user_text 是受信宿主的实时读取函数，按 (raw_id, source,
source_event_id) 返回目前可用于普通聊天的用户原文。身份不唯一、撤回、
删除或权限不允许时返回 None。它属于调用配置，不能由候选提供。
"""
from contextlib import closing
import json

from . import evidence_resolver, revision_store


STATES = ("ongoing", "update", "closed")
EVIDENCE_TABLES = ("card_evidence", "memory_extension_evidence")


class EventStateRejected(ValueError):
    pass


def install(db):
    """显式加列，旧证据保留 NULL；与 schema.initialize 共用调用方事务。"""
    for table in EVIDENCE_TABLES:
        if not _installed(db, table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN event_state TEXT "
                       "CHECK(event_state IN ('ongoing','update','closed') "
                       "AND source_kind='user_text_exact' OR event_state IS NULL)")
            db.execute(f"CREATE INDEX idx_{table}_event_raw ON {table}(raw_event_start_id) "
                       "WHERE event_state IS NOT NULL")


def _installed(db, table):
    return any(row[1] == "event_state" for row in db.execute(f"PRAGMA table_info({table})"))


def verified_quote(evidence, raw_db_path, current_public_user_text):
    """重读完整用户发言，避免截去否定或条件；Raw 与宿主当前可见原文须一致。"""
    raw_id = evidence.get("raw_event_start_id")
    if (current_public_user_text is None or evidence.get("source_kind") != "user_text_exact"
            or type(raw_id) is not int or raw_id <= 0
            or evidence.get("raw_event_end_id") != raw_id or not evidence.get("event_hash")):
        return None
    with closing(evidence_resolver._readonly_connection(raw_db_path)) as db:
        raw = db.execute(
            "SELECT id,source,source_event_id,event_hash,role,text,created_at,metadata_json,deleted "
            "FROM raw_events WHERE id=? AND role='user' AND deleted=0", (raw_id,)
        ).fetchone()
    if raw is None or not raw["source"] or not raw["source_event_id"]:
        return None
    try:
        metadata = json.loads(raw["metadata_json"])
    except (TypeError, ValueError):
        return None
    if (not isinstance(metadata, dict) or metadata.get("channel", "main") != "main"
            or metadata.get("visibility", "normal") != "normal"
            or metadata.get("sensitivity", "normal") != "normal"
            or metadata.get("private") or metadata.get("sensitive") or metadata.get("revoked")):
        return None
    text = metadata.get("authored_text")
    if (not isinstance(text, str) or not text.strip() or evidence.get("char_start") != 0
            or evidence.get("char_end") != len(text) or not evidence.get("quote_hash")):
        return None
    resolved = evidence_resolver._resolve_exact(
        {**evidence, "source_ref": evidence.get("source_ref")}, {raw_id: raw}, None)
    if (resolved["verified"]
            and current_public_user_text(raw_id, raw["source"], raw["source_event_id"]) == text):
        return text
    return None


def _eligible_card(db, card_id):
    if revision_store._table_exists(db, "memory_card_lineage") and db.execute(
        "SELECT 1 FROM memory_card_lineage WHERE from_card_id=? AND retired_at IS NULL", (card_id,)
    ).fetchone():
        return None
    return db.execute(
        "SELECT r.id FROM cards c JOIN card_revisions r ON r.id=c.current_revision_id "
        "AND r.card_id=c.id WHERE c.id=? AND c.memory_kind='episodic' "
        "AND c.status IN ('fresh','sunk') AND c.source!='manual' AND r.origin!='manual_ui'",
        (card_id,)
    ).fetchone()


def check_write(db, card_id, raw_id):
    # BEGIN IMMEDIATE 串行检查两类写入；同一条完整原话只能认领一次。
    for table in EVIDENCE_TABLES:
        if not _installed(db, table):
            raise EventStateRejected("event_state_schema_required")
        if db.execute(f"SELECT 1 FROM {table} WHERE event_state IS NOT NULL "
                      "AND raw_event_start_id=?", (raw_id,)).fetchone():
            raise EventStateRejected("event_state_duplicate_evidence")
    if card_id is None:
        return
    if not _eligible_card(db, card_id):
        raise EventStateRejected("event_state_card_ineligible")
    for evidence, parent, fk in (("card_evidence", "card_revisions", "revision_id"),
                                 ("memory_extension_evidence", "memory_extensions", "extension_id")):
        if db.execute(f"SELECT 1 FROM {parent} p JOIN {evidence} e ON p.id=e.{fk} "
                      "WHERE p.card_id=? AND e.event_state IS NOT NULL AND e.raw_event_start_id>?",
                      (card_id, raw_id)).fetchone():
            raise EventStateRejected("event_state_stale_source")


def mark_new_evidence(db, result, state):
    if not result["changed"]:
        raise EventStateRejected("event_state_requires_new_mutation")
    if "extension_id" in result:
        table, key, owner = "memory_extension_evidence", "extension_id", result["extension_id"]
    else:
        table, key, owner = "card_evidence", "revision_id", result["current_revision_id"]
    count = db.execute(f"UPDATE {table} SET event_state=? WHERE {key}=? AND event_state IS NULL "
                       "AND source_kind='user_text_exact'", (state, owner)).rowcount
    if count != 1:
        raise EventStateRejected("event_state_requires_one_quote")


def read_current(card_id, *, memory_db_path, raw_db_path, current_public_user_text):
    """返回该卡当前版本的最新标记及原话出处。最新依据失效即隐藏，不复活旧状态。"""
    with closing(evidence_resolver._readonly_connection(memory_db_path)) as db:
        db.execute("BEGIN")
        if not all(_installed(db, table) for table in EVIDENCE_TABLES):
            return None
        base = _eligible_card(db, card_id)
        if base is None:
            return None
        evidence = db.execute(
            "SELECT e.*, x.id AS extension_id, x.status FROM memory_extensions x "
            "JOIN memory_extension_evidence e ON e.extension_id=x.id "
            "WHERE x.card_id=? AND x.base_revision_id=? AND e.event_state IS NOT NULL "
            "ORDER BY x.id DESC LIMIT 1", (card_id, base[0])
        ).fetchone()
        if evidence is None:
            evidence = db.execute(
                "SELECT e.*, NULL AS extension_id, 'active' AS status FROM card_evidence e "
                "WHERE e.revision_id=? AND e.event_state IS NOT NULL", (base[0],)
            ).fetchone()
        if evidence is None or evidence["status"] != "active":
            return None
        evidence = dict(evidence)
        quote = verified_quote(evidence, raw_db_path, current_public_user_text)
        if quote is None:
            return None
        return dict(card_id=card_id, revision_id=base[0], extension_id=evidence["extension_id"],
                    event_state=evidence["event_state"], evidence_id=evidence["id"],
                    raw_event_id=evidence["raw_event_start_id"], event_hash=evidence["event_hash"],
                    quote_hash=evidence["quote_hash"], quote=quote)
