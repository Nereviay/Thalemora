"""念念 canonical message_records 的事务内变更源；安装须由宿主明确执行。

不接管 Runtime。触发器和原变更同事务提交，消费者只读、分页，版本使用
实际 feed seq；既有快照不反推旧修订。仅 user/assistant 普通消息进入 main，
私有通道/工具/no_response 不进入。删除及过期 epoch 清除保留 tombstone。
"""
from contextlib import closing
from pathlib import Path
import sqlite3

TABLE = "thalemora_history_changes"
_FIELDS = ("event_id", "epoch_id", "role", "content", "content_sha256",
           "raw_event_id", "source_created_at", "first_seen_at", "source",
           "status", "metadata_json", "kind")


def _scope(alias):
    return (f"{alias}.role IN ('user','assistant') "
            f"AND {alias}.kind IN ('message','proactive_message','legacy_display') "
            f"AND COALESCE(json_extract(NULLIF({alias}.metadata_json,''),'$.channel'),'main')='main'")


def _values(alias, *, deleted=False, epoch_status=None):
    inactive_epoch = (f"{epoch_status} IN ('deleted','purged')" if epoch_status
                      else f"EXISTS(SELECT 1 FROM context_epochs e WHERE e.epoch_id={alias}.epoch_id "
                           "AND e.status IN ('deleted','purged'))")
    gone = "1" if deleted else f"({alias}.status!='active' OR {inactive_epoch})"
    # authored_text 由实际 canonical metadata 提供；无此字段时保留 canonical content。
    text = (f"COALESCE(json_extract(NULLIF({alias}.metadata_json,''),'$.authored_text'),"
            f"{alias}.content)")
    return (f"{alias}.event_id,{alias}.epoch_id,{alias}.role,"
            f"CASE WHEN {gone} THEN NULL ELSE {text} END,"
            f"{alias}.content_sha256,{alias}.raw_event_id,"
            f"COALESCE({alias}.source_created_at,{alias}.first_seen_at),"
            f"{alias}.source,CASE WHEN {gone} THEN 1 ELSE 0 END")


def install(connection):
    """仅在显式宿主连接上安装；事务由调用方负责，禁止在既有事务中暗自提交。"""
    if connection.in_transaction:
        raise ValueError("安装 feed 需要独立事务")
    columns = {r[1] for r in connection.execute("PRAGMA table_info(message_records)")}
    if not set(_FIELDS) <= columns:
        raise ValueError("message_records 与念念现役合同不匹配")
    connection.execute("BEGIN IMMEDIATE")
    try:
        installed = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        if installed:
            connection.rollback()
            return False
        connection.execute(f"""CREATE TABLE {TABLE}(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL, epoch_id TEXT, role TEXT,
            text TEXT, content_hash TEXT, raw_event_id INTEGER,
            occurred_at TEXT, source TEXT, deleted INTEGER NOT NULL
        )""")
        columns_sql = "(event_id,epoch_id,role,text,content_hash,raw_event_id,occurred_at,source,deleted)"
        connection.execute(
            f"INSERT INTO {TABLE}{columns_sql} SELECT {_values('m')} "
            f"FROM message_records m WHERE {_scope('m')} ORDER BY m.id"
        )
        def trigger(name, timing, when, values):
            connection.execute(
                f"CREATE TRIGGER {name} {timing} ON message_records WHEN {when} BEGIN "
                f"INSERT INTO {TABLE}{columns_sql} VALUES({values}); END"
            )
        trigger("thalemora_message_insert", "AFTER INSERT", _scope("NEW"), _values("NEW"))
        # 原身份离开 main 或被替换时，旧档案应被撤销。
        trigger("thalemora_message_leave", "AFTER UPDATE",
                f"({_scope('OLD')}) AND (NOT ({_scope('NEW')}) OR OLD.event_id IS NOT NEW.event_id)",
                _values("OLD", deleted=True))
        changed = " OR ".join(f"OLD.{f} IS NOT NEW.{f}" for f in _FIELDS)
        trigger("thalemora_message_update", "AFTER UPDATE",
                f"({_scope('NEW')}) AND ({changed})", _values("NEW"))
        trigger("thalemora_message_delete", "AFTER DELETE", _scope("OLD"),
                _values("OLD", deleted=True))
        connection.execute(
            "CREATE TRIGGER thalemora_epoch_status AFTER UPDATE OF status ON context_epochs "
            "WHEN OLD.status IS NOT NEW.status BEGIN "
            f"INSERT INTO {TABLE}{columns_sql} SELECT {_values('m', epoch_status='NEW.status')} "
            f"FROM message_records m WHERE m.epoch_id=NEW.epoch_id AND {_scope('m')} ORDER BY m.id; END"
        )
        connection.commit()
        return True
    except BaseException:
        connection.rollback()
        raise


