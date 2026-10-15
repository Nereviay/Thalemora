"""授权 Archive 的只读 SceneRuntime 输入；不创建或声称拥有 RawEvent。

Scene 的历史字段 raw_event_id 在此仅为 Archive 的本地 rowid，event_hash
仅存来源版本标记。两者均不得作为 canonical raw 引用或 verified hash。
"""
import json
import sqlite3
from contextlib import closing

from .runtime import SceneRuntime


class ArchiveSceneRuntime(SceneRuntime):
    def __init__(self, archive, app_id, channel, path):
        self.archive = archive
        self.app_id = app_id
        self.channel = channel
        super().__init__(db_path=str(path), raw_db=str(archive.path),
                         runtime_db=str(path), embedder=None, initial_backfill_events=0)

    def rows(self):
        self.archive._authorize(self.app_id, self.channel)
        with closing(sqlite3.connect(self.archive.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(
                "SELECT rowid AS archive_rowid,* FROM history_events "
                "WHERE app=? AND channel=? ORDER BY rowid", (self.app_id, self.channel))]

    def _max_raw_id(self):
        # Start at zero: authorized history must not be silently truncated to 120.
        return 0

    @staticmethod
    def event(row):
        return dict(raw_event_id=row["archive_rowid"], role=row["role"] or "",
                    epoch_id=row["epoch_id"] or "", event_time=row["occurred_at"] or "",
                    event_hash=json.dumps([row["source_event_id"], row["version"], row["deleted"]]),
                    text="" if row["deleted"] else row["text"] or "")

    def _fetch_new_events(self, after_id, limit=1000):
        rows = [r for r in self.rows() if r["archive_rowid"] > after_id][:limit]
        events = [self.event(r) for r in rows
                  if not r["deleted"] and r["role"] in {"user", "assistant"} and (r["text"] or "").strip()]
        return events, rows[-1]["archive_rowid"] if rows else after_id

    def _fetch_raw_text(self, raw_event_id):
        self.archive._authorize(self.app_id, self.channel)
        with closing(sqlite3.connect(self.archive.path)) as db:
            row = db.execute("SELECT text,deleted FROM history_events WHERE app=? AND channel=? AND rowid=?",
                             (self.app_id, self.channel, raw_event_id)).fetchone()
        return "" if row is None or row[1] else row[0] or ""

    def _apply_idle_state(self, now):
        # A page edge is not a Scene boundary. Drain the source before idle finalization.
        with self._connect() as db:
            row = db.execute("SELECT last_processed_raw_event_id FROM scene_runtime_state WHERE singleton=1").fetchone()
        if row and any(r["archive_rowid"] > row[0] for r in self.rows()):
            return []
        return super()._apply_idle_state(now)

    def refresh_sources(self):
        """编辑/删除提高派生 Scene 版本，旧候选仍保留但不可再次落卡。"""
        current = {r["archive_rowid"]: self._event_ref(self.event(r)) for r in self.rows()}
        for summary in self.store.list_scenes():
            scene = self.store.get_scene(summary["scene_id"])
            refs = [current.get(ref["raw_event_id"], ref) for ref in scene["events"]]
            if refs != scene["events"]:
                self.store.revise_sources(scene["scene_id"], refs, reason="archive_source_version_changed",
                                          expected_revision_no=scene["current_revision_no"])

    def source_snapshot(self, scene):
        ids = {r["raw_event_id"] for r in scene["events"]}
        return [r for r in self.rows() if r["archive_rowid"] in ids]
