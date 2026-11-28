"""沿用现役时间线回执表及旧状态迁移。"""
from thalemora.gatekeeper.ops import connect_ops, _now

_TIMELINE_PERIODS_DDL = """
    CREATE TABLE timeline_periods (
        granularity TEXT NOT NULL CHECK(granularity IN ('week','month')),
        period_key TEXT NOT NULL,
        source_set_hash TEXT,
        status TEXT NOT NULL CHECK(status IN ('pending','ready','sparse','blocked','failed')),
        card_id INTEGER,
        revision_id INTEGER,
        last_error TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (granularity, period_key)
    )
"""


_TIMELINE_PERIODS_LEGACY = "timeline_periods_p5b_status_v1"


def _copy_legacy_timeline_periods(conn):
    conn.execute(
        f"""
        INSERT INTO timeline_periods (
            granularity, period_key, source_set_hash, status,
            card_id, revision_id, last_error, updated_at
        )
        SELECT granularity, period_key, source_set_hash,
            CASE
                WHEN status='ready' AND card_id IS NULL AND revision_id IS NULL
                THEN 'sparse'
                ELSE status
            END,
            card_id, revision_id, last_error, updated_at
        FROM {_TIMELINE_PERIODS_LEGACY}
        WHERE true
        ON CONFLICT(granularity, period_key) DO UPDATE SET
            source_set_hash=CASE
                WHEN excluded.card_id IS NOT NULL
                     AND excluded.revision_id IS NOT NULL
                     AND (timeline_periods.card_id IS NULL
                          OR timeline_periods.revision_id IS NULL)
                THEN COALESCE(excluded.source_set_hash, timeline_periods.source_set_hash)
                ELSE COALESCE(timeline_periods.source_set_hash, excluded.source_set_hash)
            END,
            status=CASE
                WHEN timeline_periods.card_id IS NOT NULL
                     AND timeline_periods.revision_id IS NOT NULL
                THEN timeline_periods.status
                WHEN excluded.card_id IS NOT NULL
                     AND excluded.revision_id IS NOT NULL
                THEN excluded.status
                WHEN timeline_periods.updated_at >= excluded.updated_at
                THEN timeline_periods.status
                ELSE excluded.status
            END,
            card_id=CASE
                WHEN timeline_periods.card_id IS NOT NULL
                     AND timeline_periods.revision_id IS NOT NULL
                THEN timeline_periods.card_id
                WHEN excluded.card_id IS NOT NULL AND excluded.revision_id IS NOT NULL
                THEN excluded.card_id
                WHEN timeline_periods.updated_at >= excluded.updated_at
                THEN timeline_periods.card_id
                ELSE excluded.card_id
            END,
            revision_id=CASE
                WHEN timeline_periods.card_id IS NOT NULL
                     AND timeline_periods.revision_id IS NOT NULL
                THEN timeline_periods.revision_id
                WHEN excluded.card_id IS NOT NULL AND excluded.revision_id IS NOT NULL
                THEN excluded.revision_id
                WHEN timeline_periods.updated_at >= excluded.updated_at
                THEN timeline_periods.revision_id
                ELSE excluded.revision_id
            END,
            last_error=CASE
                WHEN timeline_periods.card_id IS NOT NULL
                     AND timeline_periods.revision_id IS NOT NULL
                THEN timeline_periods.last_error
                WHEN excluded.card_id IS NOT NULL
                     AND excluded.revision_id IS NOT NULL
                THEN excluded.last_error
                WHEN timeline_periods.updated_at >= excluded.updated_at
                THEN COALESCE(timeline_periods.last_error, excluded.last_error)
                ELSE COALESCE(excluded.last_error, timeline_periods.last_error)
            END,
            updated_at=CASE
                WHEN timeline_periods.card_id IS NOT NULL
                     AND timeline_periods.revision_id IS NOT NULL
                THEN timeline_periods.updated_at
                WHEN excluded.card_id IS NOT NULL
                     AND excluded.revision_id IS NOT NULL
                THEN excluded.updated_at
                WHEN timeline_periods.updated_at >= excluded.updated_at
                THEN timeline_periods.updated_at
                ELSE excluded.updated_at
            END
        """
    )


