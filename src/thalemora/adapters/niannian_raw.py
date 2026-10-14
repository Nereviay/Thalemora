"""念念 raw_events 的只读快照入口，不连接 Runtime，也不持有写入权。

调用方传入路径、注册 app_id 和正常聊天 source 白名单；source 名称不表示
通道。白名单只能包含已确认的正常聊天来源，不能包含私有存储。metadata_json
中显式 channel 非 main 的行不读取；缺失 channel 的正常聊天行属于 main。
session_id 不是 epoch 证据，当前表契约没有 epoch，故 epoch_id 为 None。

source_event_id 用 JSON 联合身份保存 source 与来源事件 ID；无事件 ID 时用
[source, "raw_event_id:unknown", id]，明确表示未经核实的源本地身份。
不核验 SHA1 分支或旧 hash，hash_verification 始终为 unknown。

版本 1 表示原始快照，2 表示删除状态；这不是编辑版本或变化序号。分页
next_after_id 仅覆盖新增。recheck_deleted 只精确读取调用方给定的一页已知
raw ID，不遍历历史；只能发现仍存在的 deleted 标记，不能发现物理删除、
编辑或删除后恢复。缺失行不伪造 tombstone。没有可靠 edit/delete feed，
不得将本 Reader 的分页接成 P2-C change_cursor，也不组合 deleted_at 游标。
所有 Archive 写入由调用方执行，Archive 每次通过 A 层检查授权。
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


class NiannianRawReader:
    def __init__(self, path, app_id, sources):
        if not isinstance(app_id, str) or not app_id.strip():
            raise ValueError("app_id 必须是显式注册的非空应用 ID")
        if isinstance(sources, str):
            raise ValueError("sources 必须是正常聊天来源白名单")
        self.sources = tuple(sources)
        if not self.sources or any(not isinstance(s, str) or not s.strip()
                                   for s in self.sources):
            raise ValueError("sources 必须是非空正常聊天来源白名单")
        self.app_id = app_id
        self.uri = Path(path).resolve().as_uri() + "?mode=ro"

    def _connect(self):
        db = sqlite3.connect(self.uri, uri=True)
        db.row_factory = sqlite3.Row
        return db

    def _scope(self, alias):
        slots = ",".join("?" for _ in self.sources)
        return (f"{alias}.source IN ({slots}) "
                f"AND {alias}.role IN ('user', 'assistant') "
                f"AND COALESCE(json_extract(NULLIF({alias}.metadata_json, ''), "
                "'$.channel'), 'main') = 'main'")

    @staticmethod
    def _positive(value, name, zero=False):
        if type(value) is not int or not (0 if zero else 1) <= value < 2**63:
            raise ValueError(f"{name} 必须是 SQLite 范围内的整数")

    def _envelope(self, row):
        source_id = row["source_event_id"]
        identity = ([row["source"], "source_event_id", source_id]
                    if source_id else [row["source"], "raw_event_id:unknown", row["id"]])
        deleted = bool(row["deleted"])
        return {
            "app": self.app_id, "channel": "main",
            "source_event_id": json.dumps(identity, ensure_ascii=False, separators=(",", ":")),
            "raw_event_id": row["id"], "version": 2 if deleted else 1,
            "role": row["role"], "text": None if deleted else row["text"],
            "occurred_at": row["created_at"], "conversation_id": row["conversation_id"],
            "epoch_id": None, "content_hash": row["event_hash"],
            "hash_verification": "unknown", "deleted": deleted,
        }

    @staticmethod
    def _columns():
        # 删除行的正文在 SQL 层即不取出，仍允许其作为 tombstone 导出。
        return ("r.id, r.source, r.source_event_id, r.event_hash, r.role, "
                "CASE WHEN r.deleted THEN NULL ELSE r.text END AS text, "
                "r.created_at, r.deleted, r.conversation_id")

    def read_page(self, after_id=0, limit=100):
        """按新增 ID 取有限页；仅折叠 hash、role、text、created_at 全同的跨来源镜像。

        镜像选择白名单内正常聊天的最早 ID；删除行独立保留，不因镜像丢弃
        tombstone。此策略不将来源 hash 当成经过验证的内容身份。
        """
        self._positive(after_id, "after_id", zero=True)
        self._positive(limit, "limit")
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT {self._columns()} FROM raw_events r "
                f"WHERE r.id > ? AND {self._scope('r')} "
                "ORDER BY r.id LIMIT ?",
                (after_id, *self.sources, limit),
            ).fetchall()
            hashes = tuple(dict.fromkeys(row["event_hash"] for row in rows
                                        if not row["deleted"] and row["event_hash"]))
            earliest = {}
            if hashes:
                # 无 hash 索引时也只扫描一次历史；状态仅属于当前页。
                slots = ",".join("?" for _ in hashes)
                candidates = db.execute(
                    "SELECT m.id, m.source, m.event_hash, m.role, m.text, m.created_at "
                    f"FROM raw_events m WHERE m.id < ? AND {self._scope('m')} "
                    f"AND m.deleted = 0 AND m.event_hash IN ({slots}) ORDER BY m.id",
                    (rows[-1]["id"], *self.sources, *hashes),
                )
                for candidate in candidates:
                    key = tuple(candidate[field] for field in
                                ("event_hash", "role", "text", "created_at"))
                    earliest.setdefault(key, (candidate["id"], candidate["source"]))
            events = []
            for row in rows:
                key = tuple(row[field] for field in
                            ("event_hash", "role", "text", "created_at"))
                original = earliest.get(key)
                if (not row["deleted"] and row["event_hash"] and original
                        and original[0] < row["id"] and original[1] != row["source"]):
                    continue
                events.append(self._envelope(row))
        return {"events": events,
                "next_after_id": rows[-1]["id"] if rows else after_id}

    def recheck_deleted(self, raw_ids):
        """调用方分页提供已知源本地 ID；返回幂等的 version=2 tombstone。

        精确主键查询不读原文，不从 deleted_at 推断全局顺序。页顺序和已知
        ID 的覆盖由调用方管理，本方法不声称自动发现所有历史变化。
        """
        ids = tuple(raw_ids)
        for raw_id in ids:
            self._positive(raw_id, "raw_id")
        events = []
        with closing(self._connect()) as db:
            for raw_id in ids:
                row = db.execute(
                    f"SELECT {self._columns()} FROM raw_events r "
                    f"WHERE r.id = ? AND r.deleted = 1 AND {self._scope('r')}",
                    (raw_id, *self.sources),
                ).fetchone()
                if row is not None:
                    events.append(self._envelope(row))
        return events
