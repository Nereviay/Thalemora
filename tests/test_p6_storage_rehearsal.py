"""P6-2 合成验收；不读取服务器正式路径或真实数据。"""
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
import tempfile
import unittest

from thalemora.memory import schema
from thalemora.memory.revision_store import create_card
from thalemora.self_model.contract import SCHEMA_VERSION, namespace_for_principal
from thalemora.self_model.store import SQLiteSelfModelStore
from thalemora.water import schema as water_schema
from tools.niannian_storage_rehearsal import DATABASES, compare, rehearse, snapshot


class StorageRehearsalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="p6_storage_synthetic_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in DATABASES:
            with sqlite3.connect(self.source / name) as db:
                db.execute("CREATE TABLE legacy_rows(id INTEGER PRIMARY KEY,payload BLOB)")
                db.execute("INSERT INTO legacy_rows VALUES(7,?)", (b"synthetic-old-data",))
        with sqlite3.connect(self.source / "memory.db") as db:
            schema.initialize(db)
            water_schema.initialize(db)
            db.execute("CREATE TABLE diary_entries(id INTEGER PRIMARY KEY,content TEXT)")
            db.execute("INSERT INTO diary_entries VALUES(99,'合成旧日记')")
        create_card(self.source / "memory.db", content="合成人工卡", source="manual",
                    origin="manual_ui", evidence_source_kind="manual",
                    evidence_source_ref="synthetic-manual")
        with closing(sqlite3.connect(self.source / "self_model.sqlite",
                                     isolation_level=None)) as db:
            SQLiteSelfModelStore(db).initialize()
            db.execute("INSERT INTO self_model_namespaces VALUES(?,?,?,?)",
                       (namespace_for_principal("synthetic-agent"), "synthetic-agent",
                        SCHEMA_VERSION, "2001-01-01T00:00:00Z"))
        (self.source / "assistant_channel.sqlite").write_bytes(b"excluded-synthetic-channel")
        (self.source / "prompt.json").write_text("excluded-synthetic-prompt")

    def test_snapshot_includes_wal_and_excludes_private_files(self):
        with closing(sqlite3.connect(self.source / "memory_ops.sqlite")) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO legacy_rows VALUES(8,?)", (b"synthetic-wal",))
            writer.commit()
            report = snapshot(self.source, self.root / "snapshot")
            self.assertEqual(2, report["databases"]["memory_ops.sqlite"]["tables"]["legacy_rows"])
            self.assertEqual(2, writer.execute("SELECT COUNT(*) FROM legacy_rows").fetchone()[0])
        self.assertFalse(report["cross_database_atomic"])
        self.assertFalse((self.root / "snapshot/assistant_channel.sqlite").exists())
        self.assertFalse((self.root / "snapshot/prompt.json").exists())
        self.assertEqual(0o700, os.stat(self.root / "snapshot").st_mode & 0o777)
        self.assertEqual(0o600, os.stat(self.root / "snapshot/memory.db").st_mode & 0o777)

    def test_core_reads_preserve_all_rows_manual_identity_and_legacy(self):
        report = rehearse(self.source, self.root / "work")
        self.assertEqual("PASS", report["status"])
        self.assertFalse(report["production_ready"])
        self.assertEqual(1, report["core"]["memory"]["manual_read_only"])
        self.assertTrue(report["core"]["self_model"]["identity_preserved"])
        self.assertTrue(all(r["unchanged"] for r in report["preservation"].values()))
        with sqlite3.connect(self.root / "work/memory.db") as db:
            self.assertEqual((99, "合成旧日记"), db.execute("SELECT * FROM diary_entries").fetchone())
        self.assertFalse((self.root / "work/inner.sqlite").exists())

    def test_existing_target_never_overwritten(self):
        target = self.root / "existing"
        target.mkdir()
        (target / "keep").write_text("protected")
        with self.assertRaises(FileExistsError):
            snapshot(self.source, target)
        self.assertEqual("protected", (target / "keep").read_text())

    def test_missing_source_never_creates_empty_database(self):
        (self.source / "runtime.sqlite").unlink()
        with self.assertRaises(FileNotFoundError):
            snapshot(self.source, self.root / "missing")
        self.assertFalse((self.root / "missing").exists())
        self.assertFalse((self.source / "runtime.sqlite").exists())

    def test_equal_counts_do_not_hide_changed_records(self):
        snapshot(self.source, self.root / "changed")
        with sqlite3.connect(self.root / "changed/memory_ops.sqlite") as db:
            db.execute("UPDATE legacy_rows SET payload=? WHERE id=7", (b"changed",))
        with self.assertRaisesRegex(ValueError, "记录改变"):
            compare(self.source / "memory_ops.sqlite", self.root / "changed/memory_ops.sqlite")

    def test_identity_failure_does_not_emit_pass(self):
        with sqlite3.connect(self.source / "self_model.sqlite") as db:
            # 旧表 append-only trigger 禁止改写，构造另一个不匹配的 namespace。
            db.execute("INSERT INTO self_model_namespaces VALUES(?,?,?,?)",
                       ("wrong-namespace", "other-agent", SCHEMA_VERSION, "2001-01-01"))
        with self.assertRaisesRegex(ValueError, "principal/namespace"):
            rehearse(self.source, self.root / "failed")
        self.assertFalse((self.root / "failed/compatibility.json").exists())


if __name__ == "__main__":
    unittest.main()
