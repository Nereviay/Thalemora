"""Scene → Gatekeeper 新卡时间的纯合成集成验收，不读取生产资产。"""
from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from thalemora.gatekeeper import core, ops
from thalemora.memory import evidence_resolver, schema
from thalemora.scene import candidates, runtime


EVENT_TIME = "2001-02-03T04:05:06.123456+08:00"
QUOTE = "合成星图项目采用蓝色标记"
PROFILE = dict(model="synthetic-gate", thinking_enabled=False, reasoning_effort=None)


class SceneGatekeeperTimeIntegrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="p1_scene_gate_time_")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.raw = base / "archive.sqlite"
        self.history = base / "history.sqlite"
        self.memory = base / "memory.sqlite"
        self.ops = base / "ops.sqlite"
        # 仅构造消费所需列；正文、hash、epoch 和时间均为合成值。
        with self.connect(self.raw) as connection:
            connection.execute(
                "CREATE TABLE raw_events(id INTEGER PRIMARY KEY,source TEXT,"
                "source_event_id TEXT,event_hash TEXT,role TEXT,text TEXT,"
                "created_at TEXT,metadata_json TEXT,deleted INTEGER)"
            )
            connection.execute(
                "INSERT INTO raw_events VALUES(1,?,?,?,?,?,?,?,0)",
                ("app", "synthetic-event", "synthetic-hash", "user", "合成包装",
                 EVENT_TIME, json.dumps({"authored_text": QUOTE})),
            )
        with self.connect(self.history) as connection:
            connection.execute(
                "CREATE TABLE message_records(raw_event_id INTEGER,epoch_id TEXT,status TEXT)"
            )
            connection.execute(
                "INSERT INTO message_records VALUES(1,'synthetic-epoch','active')"
            )
        with self.connect(self.memory) as connection:
            schema.initialize(connection)
        engine = runtime.SceneRuntime(
            db_path=str(base / "scene.sqlite"), raw_db=str(self.raw),
            runtime_db=str(self.history), embedder=None,
        )
        self.assertEqual(1, engine.sync(now=dt.datetime.fromisoformat(EVENT_TIME))["processed_events"])
        self.store = engine.store
        scene_id = self.store.list_scenes()[0]["scene_id"]
        self.store.finalize(scene_id)
        self.scene = self.store.get_scene(scene_id)

    @staticmethod
    @contextmanager
    def connect(path):
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def enqueue(self, **time_fields):
        route = dict(
            route="memory", title="合成项目偏好", content=QUOTE,
            keywords=["星图项目"], importance=6, memory_type="preference",
            identity="synthetic-preference", reason="synthetic-durable",
            claim_scope="user_fact", evidence=[dict(event_id=1, quote=QUOTE)],
        )
        route.update(time_fields)
        return candidates.enqueue_candidates(
            [route], scene_store=self.store, scene_id=self.scene["scene_id"],
            expected_revision_no=self.scene["current_revision_no"],
            raw_db_path=str(self.raw), ops_db_path=str(self.ops),
            batch_key="synthetic-time-batch", extractor_model="synthetic-extractor",
            gate_previews=[dict(
                candidate_index=0, action="new",
                decision_summary=dict(action="new", long_term_value=True),
                frozen_memory_refs=[],
            )],
        )

    def apply(self, candidate_id, **changes):
        arguments = dict(
            ops_db_path=str(self.ops), memory_db_path=str(self.memory),
            raw_db_path=str(self.raw), profile=PROFILE,
            provider=lambda _: dict(
                action="new", content=QUOTE, title="合成项目偏好",
                keywords=["星图项目"], category="项目", importance=6,
                long_term_value=True,
            ),
        )
        arguments.update(changes)
        return core.apply_candidate(candidate_id, **arguments)

    def assert_landed(self, candidate_id, expected_time):
        result, status = self.apply(candidate_id)
        self.assertEqual((200, "applied", "new"), (status, result["status"], result["action"]))
        candidate = ops.get_candidate(candidate_id, self.ops)
        payload = json.loads(candidate["payload_json"])
        self.assertEqual("applied", candidate["status"])
        self.assertEqual("verified", payload["evidence_state"])
        self.assertEqual(expected_time, payload["happened_at"])
        self.assertEqual(EVENT_TIME, self.scene["events"][0]["event_time"])
        with self.connect(self.memory) as connection:
            card = connection.execute("SELECT * FROM cards WHERE id=?", (result["card_id"],)).fetchone()
            revision = connection.execute(
                "SELECT * FROM card_revisions WHERE id=?", (card["current_revision_id"],)
            ).fetchone()
            self.assertEqual(expected_time, card["happened_at"])
            self.assertEqual(expected_time, revision["happened_at"])
            self.assertEqual((card["id"], candidate_id, 1, "create"),
                             (revision["card_id"], revision["candidate_id"],
                              revision["revision_no"], revision["change_type"]))
            evidence = connection.execute(
                "SELECT * FROM card_evidence WHERE revision_id=?", (revision["id"],)
            ).fetchall()
            self.assertEqual(1, len(evidence))
            for key, value in payload["verified_evidence"][0].items():
                if key != "event_id":
                    self.assertEqual(value, evidence[0][key], key)
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
        resolved = evidence_resolver.resolve_card_evidence(
            result["card_id"], memory_db_path=self.memory, raw_db_path=self.raw,
        )
        self.assertEqual(1, len(resolved))
        self.assertEqual((True, QUOTE, EVENT_TIME),
                         (resolved[0]["verified"], resolved[0]["quote"], resolved[0]["created_at"]))
        return result

    def test_explicit_time_preserves_offset_precision_and_backdate(self):
        candidate_id = self.enqueue(happened_at=EVENT_TIME)["candidate_ids"][0]
        self.assert_landed(candidate_id, EVENT_TIME)

    def test_candidate_time_is_not_replaced_by_scene_event_time(self):
        happened_at = "1999-12-31T23:59:59.987654-05:00"
        candidate_id = self.enqueue(happened_at=happened_at)["candidate_ids"][0]
        self.assert_landed(candidate_id, happened_at)

    def test_missing_time_stays_null_despite_dated_evidence(self):
        candidate_id = self.enqueue()["candidate_ids"][0]
        self.assert_landed(candidate_id, None)

    def test_explicit_null_time_stays_null(self):
        candidate_id = self.enqueue(happened_at=None)["candidate_ids"][0]
        self.assert_landed(candidate_id, None)

    def test_candidate_and_gate_replay_preserve_time_and_all_rows(self):
        first = self.enqueue(happened_at=EVENT_TIME)
        self.assertTrue(first["inserted"])
        candidate_id = first["candidate_ids"][0]
        result = self.assert_landed(candidate_id, EVENT_TIME)
        with self.connect(self.memory) as connection:
            before = {table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
                      for table in ("cards", "card_revisions", "card_evidence")}
        second = self.enqueue(happened_at=EVENT_TIME)
        self.assertFalse(second["inserted"])
        self.assertEqual(first["candidate_ids"], second["candidate_ids"])
        replay, status = self.apply(
            candidate_id, provider=lambda _: self.fail("重放不应再次调用 provider"),
        )
        self.assertEqual((200, True, result["card_id"]),
                         (status, replay["idempotent"], replay["card_id"]))
        with self.connect(self.memory) as connection:
            after = {table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
                     for table in before}
        self.assertEqual(before, after)
        self.assertTrue(all(len(rows) == 1 for rows in after.values()))


if __name__ == "__main__":
    unittest.main()