def _ensure_timeline_periods(conn):
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='timeline_periods'"
        ).fetchone()
        legacy_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (_TIMELINE_PERIODS_LEGACY,),
        ).fetchone() is not None
        if row is not None and "'sparse'" not in (row[0] or ""):
            if legacy_exists:
                raise RuntimeError("both legacy timeline period tables use old schema")
            conn.execute(
                f"ALTER TABLE timeline_periods RENAME TO {_TIMELINE_PERIODS_LEGACY}"
            )
            legacy_exists = True
            row = None
        if row is None:
            conn.execute(_TIMELINE_PERIODS_DDL)
        if legacy_exists:
            _copy_legacy_timeline_periods(conn)
            conn.execute(f"DROP TABLE {_TIMELINE_PERIODS_LEGACY}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def connect_timeline_ops(db_path=None):
    conn = connect_ops(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timeline_day_state (
                date TEXT PRIMARY KEY,
                source_hash TEXT NOT NULL,
                source_count INTEGER NOT NULL,
                source_max_raw_event_id INTEGER,
                status TEXT NOT NULL CHECK(status IN ('ready','empty','failed')),
                card_id INTEGER,
                revision_id INTEGER,
                last_error TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        _ensure_timeline_periods(conn)
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


def get_timeline_day_state(date, db_path=None):
    conn = connect_timeline_ops(db_path)
    try:
        row = conn.execute(
            "SELECT date, source_hash, source_count, source_max_raw_event_id, "
            "status, card_id, revision_id, last_error, updated_at "
            "FROM timeline_day_state WHERE date=?",
            (date,),
        ).fetchone()
        if row is None:
            return None
        columns = (
            "date", "source_hash", "source_count", "source_max_raw_event_id",
            "status", "card_id", "revision_id", "last_error", "updated_at",
        )
        return dict(zip(columns, row))
    finally:
        conn.close()


def upsert_timeline_day_state(state, db_path=None):
    conn = connect_timeline_ops(db_path)
    try:
        conn.execute(
            """
            INSERT INTO timeline_day_state (
                date, source_hash, source_count, source_max_raw_event_id,
                status, card_id, revision_id, last_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                source_hash=excluded.source_hash,
                source_count=excluded.source_count,
                source_max_raw_event_id=excluded.source_max_raw_event_id,
                status=excluded.status,
                card_id=excluded.card_id,
                revision_id=excluded.revision_id,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            (
                state["date"], state["source_hash"], state["source_count"],
                state.get("source_max_raw_event_id"), state["status"],
                state.get("card_id"), state.get("revision_id"),
                state.get("last_error"), state.get("updated_at") or _now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_timeline_period(granularity, period_key, db_path=None):
    conn = connect_timeline_ops(db_path)
    try:
        row = conn.execute(
            "SELECT granularity, period_key, source_set_hash, status, card_id, "
            "revision_id, last_error, updated_at FROM timeline_periods "
            "WHERE granularity=? AND period_key=?",
            (granularity, period_key),
        ).fetchone()
        if row is None:
            return None
        columns = (
            "granularity", "period_key", "source_set_hash", "status", "card_id",
            "revision_id", "last_error", "updated_at",
        )
        return dict(zip(columns, row))
    finally:
        conn.close()


def upsert_timeline_period(state, db_path=None):
    conn = connect_timeline_ops(db_path)
    try:
        conn.execute(
            """
            INSERT INTO timeline_periods (
                granularity, period_key, source_set_hash, status,
                card_id, revision_id, last_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(granularity, period_key) DO UPDATE SET
                source_set_hash=excluded.source_set_hash,
                status=excluded.status,
                card_id=excluded.card_id,
                revision_id=excluded.revision_id,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            (
                state["granularity"], state["period_key"],
                state.get("source_set_hash"), state["status"],
                state.get("card_id"), state.get("revision_id"),
                state.get("last_error"), state.get("updated_at") or _now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def mark_timeline_period_pending(granularity, period_key, db_path=None):
    previous = get_timeline_period(granularity, period_key, db_path=db_path) or {}
    state = {
        "granularity": granularity,
        "period_key": period_key,
        "source_set_hash": previous.get("source_set_hash"),
        "status": "pending",
        "card_id": previous.get("card_id"),
        "revision_id": previous.get("revision_id"),
        "last_error": None,
        "updated_at": _now(),
    }
    upsert_timeline_period(state, db_path=db_path)
    return state


def list_retry_timeline_periods(limit, exclude=(), db_path=None):
    excluded = set(exclude)
    conn = connect_timeline_ops(db_path)
    try:
        rows = conn.execute(
            "SELECT granularity, period_key, source_set_hash, status, card_id, "
            "revision_id, last_error, updated_at FROM timeline_periods "
            "WHERE status IN ('pending','blocked','failed') "
            "ORDER BY updated_at, granularity, period_key"
        ).fetchall()
        columns = (
            "granularity", "period_key", "source_set_hash", "status", "card_id",
            "revision_id", "last_error", "updated_at",
        )
        return [
            dict(zip(columns, row)) for row in rows
            if (row[0], row[1]) not in excluded
        ][:limit]
    finally:
        conn.close()
