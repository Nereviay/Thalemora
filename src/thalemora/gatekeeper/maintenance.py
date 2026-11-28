"""人工字段优先的元数据补齐、正文整理与 Water 分流。"""
import json
import sqlite3
from thalemora.gatekeeper import core as gatekeeper, keywords as keyword_policy
from thalemora.memory import field_sources as memory_field_sources, revision_store
POLICY = "gatekeeper-maintain-v2-first-person"


def connect(path):
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure(conn):
    memory_field_sources.ensure(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_maintenance_state (
            id INTEGER PRIMARY KEY CHECK(id=1),
            last_revision_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_maintenance_receipts (
            card_id INTEGER NOT NULL,
            source_revision_id INTEGER NOT NULL,
            policy_version TEXT NOT NULL,
            changes_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            PRIMARY KEY(card_id,source_revision_id,policy_version)
        )
    """)
    # A derived first-person reading projection; the handwritten canonical
    # card body and evidence remain unchanged.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_organized_content (
            card_id INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
            based_on_revision_id INTEGER NOT NULL REFERENCES card_revisions(id),
            content TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)


def initialize(db_path):
    with connect(db_path) as conn:
        ensure(conn)
        before = conn.execute(
            "SELECT last_revision_id FROM memory_maintenance_state WHERE id=1"
        ).fetchone()
        if before is None:
            watermark = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM card_revisions"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO memory_maintenance_state(id,last_revision_id) VALUES(1,?)",
                (int(watermark),),
            )
        else:
            watermark = before[0]
        return int(watermark)


def _streams(conn):
    return [
        dict(row)
        for row in conn.execute("""
            SELECT n.node_id,n.title AS stream_title,p.title AS lake_title
            FROM memory_water_nodes n JOIN memory_water_nodes p
            ON p.node_id=n.parent_node_id
            WHERE n.node_type='stream' AND n.retired_at IS NULL
            ORDER BY n.sort_order,n.node_id
        """)
    ]


def _pending_fields(row, origins, agent_explicit_origin):
    # Do not replace the current manually edited authority of an automatic card
    # with a technical revision. Its Water membership can still be assigned.
    if row["source"] != "manual" and row["revision_origin"] == "manual_ui":
        return []
    pending = []
    for field in memory_field_sources.FIELDS:
        value = row[field]
        explicit = origins.get(field) in {"user_explicit", agent_explicit_origin}
        if explicit:
            continue
        if origins.get(field) == "pending" or (value is None if field=="importance" else not str(value or "").strip()):
            pending.append(field)
    # "未分类" is an existing system placeholder only when its field origin is pending.
    return pending


def _candidates(conn, watermark, limit, agent_source):
    return conn.execute("""
        SELECT c.*,r.origin AS revision_origin
        FROM cards c JOIN card_revisions r ON r.id=c.current_revision_id
        WHERE c.current_revision_id > ?
        AND c.memory_kind='episodic'
        AND c.status!='trashed'
        AND c.source IN ('manual',?,'auto')
        AND NOT EXISTS (
            SELECT 1 FROM memory_card_lineage l
            WHERE l.from_card_id=c.id AND l.retired_at IS NULL
        )
        ORDER BY c.current_revision_id LIMIT ?
    """, (watermark,agent_source,limit)).fetchall()


def run(db_path, *, commit=False, limit=25, provider=None, index_refresher=None,
        agent_source="agent", agent_explicit_origin="agent_explicit", perspective=None):
    if commit:
        with connect(db_path) as conn:
            ensure(conn)
    with connect(db_path) as conn:
        row=conn.execute(
            "SELECT last_revision_id FROM memory_maintenance_state WHERE id=1"
        ).fetchone()
        if row is None:
            raise RuntimeError("run memory_maintenance.py --init before scheduling")
        watermark=int(row[0])
        cards=_candidates(conn,watermark,limit,agent_source)
        streams=_streams(conn)
    stats={"scanned":len(cards),"proposals":0,"revisions":0,"water_assignments":0,
           "organized_contents":0,"sea":0,"model_calls":0,
           "last_revision_id":watermark,"mode":"run" if commit else "dry_run"}
    for card_row in cards:
        card=dict(card_row)
        card_id=int(card["id"])
        revision_id=int(card["current_revision_id"])
        with connect(db_path) as conn:
            origins=memory_field_sources.get(conn,card_id)
            membership=conn.execute(
                "SELECT container_node_id FROM memory_water_membership WHERE card_id=?",
                (card_id,),
            ).fetchone()
            organized=conn.execute(
                "SELECT based_on_revision_id FROM memory_organized_content WHERE card_id=?",
                (card_id,),
            ).fetchone()
            already_checked_sea=conn.execute(
                "SELECT 1 FROM card_evidence WHERE revision_id=? "
                "AND source_ref='gatekeeper_maintain:v1' LIMIT 1",
                (revision_id,),
            ).fetchone() is not None
        pending=_pending_fields(card,origins,agent_explicit_origin)
        need_content = (
            card["source"] in {"manual", agent_source}
            and (organized is None or int(organized["based_on_revision_id"]) != revision_id)
        )
        need_water=membership is None and not (already_checked_sea and not pending)
        card["fill_fields"]=pending
        card["needs_water"]=need_water
        card["needs_organized_content"]=need_content
        if pending or need_water or need_content:
            if perspective:
                card.update(perspective(card["source"]))
            decision=gatekeeper.maintain(card,streams,provider=provider)
            stats["model_calls"]+=1
        else:
            decision={}
        normalized = str(decision.get("organized_content") or "").strip() if need_content else ""
        if need_content and not normalized:
            raise gatekeeper.InvalidGateDecision("missing_organized_content")
        if "importance" in pending and decision.get("importance") is None:
            raise gatekeeper.InvalidGateDecision("missing_maintenance_importance")
        updates={}
        new_origins={}
        for field in pending:
            proposed=decision.get(field)
            if field=="keywords":
                proposed=keyword_policy.normalize_csv(
                    proposed, title=decision.get("title") or card.get("title") or "",
                    tags=decision.get("tags") or card.get("tags") or ""
                )
            if proposed is not None and str(proposed).strip():
                updates[field]=proposed
                new_origins[field]="derived"
        chosen_stream=decision.get("water_stream_id") if need_water else None
        if need_water and chosen_stream is None:
            stats["sea"]+=1
        if updates or chosen_stream or normalized:
            stats["proposals"]+=1
        if commit:
            with connect(db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                current=conn.execute(
                    "SELECT current_revision_id FROM cards WHERE id=?", (card_id,)
                ).fetchone()
                if current is None or int(current[0])!=revision_id:
                    raise revision_store.RevisionConflict(revision_id,current[0] if current else None)
                new_revision_id=revision_id
                if updates:
                    changed=revision_store.update_card_in_transaction(
                        conn,
                        card_id=card_id,
                        expected_current_revision_id=revision_id,
                        change_type="derived_refresh",
                        origin="migration",
                        evidence_source_kind="derived",
                        evidence_source_ref="gatekeeper_maintain:v1",
                        field_sources=new_origins,
                        maintenance_fields=tuple(updates),
                        **updates,
                    )
                    new_revision_id=int(changed["current_revision_id"])
                    stats["revisions"]+=int(changed["changed"])
                if normalized:
                    conn.execute("""
                        INSERT INTO memory_organized_content (
                            card_id,based_on_revision_id,content,policy_version
                        ) VALUES(?,?,?,?)
                        ON CONFLICT(card_id) DO UPDATE SET
                            based_on_revision_id=excluded.based_on_revision_id,
                            content=excluded.content,
                            policy_version=excluded.policy_version,
                            updated_at=datetime('now','localtime')
                    """,(card_id,new_revision_id,normalized,POLICY))
                    stats["organized_contents"]+=1
                if chosen_stream:
                    now=conn.execute("SELECT datetime('now','localtime')").fetchone()[0]
                    conn.execute("""
                        INSERT INTO memory_water_membership(
                            card_id,container_node_id,based_on_revision_id,origin,
                            assignment_reason,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?)
                    """,(card_id,chosen_stream,new_revision_id,"gatekeeper_maintain",
                         decision.get("water_reason") or "matched existing stream",now,now))
                    stats["water_assignments"]+=1
                conn.execute("""
                    INSERT OR IGNORE INTO memory_maintenance_receipts(
                        card_id,source_revision_id,policy_version,changes_json
                    ) VALUES(?,?,?,?)
                """,(card_id,revision_id,POLICY,json.dumps({
                    "fields":sorted(updates),"water":chosen_stream,
                    "organized_content":bool(normalized),
                    "sea":bool(need_water and not chosen_stream),
                    "sea_reason": (
                        decision.get("water_reason") if need_water and not chosen_stream
                        else None
                    ),
                },ensure_ascii=False,sort_keys=True)))
                conn.execute("""
                    UPDATE memory_maintenance_state
                    SET last_revision_id=?,updated_at=datetime('now','localtime')
                    WHERE id=1
                """,(revision_id,))
                conn.commit()
            # Existing Scene Writer's local index refresh is reused, not duplicated.
            if (new_revision_id!=revision_id or normalized) and index_refresher is not None:
                index_refresher(card_id)
        stats["last_revision_id"]=revision_id
    return stats
