"""调用方供给的派生激活账本；不拥有 Memory 或 Runtime 事件权威。"""
from __future__ import annotations

import datetime as dt
from contextlib import closing
from pathlib import Path
import sqlite3
import uuid
from typing import Callable

SCHEMA_VERSION = 1

ACTIVATION_WEIGHTS = {
    'retrieved': 0.0,
    'injected': 0.0,
    'used': 0.8,
    'user_reactivated': 1.0,
    'updated': 1.2,
    'lifecycle_sunk': 0.0,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_activation_schema (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    schema_version INTEGER NOT NULL,
    authority_note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_activation_events (
    event_id TEXT PRIMARY KEY,
    card_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    weight REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(card_id,event_type,source_key)
);
CREATE INDEX IF NOT EXISTS idx_memory_activation_card_time
ON memory_activation_events(card_id,created_at DESC);
"""


def parse_time(value) -> dt.datetime | None:
    """无时区合成/适配时间按 UTC 解读；不依赖服务器本地时区。"""
    try:
        stamp = dt.datetime.fromisoformat(str(value or '').replace('Z', '+00:00'))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc)


def timestamp(now: dt.datetime) -> str:
    return parse_time(now.isoformat()).isoformat(timespec='seconds')


def connect_existing(path, *, readonly=False):
    connection = sqlite3.connect(
        Path(path).resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    return connection


class MemoryActivationError(RuntimeError):
    pass


class ActivationLedger:
    """来源 key 由真实调用方核验；本层不冒充 generation/Agent 核验器。

    user_source_reader(card_id, source_key) 仅接受调用方已核实的显式用户事件。
    读写失败直接抛错；初始化只创建明确传入的派生库。
    """

    def __init__(self, db_path, *, user_source_reader: Callable[[int, str], bool]):
        self.db_path = str(Path(db_path).resolve())
        self.user_source_reader = user_source_reader

    def initialize(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.executescript(SCHEMA)
            row = conn.execute(
                'SELECT schema_version FROM memory_activation_schema WHERE singleton=1'
            ).fetchone()
            if row is None:
                conn.execute(
                    'INSERT INTO memory_activation_schema VALUES(1,?,?)',
                    (SCHEMA_VERSION, 'derived activation only; canonical Memory authority unchanged'),
                )
            elif row[0] != SCHEMA_VERSION:
                raise MemoryActivationError('memory activation schema mismatch')

    def record(self, card_id, event_type, source_key, *, now, connection=None):
        """相同 card/type/source 只保留首次时间；可加入调用方的 act 事务。"""
        cid = int(card_id)
        if cid <= 0:
            raise ValueError('valid card_id required')
        kind, key = str(event_type).strip(), str(source_key or '').strip()
        if kind not in ACTIVATION_WEIGHTS:
            raise ValueError('invalid activation event_type')
        if not key or len(key) > 240:
            raise ValueError('source_key required, maximum 240 characters')
        weight = ACTIVATION_WEIGHTS[kind]
        if weight == 0 and kind != 'lifecycle_sunk':
            return dict(recorded=False, card_id=cid, event_type=kind,
                        weight=0.0, reason='zero_heat_event')
        if kind == 'user_reactivated' and not self.user_source_reader(cid, key):
            raise ValueError('explicit user activation source required')
        if connection is None:
            with closing(connect_existing(self.db_path)) as conn, conn:
                return self._insert(conn, 'main', cid, kind, key, weight, now)
        return self._insert(connection, 'act', cid, kind, key, weight, now)

    @staticmethod
    def _insert(conn, schema, cid, kind, key, weight, now):
        event_id = 'mem-activation-' + uuid.uuid4().hex
        stamp = timestamp(now)
        cur = conn.execute(
            f'INSERT INTO {schema}.memory_activation_events '
            '(event_id,card_id,event_type,source_key,weight,created_at) VALUES(?,?,?,?,?,?) '
            'ON CONFLICT(card_id,event_type,source_key) DO NOTHING',
            (event_id, cid, kind, key, weight, stamp),
        )
        row = conn.execute(
            f'SELECT event_id,weight,created_at FROM {schema}.memory_activation_events '
            'WHERE card_id=? AND event_type=? AND source_key=?', (cid, kind, key),
        ).fetchone()
        return dict(recorded=cur.rowcount == 1, card_id=cid, event_type=kind,
                    **dict(row), reason=None if cur.rowcount == 1 else 'duplicate')

    def rows(self, connection=None):
        if connection is None:
            with closing(connect_existing(self.db_path, readonly=True)) as conn:
                return conn.execute('SELECT * FROM memory_activation_events ORDER BY rowid').fetchall()
        return connection.execute('SELECT * FROM act.memory_activation_events ORDER BY rowid').fetchall()

    def summary(self, connection=None):
        result = {}
        for row in self.rows(connection):
            if row['event_type'] not in {'used', 'updated', 'user_reactivated'}:
                continue
            cid = int(row['card_id'])
            if row['event_type'] == 'user_reactivated' and not self.user_source_reader(cid, row['source_key']):
                continue
            when = parse_time(row['created_at'])
            if when is None:
                raise MemoryActivationError('invalid activation timestamp')
            item = result.setdefault(cid, dict(weight_sum=0.0, last_at='', event_count=0))
            item['weight_sum'] += float(row['weight'])
            item['event_count'] += 1
            if not item['last_at'] or when > parse_time(item['last_at']):
                item['last_at'] = row['created_at']
        return result

    def epochs(self, connection=None):
        users, sinks = {}, {}
        for row in self.rows(connection):
            kind, key, cid = row['event_type'], row['source_key'], int(row['card_id'])
            if kind not in {'user_reactivated', 'lifecycle_sunk'}:
                continue
            when = parse_time(row['created_at'])
            if when is None:
                raise MemoryActivationError('invalid activation timestamp')
            if kind == 'user_reactivated':
                if row['weight'] != 1.0 or not self.user_source_reader(cid, key):
                    continue
                target, value = users, (when, row['created_at'])
            else:
                if row['weight'] != 0.0 or not key.startswith('lifecycle:rev:'):
                    continue
                parts = key.split(':', 3)
                try:
                    revision = int(parts[2])
                except (ValueError, IndexError):
                    continue
                if revision <= 0 or len(parts) != 4 or not parts[3]:
                    continue
                target, value = sinks, (when, row['created_at'], revision)
            if cid not in target or when >= target[cid][0]:
                target[cid] = value
        return users, sinks
