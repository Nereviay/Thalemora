"""旧宿主 Diary 表适配：保留 action_key 和原字段，不伪造 Agent 事件。"""

import sqlite3

_DIARY_PUBLIC_COLUMNS = (
    "id, date, title, content, weather, mood, mood_score, created_at"
)

def _ensure_diary_schema(conn):
    """Keep the live diary table additive and safe for existing rows."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diary_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            title TEXT DEFAULT '',
            content TEXT NOT NULL,
            weather TEXT DEFAULT '',
            mood TEXT DEFAULT '',
            mood_score INTEGER DEFAULT NULL,
            action_key TEXT DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
        """
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(diary_entries)").fetchall()
    }
    if "mood_score" not in columns:
        conn.execute(
            "ALTER TABLE diary_entries ADD COLUMN mood_score INTEGER DEFAULT NULL"
        )
    if "action_key" not in columns:
        conn.execute("ALTER TABLE diary_entries ADD COLUMN action_key TEXT DEFAULT NULL")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_diary_entries_action_key "
        "ON diary_entries(action_key) "
        "WHERE action_key IS NOT NULL AND action_key <> ''"
    )

def _create_diary_entry(conn, body):
    """Insert one diary row or return the row already owning action_key."""
    date = str(body.get("date") or "").strip()
    title = str(body.get("title") or "").strip()
    content = str(body.get("content") or "").strip()
    weather = str(body.get("weather") or "").strip()
    mood = str(body.get("mood") or "").strip()
    action_key = str(body.get("action_key") or "").strip() or None
    raw_score = body.get("mood_score")
    if raw_score in (None, ""):
        mood_score = None
    else:
        try:
            mood_score = int(raw_score)
        except (TypeError, ValueError):
            mood_score = None
        if mood_score is not None:
            mood_score = max(0, min(10, mood_score))
    if not date or not content:
        raise ValueError("need date + content")

    if action_key:
        existing = conn.execute(
            f"SELECT {_DIARY_PUBLIC_COLUMNS} FROM diary_entries WHERE action_key=?",
            (action_key,),
        ).fetchone()
        if existing is not None:
            return dict(existing)

    try:
        cur = conn.execute(
            "INSERT INTO diary_entries "
            "(date, title, content, weather, mood, mood_score, action_key) "
            "VALUES (?,?,?,?,?,?,?)",
            (date, title, content, weather, mood, mood_score, action_key),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = conn.execute(
            f"SELECT {_DIARY_PUBLIC_COLUMNS} FROM diary_entries WHERE action_key=?",
            (action_key,),
        ).fetchone()
        if existing is None:
            raise
        return dict(existing)

    row = conn.execute(
        f"SELECT {_DIARY_PUBLIC_COLUMNS} FROM diary_entries WHERE id=?",
        (cur.lastrowid,),
    ).fetchone()
    return dict(row)


def create_whisper(conn, content, pinned, embedding):
    cur = conn.execute(
        "INSERT INTO whispers(content,pinned,consumed,embedding) VALUES(?,?,0,?)",
        (content, pinned, embedding))
    conn.commit()
    return dict(conn.execute(
        "SELECT id,content,pinned,consumed,created_at FROM whispers WHERE id=?",
        (cur.lastrowid,)).fetchone())


def consume_whispers(conn, ids):
    marks = ",".join("?" for _ in ids)
    cur = conn.execute(f"UPDATE whispers SET consumed=1 WHERE id IN ({marks})", ids)
    conn.commit()
    return cur.rowcount


def create_dream(conn, date, content, clarity):
    cur = conn.execute("INSERT INTO dreams(date,content,clarity) VALUES(?,?,?)",
                       (date, content, clarity))
    conn.commit()
    return dict(conn.execute("SELECT * FROM dreams WHERE id=?", (cur.lastrowid,)).fetchone())


def create_contemplation(conn, content, source, embedding):
    cur = conn.execute("INSERT INTO contemplations(content,source,embedding) VALUES(?,?,?)",
                       (content, source, embedding))
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM contemplations").fetchone()[0]
    return {"ok": True, "id": cur.lastrowid, "count": int(count)}
