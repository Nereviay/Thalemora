"""09-3 隔离合同。只用临时 SQLite、确定的 Gate 答复和可撤回的宿主来源。"""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from thalemora.gatekeeper import core, ops
from thalemora.adapters.niannian_memory import NiannianGatekeeper
from thalemora.memory import event_state, revision_store, schema


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


@contextmanager
def connect(path):
    with closing(sqlite3.connect(path)) as db, db:
        yield db


class EventStateTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.memory, self.raw, self.ops = (root / name for name in ("memory", "raw", "ops"))
        self.history = root / "history"
        with connect(self.memory) as db:
            schema.initialize(db)
        with connect(self.raw) as db:
            db.execute("CREATE TABLE raw_events(id INTEGER PRIMARY KEY, source, source_event_id, "
                       "event_hash, role, text, created_at, metadata_json, deleted)")
        with connect(self.history) as db:
            db.execute("CREATE TABLE message_records(raw_event_id, event_id, role, content, "
                       "kind, status, channel, sensitive)")

    def public_user_text(self, raw_id, source, source_event_id):
        if source != "app":
            return None
        with connect(self.history) as db:
            rows = db.execute("SELECT content FROM message_records WHERE raw_event_id=? AND event_id=? "
                              "AND role='user' AND kind='message' AND status='active' "
                              "AND channel='main' AND sensitive=0", (raw_id, source_event_id)).fetchall()
            return rows[0][0] if len(rows) == 1 else None

    def raw_quote(self, text):
        with connect(self.raw) as db:
            raw_id = db.execute(
                "INSERT INTO raw_events(source,event_hash,role,text,created_at,metadata_json,deleted) "
                "VALUES('app',?,'user',?,'2001-01-01',?,0)",
                (digest(text), text, json.dumps(dict(authored_text=text, channel="main"))),
            ).lastrowid
            source_id = f"synthetic-{raw_id}"
            db.execute("UPDATE raw_events SET source_event_id=? WHERE id=?", (source_id, raw_id))
        with connect(self.history) as db:
            db.execute("INSERT INTO message_records VALUES(?,?,'user',?,'message','active','main',0)",
                       (raw_id, source_id, text))
        return dict(source_kind="user_text_exact", source_ref=source_id,
                    raw_event_start_id=raw_id, raw_event_end_id=raw_id, event_hash=digest(text),
                    char_start=0, char_end=len(text), quote_hash=digest(text))

    def enqueue(self, key, text, state=None, card_id=None, evidence=None):
        if evidence is None:
            evidence = [self.raw_quote(text)]
        payload = dict(content="Writer 提炼", title="模型猜测的标题", claim_scope="user_fact",
                       importance=6, evidence_state="verified", verified_evidence=evidence)
        if state is not None:
            payload.update(event_state=state, event_card_id=card_id)
        ops.put_writer_batch(dict(batch_key=key, raw_start_id=1, raw_end_id=10,
                                  input_hash=key, extractor_model="fake", policy_version="test"),
                             [dict(candidate_id=key, payload=payload)], self.ops)
        return evidence

    def apply(self, key, action, state=None, card_id=None, *, provider=None, frozen=None,
              **kwargs):
        if frozen is None:
            frozen = core.freeze_candidate_map_from_card_ids([card_id], memory_db_path=self.memory,
                                                             include_read_only=True)

        def gate(messages):
            prompt = json.loads(messages[-1]["content"])["candidate"]
            response = dict(action=action, importance=6, same_memory=True, same_memory_confidence=1,
                            target_card_id=card_id, long_term_value=True, content="合成记忆")
            if state is not None:
                self.assertNotIn("event_state", prompt)
                self.assertEqual("", prompt["title"])
                response.update(confirmed_event_state=state, event_state_basis=prompt["content"])
            return response

        return core.apply_candidate(key, memory_db_path=self.memory, raw_db_path=self.raw,
                                    ops_db_path=self.ops, profile=dict(model="fake", thinking_enabled=False),
                                    current_public_user_text=kwargs.get("reader", self.public_user_text),
                                    frozen_map=frozen, provider=provider or gate)

    def land(self, key, text, state, action, card_id=None):
        self.enqueue(key, text, state, card_id)
        result, code = self.apply(key, action, state, card_id)
        self.assertEqual((code, result["status"]), (200, "applied"), result)
        return result

    def read(self, card_id):
        return event_state.read_current(card_id, memory_db_path=self.memory, raw_db_path=self.raw,
                                        current_public_user_text=self.public_user_text)

    def counts(self):
        tables = ("cards", "card_revisions", "card_evidence", "memory_extensions",
                  "memory_extension_evidence")
        with connect(self.memory) as db:
            return tuple(db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in tables)

    def test_niannian_adapter_passes_trusted_public_user_reader(self):
        self.enqueue("adapter", "我正在准备考试。", "ongoing")
        host = NiannianGatekeeper(
            memory_db=self.memory, ops_db=self.ops, raw_db=self.raw,
            profile=lambda: {"model": "synthetic", "thinking_enabled": False},
            provider=lambda _: {"action": "new", "importance": 6, "long_term_value": True,
                                "same_memory": False, "same_memory_confidence": 0,
                                "content": "我正在准备考试。", "confirmed_event_state": "ongoing",
                                "event_state_basis": "我正在准备考试。"},
            frozen_candidates=lambda _: [],
            current_public_user_text=self.public_user_text,
        )
        accepted, status = host.apply_candidate("adapter", frozen_map=[])
        self.assertEqual((200, "applied"), (status, accepted["status"]))
        self.assertEqual("ongoing", self.read(accepted["card_id"])["event_state"])

    def test_two_interleaved_events_revision_and_extension_history(self):
        a = self.land("a1", "我正在准备搬家。", "ongoing", "new")
        b = self.land("b1", "我正在筹备展览。", "ongoing", "new")
        aid, bid = a["card_id"], b["card_id"]
        self.assertNotEqual(aid, bid)
        a2 = self.land("a2", "搬家日期改到周五了。", "update", "extend", aid)
        self.assertEqual(a["current_revision_id"], a2["current_revision_id"])
        self.assertEqual(("update", a2["extension_id"]),
                         (self.read(aid)["event_state"], self.read(aid)["extension_id"]))
        self.assertEqual("ongoing", self.read(bid)["event_state"])
        b2 = self.land("b2", "我的展览换了场地。", "update", "correct", bid)
        self.assertNotEqual(b["current_revision_id"], b2["current_revision_id"])
        self.assertEqual("update", self.read(bid)["event_state"])
        a3 = self.land("a3", "我的搬家已经结束了。", "closed", "extend", aid)
        b3 = self.land("b3", "我的展览已经结束了。", "closed", "supersede", bid)
        for cid, result in ((aid, a3), (bid, b3)):
            current = self.read(cid)
            self.assertEqual(("closed", result["current_revision_id"], result.get("extension_id")),
                             (current["event_state"], current["revision_id"], current["extension_id"]))
            self.assertEqual(digest(current["quote"]), current["quote_hash"])
        self.assertEqual((2, 4, 4, 2, 2), self.counts())
        with connect(self.memory) as db:
            self.assertEqual([("ongoing",), ("ongoing",), ("update",), ("closed",)],
                             db.execute("SELECT event_state FROM card_evidence ORDER BY id").fetchall())
            self.assertEqual([("update",), ("closed",)], db.execute(
                "SELECT event_state FROM memory_extension_evidence ORDER BY id").fetchall())
            self.assertEqual([], db.execute("PRAGMA foreign_key_check").fetchall())
        with connect(self.ops) as db:
            receipt, status = db.execute("SELECT event_gate_receipt,status FROM memory_candidates "
                                         "WHERE candidate_id='a3'").fetchone()
            self.assertEqual("applied", status)
            self.assertEqual(a["current_revision_id"], json.loads(receipt)["base_revision_id"])
        replay, code = self.apply("a3", "extend", provider=lambda _: self.fail("重复调用 Gate"))
        self.assertEqual((200, True, a3["extension_id"]), (code, replay["idempotent"], replay["extension_id"]))
        self.assertEqual((2, 4, 4, 2, 2), self.counts())

    def test_current_rechecks_deletion_private_revocation_and_exact_text(self):
        a = self.land("a", "我正在搬家。", "ongoing", "new")["card_id"]
        self.land("b", "我的搬家结束了。", "closed", "extend", a)
        for table, db_path, change in (
            ("message_records", self.history, "status='withdrawn'"),
            ("message_records", self.history, "channel='private'"),
            ("message_records", self.history, "sensitive=1"),
            ("message_records", self.history, "content='已被编辑'"),
            ("raw_events", self.raw, "deleted=1"),
            ("raw_events", self.raw, "event_hash='different'"),
            ("raw_events", self.raw, """metadata_json='{"channel":"private","authored_text":"我的搬家结束了。"}'"""),
            ("raw_events", self.raw, """metadata_json='{"visibility":"private","authored_text":"我的搬家结束了。"}'"""),
            ("raw_events", self.raw, """metadata_json='{"sensitivity":"sensitive","authored_text":"我的搬家结束了。"}'"""),
            ("raw_events", self.raw, """metadata_json='{"revoked":true,"authored_text":"我的搬家结束了。"}'"""),
        ):
            key = "raw_event_id" if table == "message_records" else "id"
            with self.subTest(change=change), connect(db_path) as db:
                before = db.execute(f"SELECT * FROM {table} WHERE {key}=2").fetchone()
                db.execute(f"UPDATE {table} SET {change} WHERE {key}=2")
                db.commit()
                self.assertIsNone(self.read(a))
                db.execute(f"DELETE FROM {table} WHERE {key}=2")
                db.execute(f"INSERT INTO {table} VALUES(" + ",".join("?" for _ in before) + ")", before)
        with connect(self.raw) as db:
            db.execute("DELETE FROM raw_events WHERE id=2")
        self.assertIsNone(self.read(a))

    def test_unconfirmed_state_wrong_quote_or_missing_public_source_never_commits(self):
        text = "我在考虑明年要不要搬家。"
        row = self.raw_quote(text)
        for key, changes in (
            ("empty", []), ("wrong_role", [dict(row, source_kind="assistant_text_exact")]),
            ("bad_hash", [dict(row, quote_hash="forged")]),
            ("partial", [dict(row, char_start=1, quote_hash=digest(text[1:]))]),
            ("range", [dict(row, source_kind="raw_range")]),
        ):
            with self.subTest(key=key):
                self.enqueue(key, text, "closed", evidence=changes)
                self.assertEqual(422, self.apply(key, "new", provider=lambda _: self.fail("无证据调用 Gate"))[1])
        self.enqueue("unknown", text, "closed", evidence=[row])
        self.assertEqual(422, self.apply("unknown", "new", reader=None)[1])
        self.assertEqual(422, self.apply("unknown", "new")[1])  # 常规 new 没有确认事项状态
        self.assertEqual(422, self.apply("unknown", "new", "ongoing")[1])
        forged = dict(action="new", importance=6, confirmed_event_state="closed",
                      event_state_basis="我已搬完。")
        self.assertEqual(422, self.apply("unknown", "new", provider=lambda _: forged)[1])
        for change in ("status='withdrawn'", "sensitive=1", "channel='private'"):
            with connect(self.history) as db:
                db.execute("UPDATE message_records SET status='active',channel='main',sensitive=0")
                db.execute(f"UPDATE message_records SET {change}")
            self.assertEqual(422, self.apply("unknown", "new", provider=lambda _: self.fail("敏感来源调用 Gate"))[1])
        self.assertEqual((0, 0, 0, 0, 0), self.counts())

    def test_gate_decides_existing_identity_without_writer_card_id(self):
        first = self.land("first", "我正在准备一场展览。", "ongoing", "new")
        card_id = first["card_id"]
        # Writer only knows the new user statement; Gatekeeper chooses the card.
        self.enqueue("progress", "这场展览已经开始布展了。", "update")
        result, code = self.apply("progress", "extend", "update", card_id)
        self.assertEqual((200, "applied", card_id), (code, result["status"], result["card_id"]))
        self.assertEqual("update", self.read(card_id)["event_state"])
        self.assertEqual(1, self.counts()[0])
        # A supplied card hint still cannot override a different Gatekeeper target.
        self.enqueue("wrong", "展览已经结束了。", "closed", card_id + 100)
        denied, code = self.apply("wrong", "extend", "closed", card_id)
        self.assertEqual((422, "event_state_target_mismatch"), (code, denied["error"]))
        self.assertEqual("update", self.read(card_id)["event_state"])

    def test_receipt_failure_rolls_back_and_delivery_failure_recovers(self):
        self.enqueue("a", "我正在筹备展览。", "ongoing")
        with connect(self.ops) as db:
            db.execute("CREATE TRIGGER deny_receipt BEFORE UPDATE OF event_gate_receipt ON memory_candidates "
                       "BEGIN SELECT RAISE(ABORT,'synthetic receipt failed'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "receipt failed"):
            self.apply("a", "new", "ongoing")
        self.assertEqual((0, 0, 0, 0, 0), self.counts())
        with connect(self.ops) as db:
            self.assertEqual((None, "pending"), db.execute(
                "SELECT event_gate_receipt,status FROM memory_candidates").fetchone())
            db.execute("DROP TRIGGER deny_receipt")
        with patch.object(ops, "mark_candidate_applied", side_effect=RuntimeError("delivery failed")):
            with self.assertRaisesRegex(RuntimeError, "delivery failed"):
                self.apply("a", "new", "ongoing")
        recovered, code = self.apply("a", "new", provider=lambda _: self.fail("不应重复决策"))
        self.assertEqual((200, True), (code, recovered["idempotent"]))
        self.assertEqual((1, 1, 1, 0, 0), self.counts())
        self.assertEqual("ongoing", self.read(recovered["card_id"])["event_state"])

    def test_stale_duplicate_target_and_manual_authority(self):
        older = self.raw_quote("我正在搭展台。")
        current = self.land("current", "我已经搭完展台了。", "closed", "new")
        cid = current["card_id"]
        original = json.loads(ops.get_candidate("current", self.ops)["payload_json"])
        self.enqueue("dup", "", "closed", evidence=original["verified_evidence"])
        self.assertEqual("event_state_duplicate_evidence", self.apply("dup", "new", "closed")[0]["error"])
        self.enqueue("late", "", "ongoing", cid, evidence=[older])
        self.assertEqual("event_state_stale_source", self.apply("late", "extend", "ongoing", cid)[0]["error"])
        self.assertEqual("closed", self.read(cid)["event_state"])
        self.enqueue("mismatch", "我又开始搭展台了。", "ongoing", cid)
        self.assertEqual("event_state_target_mismatch", self.apply("mismatch", "new", "ongoing")[0]["error"])
        revision_store.update_card(self.memory, card_id=cid, expected_current_revision_id=current["current_revision_id"],
                                   content="人工确认的版本")
        self.assertIsNone(self.read(cid))
        before = self.counts()
        self.assertEqual("dropped", self.apply("mismatch", "extend", "ongoing", cid)[0]["status"])
        frozen = core.freeze_candidate_map_from_card_ids([cid], memory_db_path=self.memory, include_read_only=True)
        frozen[0]["read_only"] = False  # 调用者旧快照不能越过事务内人工权威检查
        self.enqueue("manual", "我的展台已经完成了。", "closed", cid)
        self.assertEqual(422, self.apply("manual", "correct", "closed", cid, frozen=frozen)[1])
        self.assertEqual(before, self.counts())

    def test_gate_failure_and_revocation_between_gate_and_commit(self):
        self.enqueue("a", "我的展览已经结束了。", "closed")
        for action in ("uncertain", "drop"):
            key = "a-" + action
            self.enqueue(key, "我的展览已经结束了。", "closed")
            result, _ = self.apply(key, action)
            self.assertIn(result["status"], ("pending", "dropped"))
        with patch.object(core, "decide", side_effect=RuntimeError("synthetic provider failure")):
            self.assertEqual(503, self.apply("a", "new")[1])

        def revoke(messages):
            with connect(self.history) as db:
                db.execute("UPDATE message_records SET status='withdrawn' WHERE raw_event_id=1")
            return dict(action="new", content="合成卡", importance=6, confirmed_event_state="closed",
                        event_state_basis="已经结束了")
        self.assertEqual("event_state_source_unavailable", self.apply("a", "new", provider=revoke)[0]["error"])
        self.assertEqual((0, 0, 0, 0, 0), self.counts())

    def test_old_schema_readonly_then_additive_install_and_legacy_apply(self):
        self.memory = self.memory.with_name("legacy")
        with connect(self.memory) as db:
            db.executescript(schema.DDL)  # 本次加列前的全量合成空库
        self.enqueue("old", "我正在搬家。")
        old, code = self.apply("old", "new", reader=None)
        self.assertEqual(200, code)
        self.assertIsNone(self.read(old["card_id"]))
        self.enqueue("new", "我的搬家结束了。", "closed", old["card_id"])
        self.assertEqual("event_state_schema_required",
                         self.apply("new", "extend", "closed", old["card_id"])[0]["error"])
        with connect(self.memory) as db:
            before = db.execute("SELECT * FROM card_revisions").fetchall()
            self.assertNotIn("event_state", [r[1] for r in db.execute("PRAGMA table_info(card_evidence)")])
            schema.initialize(db)
            schema.initialize(db)
            self.assertEqual(before, db.execute("SELECT * FROM card_revisions").fetchall())
            self.assertEqual([(None,)], db.execute("SELECT event_state FROM card_evidence").fetchall())
        self.assertIsNone(self.read(old["card_id"]))
        self.assertEqual(200, self.apply("new", "extend", "closed", old["card_id"])[1])
        self.assertEqual("closed", self.read(old["card_id"])["event_state"])
