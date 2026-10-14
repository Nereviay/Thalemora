"""短词原话检索：仅使用临时库和合成档案，不调用模型。"""

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from thalemora.app import Application
from thalemora.app.http import create_http
from thalemora.history import Archive, HistorySync, VersionConflict
from thalemora.identity import AccessDenied


class ShortSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.service = Application(self.root)
        self.agent = self.service.register_agent("合成 Agent")["agent_id"]
        self.app = "synthetic-app"
        self.service.grant_source(self.agent, self.app)
        # 先通过 P2 创建档案；尚未进行应用索引接线。
        self.archive = Archive(self.service.registry, self.service.owner, self.agent)

    def event(self, **updates):
        return dict(dict(app=self.app, channel="main", source_event_id="e01",
                         version=1, text="头部旅行计划 ABcd 🙂🚀 末尾", deleted=False), **updates)

    def search(self, query, **options):
        return self.service.search(self.agent, self.app, query, **options)

    def ids(self, query, **options):
        return [row["source_event_id"] for row in self.search(query, **options)]

    def test_one_two_char_chinese_english_emoji_and_literal_symbols(self):
        text = "前缀旅旅行计划 ABcd 🙂🚀 e\u0301 İİ _%\" 后缀"
        self.archive.upsert(self.event(text=text))
        for query in ("旅", "旅行", "划", "计划", "A", "a", "ab", "Bc", "d",
                      "🙂", "🚀", "🙂🚀", "e\u0301", "İİ", "_", "%", "_%", '"'):
            with self.subTest(query=query):
                self.assertEqual(self.ids(query), ["e01"])
        for query in ("行旅", "ac", "🚀🙂", "缺", "?", "%%"):
            with self.subTest(query=query):
                self.assertEqual(self.ids(query), [])

    def test_long_queries_still_use_fts_and_trim_validation(self):
        self.archive.upsert(self.event())
        self.assertEqual(self.ids("旅行计划"), ["e01"])
        statements = []
        connect = sqlite3.connect
        def traced_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch("thalemora.app.service.sqlite3.connect", side_effect=traced_connect):
            self.assertEqual(self.ids("ABc"), ["e01"])
        self.assertTrue(any("history_fts MATCH" in sql for sql in statements))
        self.assertFalse(any("FROM history_short_grams s" in sql for sql in statements))
        self.assertEqual(self.ids(" 旅 "), ["e01"])
        for query in ("", "   ", "字" * 201, None, 1):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.search(query)

    def test_existing_fts_gets_short_backfill_once_and_survives_restart(self):
        self.archive.import_history([
            self.event(), self.event(source_event_id="null", text=None),
            self.event(source_event_id="deleted", text="旅", deleted=True),
        ])
        self.service._lexical_index(self.archive.path)
        with closing(sqlite3.connect(self.archive.path)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='history_short_grams'").fetchone())
        self.assertEqual(self.ids("旅"), ["e01"])
        restarted = Application(self.root)
        with patch.object(Archive, "_update_short_index", side_effect=AssertionError("不应重建")):
            self.assertEqual(restarted.search(self.agent, self.app, "旅行")[0]["source_event_id"], "e01")
        with closing(sqlite3.connect(self.archive.path)) as db:
            self.assertEqual(db.execute("SELECT DISTINCT source_event_id FROM history_short_grams").fetchall(), [("e01",)])

    def test_direct_p2_import_edit_delete_and_version_rules(self):
        self.search("旅")  # 首次接线创建空索引。
        self.assertEqual(self.archive.upsert(self.event())["status"], "inserted")
        self.assertEqual(self.ids("旅"), ["e01"])
        self.archive.upsert(self.event(version=2, text="更新城市漫步 XY 🚀"))
        self.assertEqual(self.ids("旅"), [])
        self.assertEqual(self.ids("城市"), ["e01"])
        self.assertEqual(self.ids("xy"), ["e01"])
        self.assertEqual(self.archive.upsert(self.event())["status"], "ignored_stale")
        self.assertEqual(self.archive.upsert(self.event(version=2, text="更新城市漫步 XY 🚀"))["status"], "unchanged")
        with self.assertRaises(VersionConflict):
            self.archive.upsert(self.event(version=2, text="冲突旅"))
        self.assertEqual(self.ids("旅"), [])
        self.archive.upsert(self.event(version=3, deleted=True))
        self.assertEqual(self.ids("城"), [])
        self.assertEqual(self.ids("城市漫步"), [])
        with closing(sqlite3.connect(self.archive.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM history_short_grams").fetchone()[0], 0)
        self.archive.upsert(self.event(version=4))
        self.assertEqual(self.ids("旅行"), ["e01"])

    def test_p2_sync_partial_commit_replay_and_tombstone(self):
        self.search("旅")
        sync = HistorySync(self.service.registry, self.service.owner, self.agent)
        sync.sync_page(self.app, "main", 0, 1, [self.event(change_cursor=1)])
        with self.assertRaises(VersionConflict):
            sync.sync_page(self.app, "main", 1, 3, [
                self.event(source_event_id="partial", text="保留", change_cursor=2),
                self.event(text="冲突", change_cursor=3),
            ])
        self.assertEqual(self.ids("保留"), ["partial"])
        self.assertEqual(sync.get_cursor(self.app), 1)
        self.assertEqual(self.ids("旅行"), ["e01"])
        sync.sync_page(self.app, "main", 1, 3, [
            self.event(source_event_id="partial", text="保留", change_cursor=2),
            self.event(version=2, text="城市漫步", change_cursor=3),
        ])
        self.assertEqual(self.ids("城市"), ["e01"])
        sync.sync_page(self.app, "main", 3, 4, [self.event(version=3, deleted=True, change_cursor=4)])
        self.assertEqual(self.ids("旅"), [])
        self.assertEqual(self.ids("城"), [])

    def test_index_write_failure_rolls_back_archive_and_fts(self):
        self.archive.upsert(self.event())
        self.search("旅")
        update = Archive._update_short_index
        def fail_after_write(event, db):
            update(event, db)
            raise RuntimeError("合成事务故障")
        with patch.object(Archive, "_update_short_index", side_effect=fail_after_write):
            with self.assertRaises(RuntimeError):
                self.archive.upsert(self.event(version=2, text="城市漫步"))
        self.assertEqual(self.ids("旅"), ["e01"])
        self.assertEqual(self.ids("旅行计划"), ["e01"])
        self.assertEqual(self.ids("城"), [])

    def test_backfill_failure_is_atomic(self):
        self.archive.upsert(self.event())
        with patch.object(Archive, "_update_short_index", side_effect=RuntimeError("合成回填故障")):
            with self.assertRaises(RuntimeError):
                self.archive.ensure_short_index()
        with closing(sqlite3.connect(self.archive.path)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='history_short_grams'").fetchone())
        self.assertEqual(self.ids("旅"), ["e01"])

    def test_agent_app_private_and_revoked_source_isolation(self):
        other = self.service.register_agent("另一合成 Agent")["agent_id"]
        self.service.grant_source(other, self.app)
        self.service.grant_source(self.agent, "other-app")
        self.service.grant_source(self.agent, self.app, "private")
        self.archive.import_history([
            self.event(text="共同主域"), self.event(channel="private", text="共同私域"),
            self.event(app="other-app", text="共同别域"),
        ])
        Archive(self.service.registry, self.service.owner, other).upsert(self.event(text="共同他人"))
        self.assertEqual([r["text"] for r in self.search("共同")], ["共同主域"])
        self.assertEqual(self.search("共同", channel="private")[0]["text"], "共同私域")
        self.assertEqual(self.service.search(self.agent, "other-app", "共同")[0]["text"], "共同别域")
        self.assertEqual(self.service.search(other, self.app, "共同")[0]["text"], "共同他人")
        with self.assertRaises(AccessDenied):
            self.service.search(other, self.app, "共", "private")
        with closing(sqlite3.connect(self.service.registry.registry_path)) as db, db:
            db.execute("DELETE FROM sources WHERE agent_id=? AND app_id=? AND channel='private'", (self.agent, self.app))
        with self.assertRaises(AccessDenied):
            self.search("共", channel="private")

    def test_deduplicated_grams_ordered_pages_and_limits(self):
        self.archive.import_history([self.event(source_event_id=f"e{i:03}", text="旅" * 20) for i in reversed(range(105))])
        expected = [f"e{i:03}" for i in range(105)]
        for query in ("旅", "旅旅"):
            pages = [self.ids(query, limit=20, offset=offset) for offset in range(0, 120, 20)]
            self.assertEqual([event_id for page in pages for event_id in page], expected)
            self.assertEqual(self.ids(query, offset=105), [])
            self.assertEqual(self.ids(query, limit=100), expected[:100])
        for options in (dict(limit=101), dict(limit=0), dict(offset=-1), dict(offset=True)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.ids("旅", **options)

    def test_actual_short_query_plan_uses_btree_without_history_scan(self):
        self.archive.upsert(self.event())
        self.search("旅")
        statements = []
        connect = sqlite3.connect
        def traced_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch("thalemora.app.service.sqlite3.connect", side_effect=traced_connect), \
                patch.object(Archive, "_update_short_index", side_effect=AssertionError("不应重建")):
            self.assertEqual(self.ids("旅行"), ["e01"])
        query = next(sql for sql in statements if "FROM history_short_grams s" in sql)
        with closing(connect(self.archive.path)) as db:
            plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query)]
            update_plan = [row[3] for row in db.execute(
                "EXPLAIN QUERY PLAN DELETE FROM history_short_grams WHERE app=? AND channel=? AND source_event_id=?",
                (self.app, "main", "e01"))]
        self.assertTrue(any("SEARCH s USING PRIMARY KEY" in line for line in plan), plan)
        self.assertTrue(any("SEARCH h USING INDEX" in line for line in plan), plan)
        self.assertFalse(any("SCAN" in line or "TEMP B-TREE" in line for line in plan), plan)
        self.assertTrue(any("history_short_event" in line for line in update_plan), update_plan)
        self.assertFalse(any("WITH RECURSIVE" in sql.upper() for sql in statements))
        self.assertFalse(any("BEGIN IMMEDIATE" in sql.upper() for sql in statements))

    def test_large_text_backfill_and_upsert_without_sql_recursion(self):
        text = "长文 AB🙂🚀" * 10000 + "结尾"
        self.archive.upsert(self.event(text=text))
        self.assertEqual(self.ids("结尾"), ["e01"])
        self.archive.upsert(self.event(version=2, text=text + "新字"))
        self.assertEqual(self.ids("新字"), ["e01"])
        with closing(sqlite3.connect(self.archive.path)) as db:
            count = db.execute("SELECT count(*) FROM history_short_grams").fetchone()[0]
        self.assertLess(count, 40)

    def test_http_short_queries_and_ui_prompt(self):
        self.archive.upsert(self.event())
        with TestClient(create_http(self.service, "synthetic-token")) as client:
            for query in ("旅", "旅行", "🙂", "🙂🚀"):
                response = client.get("/api/search", params=dict(agent_id=self.agent, app_id=self.app, query=query),
                                      headers={"Authorization": "Bearer synthetic-token"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["result"][0]["source_event_id"], "e01")
            self.assertIn("历史原文检索（1–200 字符）", client.get("/app.js").text)


if __name__ == "__main__":
    unittest.main()
