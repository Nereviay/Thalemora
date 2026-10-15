"""念念 RawEvent 的事务内变更源；旧快照升级后继续追踪编辑/删除。

只在宿主显式安装时改源 schema。Runtime 和 RawEvent 各自持有事务/游标，
归档保留两种来源，不将两个投影的行数冒称唯一对话数。
"""
from contextlib import closing
import sqlite3

from .niannian_history import CanonicalHistoryReader

TABLE = "thalemora_raw_history_changes"
SOURCES = ("app", "rikkahub", "rikkahub_import", "gateway", "heartbeat")
FIELDS = ("id","source","source_event_id","event_hash","role","text","created_at",
          "conversation_id","metadata_json","deleted")


def _scope(alias):
    names = ",".join("'" + s + "'" for s in SOURCES)
    return (f"{alias}.source IN ({names}) AND {alias}.role IN ('user','assistant') "
            f"AND COALESCE(json_extract(NULLIF({alias}.metadata_json,''),'$.channel'),'main')='main'")


def _identity(alias):
    present = f"COALESCE({alias}.source_event_id,'')!=''"
    return (f"json_array({alias}.source,CASE WHEN {present} THEN 'source_event_id' "
            f"ELSE 'raw_event_id:unknown' END,CASE WHEN {present} THEN {alias}.source_event_id "
            f"ELSE {alias}.id END)")


def _values(alias, deleted=False):
    gone = "1" if deleted else f"COALESCE({alias}.deleted,0)"
    return (f"{_identity(alias)},NULL,{alias}.role,"
            f"CASE WHEN {gone} THEN NULL ELSE {alias}.text END,{alias}.event_hash,"
            f"{alias}.id,{alias}.created_at,{alias}.source,CASE WHEN {gone} THEN 1 ELSE 0 END,"
            f"{alias}.conversation_id")


def install_raw(connection):
    if connection.in_transaction:
        raise ValueError("安装 Raw feed 需要独立事务")
    actual = {r[1] for r in connection.execute("PRAGMA table_info(raw_events)")}
    if not set(FIELDS) <= actual:
        raise ValueError("raw_events 与念念现役合同不匹配")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name=?",(TABLE,)).fetchone():
            connection.rollback()
            return False
        connection.execute(f"""CREATE TABLE {TABLE}(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL,epoch_id TEXT,
            role TEXT,text TEXT,content_hash TEXT,raw_event_id INTEGER,occurred_at TEXT,
            source TEXT,deleted INTEGER NOT NULL,conversation_id TEXT)""")
        columns = "(event_id,epoch_id,role,text,content_hash,raw_event_id,occurred_at,source,deleted,conversation_id)"
        connection.execute(f"INSERT INTO {TABLE}{columns} SELECT {_values('r')} "
                           f"FROM raw_events r WHERE {_scope('r')} ORDER BY r.id")
        def trigger(name,timing,condition,values):
            connection.execute(f"CREATE TRIGGER {name} {timing} ON raw_events "
                               f"WHEN {condition} BEGIN INSERT INTO {TABLE}{columns} VALUES({values}); END")
        trigger("thalemora_raw_insert","AFTER INSERT",_scope("NEW"),_values("NEW"))
        trigger("thalemora_raw_leave","AFTER UPDATE",
                f"({_scope('OLD')}) AND (NOT ({_scope('NEW')}) OR {_identity('OLD')} IS NOT {_identity('NEW')})",
                _values("OLD",True))
        changed = " OR ".join(f"OLD.{f} IS NOT NEW.{f}" for f in FIELDS)
        trigger("thalemora_raw_update","AFTER UPDATE",f"({_scope('NEW')}) AND ({changed})",_values("NEW"))
        trigger("thalemora_raw_delete","AFTER DELETE",_scope("OLD"),_values("OLD",True))
        connection.commit()
        return True
    except BaseException:
        connection.rollback()
        raise


class RawHistoryReader(CanonicalHistoryReader):
    table = TABLE
    # 旧 NiannianRawReader 的版本 1/2 是快照标记；新 feed 版本从 3 开始。
    version_offset = 2

    @staticmethod
    def identity(event_id):
        return event_id
