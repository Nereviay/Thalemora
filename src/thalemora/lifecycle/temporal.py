"""只读显式修订、证据与 lineage；存储时间不证明现实状态。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from contextlib import closing


def _revision_role(change_type: str, origin: str) -> str:
    if origin == "migration":
        return "technical_import"
    if change_type == "derived_refresh":
        return "technical_refresh"
    if change_type in {"legacy_snapshot", "consolidate"}:
        return "technical_record"
    if change_type == "manual_edit":
        return "manual_edit_not_proven_correction"
    return {
        "create": "recorded_creation",
        "extend": "recorded_extension",
        "correct": "recorded_correction",
        "supersede": "recorded_supersession",
    }.get(change_type, "unclassified_record")


def read_card_temporal_relations(card_id: int, memory_db) -> dict | None:
    """只读显式祖先与 lineage；缺卡返回 None。事件日期与存储时间各守其义，均不证明现状。"""
    database = Path(memory_db).expanduser().resolve()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        card = db.execute(
            """SELECT id, title, status, memory_kind, current_revision_id,
                      happened_at, created_at FROM cards WHERE id=?""",
            (int(card_id),),
        ).fetchone()
        if card is None:
            return None

        revisions = []
        issues = []
        seen = set()
        revision_id = card["current_revision_id"]
        while revision_id is not None:
            if revision_id in seen:
                issues.append("cyclic_revision_ancestry")
                break
            seen.add(revision_id)
            rev = db.execute(
                """SELECT id, card_id, revision_no, previous_revision_id,
                          merged_from_revision_id, title, change_type, origin,
                          happened_at, created_at
                   FROM card_revisions WHERE id=?""",
                (revision_id,),
            ).fetchone()
            if rev is None:
                issues.append("missing_revision")
                break
            if rev["card_id"] != card["id"]:
                issues.append("revision_belongs_to_another_card")
                break
            revision = dict(rev)
            revision["record_role"] = _revision_role(rev["change_type"], rev["origin"])
            revision["is_current_revision"] = not revisions
            revision["evidence"] = [
                dict(e) for e in db.execute(
                    """SELECT source_kind, source_ref, raw_event_start_id,
                              raw_event_end_id FROM card_evidence
                       WHERE revision_id=? ORDER BY id""",
                    (revision_id,),
                )
            ]
            revisions.append(revision)
            revision_id = rev["previous_revision_id"]

        # 只返回显式 identity lineage，并核对两端修订归属。
        lineage = []
        for rel in db.execute(
            """SELECT lineage_id, from_card_id, to_card_id, from_revision_id,
                      to_revision_id, relation_type, origin, reason, created_at
               FROM memory_card_lineage
               WHERE retired_at IS NULL AND (from_card_id=? OR to_card_id=?)
               ORDER BY lineage_id""",
            (card["id"], card["id"]),
        ):
            src = db.execute(
                "SELECT card_id FROM card_revisions WHERE id=?",
                (rel["from_revision_id"],),
            ).fetchone()
            dst = db.execute(
                "SELECT card_id FROM card_revisions WHERE id=?",
                (rel["to_revision_id"],),
            ).fetchone()
            if (src is None or dst is None
                    or src["card_id"] != rel["from_card_id"]
                    or dst["card_id"] != rel["to_card_id"]):
                issues.append("invalid_lineage_revision_ownership")
                continue
            item = dict(rel)
            item["direction"] = (
                "outgoing" if rel["from_card_id"] == card["id"] else "incoming"
            )
            lineage.append(item)

        return {
            "card": dict(card),
            "revisions_newest_first": revisions,
            "explicit_active_lineage": lineage,
            "issues": issues,
            "interpretation": (
                "happened_at is a recorded event date; created_at is a storage "
                "timestamp. Revision labels are recorded operations, not "
                "independent proof of changes in real-world state."
            ),
        }


_TEMPORAL_QUESTION_CUES = (
    "什么时候", "哪天", "几月", "哪年", "当时", "那时候", "以前",
    "之前", "上次", "那次", "后来", "曾经", "过去", "纠正",
    "更正", "改过", "变了", "变化", "前后",
)


def temporal_context_hint(query: str, memory_hit: dict, memory_db) -> dict | None:
    """仅补充已选中且版本匹配的命中；不扩展检索，不由存储时间猜测现实变化。"""
    if not any(cue in str(query or "") for cue in _TEMPORAL_QUESTION_CUES):
        return None
    provenance = memory_hit.get("provenance") or {}
    card_id = provenance.get("card_id", memory_hit.get("source_id"))
    revision_id = provenance.get("revision_id")
    if card_id is None or revision_id is None:
        return None
    item = read_card_temporal_relations(int(card_id), memory_db)
    if item is None or item["issues"]:
        return None
    card = item["card"]
    if card["status"] in {"trashed", "archived"}:
        return None
    if int(card["current_revision_id"] or 0) != int(revision_id):
        return None
    if any(
        relation["direction"] == "outgoing"
        for relation in item["explicit_active_lineage"]
    ):
        return None

    notes = []
    event_date = str(card.get("happened_at") or "")[:10]
    if event_date and event_date not in str(memory_hit.get("text") or ""):
        notes.append(f"事件日期：{event_date}（不代表现状）")

    revisions = item["revisions_newest_first"]
    if revisions:
        # 技术迁入或清理不能冒充现实关系变化。
        if (revisions[0]["record_role"] in {"recorded_correction", "recorded_supersession"}
                and len(revisions) > 1):
            notes.append("本卡有修订，按当前版理解")

    if not notes:
        return None
    return {
        "card_id": int(card_id),
        "revision_id": int(revision_id),
        "note": "；".join(notes)[:120],
        "source": "canonical_revision_and_event_date",
    }
