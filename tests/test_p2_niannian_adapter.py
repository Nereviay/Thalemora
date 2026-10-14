"""P2-D / P2 最终合成集成验收；仅访问本测试创建的临时 SQLite。"""

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from thalemora.adapters import NiannianRawReader
from thalemora.history import Archive, HistorySync, VersionConflict
from thalemora.identity import AccessDenied, IdentityRegistry, OwnerPrincipal


class NiannianAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="p2_synthetic_")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.raw = root / "synthetic raw?#.sqlite"
        with closing(sqlite3.connect(self.raw)) as db, db:
            db.execute("""CREATE TABLE raw_events (
                id INTEGER PRIMARY KEY, source TEXT, source_event_id TEXT,
                event_hash TEXT, role TEXT, text TEXT, created_at TEXT,
                deleted INTEGER, deleted_at TEXT, conversation_id TEXT,
                session_id TEXT, metadata_json TEXT
            )""")
        self.insert(1, text="合成原文： 空格  \n🙂")
        self.insert(2, source="rikkahub", source_event_id="mirror", event_hash="old-hash-1",
                    text="合成原文： 空格  \n🙂")
        self.insert(3, source_event_id="reply", role="assistant")
        self.insert(4, source_event_id=None, conversation_id=None)
        self.insert(5, source_event_id="deleted", deleted=1, deleted_at="2026-10-14T11:00:00+08:00")
        self.insert(6, source_event_id="private", metadata_json='{"channel":"private"}')
        self.insert(7, source="private-store", source_event_id="secret")
        self.insert(8, role="system")
        self.insert(9, source="rikkahub", source_event_id="event-1")
        self.insert(10, source_event_id="same-source-hash", event_hash="old-hash-1")
        self.registry = IdentityRegistry(root / "registry")
        self.owner = OwnerPrincipal("合成 owner")
        self.agent = self.registry.register_agent(self.owner, "同名 Agent")
        self.registry.grant_source(self.owner, self.agent.agent_id, "niannian")
        self.archive = Archive(self.registry, self.owner, self.agent.agent_id)
        self.reader = NiannianRawReader(self.raw, "niannian", ["app", "rikkahub"])

    def insert(self, raw_id, **changes):
        row = dict(id=raw_id, source="app", source_event_id="event-1",
                   event_hash=f"old-hash-{raw_id}", role="user", text=f"合成正文 {raw_id}",
                   created_at="2026-10-14T08:09:10+08:00", deleted=0, deleted_at=None,
                   conversation_id="real-conversation", session_id="session-is-not-epoch",
                   metadata_json="{}")
        row.update(changes)
        with closing(sqlite3.connect(self.raw)) as db, db:
            db.execute("INSERT INTO raw_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row.values()))

    def all_events(self):
        return self.reader.read_page(limit=100)["events"]

    def test_new_id_pagination_and_cross_source_mirrors(self):
        cursor, events = 0, []
        while True:
            page = self.reader.read_page(cursor, limit=2)
            if page["next_after_id"] == cursor:
                break
            self.assertGreater(page["next_after_id"], cursor)
            events.extend(page["events"])
            cursor = page["next_after_id"]
        self.assertEqual([e["raw_event_id"] for e in events], [1, 3, 4, 5, 9, 10])
        self.assertEqual(cursor, 10)
        self.insert(11, source_event_id="new")
        self.assertEqual([e["raw_event_id"] for e in self.reader.read_page(cursor)["events"]], [11])
        self.assertTrue(all("change_cursor" not in e for e in events))

    def test_mirror_only_page_advances_raw_cursor(self):
        page = self.reader.read_page(after_id=1, limit=1)
        self.assertEqual(page, {"events": [], "next_after_id": 2})
        self.assertEqual(self.reader.read_page(2, limit=1)["events"][0]["raw_event_id"], 3)

    def test_exact_mirrors_collisions_and_tombstones(self):
        original = "合成原文： 空格  \n🙂"
        for raw_id, changes in (
            (11, {"text": "不同正文"}),
            (12, {"text": original, "role": "assistant"}),
            (13, {"text": original, "created_at": "2026-10-14T08:09:11+08:00"}),
            (14, {"text": original, "deleted": 1}),
            (15, {"text": original}),
            (16, {"text": original, "event_hash": ""}),
            (17, {"text": original, "event_hash": None}),
            (18, {"text": original, "source": "app"}),
        ):
            self.insert(raw_id, **{"source": "rikkahub", "event_hash": "old-hash-1", **changes})
        page = self.reader.read_page(10)
        self.assertEqual([e["raw_event_id"] for e in page["events"]], [11, 12, 13, 14, 16, 17, 18])
        self.assertIsNone(page["events"][3]["text"])
        self.assertEqual(page["next_after_id"], 18)

    def test_page_uses_one_batch_query_without_correlated_subqueries(self):
        queries = []
        with closing(self.reader._connect()) as db:
            db.set_trace_callback(queries.append)
            with patch.object(self.reader, "_connect", return_value=db):
                self.reader.read_page(limit=100)
        self.assertEqual(len(queries), 2)
        self.assertIn("LIMIT 100", queries[0])
        self.assertIn("m.event_hash IN (", queries[1])
        with closing(self.reader._connect()) as db:
            self.assertEqual(db.execute("PRAGMA index_list(raw_events)").fetchall(), [])
            for query in queries:
                plan = db.execute("EXPLAIN QUERY PLAN " + query).fetchall()
                self.assertFalse(any("SUBQUERY" in row[3] for row in plan), plan)

    def test_original_text_time_conversation_and_unverified_hash(self):
        event = self.all_events()[0]
        self.assertEqual(event["text"], "合成原文： 空格  \n🙂")
        self.assertEqual(event["occurred_at"], "2026-10-14T08:09:10+08:00")
        self.assertEqual(event["conversation_id"], "real-conversation")
        self.assertIsNone(event["epoch_id"])
        self.assertEqual(event["content_hash"], "old-hash-1")
        self.assertEqual(event["hash_verification"], "unknown")
        self.assertEqual(json.loads(event["source_event_id"]), ["app", "source_event_id", "event-1"])
        self.assertEqual(event["role"], "user")
        self.assertEqual(self.all_events()[1]["role"], "assistant")

    def test_missing_event_id_is_traceable_and_relations_are_not_guessed(self):
        event = next(e for e in self.all_events() if e["raw_event_id"] == 4)
        self.assertEqual(json.loads(event["source_event_id"]), ["app", "raw_event_id:unknown", 4])
        self.assertIsNone(event["conversation_id"])
        self.assertIsNone(event["epoch_id"])
        self.assertEqual(event["hash_verification"], "unknown")

    def test_deleted_snapshot_and_archive_export_never_expose_body(self):
        events = self.all_events()
        deleted = next(e for e in events if e["deleted"])
        self.assertIsNone(deleted["text"])
        self.assertEqual(deleted["version"], 2)
        self.archive.import_history(events)
        self.assertFalse(any(e["deleted"] for e in self.archive.export_history("niannian")))
        exported = self.archive.export_history("niannian", include_deleted=True)
        self.assertNotIn("合成正文 5", json.dumps(exported, ensure_ascii=False))
        self.assertIsNone(next(e for e in exported if e["deleted"])["text"])

    def test_known_id_delete_recheck_updates_archive_and_is_idempotent(self):
        self.archive.import_history(self.all_events())
        with closing(sqlite3.connect(self.raw)) as db, db:
            db.execute("UPDATE raw_events SET deleted=1, deleted_at=? WHERE id=1",
                       ("1999-01-01",))  # 时间无序也不影响精确 ID 重核。
        self.assertEqual(self.reader.read_page(after_id=10)["events"], [])
        tombstones = self.reader.recheck_deleted([1, 3, 999])
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(tombstones[0]["version"], 2)
        self.assertIsNone(tombstones[0]["text"])
        self.assertEqual(self.archive.import_history(tombstones)[0]["status"], "updated")
        self.assertEqual(self.archive.import_history(tombstones)[0]["status"], "unchanged")
        exported = self.archive.export_history("niannian", include_deleted=True)
        self.assertNotIn("合成原文", json.dumps(exported, ensure_ascii=False))
        old = next(e for e in exported if e["raw_event_id"] == 1)
        self.assertEqual(old, tombstones[0])
        with closing(sqlite3.connect(self.archive.path)) as db:
            self.assertEqual(db.execute("SELECT previous_version FROM history_events WHERE raw_event_id=1").fetchone()[0], 1)

    def test_private_and_other_channels_do_not_enter_main(self):
        self.insert(11, metadata_json='{"channel":"water"}')
        self.assertEqual([e["raw_event_id"] for e in self.all_events()], [1, 3, 4, 5, 9, 10])
        with closing(sqlite3.connect(self.raw)) as db, db:
            db.execute("UPDATE raw_events SET deleted=1 WHERE id IN (6,7,8,11)")
        self.assertEqual(self.reader.recheck_deleted([6, 7, 8, 11]), [])

    def test_ungranted_app_and_private_rejected_before_archive_write(self):
        event = self.all_events()[0]
        for changes in ({"app": "ungranted"}, {"channel": "private"}):
            with self.assertRaises(AccessDenied):
                self.archive.upsert(dict(event, **changes))
        self.assertEqual(self.archive.list_events("niannian"), [])

    def test_same_name_agents_and_cross_app_sources_are_isolated(self):
        second = self.registry.register_agent(self.owner, "同名 Agent")
        other = Archive(self.registry, self.owner, second.agent_id)
        with self.assertRaises(AccessDenied):
            other.import_history(self.all_events())
        self.registry.grant_source(self.owner, second.agent_id, "niannian")
        self.archive.import_history(self.all_events())
        self.assertEqual(other.list_events("niannian"), [])
        self.registry.grant_source(self.owner, self.agent.agent_id, "rikkahub-app")
        reader = NiannianRawReader(self.raw, "rikkahub-app", ["rikkahub"])
        self.archive.import_history(reader.read_page()["events"])
        events = self.archive.export_history("rikkahub-app")
        self.assertTrue(all(json.loads(e["source_event_id"])[0] == "rikkahub" for e in events))
        same_ids = [e for e in self.archive.export_history("niannian")
                    if json.loads(e["source_event_id"])[-1] == "event-1"]
        self.assertEqual(len(same_ids), 2)
        self.assertNotEqual(same_ids[0]["source_event_id"], same_ids[1]["source_event_id"])

    def test_export_import_preserves_portable_references(self):
        self.archive.import_history(self.all_events())
        portable = json.loads(json.dumps(self.archive.export_history("niannian", include_deleted=True)))
        target = IdentityRegistry(Path(self.temp.name) / "portable")
        agent = target.register_agent(self.owner, "同名 Agent")
        target.grant_source(self.owner, agent.agent_id, "niannian")
        imported = Archive(target, self.owner, agent.agent_id)
        imported.import_history(portable)
        self.assertEqual(imported.export_history("niannian", include_deleted=True), portable)

    def test_readonly_uri_missing_file_and_no_source_mutation(self):
        before = self.raw.read_bytes()
        self.all_events()
        self.reader.recheck_deleted([1, 5])
        self.assertEqual(self.raw.read_bytes(), before)
        with closing(self.reader._connect()) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE raw_events SET text='changed'")
        missing = Path(self.temp.name) / "missing.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            NiannianRawReader(missing, "niannian", ["app"]).read_page()
        self.assertFalse(missing.exists())

    def test_no_edit_or_physical_delete_feed_is_claimed(self):
        self.archive.import_history(self.all_events())
        with closing(sqlite3.connect(self.raw)) as db, db:
            db.execute("UPDATE raw_events SET text='合成编辑' WHERE id=1")
            db.execute("DELETE FROM raw_events WHERE id=3")
        self.assertEqual(self.reader.read_page(after_id=10)["events"], [])
        self.assertEqual(self.reader.recheck_deleted([1, 3]), [])
        edited = self.reader.read_page(limit=1)["events"][0]
        self.assertEqual(edited["version"], 1)
        with self.assertRaises(VersionConflict):
            self.archive.upsert(edited)

    def test_other_source_reliable_feed_uses_p2_c_for_old_edit_and_delete(self):
        self.registry.grant_source(self.owner, self.agent.agent_id, "reliable-feed")
        sync = HistorySync(self.registry, self.owner, self.agent.agent_id)
        event = dict(self.all_events()[0], app="reliable-feed", change_cursor=1)
        sync.sync_page("reliable-feed", "main", 0, 1, [event])
        sync.sync_page("reliable-feed", "main", 1, 2,
                       [dict(event, change_cursor=2, version=2, text="合成编辑")])
        sync.sync_page("reliable-feed", "main", 2, 3,
                       [dict(event, change_cursor=3, version=3, deleted=True, text=None)])
        final = sync.archive.export_history("reliable-feed", include_deleted=True)[0]
        self.assertIsNone(final["text"])
        self.assertEqual(final["version"], 3)
        self.assertEqual(sync.get_cursor("reliable-feed"), 3)
        self.assertEqual(sync.get_cursor("niannian"), 0)

    def test_invalid_parameters_and_malformed_metadata_fail_explicitly(self):
        for kwargs in ({"after_id": -1}, {"after_id": True}, {"limit": 0}):
            with self.assertRaises(ValueError):
                self.reader.read_page(**kwargs)
        with self.assertRaises(ValueError):
            self.reader.recheck_deleted([False])
        self.insert(11, metadata_json="malformed")
        with self.assertRaises(sqlite3.OperationalError):
            self.reader.read_page(after_id=10)


if __name__ == "__main__":
    unittest.main()
