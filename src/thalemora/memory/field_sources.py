"""字段来源元数据；正文仍由 cards/revisions 持有。"""
import sqlite3

FIELDS = ("title", "keywords", "importance", "tags")
# 旧宿主枚举只作历史兼容，保留原字段来源，不重写旧记录。
VALID = {"user_explicit", "agent_explicit", "chenlin_explicit", "derived", "pending", "legacy_unknown"}


def ensure(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_field_sources (
            card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            field_name TEXT NOT NULL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            PRIMARY KEY(card_id, field_name)
        )
    """)


def mark(conn, card_id: int, sources: dict):
    if not sources:
        return
    ensure(conn)
    for field, source in sources.items():
        if field not in FIELDS or source not in VALID:
            raise ValueError(f"invalid field provenance: {field}={source}")
        conn.execute("""
            INSERT INTO memory_field_sources(card_id,field_name,source)
            VALUES(?,?,?)
            ON CONFLICT(card_id,field_name)
            DO UPDATE SET source=excluded.source, updated_at=datetime('now','localtime')
        """, (int(card_id), field, source))


def get(conn, card_id: int) -> dict:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_field_sources'"
    ).fetchone()
    if not exists:
        return {}
    rows = conn.execute(
        "SELECT field_name,source FROM memory_field_sources WHERE card_id=?",
        (int(card_id),),
    )
    return {str(row[0]): str(row[1]) for row in rows}
