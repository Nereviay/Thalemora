"""核对卡片当前修订及派生索引的内容和状态。"""
import hashlib
import json
import os
import sqlite3

def retrieval_sync_drift(memory_db, index_db):
    """Compare canonical episodic Memory with the derived retrieval lane.

    A row is stale when its canonical revision, text hash, status or importance
    differs from the indexed copy. Extra index rows are also returned so the
    same refresh helper can delete them through refresh_memory_card().
    """
    with sqlite3.connect(
        f"file:{os.path.abspath(memory_db)}?mode=ro", uri=True
    ) as memory:
        memory.row_factory = sqlite3.Row
        has_lineage = memory.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='memory_card_lineage'"
        ).fetchone()
        lineage_filter = (
            " AND NOT EXISTS (SELECT 1 FROM memory_card_lineage l "
            "WHERE l.from_card_id=cards.id AND l.retired_at IS NULL)"
            if has_lineage else ""
        )
        canonical_rows = memory.execute(
            "SELECT id,title,content,status,importance,current_revision_id "
            "FROM cards WHERE status!='trashed' AND memory_kind='episodic'"
            + lineage_filter
        ).fetchall()
        # Match RetrievalShadow._memory_rows: a current organized projection
        # replaces the canonical body for the derived retrieval chunk.
        has_organized = memory.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='memory_organized_content'"
        ).fetchone()
        organized = (
            {
                int(row["card_id"]): row
                for row in memory.execute(
                    "SELECT card_id,based_on_revision_id,content "
                    "FROM memory_organized_content"
                ).fetchall()
            }
            if has_organized else {}
        )

    with sqlite3.connect(
        f"file:{os.path.abspath(index_db)}?mode=ro", uri=True
    ) as index:
        index.row_factory = sqlite3.Row
        indexed_rows = index.execute(
            "SELECT source_id,status,importance,content_hash,provenance_json "
            "FROM chunks WHERE source_kind='memory_card'"
        ).fetchall()

    canonical = {}
    for row in canonical_rows:
        projection = organized.get(int(row["id"]))
        body = (
            projection["content"]
            if projection is not None
            and int(projection["based_on_revision_id"]) == int(row["current_revision_id"] or 0)
            else row["content"]
        )
        text = (str(row["title"] or "").strip() + "\n" + str(body or "").strip()).strip()
        canonical[int(row["id"])] = {
            "revision_id": int(row["current_revision_id"] or 0),
            "status": str(row["status"] or ""),
            "importance": (
                None if row["importance"] is None else float(row["importance"])
            ),
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    indexed = {}
    for row in indexed_rows:
        try:
            provenance = json.loads(row["provenance_json"] or "{}")
        except Exception:
            provenance = {}
        indexed[int(row["source_id"])] = {
            "revision_id": int(provenance.get("revision_id") or 0),
            "status": str(row["status"] or ""),
            "importance": (
                None if row["importance"] is None else float(row["importance"])
            ),
            "content_hash": str(row["content_hash"] or ""),
        }

    missing = sorted(set(canonical) - set(indexed))
    extra = sorted(set(indexed) - set(canonical))
    stale = []
    for card_id in sorted(set(canonical) & set(indexed)):
        expected = canonical[card_id]
        actual = indexed[card_id]
        if (
            expected["revision_id"] != actual["revision_id"]
            or expected["status"] != actual["status"]
            or expected["importance"] != actual["importance"]
            or expected["content_hash"] != actual["content_hash"]
        ):
            stale.append(card_id)

    repair = sorted(set(missing) | set(stale) | set(extra))
    return {
        "canonical_count": len(canonical),
        "indexed_count": len(indexed),
        "missing_card_ids": missing,
        "stale_card_ids": stale,
        "extra_card_ids": extra,
        "repair_card_ids": repair,
    }
