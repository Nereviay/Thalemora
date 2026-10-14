"""P2-C 按需分页同步，不调度、不扫描历史。

来源合同：change_cursor 是覆盖新增、编辑和删除的单调正整数变化序号，
页面完整覆盖 (before_cursor, next_cursor]，事件按变化序号递增。
初始游标为 0；空页可推进来源已确认完整覆盖的区间。
本模块无法替来源证明 feed 完整性。仅新增 raw_events.id 和 deleted_at
不保证编辑 feed；念念来源的真实覆盖留给 P2-D 说明。
"""

import hashlib
import json
import sqlite3
from contextlib import closing

from .archive import Archive


class CursorConflict(ValueError):
    """页面游标不接续，或已提交页的内容发生变化。"""


class HistorySync:
    """每 Agent 独立游标；owner 必须来自宿主认证。

    仅最近成功页允许原样重复 ack；更早页面按乱序拒绝。
    页指纹仅用于重复回执的内容一致性，不认证来源或证明 feed 完整。
    """

    def __init__(self, registry, owner, agent_id):
        self.archive = Archive(registry, owner, agent_id)
        with closing(sqlite3.connect(self.archive.path)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS sync_cursors (
                    app TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    cursor INTEGER NOT NULL CHECK(cursor > 0),
                    before_cursor INTEGER NOT NULL CHECK(before_cursor >= 0),
                    page_digest TEXT NOT NULL,
                    PRIMARY KEY (app, channel)
                )
            """)

    @staticmethod
    def _cursor(value, allow_zero=False):
        if type(value) is not int or not (0 if allow_zero else 1) <= value < 2**63:
            raise ValueError("cursor 必须是 SQLite 可存储的整数，变化序号必须为正")

    def get_cursor(self, app_id, channel="main"):
        self.archive._authorize(app_id, channel)
        with closing(sqlite3.connect(self.archive.path)) as db:
            row = db.execute(
                "SELECT cursor FROM sync_cursors WHERE app=? AND channel=?",
                (app_id, channel),
            ).fetchone()
        return row[0] if row else 0

    def sync_page(self, app_id, channel, before_cursor, next_cursor, events):
        """成功返回 committed / already_committed 及事件回执；失败直接抛出。

        冲突前已写入的事件保留，游标不前移；修正来源页后通过 upsert 重放。
        页内变化序号不可用消息 ID 替代。调用方只提供当前页，不遍历全库。
        """
        self.archive._authorize(app_id, channel)
        self._cursor(before_cursor, allow_zero=True)
        self._cursor(next_cursor)
        if next_cursor <= before_cursor:
            raise CursorConflict("next_cursor 必须严格大于 before_cursor")
        events = list(events)
        previous = before_cursor
        for event in events:
            if event.get("app") != app_id or event.get("channel") != channel:
                raise ValueError("事件 app/channel 必须与页面来源一致")
            self.archive._authorize(event["app"], event["channel"])
            change = event.get("change_cursor")
            self._cursor(change)
            if not previous < change <= next_cursor:
                raise CursorConflict("事件 change_cursor 必须在页面区间内严格递增")
            previous = change
        # 完整页内容参与指纹，拒绝同游标异文；不持久保存旧正文。
        digest = hashlib.sha256(json.dumps(
            events, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")).hexdigest()
        with closing(sqlite3.connect(self.archive.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT cursor, before_cursor, page_digest FROM sync_cursors "
                "WHERE app=? AND channel=?", (app_id, channel),
            ).fetchone()
            current = row[0] if row else 0
            if row and (before_cursor, next_cursor) == (row[1], current):
                if digest != row[2]:
                    raise CursorConflict("已提交页的内容不一致")
                return {"status": "already_committed", "cursor": current, "events": []}
            if before_cursor != current:
                raise CursorConflict("before_cursor 与持久游标不一致")
            receipts = []
            try:
                for event in events:
                    receipts.append(self.archive._upsert(event, db))
            except Exception:
                # 与 Archive 的逐事件导入契约一致：保留成功项以便幂等重放。
                db.commit()
                raise
            db.execute(
                "INSERT INTO sync_cursors VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(app, channel) DO UPDATE SET cursor=excluded.cursor, "
                "before_cursor=excluded.before_cursor, page_digest=excluded.page_digest",
                (app_id, channel, next_cursor, before_cursor, digest),
            )
        return {"status": "committed", "cursor": next_cursor, "events": receipts}
