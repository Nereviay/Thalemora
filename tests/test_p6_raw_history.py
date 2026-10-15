"""P6-3 旧 RawEvent 持续更新的纯合成验收。"""
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from thalemora.adapters.niannian_history import NiannianHistoryBinding, sync_once
from thalemora.adapters.niannian_raw import NiannianRawReader
from thalemora.adapters.niannian_raw_feed import RawHistoryReader, install_raw
from thalemora.history import HistorySync
from thalemora.identity import OwnerPrincipal


class RawHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="p6_raw_feed_")
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/"raw.sqlite"
        self.db=sqlite3.connect(self.path);self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE raw_events(id INTEGER PRIMARY KEY,source TEXT,source_event_id TEXT,"
                        "event_hash TEXT,role TEXT,text TEXT,created_at TEXT,conversation_id TEXT,"
                        "metadata_json TEXT,deleted INTEGER)")
        self.db.commit()
        self.owner=OwnerPrincipal("synthetic-owner")
        self.binding=NiannianHistoryBinding(archive_path=self.root/"archive.sqlite",owner=self.owner,
            agent_id="existing-subject",apps=["niannian-legacy"])
        self.sync=HistorySync(self.binding,self.owner,"existing-subject")
        self.reader=RawHistoryReader(self.path,"niannian-legacy")

    def add(self,event="one",channel="main",deleted=0):
        self.db.execute("INSERT INTO raw_events(source,source_event_id,event_hash,role,text,created_at,"
                        "conversation_id,metadata_json,deleted) VALUES('app',?,'old-hash','user',"
                        "'synthetic source text','2001-01-01','legacy-conversation',?,?)",
                        (event,json.dumps({"channel":channel}),deleted))
        self.db.commit()

    def drain(self):
        while sync_once(self.sync,self.reader,limit=2)["status"]!="idle":pass
        return self.sync.archive.list_events("niannian-legacy",include_deleted=True)

    def test_snapshot_versions_upgrade_and_edit_delete_restore_purge(self):
        self.add()
        event=NiannianRawReader(self.path,"niannian-legacy",["app"]).read_page()["events"][0]
        self.sync.archive.upsert(event)
        install_raw(self.db)
        row=self.drain()[0]
        self.assertGreater(row["version"],2)
        self.assertEqual("legacy-conversation",row["conversation_id"])
        self.db.execute("UPDATE raw_events SET text='synthetic edit'");self.db.commit()
        self.assertEqual("synthetic edit",self.drain()[0]["text"])
        self.db.execute("UPDATE raw_events SET deleted=1");self.db.commit()
        self.assertIsNone(self.drain()[0]["text"])
        self.db.execute("UPDATE raw_events SET deleted=0");self.db.commit()
        self.assertFalse(self.drain()[0]["deleted"])
        self.db.execute("DELETE FROM raw_events");self.db.commit()
        self.assertTrue(self.drain()[0]["deleted"])

    def test_private_scope_and_scope_revocation(self):
        self.add("private",channel="private");self.add("main")
        install_raw(self.db)
        self.assertEqual(1,len(self.drain()))
        self.db.execute("UPDATE raw_events SET metadata_json=? WHERE source_event_id='main'",
                        (json.dumps({"channel":"private"}),));self.db.commit()
        self.assertIsNone(self.drain()[0]["text"])

    def test_rollback_restart_and_reinstall(self):
        install_raw(self.db);self.add()
        before=self.reader.page()["next_cursor"]
        self.db.execute("UPDATE raw_events SET text='uncommitted'");self.db.rollback()
        self.assertEqual(before,self.reader.page()["next_cursor"])
        self.drain()
        self.assertFalse(install_raw(self.db))
        self.sync=HistorySync(self.binding,self.owner,"existing-subject")
        self.assertEqual("idle",sync_once(self.sync,self.reader)["status"])

    def test_deleted_bootstrap_never_copies_body_into_feed(self):
        self.add(deleted=1);install_raw(self.db)
        self.assertIsNone(self.db.execute("SELECT text FROM thalemora_raw_history_changes").fetchone()[0])
        self.assertTrue(self.drain()[0]["deleted"])


if __name__=="__main__":unittest.main()
