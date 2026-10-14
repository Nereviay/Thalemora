"""P2-C 合成可靠变化 feed，仅使用临时 SQLite。"""

import sqlite3
import unittest
from contextlib import closing
from tempfile import TemporaryDirectory
from unittest.mock import patch

from thalemora.history import CursorConflict, HistorySync, VersionConflict
from thalemora.identity import AccessDenied, IdentityRegistry, OwnerPrincipal


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = IdentityRegistry(self.temp.name)
        self.owner = OwnerPrincipal("合成用户")
        self.agent = self.registry.register_agent(self.owner, "合成 Agent")
        self.registry.grant_source(self.owner, self.agent.agent_id, "app-a")
        self.sync = HistorySync(self.registry, self.owner, self.agent.agent_id)

    @staticmethod
    def event(change=1, **changes):
        event = {"app": "app-a", "channel": "main", "source_event_id": "old",
                 "version": 1, "text": "合成正文", "change_cursor": change}
        event.update(changes)
        return event

    def page(self, before, after, events):
        return self.sync.sync_page("app-a", "main", before, after, events)

    def rows(self):
        return self.sync.archive.list_events("app-a", include_deleted=True)

    def test_new_then_old_edit_and_delete_with_new_change_cursors(self):
        self.page(0, 1, [self.event()])
        self.page(1, 2, [self.event(2, source_event_id="new")])
        self.page(2, 3, [self.event(3, version=2, text="旧消息编辑")])
        self.assertEqual(next(e for e in self.rows() if e["source_event_id"] == "old")
                         ["text"], "旧消息编辑")
        self.page(3, 4, [self.event(4, version=3, deleted=True)])
        old = next(e for e in self.rows() if e["source_event_id"] == "old")
        self.assertIsNone(old["text"])
        self.assertTrue(old["deleted"])
        self.assertEqual(old["version"], 3)
        self.assertEqual(self.sync.get_cursor("app-a"), 4)
        restarted = HistorySync(self.registry, self.owner, self.agent.agent_id)
        self.assertEqual(restarted.get_cursor("app-a"), 4)

    def test_exact_duplicate_ack_and_changed_duplicate_rejected(self):
        events = [self.event()]
        self.page(0, 1, events)
        with patch.object(self.sync.archive, "_upsert") as write:
            self.assertEqual(self.page(0, 1, events)["status"], "already_committed")
            write.assert_not_called()
        with self.assertRaises(CursorConflict):
            self.page(0, 1, [self.event(text="异文")])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["text"], "合成正文")

    def test_out_of_order_skip_and_invalid_next_do_not_write(self):
        self.page(0, 1, [self.event()])
        self.page(1, 2, [self.event(2, source_event_id="new")])
        for before, after in ((0, 1), (3, 4), (2, 2), (2, 1)):
            with self.subTest(before=before, after=after):
                with self.assertRaises(CursorConflict):
                    self.page(before, after, [self.event(after, version=3)])
        self.assertEqual(self.sync.get_cursor("app-a"), 2)
        self.assertEqual(len(self.rows()), 2)

    def test_conflict_keeps_partial_events_and_replay_commits_cursor(self):
        self.page(0, 1, [self.event()])
        first = self.event(2, source_event_id="new")
        with self.assertRaises(VersionConflict):
            self.page(1, 3, [first, self.event(3, text="同版本冲突")])
        self.assertEqual(self.sync.get_cursor("app-a"), 1)
        self.assertEqual(len(self.rows()), 2)
        result = self.page(1, 3, [first, self.event(3, version=2, text="来源修正")])
        self.assertEqual([e["status"] for e in result["events"]], ["unchanged", "updated"])
        self.assertEqual(self.sync.get_cursor("app-a"), 3)
        self.assertEqual(len(self.rows()), 2)

    def test_stale_version_is_ignored_without_blocking_new_change_cursor(self):
        self.page(0, 1, [self.event(version=2, text="新版")])
        result = self.page(1, 2, [self.event(2)])
        self.assertEqual(result["events"][0]["status"], "ignored_stale")
        self.assertEqual(self.rows()[0]["text"], "新版")
        self.assertEqual(self.sync.get_cursor("app-a"), 2)

    def test_owner_app_private_authorization_and_agent_isolation(self):
        with self.assertRaises(AccessDenied):
            HistorySync(self.registry, OwnerPrincipal("其他用户"), self.agent.agent_id)
        second = self.registry.register_agent(self.owner, "第二 Agent")
        other = HistorySync(self.registry, self.owner, second.agent_id)
        with self.assertRaises(AccessDenied):
            other.sync_page("app-a", "main", 0, 1, [self.event()])
        for app, channel in (("app-b", "main"), ("app-a", "private")):
            with self.assertRaises(AccessDenied):
                self.sync.sync_page(app, channel, 0, 1,
                                    [self.event(app=app, channel=channel)])
            with self.assertRaises(AccessDenied):
                self.sync.get_cursor(app, channel)
        self.registry.grant_source(self.owner, second.agent_id, "app-a")
        self.page(0, 1, [self.event()])
        self.assertEqual(other.get_cursor("app-a"), 0)
        self.assertEqual(other.archive.list_events("app-a"), [])

    def test_each_event_must_match_page_source_before_any_write(self):
        for changes in ({"app": "app-b"}, {"channel": "private"}, {"channel": None}):
            with self.assertRaises(ValueError):
                self.page(0, 2, [self.event(), self.event(2, **changes)])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.sync.get_cursor("app-a"), 0)

    def test_change_cursor_required_positive_and_strictly_ordered(self):
        for cursor in (None, 0, -1, True, "1", 2**63):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                self.page(0, 1, [self.event(cursor)])
        for events in ([self.event(2), self.event(1)],
                       [self.event(), self.event()], [self.event(3)]):
            with self.assertRaises(CursorConflict):
                self.page(0, 2, events)
        missing = self.event(raw_event_id=1)
        del missing["change_cursor"]
        with self.assertRaises(ValueError):
            self.page(0, 1, [missing])
        self.assertEqual(self.rows(), [])

    def test_empty_covered_interval_and_independent_source_cursors(self):
        self.page(0, 5, [])
        self.assertEqual(self.sync.get_cursor("app-a"), 5)
        self.assertEqual(self.page(0, 5, [])["status"], "already_committed")
        for app, channel in (("app-b", "main"), ("app-a", "private")):
            self.registry.grant_source(self.owner, self.agent.agent_id, app, channel)
            self.assertEqual(self.sync.get_cursor(app, channel), 0)
            self.sync.sync_page(app, channel, 0, 1,
                                [self.event(app=app, channel=channel)])
            self.assertEqual(self.sync.get_cursor(app, channel), 1)
        self.assertEqual(self.sync.get_cursor("app-a"), 5)

    def test_duplicate_ack_rechecks_current_authorization(self):
        self.page(0, 1, [self.event()])
        with closing(sqlite3.connect(self.registry.registry_path)) as db, db:
            db.execute("DELETE FROM sources WHERE agent_id=?", (self.agent.agent_id,))
        with self.assertRaises(AccessDenied):
            self.page(0, 1, [self.event()])

    def test_page_uses_primary_key_lookups_without_history_scan(self):
        queries = []
        connect = sqlite3.connect

        def traced(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(queries.append)
            return db

        with patch("sqlite3.connect", side_effect=traced):
            self.page(0, 2, [self.event(), self.event(2, source_event_id="new")])
        history_reads = [q for q in queries if q.startswith("SELECT")
                         and "history_events" in q]
        self.assertEqual(len(history_reads), 2)
        self.assertTrue(all("source_event_id=" in q for q in history_reads))
        cursor_reads = [q for q in queries if q.startswith("SELECT")
                        and "sync_cursors" in q]
        self.assertEqual(len(cursor_reads), 1)
        self.assertIn("WHERE app=", cursor_reads[0])
        self.assertIn("AND channel=", cursor_reads[0])


if __name__ == "__main__":
    unittest.main()
