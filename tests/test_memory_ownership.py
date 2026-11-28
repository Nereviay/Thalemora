"""职责交接回归。临时数据库、合成对话，无网络和付费模型。"""
from contextlib import closing
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from thalemora.memory import schema, revision_store, manual
from thalemora.memory.management import CardManagement
from thalemora.retrieval.current_cards import current_items
from thalemora.scene.admission import validate_judgement
from thalemora.timeline import content, maintenance, state, lookup


def selected(messages):
    return {"themes": [{"theme": "现场沟通", "facts": [
        {"event_id": m["event_id"], "quote": m["quotable_text"]} for m in messages]}]}


def route(lane, quote, event_id):
    return dict(route=lane, identity="木柜选择", memory_type="plan" if lane == "plan" else "stable_fact",
                title="柜体方案", content=quote, keywords=["成品木柜"], importance=6,
                valence=0.5, arousal=0.3, claim_scope="user_fact", reason="保留明确选择",
                evidence=[dict(event_id=event_id, quote=quote)])


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = str(Path(temp.name) / "memory.sqlite")
        self.ops = str(Path(temp.name) / "ops.sqlite")
        with closing(sqlite3.connect(self.db)) as db, db:
            schema.initialize(db)
        self.text = "设计组和验收组要求延期。小林负责通知会场。"
        self.calls = []
        self.speakers = {"user": "记录人", "assistant": "助手"}

    def run_day(self, day, text=None, generate=None):
        start = dt.date.fromisoformat(day)
        rows = [dict(id=start.toordinal(), created_at=day+" 06:00:00", role="user", text=text or self.text)]
        def summarize(date, messages):
            self.calls.append(date)
            return content.generate_day(date, messages, lambda *_: selected(messages), speakers=self.speakers)
        return maintenance.process_day(day, rows=rows, utc_range=(day,(start+dt.timedelta(days=1)).isoformat()),
                                  memory_db_path=self.db, ops_db_path=self.ops, summarize_fn=generate or summarize)

    def test_mixed_plan_and_confirmed_change(self):
        changed = "木柜改成买成品。"
        pending = "型号还在挑。"
        events = [dict(event_id=i, role="user", quotable_text=q) for i,q in [(1, changed),(2, pending)]]
        judgement = dict(disposition="pending", reason="选型还未完成",
                         memory_candidates=[route("plan", pending, 2), route("memory", changed, 1)])
        result = validate_judgement(events, judgement)
        self.assertEqual("promote_memory", result["disposition"])
        self.assertEqual(["plan","memory"], [r["route"] for r in result["candidates"]])
        judgement["memory_candidates"] = [route("plan", pending, 2)]
        self.assertEqual("pending", validate_judgement(events, judgement)["disposition"])

    def test_timeline_prompts_satisfy_provider_json_mode(self):
        message = dict(event_id=1, role="user", quotable_text=self.text)
        def complete(module, prompt, data, temperature):
            self.assertIn("json", prompt.lower(), module)
            return json.dumps({"daily": selected([message]),
                               "weekly": {"threads": [{"thread": "项目", "summary": "准备布展。"}]},
                               "monthly": {"arcs": [{"arc": "项目", "summary": "完成布展。"}]}}[module])
        content.generate_day("2001-01-01", [message], complete, speakers=self.speakers)
        for period, key in (("week", "2001-W01"), ("month", "2001-01")):
            content.generate_period(period, key, [{"date": "2001-01-01", "content": self.text}], complete)

    def test_speaker_sources_reject_fused_attribution(self):
        msg = dict(event_id=1, role="user", quotable_text=self.text)
        bad = {"themes":[{"theme":"现场","facts":[{"event_id":1,"quote":"验收组的小林要求延期。"}]}]}
        with self.assertRaisesRegex(ValueError,"unsupported_attribution"):
            content.render_grounded_day(bad, [msg], self.speakers)
        raw = content.render_grounded_day(selected([msg]), [msg], self.speakers)
        self.assertEqual("记录人提到「设计组和验收组要求延期。小林负责通知会场。」。", raw)

    def test_daily_sentence_numbers_resolve_exact_speaker_and_multiline_source(self):
        messages = [dict(event_id=7, role="user", quotable_text="改过日期。会场说：\n请小林通知。未说完", truncated=True),
                    dict(event_id=8, role="assistant", quotable_text="我负责转告。")]
        def choose(module, prompt, data, temperature):
            choices = json.loads(data)
            self.assertEqual([(7, 1), (7, 2), (8, 1)], [(c["event_id"], c["sentence_id"]) for c in choices])
            return {"themes": [{"theme": "通知", "facts": [{"event_id": 7, "sentence_id": 2}]}]}
        self.assertEqual("记录人提到「会场说： 请小林通知。」。",
                         content.generate_day("2001-01-01", messages, choose, speakers=self.speakers))
        for number in (0, 3, True):
            bad = {"themes": [{"theme": "通知", "facts": [{"event_id": 7, "sentence_id": number}]}]}
            with self.assertRaisesRegex(ValueError, "invalid_source"):
                content.render_grounded_day(bad, messages, self.speakers)

    def test_failed_day_recovery_manual_protection_and_periods(self):
        first = "2001-01-01"
        def fail(*_):
            raise ValueError("synthetic-provider-failed")
        with self.assertRaisesRegex(ValueError,"synthetic"):
            self.run_day(first, generate=fail)
        retry = lambda: maintenance.retry_failed_days(before="2001-02-02", ops_db_path=self.ops, run_day=self.run_day)
        recovered, = retry()
        self.assertEqual("ready", recovered["status"])
        self.assertEqual([], retry())
        self.assertEqual("skipped", self.run_day(first)["status"])
        receipt = state.get_timeline_day_state(first, self.ops)
        revision_store.update_card(self.db, card_id=receipt["card_id"],
            expected_current_revision_id=receipt["revision_id"], content="手工订正的日摘要。",
            origin="manual_ui", change_type="manual_edit", evidence_source_kind="manual", evidence_source_ref="test")
        self.assertEqual("manual_override", self.run_day(first, "改过的原始事件。")["status"])
        for i in range(2,32):
            self.run_day(f"2001-01-{i:02}")
        for kind,key in (("week","2001-W01"),("month","2001-01")):
            def period(g,k,src):
                self.assertIn("手工订正的日摘要。", [s["content"] for s in src])
                def complete(module, *_):
                    if module == "weekly":
                        return json.dumps({"threads":[{"thread":"计划","summary":"会场开门时间调整。"}]})
                    return json.dumps({"arcs":[{"arc":"计划","summary":"会场排期确定。"}]})
                return content.generate_period(g,k,src,complete)
            kw = dict(memory_db_path=self.db, ops_db_path=self.ops, summarize_fn=period)
            self.assertEqual("ready", maintenance.process_period(kind,key,**kw)["status"])
            self.assertEqual("skipped", maintenance.process_period(kind,key,**kw)["status"])
        self.assertEqual(31,len(self.calls))

    def test_failed_date_only_one_automatic_retry(self):
        def fail(*_):
            raise ValueError("synthetic")
        def run(day):
            return self.run_day(day, generate=fail)
        with self.assertRaises(ValueError):
            run("2001-01-01")
        args = dict(before="2001-02-02", ops_db_path=self.ops, run_day=run)
        self.assertEqual("failed", maintenance.retry_failed_days(**args)[0]["status"])
        self.assertEqual([], maintenance.retry_failed_days(**args))

    def test_another_client_reads_current_manual_revision_and_grounded_timeline(self):
        def connect():
            db = sqlite3.connect(self.db)
            db.row_factory = sqlite3.Row
            return db

        created, status = manual.create({}, "位置暂定东厅。", "布展", "", db_path=self.db,
            embed=lambda *a, **kw: None, embedding_model=None, evidence_ref="fixture:manual")
        self.assertEqual(200, status)
        cid = created["card_id"]
        def card(card_id):
            with closing(connect()) as db:
                return dict(db.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone())
        revised, code = manual.update(dict(card_id=cid, expected_current_revision_id=created["current_revision_id"],
            content="位置改在西厅。"), db_path=self.db, read_card=card, embed=lambda *a, **kw: None, embedding_model=None)
        self.assertEqual(200, code)
        self.assertNotEqual(created["current_revision_id"], revised["current_revision_id"])
        inputs = [(cid, "位置暂定东厅。", .8), (999, "已消失", .5)]
        self.assertEqual(["位置改在西厅。"], [row["content"] for row in current_items(inputs, connect)])
        with closing(connect()) as db, db:
            db.execute("UPDATE cards SET status='archived' WHERE id=?", (cid,))
        self.assertEqual([], current_items(inputs, connect))
        self.run_day("2001-01-01")
        result, code = lookup.lookup({"mode":"period","granularity":"month","period_key":"2001-01"},
                                     connect=connect, ops_db=self.ops)
        self.assertEqual(200, code)
        self.assertIn(self.text, result["days"][0]["content"])


    def test_embedding_in_flight_cannot_restore_old_card_and_receipt_is_idempotent(self):
        def connect():
            return sqlite3.connect(self.db)
        manager = CardManagement(connect)
        with closing(connect()) as db, db:
            cid = db.execute("INSERT INTO cards(content) VALUES ('旧方案')").lastrowid
        def embed(chunks, **_):
            self.assertEqual(["旧方案"], chunks)
            with closing(connect()) as db, db:
                db.execute("UPDATE cards SET content='新方案' WHERE id=?", (cid,))
            return [[1.0]]
        opts = dict(batch=10, caller="fixture", purpose="maintenance")
        self.assertEqual(0, manager.backfill_embeddings(embed, "demo", **opts))
        self.assertEqual(1, manager.backfill_embeddings(lambda *a, **kw: [[2.0]], "demo", **opts))
        self.assertTrue(manager.record_provider_injection_receipt("trace", cid, 2))
        self.assertFalse(manager.record_provider_injection_receipt("trace", cid, 2))
        with closing(connect()) as db:
            self.assertEqual(("新方案", b"[2.0]", 0), db.execute(
                "SELECT content, embedding, hits FROM cards WHERE id=?", (cid,)).fetchone())


class HostConfigurationTests(unittest.TestCase):
    def test_model_profiles_use_only_explicit_defaults_or_saved_configuration(self):
        from thalemora.gatekeeper import profiles

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "models.json"
            with self.assertRaises(profiles.ModelConfigError):
                profiles.load_config(path)
            defaults = {name: dict(model="sample-model", thinking_enabled=False, reasoning_effort="low")
                        for name in profiles.PROFILE_NAMES}
            config = profiles.load_config(path, defaults=defaults)
            config["daily"]["model"] = "other-sample"
            profiles.save_config(config, path)
            selected, request = profiles.request_profile("daily", path, defaults=defaults)
            self.assertEqual("other-sample", request["model"])
            self.assertEqual("sample-model", defaults["daily"]["model"])
            self.assertEqual(selected, profiles.get_profile("daily", path))
