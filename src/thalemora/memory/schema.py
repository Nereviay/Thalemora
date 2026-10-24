"""仅新隔离库的本批 schema；不提供生产迁移或历史回填。

cards 来自静态 schema.sql；修订/证据/extension 来自 migrate_v2.py
的 _ensure_v2_schema；字段来源由 field_sources.ensure 建立。
"""
from . import field_sources

DDL = """
CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT DEFAULT '',
    content     TEXT NOT NULL,
    happened_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    importance  REAL DEFAULT 5.0,
    tags        TEXT DEFAULT '',
    keywords    TEXT DEFAULT '',
    valence     REAL DEFAULT NULL,
    arousal     REAL DEFAULT NULL,
    embedding   BLOB,
    embedding_content_hash TEXT,
    embedding_model TEXT,
    embedded_at TEXT,
    hits        INTEGER DEFAULT 0,
    last_hit_at TEXT,
    status      TEXT DEFAULT 'fresh',
    pinned      INTEGER DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'auto',
    current_revision_id INTEGER REFERENCES card_revisions(id) ON DELETE SET NULL,
    memory_kind TEXT NOT NULL DEFAULT 'episodic'
                CHECK(memory_kind IN ('episodic', 'timeline'))
);

CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);

CREATE TABLE IF NOT EXISTS archive_card_scopes (
    card_id INTEGER PRIMARY KEY REFERENCES cards(id),
    app TEXT NOT NULL,
    channel TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_archive_card_scopes_source ON archive_card_scopes(app,channel,card_id);

CREATE TABLE IF NOT EXISTS card_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            revision_no INTEGER NOT NULL CHECK(revision_no >= 1),
            previous_revision_id INTEGER REFERENCES card_revisions(id),
            merged_from_revision_id INTEGER REFERENCES card_revisions(id),
            content TEXT NOT NULL,
            title TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            keywords TEXT DEFAULT '',
            importance REAL DEFAULT 5.0,
            valence REAL DEFAULT NULL,
            arousal REAL DEFAULT NULL,
            happened_at TEXT,
            content_hash TEXT NOT NULL,
            change_type TEXT NOT NULL CHECK(change_type IN (
                'legacy_snapshot', 'create', 'extend', 'supersede', 'correct',
                'manual_edit', 'consolidate', 'derived_refresh')),
            origin TEXT NOT NULL CHECK(origin IN (
                'migration', 'auto_writer', 'manual_ui', 'agent_mcp',
                'bottle_answer', 'daily_summary', 'merge_sweep')),
            candidate_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(card_id, revision_no)
        );

CREATE TABLE IF NOT EXISTS card_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id INTEGER NOT NULL
                REFERENCES card_revisions(id) ON DELETE CASCADE,
            source_kind TEXT NOT NULL CHECK(source_kind IN (
                'user_text_exact', 'assistant_text_exact', 'raw_range',
                'manual', 'agent_mcp', 'bottle_answer', 'derived',
                'legacy_unverified')),
            source_ref TEXT,
            raw_event_start_id INTEGER,
            raw_event_end_id INTEGER,
            event_hash TEXT,
            char_start INTEGER,
            char_end INTEGER,
            quote_hash TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

CREATE UNIQUE INDEX IF NOT EXISTS idx_card_revisions_candidate ON card_revisions(candidate_id) WHERE candidate_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_card_revisions_card ON card_revisions(card_id, revision_no);

CREATE INDEX IF NOT EXISTS idx_card_evidence_revision ON card_evidence(revision_id);

CREATE TABLE IF NOT EXISTS memory_extensions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            base_revision_id INTEGER NOT NULL
                REFERENCES card_revisions(id),
            content TEXT NOT NULL,
            keywords TEXT DEFAULT '',
            importance REAL DEFAULT 5.0,
            happened_at TEXT,
            origin TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            candidate_id TEXT,
            same_memory_confidence REAL NOT NULL
                CHECK(same_memory_confidence >= 0.0
                      AND same_memory_confidence <= 1.0),
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

CREATE TABLE IF NOT EXISTS memory_extension_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            extension_id INTEGER NOT NULL
                REFERENCES memory_extensions(id) ON DELETE CASCADE,
            source_kind TEXT NOT NULL CHECK(source_kind IN (
                'user_text_exact', 'assistant_text_exact', 'raw_range',
                'manual', 'agent_mcp', 'bottle_answer', 'derived',
                'legacy_unverified')),
            source_ref TEXT,
            raw_event_start_id INTEGER,
            raw_event_end_id INTEGER,
            event_hash TEXT,
            char_start INTEGER,
            char_end INTEGER,
            quote_hash TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

CREATE INDEX IF NOT EXISTS idx_memory_extensions_card_status ON memory_extensions(card_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_extensions_candidate ON memory_extensions(candidate_id) WHERE candidate_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memory_extension_evidence_extension ON memory_extension_evidence(extension_id);
"""


def initialize(connection):
    """在调用方事务内创建目标表；不提交、不回滚。

    调用方负责连接生命周期、sqlite3.Row 和 PRAGMA foreign_keys=ON。
    """
    for statement in DDL.split(";"):
        if statement.strip():
            connection.execute(statement)
    field_sources.ensure(connection)
