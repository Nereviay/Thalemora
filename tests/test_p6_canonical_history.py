"""P6-3 canonical 变更源到 Archive 的合成端到端；不访问正式 Runtime。"""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from thalemora.adapters.niannian_history import CanonicalHistoryReader, install, sync_once
from thalemora.history import HistorySync
from thalemora.identity import IdentityRegistry, OwnerPrincipal


class CanonicalHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="p6_canonical_feed_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "runtime.sqlite"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript("""
          CREATE TABLE context_epochs(epoch_id TEXT PRIMARY KEY,status TEXT);
          CREATE TABLE message_records(
            id INTEGER PRIMARY KEY,event_id TEXT UNIQUE,epoch_id TEXT,role TEXT,
            content TEXT,content_sha256 TEXT,raw_event_id INTEGER,
            source_created_at TEXT,first_seen_at TEXT,source TEXT,status TEXT,
            metadata_json TEXT,kind TEXT,last_seen_at TEXT);
          INSERT INTO context_epochs VALUES('synthetic-epoch','active');
        """)
        self.registry = IdentityRegistry(self.root / "thalemora")
        self.owner = OwnerPrincipal("synthetic-owner")
        self.agent = self.registry.register_agent(self.owner, "synthetic-agent")
        self.registry.grant_source(self.owner, self.agent.agent_id, "niannian")
        self.sync = HistorySync(self.registry, self.owner, self.agent.agent_id)

    def add(self, event="one", *, role="user", channel="main", kind="message"):
        self.db.execute(
            "INSERT INTO message_records(event_id,epoch_id,role,content,content_sha256,"
            "raw_event_id,source_created_at,first_seen_at,source,status,metadata_json,kind)"
            "VALUES(?,'synthetic-epoch',?,'合成正文','legacy-hash',1,NULL,"
            "'2001-01-01T00:00:00Z','chat_submit','active',?,?)",
            (event, role, json.dumps({"channel": channel}), kind),
        )
        self.db.commit()

    def reader(self):
        return CanonicalHistoryReader(self.path, "niannian")

    def drain(self, limit=2):
        while sync_once(self.sync, self.reader(), limit=limit)["status"] != "idle":
            pass
        return {r["source_event_id"]: r for r in self.sync.archive.list_events(
            "niannian", include_deleted=True)}

    def test_bootstrap_then_edit_replace_delete_restore_and_purge(self):
        self.add()
        self.assertTrue(install(self.db))
        self.assertEqual("合成正文", self.drain()["canonical:one"]["text"])
        self.db.execute("UPDATE message_records SET content='合成更正' WHERE event_id='one'")
        self.db.commit()
        self.assertEqual("合成更正", self.drain()["canonical:one"]["text"])
        self.db.execute("UPDATE message_records SET status='superseded' WHERE event_id='one'")
        self.db.commit()
        self.add("two")
        rows = self.drain()
        self.assertTrue(rows["canonical:one"]["deleted"])
        self.assertFalse(rows["canonical:two"]["deleted"])
        self.db.execute("UPDATE message_records SET status='deleted' WHERE event_id='two'")
        self.db.commit()
        self.assertIsNone(self.drain()["canonical:two"]["text"])
        self.db.execute("UPDATE message_records SET status='active' WHERE event_id='two'")
        self.db.commit()
        self.assertFalse(self.drain()["canonical:two"]["deleted"])
        self.db.execute("DELETE FROM message_records WHERE event_id='two'")
        self.db.commit()
        self.assertTrue(self.drain()["canonical:two"]["deleted"])

    def test_epoch_delete_restore_and_physical_purge(self):
        install(self.db)
        self.add()
        self.drain()
        self.db.execute("UPDATE context_epochs SET status='deleted'")
        self.db.commit()
        self.assertTrue(self.drain()["canonical:one"]["deleted"])
        self.db.execute("UPDATE context_epochs SET status='active'")
        self.db.commit()
        self.assertFalse(self.drain()["canonical:one"]["deleted"])
        self.db.execute("DELETE FROM message_records")
        self.db.execute("UPDATE context_epochs SET status='purged'")
        self.db.commit()
        self.assertTrue(self.drain()["canonical:one"]["deleted"])

    def test_transaction_rollback_never_publishes_uncommitted_change(self):
        install(self.db)
        self.add()
        before = self.reader().page()["next_cursor"]
        self.db.execute("UPDATE message_records SET content='未提交正文'")
        self.db.rollback()
        self.assertEqual(before, self.reader().page()["next_cursor"])
        self.assertEqual("合成正文", self.drain()["canonical:one"]["text"])

    def test_scope_filters_and_main_to_private_revokes_archive(self):
        install(self.db)
        self.add("private", channel="private")
        self.add("tool", role="tool_done")
        self.add("no-response", role="assistant", kind="no_response")
        self.assertEqual([], self.reader().page()["events"])
        self.add("main")
        self.drain()
        self.db.execute("UPDATE message_records SET metadata_json=? WHERE event_id='main'",
                        (json.dumps({"channel": "private"}),))
        self.db.commit()
        row = self.drain()["canonical:main"]
        self.assertTrue(row["deleted"])
        self.assertIsNone(row["text"])

    def test_restart_replay_and_install_are_idempotent(self):
        self.add()
        install(self.db)
        page = self.reader().page()
        self.sync.sync_page("niannian", "main", 0, page["next_cursor"], page["events"])
        self.assertEqual("already_committed", self.sync.sync_page(
            "niannian", "main", 0, page["next_cursor"], page["events"])["status"])
        self.assertFalse(install(self.db))
        self.sync = HistorySync(self.registry, self.owner, self.agent.agent_id)
        self.assertEqual("idle", sync_once(self.sync, self.reader())["status"])
        self.assertEqual(1, len(self.drain()))

    def test_noop_and_nonarchive_fields_do_not_emit_changes(self):
        install(self.db)
        self.add()
        before = self.reader().page()["next_cursor"]
        self.db.execute("UPDATE message_records SET content=content,last_seen_at='later'")
        self.db.commit()
        self.assertEqual(before, self.reader().page()["next_cursor"])
        plan = self.db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM thalemora_history_changes "
            "WHERE seq>? ORDER BY seq LIMIT ?", (before, 100)).fetchall()
        self.assertIn("INTEGER PRIMARY KEY", str(plan))

    def test_deleted_epoch_bootstrap_and_authored_text(self):
        self.add()
        self.db.execute("UPDATE context_epochs SET status='deleted'")
        self.db.commit()
        install(self.db)
        self.assertTrue(self.drain()["canonical:one"]["deleted"])
        self.db.execute("UPDATE context_epochs SET status='active'")
        self.db.execute("UPDATE message_records SET metadata_json=?",
                        (json.dumps({"authored_text": "合成作者原话"}),))
        self.db.commit()
        row = self.drain()["canonical:one"]
        self.assertEqual("合成作者原话", row["text"])
        self.assertEqual("unknown", row["hash_verification"])

    def test_install_contract_failure_does_not_create_feed(self):
        with closing(sqlite3.connect(":memory:")) as bad:
            with self.assertRaisesRegex(ValueError, "合同不匹配"):
                install(bad)
            self.assertFalse(bad.execute(
                "SELECT 1 FROM sqlite_master WHERE name='thalemora_history_changes'").fetchall())


if __name__ == "__main__":
    unittest.main()
