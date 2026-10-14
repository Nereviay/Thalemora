"""P2-B：仅使用临时 SQLite 与合成事件，不连接任何现役数据。"""

import json
import sqlite3
import unittest
from contextlib import closing
from tempfile import TemporaryDirectory
from unittest.mock import patch

from thalemora.history import Archive, VersionConflict
from thalemora.identity import AccessDenied, IdentityRegistry, OwnerPrincipal


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = IdentityRegistry(self.temp.name)
        self.owner = OwnerPrincipal("合成用户")
        self.agent = self.registry.register_agent(self.owner, "合成 Agent")
        self.registry.grant_source(self.owner, self.agent.agent_id, "app-a")
        self.archive = Archive(self.registry, self.owner, self.agent.agent_id)

    @staticmethod
    def event(**changes):
        event = {
            "app": "app-a", "channel": "main", "source_event_id": "event-01",
            "version": 1, "role": "user", "text": "合成原文：空格  与换行\n🙂",
            "occurred_at": "2026-10-14T08:09:10+08:00",
            "conversation_id": "conversation-real", "epoch_id": "epoch-real",
            "content_hash": "historical:source-defined-hash", "deleted": False,
            "hash_verification": "legacy_unverified", "raw_event_id": 17,
        }
        event.update(changes)
        return event

    def test_complete_envelope_and_restart(self):
        events = [self.event(), self.event(source_event_id="event-02", role="assistant",
                                          text="合成回复\n完整保留", raw_event_id="local-18")]
        self.assertEqual([r["status"] for r in self.archive.import_history(events)],
                         ["inserted", "inserted"])
        restarted = Archive(IdentityRegistry(self.temp.name), self.owner, self.agent.agent_id)
        self.assertEqual(restarted.list_events("app-a"), events)
        self.assertEqual(json.loads(json.dumps(restarted.export_history("app-a"))), events)

    def test_same_ids_isolated_by_agent_app_and_channel(self):
        second = self.registry.register_agent(self.owner, "合成 Agent")
        self.registry.grant_source(self.owner, second.agent_id, "app-a")
        other = Archive(self.registry, self.owner, second.agent_id)
        self.registry.grant_source(self.owner, self.agent.agent_id, "app-b")
        self.registry.grant_source(self.owner, self.agent.agent_id, "app-a", "private")
        main = self.event(text="甲公开")
        private = self.event(channel="private", text="甲私有")
        app_b = self.event(app="app-b", text="乙应用")
        other_agent = self.event(text="另一个 Agent")
        self.archive.import_history([main, private, app_b])
        other.upsert(other_agent)
        self.assertEqual(self.archive.list_events("app-a"), [main])
        self.assertEqual(self.archive.export_history("app-a", "private"), [private])
        self.assertEqual(self.archive.export_history("app-b"), [app_b])
        self.assertEqual(other.list_events("app-a"), [other_agent])
        for call in (lambda: other.export_history("app-b"),
                     lambda: other.export_history("app-a", "private"),
                     lambda: Archive(self.registry, OwnerPrincipal("其他用户"), self.agent.agent_id)):
            with self.assertRaises(AccessDenied):
                call()

    def test_ungranted_app_and_private_are_denied_on_all_paths(self):
        for app, channel in (("app-b", "main"), ("app-a", "private")):
            calls = (
                lambda: self.archive.upsert(self.event(app=app, channel=channel)),
                lambda: self.archive.import_history([self.event(app=app, channel=channel)]),
                lambda: self.archive.list_events(app, channel, include_deleted=True),
                lambda: self.archive.export_history(app, channel),
            )
            for call in calls:
                with self.subTest(app=app, channel=channel):
                    with self.assertRaises(AccessDenied):
                        call()
        self.assertEqual(self.archive.list_events("app-a", include_deleted=True), [])

    def test_source_authorization_is_checked_every_time(self):
        with patch.object(self.registry, "assert_source",
                          wraps=self.registry.assert_source) as authorize:
            self.archive.upsert(self.event())
            self.archive.upsert(self.event())
            self.archive.list_events("app-a")
            self.archive.export_history("app-a")
            self.archive.import_history([self.event()])
            self.assertEqual(authorize.call_count, 5)
            for call in authorize.call_args_list:
                self.assertEqual(call.args, (self.owner, self.agent.agent_id, "app-a", "main"))
        # 仅修改临时注册库，证明 Archive 没有缓存已有来源授权。
        with closing(sqlite3.connect(self.registry.registry_path)) as db, db:
            db.execute("DELETE FROM sources WHERE agent_id=?", (self.agent.agent_id,))
        for call in (lambda: self.archive.upsert(self.event(version=2)),
                     lambda: self.archive.list_events("app-a"),
                     lambda: self.archive.export_history("app-a"),
                     lambda: self.archive.import_history([self.event()])):
            with self.assertRaises(AccessDenied):
                call()

    def test_idempotence_and_same_version_conflicts(self):
        event = self.event()
        self.assertEqual(self.archive.upsert(event)["status"], "inserted")
        self.assertEqual(self.archive.upsert(event)["status"], "unchanged")
        for changes in ({"text": "异文"}, {"deleted": True}, {"role": "assistant"},
                        {"occurred_at": None}, {"epoch_id": None},
                        {"content_hash": "different"}, {"hash_verification": "unknown"}):
            with self.subTest(changes=changes):
                with self.assertRaises(VersionConflict):
                    self.archive.upsert(self.event(**changes))
        self.assertEqual(self.archive.list_events("app-a"), [event])

    def test_versions_can_skip_and_late_versions_cannot_overwrite(self):
        self.archive.upsert(self.event())
        edited = self.event(version=3, text="第三版")
        self.assertEqual(self.archive.upsert(edited)["status"], "updated")
        receipt = self.archive.upsert(self.event(version=2, text="晚到第二版"))
        self.assertEqual((receipt["status"], receipt["current_version"]), ("ignored_stale", 3))
        self.assertEqual(self.archive.list_events("app-a"), [edited])
        with closing(sqlite3.connect(self.archive.path)) as db:
            self.assertEqual(db.execute(
                "SELECT version, previous_version, updated_at IS NOT NULL FROM history_events"
            ).fetchone(), (3, 1, 1))
        late = Archive(self.registry, self.owner, self.registry.register_agent(
            self.owner, "先收到新版").agent_id)
        self.registry.grant_source(self.owner, late.agent_id, "app-a")
        late.upsert(edited)
        self.assertEqual(late.upsert(self.event())["status"], "ignored_stale")
        self.assertEqual(late.list_events("app-a"), [edited])

    def test_delete_clears_body_and_stale_versions_do_not_restore(self):
        self.archive.upsert(self.event())
        self.archive.upsert(self.event(version=2, text="旧编辑正文"))
        deleted = self.event(version=3, deleted=True, text="删除输入也不保存")
        self.assertEqual(self.archive.upsert(deleted)["status"], "updated")
        tombstone = dict(deleted, text=None)
        self.assertEqual(self.archive.list_events("app-a"), [])
        self.assertEqual(self.archive.export_history("app-a"), [])
        self.assertEqual(self.archive.export_history("app-a", include_deleted=True), [tombstone])
        self.assertEqual(self.archive.upsert(dict(deleted, text=None))["status"], "unchanged")
        for version in (1, 2):
            self.assertEqual(self.archive.upsert(self.event(version=version))["status"],
                             "ignored_stale")
        with self.assertRaises(VersionConflict):
            self.archive.upsert(self.event(version=3))
        with closing(sqlite3.connect(self.archive.path)) as db:
            self.assertEqual(db.execute(
                "SELECT text, deleted, version, previous_version FROM history_events"
            ).fetchall(), [(None, 1, 3, 2)])
            self.assertEqual(db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall(), [("history_events",)])
        self.assertEqual(self.archive.list_events("app-a"), [])

    def test_sparse_tombstone_preserves_known_time_and_relations(self):
        self.archive.upsert(self.event())
        deletion = {"app": "app-a", "source_event_id": "event-01",
                    "version": 2, "deleted": True}
        self.archive.upsert(deletion)
        self.assertEqual(self.archive.upsert(deletion)["status"], "unchanged")
        self.assertEqual(self.archive.list_events("app-a", include_deleted=True),
                         [self.event(version=2, deleted=True, text=None)])
        first = dict(deletion, source_event_id="delete-first")
        self.archive.upsert(first)
        row = self.archive.list_events("app-a", include_deleted=True)[0]
        self.assertIsNone(row["occurred_at"])
        self.assertIsNone(row["epoch_id"])
        self.assertEqual(self.archive.upsert(self.event(source_event_id="delete-first"))["status"],
                         "ignored_stale")

    def test_unknown_relations_and_hash_status_are_preserved(self):
        events = []
        for i, status in enumerate(("unknown", "legacy_unverified", "verified")):
            event = self.event(source_event_id=f"unknown-{i}", role=None, text=None,
                               occurred_at=None, conversation_id=None, epoch_id=None,
                               raw_event_id=None, content_hash=None if i == 0 else "old-sha1",
                               hash_verification=status)
            events.append(event)
        self.archive.import_history(events)
        self.assertEqual(self.archive.export_history("app-a"), events)
        self.archive.upsert({"app": "app-a", "source_event_id": "unspecified", "version": 1})
        row = self.archive.list_events("app-a")[3]
        self.assertEqual(row["hash_verification"], "unknown")
        self.assertIsNone(row["epoch_id"])
        self.assertIsNone(row["content_hash"])

    def test_export_import_into_fresh_database_preserves_current_states(self):
        self.archive.import_history([self.event(), self.event(source_event_id="event-02"),
                                     self.event(source_event_id="event-03", epoch_id=None)])
        self.archive.upsert(self.event(version=3, role="assistant", text="修改原文"))
        self.archive.upsert(self.event(source_event_id="event-02", version=2, deleted=True))
        payload = json.loads(json.dumps(self.archive.export_history("app-a", include_deleted=True)))
        with TemporaryDirectory() as target_root:
            registry = IdentityRegistry(target_root)
            agent = registry.register_agent(self.owner, "导入目标")
            registry.grant_source(self.owner, agent.agent_id, "app-a")
            target = Archive(registry, self.owner, agent.agent_id)
            self.assertEqual([r["status"] for r in target.import_history(payload)], ["inserted"] * 3)
            self.assertEqual(target.export_history("app-a", include_deleted=True), payload)
            self.assertEqual(target.list_events("app-a"), self.archive.list_events("app-a"))
            self.assertEqual([r["status"] for r in target.import_history(payload)], ["unchanged"] * 3)
            self.assertEqual(len(target.list_events("app-a")), 2)

    def test_pagination_uses_index_and_filters_deleted(self):
        self.archive.import_history([self.event(source_event_id=f"event-{i:02}",
                                              deleted=(i == 2)) for i in range(6)])
        page = self.archive.list_events("app-a", limit=2, offset=2)
        self.assertEqual([event["source_event_id"] for event in page], ["event-03", "event-04"])
        self.assertEqual(self.archive.list_events("app-a", offset=99), [])
        self.assertEqual(len(self.archive.list_events("app-a", include_deleted=True)), 6)
        with closing(sqlite3.connect(self.archive.path)) as db:
            plan = db.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM history_events "
                "WHERE app=? AND channel=? AND deleted=0 ORDER BY source_event_id LIMIT ? OFFSET ?",
                ("app-a", "main", 2, 2),
            ).fetchall()
        self.assertTrue(any("history_visible_page" in row[3] for row in plan), plan)
        self.assertFalse(any("TEMP B-TREE" in row[3] or "SCAN " in row[3] for row in plan), plan)

    def test_invalid_envelopes_and_pages_fail_explicitly(self):
        for changes in ({"version": 0}, {"version": -1}, {"version": True},
                        {"version": None}, {"version": 1.5}, {"source_event_id": ""},
                        {"hash_verification": "assumed_valid"}, {"deleted": 1},
                        {"text": ["不是正文字符串"]}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.archive.upsert(self.event(**changes))
        for page in ({"limit": 0}, {"limit": True}, {"offset": -1}, {"include_deleted": 1}):
            with self.subTest(page=page):
                with self.assertRaises(ValueError):
                    self.archive.list_events("app-a", **page)
        self.assertEqual(self.archive.list_events("app-a"), [])


if __name__ == "__main__":
    unittest.main()
