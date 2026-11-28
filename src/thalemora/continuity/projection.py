"""已准入卡片的连续性资料投影及修订回执。"""
import os
import json
import sqlite3
from thalemora.scene.profile_proposal import profile_proposal_evidence
from thalemora.gatekeeper import ops as ops_store

def get_profile_projection_candidates(db_path=None):
    """Retry accepted Writer proposals in place; card statuses never change."""
    conn = ops_store.connect_writer_ops(db_path)
    try:
        return [row[0] for row in conn.execute(
            "SELECT candidate_id FROM memory_candidates WHERE status='applied' "
            "AND json_type(payload_json, '$.profile_update') IS NOT NULL "
            "AND json_type(payload_json, '$.profile_update') != 'null' "
            "AND COALESCE(json_extract(payload_json, '$.profile_projection.status'), 'pending') "
            "IN ('pending','failed') "
            "AND COALESCE(json_extract(payload_json, '$.profile_projection.attempts'), 0) < 4 "
            "ORDER BY rowid LIMIT 64"
        )]
    finally:
        conn.close()

def record_profile_projection(candidate, projection, db_path=None):
    """CAS only an applied candidate's projection receipt, not its gate state."""
    conn = ops_store.connect_writer_ops(db_path)
    try:
        changed = conn.execute(
            "UPDATE memory_candidates SET "
            "payload_json=json_set(payload_json, '$.profile_projection', json(?)), updated_at=? "
            "WHERE candidate_id=? AND status='applied' AND payload_json=?",
            (json.dumps(projection, ensure_ascii=False, sort_keys=True), ops_store._now(),
             candidate['candidate_id'], candidate['payload_json']),
        ).rowcount
        conn.commit()
        return changed == 1
    finally:
        conn.close()

def project_candidate_profile(candidate_id, *, ops_db, memory_db, fact_store, principal_id, event_reader, batch_reader, evidence_verifier):
    """Project only a durable, accepted card; failures stay on its ops payload."""
    candidate = ops_store.get_candidate(candidate_id, ops_db)
    if candidate is None or candidate["status"] != "applied":
        return
    payload = json.loads(candidate["payload_json"])
    if payload.get("profile_update") is None:
        return
    receipt = payload.get("profile_projection") or {}
    if receipt.get("status") == "applied":
        return
    receipt = {"status": "failed", "attempts": int(receipt.get("attempts", 0)) + 1}
    try:
        from thalemora.memory import revision_store
        memory_path = memory_db
        # Check the canonical mutation too: HTTP 200 and a bare ops flag are
        # insufficient. The Gatekeeper owns both accepted revisions/extensions.
        mutation = revision_store.find_candidate_mutation(memory_path, candidate_id)
        if (not mutation or mutation["card_id"] != candidate["resulting_card_id"]
                or mutation["current_revision_id"] != candidate["resulting_revision_id"]):
            raise ValueError("accepted candidate has no matching canonical card mutation")
        connection = sqlite3.connect("file:" + os.path.abspath(memory_path) + "?mode=ro", uri=True)
        try:
            card = connection.execute("SELECT current_revision_id, status FROM cards WHERE id=?",
                                      (candidate["resulting_card_id"],)).fetchone()
            if not card or card[0] != candidate["resulting_revision_id"] or card[1] not in {"fresh", "sunk"}:
                raise ValueError("accepted card is withdrawn or has a newer revision")
            if mutation["kind"] == "extension":
                extension = connection.execute("SELECT status FROM memory_extensions WHERE id=?",
                                               (mutation["extension_id"],)).fetchone()
                if not extension or extension[0] != "active":
                    raise ValueError("accepted extension is no longer active")
        finally:
            connection.close()
        proposal, projection_candidate = profile_proposal_evidence(payload, event_reader)
        batch = ops_store.get_writer_batch(candidate["batch_key"], ops_db)
        if batch is None:
            raise ValueError("profile proposal has no Writer batch")
        eligible = batch_reader(batch["raw_start_id"], batch["raw_end_id"])
        checked, errors = evidence_verifier(projection_candidate, eligible, batch["raw_start_id"], batch["raw_end_id"])
        if errors or checked != projection_candidate["verified_evidence"]:
            raise ValueError("profile evidence changed since Writer verification")
        store = fact_store()
        current = store._field(principal_id, "user", proposal["field"])
        if current and (current.sensitivity != "normal" or current.visibility != "primer" or current.status != "active"):
            raise ValueError("current profile field is private/sensitive/retired")
        expected = current.version if current else 0
        receipt["expected_version"] = expected
        fact = store.apply_writer_proposal(
            {**proposal, "expected_version": expected}, principal_id=principal_id,
            candidate=projection_candidate, event_reader=event_reader,
        )
        receipt.update(status="applied", fact_id=fact.fact_id, version=fact.version, error=None)
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[writer] profile projection deferred candidate={candidate_id[:12]} {receipt['error']}")
    if not record_profile_projection(candidate, receipt, ops_db):
        raise RuntimeError("profile receipt changed concurrently; retry from durable candidate")
