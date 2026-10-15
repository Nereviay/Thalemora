"""独立显式 ops SQLite；仅候选/批次/merge 决策回执，非 Memory 权威。
DDL 与事务来自 ops_store.py；不建立 Context/Timeline 表，不提供生产默认路径。
"""
import datetime
import json
import sqlite3

BUSY_TIMEOUT_MS = 250
RECEIPT_DDL = '\n            CREATE TABLE IF NOT EXISTS merge_decision_receipts (\n                input_hash TEXT PRIMARY KEY,\n                a_card_id INTEGER NOT NULL,\n                b_card_id INTEGER NOT NULL,\n                judge_model TEXT NOT NULL,\n                policy_version TEXT NOT NULL,\n                decision_json TEXT NOT NULL,\n                created_at TEXT NOT NULL,\n                last_hit_at TEXT,\n                hit_count INTEGER NOT NULL DEFAULT 0\n            )\n            '


def connect_ops(db_path):
    if db_path is None:
        raise ValueError("ops_db_path_required")
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(RECEIPT_DDL)
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise

def connect_writer_ops(db_path=None):
    """Open the ops DB and ensure the P1B writer receipt schema."""
    conn = connect_ops(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS writer_batches (
                batch_key TEXT PRIMARY KEY,
                raw_start_id INTEGER NOT NULL,
                raw_end_id INTEGER NOT NULL,
                input_hash TEXT NOT NULL,
                extractor_model TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                extracted_at TEXT NOT NULL,
                candidate_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_candidates (
                candidate_id TEXT PRIMARY KEY,
                batch_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'applied', 'dropped')),
                resulting_card_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(memory_candidates)")
        }
        additions = {
            "resulting_revision_id": "INTEGER",
            "evidence_attempts": "INTEGER NOT NULL DEFAULT 0",
            "gate_attempts": "INTEGER NOT NULL DEFAULT 0",
            "last_gate_input_hash": "TEXT",
        }
        for name, ddl in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE memory_candidates ADD COLUMN {name} {ddl}"
                )
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise

def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

def get_writer_batch(batch_key, db_path=None):
    conn = connect_writer_ops(db_path)
    try:
        row = conn.execute(
            "SELECT batch_key, raw_start_id, raw_end_id, input_hash, "
            "extractor_model, policy_version, extracted_at, candidate_count "
            "FROM writer_batches WHERE batch_key=?",
            (batch_key,),
        ).fetchone()
        if row is None:
            return None
        columns = (
            "batch_key",
            "raw_start_id",
            "raw_end_id",
            "input_hash",
            "extractor_model",
            "policy_version",
            "extracted_at",
            "candidate_count",
        )
        return dict(zip(columns, row))
    finally:
        conn.close()

