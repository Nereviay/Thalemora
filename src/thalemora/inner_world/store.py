"""按 Agent 私有库保存有限状态及追加版本；领域接口拥有业务语义。"""
from contextlib import closing
import json
import sqlite3
from uuid import uuid4

from .host import HostSession, page_size, timestamp


class InnerStore:
    def __init__(self, session: HostSession):
        self.session = session
        session.check()
        self.path = session.registry.archive_db_path(session.owner, session.agent_id).with_name("inner.sqlite")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inner_versions (
                    seq INTEGER PRIMARY KEY, channel TEXT NOT NULL, kind TEXT NOT NULL,
                    item_id TEXT NOT NULL, version INTEGER NOT NULL,
                    payload TEXT NOT NULL, provenance TEXT NOT NULL,
                    status TEXT NOT NULL, recorded_at TEXT NOT NULL, source_ref TEXT,
                    UNIQUE(channel, kind, item_id, version),
                    UNIQUE(channel, kind, source_ref)
                );
                CREATE TABLE IF NOT EXISTS inner_heads (
                    channel TEXT NOT NULL, kind TEXT NOT NULL, item_id TEXT NOT NULL,
                    seq INTEGER NOT NULL, PRIMARY KEY(channel, kind, item_id)
                );
                CREATE INDEX IF NOT EXISTS inner_page ON inner_heads(channel, kind, seq);
            """)

    def connect(self):
        self.session.check()
        return closing(sqlite3.connect(self.path))

    @staticmethod
    def decode(row):
        if row is None:
            raise KeyError("状态不存在于当前 Agent/通道")
        result = dict(zip(("seq", "channel", "kind", "item_id", "version", "payload",
                           "provenance", "status", "recorded_at", "source_ref"), row))
        for key in ("payload", "provenance"):
            result[key] = json.loads(result[key])
        return result

    def get(self, kind, item_id, *, version=None):
        with self.connect() as db:
            if version is None:
                row = db.execute(
                    "SELECT v.* FROM inner_heads h JOIN inner_versions v ON v.seq=h.seq "
                    "WHERE h.channel=? AND h.kind=? AND h.item_id=?",
                    (self.session.channel, kind, item_id),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM inner_versions WHERE channel=? AND kind=? AND item_id=? AND version=?",
                    (self.session.channel, kind, item_id, version),
                ).fetchone()
        return self.decode(row)

    def list(self, kind, *, after=0, limit=20):
        page_size(limit)
        with self.connect() as db:
            rows = db.execute(
                "SELECT v.* FROM inner_heads h JOIN inner_versions v ON v.seq=h.seq "
                "WHERE h.channel=? AND h.kind=? AND h.seq>? ORDER BY h.seq LIMIT ?",
                (self.session.channel, kind, after, limit),
            ).fetchall()
        return [self.decode(row) for row in rows]

    def history(self, kind, item_id, *, after=0, limit=20):
        page_size(limit)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM inner_versions WHERE channel=? AND kind=? AND item_id=? "
                "AND version>? ORDER BY version LIMIT ?",
                (self.session.channel, kind, item_id, after, limit),
            ).fetchall()
        return [self.decode(row) for row in rows]

    def append(self, kind, payload, provenance, *, recorded_at, item_id=None,
               expected_version=0, status="active", source_ref=None):
        timestamp(recorded_at)
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 32768:
            raise ValueError("单条内在状态最多 32 KiB，请按段保存")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version 必须为非负整数")
        if item_id is None and expected_version:
            raise ValueError("修订必须指定 item_id")
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            if source_ref:
                previous = db.execute(
                    "SELECT * FROM inner_versions WHERE channel=? AND kind=? AND source_ref=?",
                    (self.session.channel, kind, source_ref),
                ).fetchone()
                if previous:
                    result = self.decode(previous)
                    if (item_id is not None and result["item_id"] != item_id
                            or result["version"] != expected_version + 1
                            or result["payload"] != payload):
                        raise ValueError("来源事件不能重用于另一版本或正文")
                    return result
            item_id = item_id or str(uuid4())
            head = db.execute(
                "SELECT v.version FROM inner_heads h JOIN inner_versions v ON v.seq=h.seq "
                "WHERE h.channel=? AND h.kind=? AND h.item_id=?",
                (self.session.channel, kind, item_id),
            ).fetchone()
            if (head[0] if head else 0) != expected_version:
                raise ValueError("版本冲突")
            cur = db.execute(
                "INSERT INTO inner_versions(channel,kind,item_id,version,payload,provenance,status,recorded_at,source_ref) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (self.session.channel, kind, item_id, expected_version + 1, encoded,
                 json.dumps(provenance, ensure_ascii=False), status, recorded_at, source_ref),
            )
            db.execute(
                "INSERT INTO inner_heads VALUES(?,?,?,?) ON CONFLICT(channel,kind,item_id) "
                "DO UPDATE SET seq=excluded.seq",
                (self.session.channel, kind, item_id, cur.lastrowid),
            )
            row = db.execute("SELECT * FROM inner_versions WHERE seq=?", (cur.lastrowid,)).fetchone()
        return self.decode(row)


class StateDomain:
    kind = ""

    def __init__(self, store: InnerStore):
        self.store = store
        self.session = store.session

    def get(self, item_id, *, version=None):
        return self.store.get(self.kind, item_id, version=version)

    def list(self, *, after=0, limit=20):
        return self.store.list(self.kind, after=after, limit=limit)

    def history(self, item_id, *, after=0, limit=20):
        return self.store.history(self.kind, item_id, after=after, limit=limit)

    def revoke(self, item_id, *, expected_version, reason, recorded_at):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("撤销必须有理由")
        current = self.get(item_id)
        provenance = {**current["provenance"], "management": {
            "principal_id": self.session.owner.principal_id, "app_id": self.session.app_id,
            "role": self.session.role, "action": "revoke", "reason": reason,
        }}
        return self.store.append(self.kind, current["payload"], provenance,
                                 recorded_at=recorded_at, item_id=item_id,
                                 expected_version=expected_version, status="revoked")


def event_provenance(session, event, record):
    return {"agent_id": session.agent_id, "author": record.actor,
            "app_id": event.app_id, "channel": event.channel,
            "source_ref": record.durable_ref, "source_type": record.source_type,
            "generation_id": record.generation_id, "root_event_id": record.root_event_id,
            "event_time": record.occurred_at, "epoch_id": event.context.context_epoch_id}
