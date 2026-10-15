"""P2-B 当前状态档案，不负责来源同步或生产数据接入。

来源版本必须为正整数；允许跳号，旧版本不覆盖新版本。同版本比较完整
envelope（删除正文统一为 None）。只保留当前正文、前一版本号和写入时间，
不保存旧编辑正文。hash 和验证状态来自来源合同，本模块不猜测 hash 算法，
也不自行将未知状态升级为 verified。
"""

import sqlite3
from contextlib import closing


class VersionConflict(ValueError):
    """同一来源事件的同版本具有不同内容或状态。"""


FIELDS = (
    "app", "channel", "source_event_id", "version", "role", "text",
    "occurred_at", "conversation_id", "epoch_id", "content_hash", "deleted",
    "hash_verification", "raw_event_id",
)


class Archive:
    """owner 必须是宿主认证主体；每次来源读写均重新检查授权。

    export_history 返回单一应用/域的一页 envelope，可直接 JSON 编码。
    import_history 接受这样的列表，逐事件提交并返回回执；遇错直接抛出，
    已提交项保留，可幂等重试。不存在隐式跨应用或跨私有域的批量导出。
    """

    def __init__(self, registry, owner, agent_id):
        self.registry = registry
        self.owner = owner
        self.agent_id = agent_id
        self.path = registry.archive_db_path(owner, agent_id)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS history_events (
                    app TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version > 0),
                    role TEXT,
                    text TEXT,
                    occurred_at TEXT,
                    conversation_id TEXT,
                    epoch_id TEXT,
                    content_hash TEXT,
                    deleted INTEGER NOT NULL CHECK(deleted IN (0, 1)),
                    hash_verification TEXT NOT NULL,
                    raw_event_id,
                    previous_version INTEGER,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (app, channel, source_event_id),
                    CHECK(deleted = 0 OR text IS NULL)
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS history_visible_page
                ON history_events(app, channel, deleted, source_event_id)
            """)

    def _authorize(self, app_id, channel):
        self.registry.assert_source(self.owner, self.agent_id, app_id, channel)

    def ensure_short_index(self):
        """首次应用接线时构建持久短 gram；建表和历史回填原子提交。"""
        with closing(sqlite3.connect(self.path)) as db, db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='history_short_grams'").fetchone():
                return
            db.execute("BEGIN IMMEDIATE")
            # 等待写锁期间可能已由另一接线完成构建。
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='history_short_grams'").fetchone():
                return
            db.execute("""
                CREATE TABLE history_short_grams (
                    app TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    gram TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    PRIMARY KEY(app, channel, gram, source_event_id)
                ) WITHOUT ROWID
            """)
            db.execute("CREATE INDEX history_short_event ON history_short_grams(app, channel, source_event_id)")
            db.row_factory = sqlite3.Row
            for event in db.execute("SELECT app,channel,source_event_id,text FROM history_events WHERE deleted=0"):
                self._update_short_index(event, db)

    @staticmethod
    def _update_short_index(event, db):
        """每个事件去重生成 1/2 字符 gram；与档案写入共用事务。"""
        key = (event["app"], event["channel"], event["source_event_id"])
        db.execute("DELETE FROM history_short_grams WHERE app=? AND channel=? AND source_event_id=?", key)
        text = event["text"] or ""
        grams = set(text)
        grams.update(text[i:i + 2] for i in range(len(text) - 1))
        db.executemany(
            "INSERT INTO history_short_grams(app,channel,gram,source_event_id) VALUES(?,?,?,?)",
            ((key[0], key[1], gram, key[2]) for gram in {gram.lower() for gram in grams}),
        )

    @staticmethod
    def _envelope(event):
        # app/source_event_id 不可缺失，否则无法形成稳定来源身份。
        source_id = event.get("source_event_id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_event_id 必须是非空字符串")
        version = event.get("version")
        if type(version) is not int or not 0 < version <= 2**63 - 1:
            raise ValueError("version 必须是 SQLite 可存储的正整数")
        deleted = event.get("deleted", False)
        if type(deleted) is not bool:
            raise ValueError("deleted 必须是布尔值")
        verification = event.get("hash_verification", "unknown")
        if verification not in ("verified", "legacy_unverified", "unknown"):
            raise ValueError("hash_verification 必须是明确的验证状态")
        normalized = {field: event.get(field) for field in FIELDS}
        normalized.update(channel=event.get("channel", "main"), deleted=deleted,
                          hash_verification=verification)
        if deleted:
            normalized["text"] = None
        for field in ("role", "text", "occurred_at", "conversation_id",
                      "epoch_id", "content_hash"):
            if normalized[field] is not None and not isinstance(normalized[field], str):
                raise ValueError(f"{field} 必须是字符串或 None")
        raw_id = normalized["raw_event_id"]
        if raw_id is not None and type(raw_id) not in (str, int):
            raise ValueError("raw_event_id 必须是来源本地字符串、整数或 None")
        if type(raw_id) is int and not -(2**63) <= raw_id < 2**63:
            raise ValueError("raw_event_id 超出 SQLite 整数范围")
        return normalized

    def upsert(self, event):
        """返回 inserted / updated / unchanged / ignored_stale；冲突抛出异常。"""
        with closing(sqlite3.connect(self.path)) as db, db:
            # 将读版本和更新置于同一写事务，避免并发旧版本覆盖新版。
            db.execute("BEGIN IMMEDIATE")
            return self._upsert(event, db)

    def _upsert(self, event, db):
        """供同步页复用；事务及提交由调用方负责。"""
        self._authorize(event.get("app"), event.get("channel", "main"))
        incoming = self._envelope(event)
        key = tuple(incoming[field] for field in FIELDS[:3])
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM history_events WHERE app=? AND channel=? AND source_event_id=?",
            key,
        ).fetchone()
        status = "inserted"
        previous = None
        if row is not None:
            if incoming["deleted"]:
                # 来源仅发送删除标记时保留已有身份关系/时间；显式 None 原样保留。
                for field in ("role", "occurred_at", "conversation_id", "epoch_id",
                              "content_hash", "hash_verification", "raw_event_id"):
                    if field not in event:
                        incoming[field] = row[field]
            previous = row["version"]
            if incoming["version"] < previous:
                status = "ignored_stale"
            elif incoming["version"] == previous:
                if any(incoming[field] != row[field] for field in FIELDS):
                    raise VersionConflict("同一来源事件的相同 version 内容或状态冲突")
                status = "unchanged"
            else:
                status = "updated"
        if status in ("inserted", "updated"):
            columns = ", ".join(FIELDS)
            placeholders = ", ".join("?" for _ in FIELDS)
            assignments = ", ".join(f"{field}=excluded.{field}" for field in FIELDS[3:])
            db.execute(
                f"INSERT INTO history_events ({columns}, previous_version) "
                f"VALUES ({placeholders}, ?) ON CONFLICT(app, channel, source_event_id) "
                f"DO UPDATE SET {assignments}, previous_version=excluded.previous_version, "
                "updated_at=CURRENT_TIMESTAMP",
                tuple(incoming[field] for field in FIELDS) + (previous,),
            )
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='history_short_grams'").fetchone():
                self._update_short_index(incoming, db)
        return {"app": incoming["app"], "channel": incoming["channel"],
                "source_event_id": incoming["source_event_id"], "status": status,
                "version": incoming["version"],
                "current_version": previous if status == "ignored_stale" else incoming["version"]}

    def list_events(self, app_id, channel="main", limit=100, offset=0,
                    include_deleted=False):
        """按来源事件 ID 稳定排序；使用来源/域索引和 SQL 分页。"""
        self._authorize(app_id, channel)
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须是正整数")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset 必须是非负整数")
        if type(include_deleted) is not bool:
            raise ValueError("include_deleted 必须是布尔值")
        visible = "" if include_deleted else " AND deleted=0"
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                f"SELECT {', '.join(FIELDS)} FROM history_events "
                f"WHERE app=? AND channel=?{visible} "
                "ORDER BY source_event_id LIMIT ? OFFSET ?",
                (app_id, channel, limit, offset),
            ).fetchall()
        return [dict(row, deleted=bool(row["deleted"])) for row in rows]

    def export_history(self, app_id, channel="main", limit=100, offset=0,
                       include_deleted=False):
        """默认不导出已删事件；显式包含 tombstone 时仍无正文。"""
        return self.list_events(app_id, channel, limit, offset, include_deleted)

    def import_history(self, events):
        """保留来源身份、缺失关系和验证状态；授权及版本规则逐条适用。"""
        receipts = []
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for event in events:
                db.execute("SAVEPOINT history_import_event")
                try:
                    receipt = self._upsert(event, db)
                except Exception:
                    # 单条的正文/索引同进同退；此前成功项保留。
                    db.execute("ROLLBACK TO history_import_event")
                    db.execute("RELEASE history_import_event")
                    db.commit()
                    raise
                db.execute("RELEASE history_import_event")
                receipts.append(receipt)
        return receipts