def put_writer_batch(batch, candidates, db_path=None):
    """Atomically persist one extraction receipt and all of its candidates."""
    conn = connect_writer_ops(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM writer_batches WHERE batch_key=?", (batch["batch_key"],)
        ).fetchone():
            conn.commit()
            return False
        now = _now()
        conn.execute(
            """
            INSERT INTO writer_batches (
                batch_key, raw_start_id, raw_end_id, input_hash,
                extractor_model, policy_version, extracted_at, candidate_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch["batch_key"],
                batch["raw_start_id"],
                batch["raw_end_id"],
                batch["input_hash"],
                batch["extractor_model"],
                batch["policy_version"],
                now,
                len(candidates),
            ),
        )
        conn.executemany(
            """
            INSERT INTO memory_candidates (
                candidate_id, batch_key, payload_json, status,
                resulting_card_id, attempts, last_error, updated_at
            ) VALUES (?, ?, ?, 'pending', NULL, 0, ?, ?)
            """,
            [
                (
                    candidate["candidate_id"],
                    batch["batch_key"],
                    json.dumps(
                        candidate["payload"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    (
                        "evidence_invalid"
                        if candidate["payload"].get("evidence_state")
                        == "evidence_invalid"
                        else None
                    ),
                    now,
                )
                for candidate in candidates
            ],
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def get_pending_candidates(batch_key=None, db_path=None):
    conn = connect_writer_ops(db_path)
    try:
        sql = (
            "SELECT candidate_id, batch_key, payload_json, status, "
            "resulting_card_id, resulting_revision_id, attempts, "
            "evidence_attempts, gate_attempts, last_gate_input_hash, "
            "last_error, updated_at "
            "FROM memory_candidates WHERE status='pending'"
        )
        params = ()
        if batch_key is not None:
            sql += " AND batch_key=?"
            params = (batch_key,)
        sql += " ORDER BY rowid"
        rows = conn.execute(sql, params).fetchall()
        columns = (
            "candidate_id",
            "batch_key",
            "payload_json",
            "status",
            "resulting_card_id",
            "resulting_revision_id",
            "attempts",
            "evidence_attempts",
            "gate_attempts",
            "last_gate_input_hash",
            "last_error",
            "updated_at",
        )
        return [dict(zip(columns, row)) for row in rows]
    finally:
        conn.close()

def get_candidate(candidate_id, db_path=None):
    conn = connect_writer_ops(db_path)
    try:
        row = conn.execute(
            "SELECT candidate_id, batch_key, payload_json, status, "
            "resulting_card_id, resulting_revision_id, attempts, "
            "evidence_attempts, gate_attempts, last_gate_input_hash, "
            "last_error, updated_at FROM memory_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        columns = (
            "candidate_id", "batch_key", "payload_json", "status",
            "resulting_card_id", "resulting_revision_id", "attempts",
            "evidence_attempts", "gate_attempts", "last_gate_input_hash",
            "last_error", "updated_at",
        )
        return dict(zip(columns, row))
    finally:
        conn.close()

def update_candidate_payload(candidate_id, payload, error=None, db_path=None):
    conn = connect_writer_ops(db_path)
    try:
        conn.execute(
            "UPDATE memory_candidates SET payload_json=?, last_error=?, updated_at=? "
            "WHERE candidate_id=? AND status='pending'",
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")),
                error,
                _now(),
                candidate_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

def record_evidence_attempt(candidate_id, payload, error, db_path=None):
    conn = connect_writer_ops(db_path)
    try:
        conn.execute(
            "UPDATE memory_candidates SET payload_json=?, "
            "evidence_attempts=evidence_attempts+1, last_error=?, updated_at=? "
            "WHERE candidate_id=? AND status='pending'",
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")),
                error,
                _now(),
                candidate_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

def record_gate_result(candidate_id, gate_hash, error, *, increment=False,
                       db_path=None):
    conn = connect_writer_ops(db_path)
    try:
        conn.execute(
            """
            UPDATE memory_candidates
               SET gate_attempts=CASE
                     WHEN COALESCE(last_gate_input_hash, '') != ? THEN ?
                     WHEN ? THEN gate_attempts+1 ELSE gate_attempts END,
                   last_gate_input_hash=?, last_error=?, updated_at=?
             WHERE candidate_id=? AND status='pending'
            """,
            (
                gate_hash,
                1 if increment else 0,
                1 if increment else 0,
                gate_hash,
                error,
                _now(),
                candidate_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

def mark_candidate_applied(candidate_id, card_id, revision_id=None, db_path=None):
    _finish_candidate(
        candidate_id, "applied", card_id, revision_id, None, db_path
    )

def mark_candidate_dropped(candidate_id, db_path=None, *, reason=None):
    _finish_candidate(candidate_id, "dropped", None, None, reason, db_path)

def supersede_legacy_candidate(candidate_id, db_path=None):
    _finish_candidate(
        candidate_id, "dropped", None, None,
        "superseded_by_p3_reextract", db_path,
    )

def mark_candidate_failed(candidate_id, error, db_path=None):
    conn = connect_writer_ops(db_path)
    try:
        conn.execute(
            """
            UPDATE memory_candidates
               SET attempts=attempts+1, last_error=?, updated_at=?
             WHERE candidate_id=? AND status='pending'
            """,
            (str(error), _now(), candidate_id),
        )
        conn.commit()
    finally:
        conn.close()

def _finish_candidate(candidate_id, status, card_id, revision_id, error,
                      db_path=None):
    conn = connect_writer_ops(db_path)
    try:
        conn.execute(
            """
            UPDATE memory_candidates
               SET status=?, resulting_card_id=?, resulting_revision_id=?,
                   attempts=attempts+1,
                   last_error=?, updated_at=?
             WHERE candidate_id=? AND status='pending'
            """,
            (status, card_id, revision_id, error, _now(), candidate_id),
        )
        conn.commit()
    finally:
        conn.close()

def get_merge_decision_receipt(input_hash, db_path=None):
    """Return a structured decision and account for a valid cache hit."""
    conn = connect_ops(db_path)
    try:
        row = conn.execute(
            "SELECT decision_json FROM merge_decision_receipts WHERE input_hash=?",
            (input_hash,),
        ).fetchone()
        if row is None:
            return None
        decision = json.loads(row[0])
        if not isinstance(decision, dict):
            return None
        conn.execute(
            """
             UPDATE merge_decision_receipts
               SET hit_count=hit_count+1, last_hit_at=?
             WHERE input_hash=?
            """,
            (_now(), input_hash),
        )
        conn.commit()
        return decision
    finally:
        conn.close()

def put_merge_decision_receipt(
    input_hash,
    a_card_id,
    b_card_id,
    judge_model,
    policy_version,
    decision,
    db_path=None,
):
    """Persist a provider decision without changing existing hit accounting."""
    if not isinstance(decision, dict):
        raise TypeError("merge decision receipt must be a dict")
    payload = json.dumps(
        decision,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    conn = connect_ops(db_path)
    try:
        conn.execute(
            """
            INSERT INTO merge_decision_receipts (
                input_hash, a_card_id, b_card_id, judge_model, policy_version,
                decision_json, created_at, last_hit_at, hit_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0)
            ON CONFLICT(input_hash) DO UPDATE SET
                a_card_id=excluded.a_card_id,
                b_card_id=excluded.b_card_id,
                judge_model=excluded.judge_model,
                policy_version=excluded.policy_version,
                decision_json=excluded.decision_json
            """,
            (
                input_hash,
                a_card_id,
                b_card_id,
                judge_model,
                policy_version,
                payload,
                _now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