class CanonicalHistoryReader:
    """来源必须是宿主已确认归属的 canonical Runtime DB，不认证自报 Agent。"""

    table = TABLE
    version_offset = 0

    @staticmethod
    def identity(event_id):
        return f"canonical:{event_id}"

    def __init__(self, path, app_id):
        if not isinstance(app_id, str) or not app_id.strip():
            raise ValueError("需要明确 app_id")
        self.uri = Path(path).resolve(strict=True).as_uri() + "?mode=ro"
        self.app_id = app_id

    def page(self, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("需要非负 cursor 和 1..1000 的 limit")
        with closing(sqlite3.connect(self.uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            rows = db.execute(
                f"SELECT * FROM {self.table} WHERE seq>? ORDER BY seq LIMIT ?", (after, limit),
            ).fetchall()
        events = [{
            "app": self.app_id, "channel": "main",
            "source_event_id": self.identity(r["event_id"]),
            "version": r["seq"] + self.version_offset, "change_cursor": r["seq"],
            "role": r["role"], "text": r["text"], "occurred_at": r["occurred_at"],
            "epoch_id": r["epoch_id"],
            "conversation_id": r["conversation_id"] if "conversation_id" in r.keys() else None,
            "raw_event_id": r["raw_event_id"], "content_hash": r["content_hash"],
            # canonical content hash 不一定是 authored_text hash，保留未知。
            "hash_verification": "unknown", "deleted": bool(r["deleted"]),
        } for r in rows]
        return {"events": events, "next_cursor": rows[-1]["seq"] if rows else after}


def sync_once(sync, reader, *, limit=100):
    """后台有限工作；复用既有 HistorySync 事务/游标，不建立第二套消费状态。"""
    before = sync.get_cursor(reader.app_id, "main")
    page = reader.page(before, limit)
    if page["next_cursor"] == before:
        return {"status": "idle", "cursor": before, "events": []}
    return sync.sync_page(reader.app_id, "main", before, page["next_cursor"], page["events"])


class NiannianHistoryBinding:
    """可信宿主的固定档案路径/既有 Agent 绑定；不创建 UUID 或第二份身份库。"""

    def __init__(self, *, archive_path, owner, agent_id, apps):
        from thalemora.identity import OwnerPrincipal
        if not isinstance(owner, OwnerPrincipal) or not isinstance(agent_id, str) or not agent_id:
            raise ValueError("需要真实宿主 owner 与既有 Agent ID")
        self.path = Path(archive_path).resolve()
        self.owner = owner
        self.agent_id = agent_id
        self.apps = frozenset(apps)
        if not self.apps or any(not isinstance(a, str) or not a for a in self.apps):
            raise ValueError("需要明确授权的来源")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def archive_db_path(self, owner, agent_id):
        from thalemora.identity import AccessDenied
        if owner != self.owner or agent_id != self.agent_id:
            raise AccessDenied("档案不属于该宿主/Agent")
        return self.path

    def assert_source(self, owner, agent_id, app_id, channel="main"):
        from thalemora.identity import AccessDenied
        self.archive_db_path(owner, agent_id)
        if app_id not in self.apps or channel != "main":
            raise AccessDenied("来源或通道未经该宿主绑定")
