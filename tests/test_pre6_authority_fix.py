"""人工卡只读关系/来源隔离验收；全部临时合成数据，不调用真实模型。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from thalemora.app import Application
from thalemora.gatekeeper import core
from test_pre6_maintenance import SyntheticProvider


class AuthorityFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(dir="/tmp", prefix="pre6-authority-")
        self.addCleanup(self.tmp.cleanup)
        self.app = Application(Path(self.tmp.name) / "state")
        self.agent = self.app.register_agent("synthetic-authority")["agent_id"]
        self.app.grant_source(self.agent, "synthetic-app")
        self.provider = SyntheticProvider()
        self.event = dict(app="synthetic-app", source_event_id="synthetic-1", version=1,
                          role="user", text="苔灯项目正式通知长期采用电子邮件。",
                          occurred_at="2001-01-01T00:00:00+00:00")
        self.app.import_history(self.agent, "synthetic-app", [self.event])

    def tick(self):
        return self.app.maintenance_tick(self.agent, "synthetic-app", provider=self.provider)

    def cards(self):
        return self.app.memory(self.agent, "synthetic-app")

    def test_manual_revision_visible_no_parallel_card_no_retry_unrelated_fact_allowed(self):
        self.tick()
        original = self.cards()[0]
        self.app.revise_memory(self.agent, "synthetic-app", original["id"], "人工确认：只用纸质信函。",
                               original["current_revision_id"])
        protected = self.cards()[0]
        self.app.import_history(self.agent, "synthetic-app", [dict(
            self.event, source_event_id="synthetic-2", text="苔灯项目通知长期改用电话。",
            occurred_at="2001-01-02T00:00:00+00:00")])
        self.provider.action = "correct"
        result = self.tick()
        self.assertFalse(result["errors"])
        self.assertEqual([protected], self.cards())
        decision_input = [json.loads(p["messages"][-1]["content"]) for m,p in self.provider.calls
                          if m == "gatekeeper"][-1]
        self.assertEqual(protected["content"], decision_input["frozen_cards"][0]["content"])
        self.assertTrue(decision_input["frozen_cards"][0]["read_only"])
        candidate = next(r for r in result["candidates"] if r["status"] == "dropped")
        self.assertEqual("manual_memory_read_only", candidate["last_error"])
        count = len(self.provider.calls)
        self.tick()
        self.assertEqual(count, len(self.provider.calls))
        self.app.import_history(self.agent, "synthetic-app", [dict(
            self.event, source_event_id="synthetic-3", text="合成植物项目长期使用绿色封面。",
            occurred_at="2001-01-03T00:00:00+00:00")])
        self.provider.action = "new"
        self.tick()
        self.assertEqual(2, len(self.cards()))
        self.assertEqual(protected, next(c for c in self.cards() if c["id"] == protected["id"]))

    def test_direct_manual_card_with_v050_provenance_is_visible_without_scope_migration(self):
        self.app.remember(self.agent, "synthetic-app", "synthetic-1")
        protected = self.cards()[0]
        self.provider.action = "correct"
        result = self.tick()
        self.assertFalse(result["errors"])
        self.assertEqual([protected], self.cards())
        self.assertEqual("dropped", result["candidates"][0]["status"])
        self.assertEqual("manual_memory_read_only", result["candidates"][0]["last_error"])

    def test_other_app_manual_card_does_not_enter_frozen_input(self):
        self.app.grant_source(self.agent, "other-app")
        other = dict(self.event, app="other-app", source_event_id="other-only",
                     text="另一个应用的人工私有约定")
        self.app.import_history(self.agent, "other-app", [other])
        self.app.remember(self.agent, "other-app", "other-only")
        self.assertEqual(self.cards(), [])
        manual = self.app.memory(self.agent, "other-app")[0]
        # 人工编辑可以来自所有者的另一界面，但不应改变这张卡的原始 app 归属。
        self.app.revise_memory(self.agent, "synthetic-app", manual["id"], other["text"],
                               manual["current_revision_id"], scope="owner-global")
        self.tick()
        inputs = [json.loads(p["messages"][-1]["content"]) for m,p in self.provider.calls if m=="gatekeeper"]
        self.assertEqual([], inputs[0]["frozen_cards"])
        self.assertNotIn(other["text"], json.dumps(inputs, ensure_ascii=False))

    def test_core_default_and_read_only_update_decisions(self):
        self.app.remember(self.agent, "synthetic-app", "synthetic-1")
        protected = self.cards()[0]
        memory = self.app.memory_path(self.agent, "synthetic-app", "main")
        self.assertEqual([], core.freeze_candidate_map_from_card_ids([protected["id"]], memory_db_path=memory))
        frozen = core.freeze_candidate_map_from_card_ids([protected["id"]], memory_db_path=memory,
                                                        include_read_only=True)
        for action in ("extend", "correct", "supersede"):
            with self.subTest(action=action):
                decision, _ = core.decide({"content":"合成通知新约定", "memory_type":"agreement"}, frozen,
                    provider=lambda _: dict(action=action, target_card_id=protected["id"], same_memory=True,
                                            same_memory_confidence=.99,long_term_value=True,importance=6,
                                            content="合成通知新约定",reason="synthetic relationship"))
                self.assertEqual("drop", decision["action"])
                self.assertEqual("manual_memory_read_only", decision["reason"])
        profile = dict(model="synthetic", thinking_enabled=False, reasoning_effort=None)
        writable = [dict(frozen[0], read_only=False)]
        self.assertNotEqual(core.gate_input_hash({"content":"合成"}, frozen, profile),
                            core.gate_input_hash({"content":"合成"}, writable, profile))


if __name__ == "__main__":
    unittest.main()
