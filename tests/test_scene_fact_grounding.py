"""用合成对话读回真实写入的卡片、证据与聊天文本。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from thalemora.app import Application
from thalemora.gatekeeper import core
from test_pre6_maintenance import SyntheticProvider


class FactProvider(SyntheticProvider):
    def __init__(self):
        super().__init__()
        self.route = {}
        self.decision = {}
        self.input = None

    def call(self, module, payload):
        if module == "writer":
            rows = payload["sources"]
            evidence = [dict(source_event_id=r["source_event_id"], version=r["version"], quote=r["text"])
                        for r in rows]
            output = {"candidates": [dict(content=rows[0]["text"], evidence=evidence,
                                           importance=6, **self.route)]}
        elif module == "gatekeeper":
            self.input = json.loads(payload["messages"][-1]["content"])
            frozen = self.input["frozen_cards"]
            output = dict(action=self.action, importance=6, long_term_value=True,
                          target_card_id=frozen[0]["card_id"] if frozen else None,
                          same_memory=bool(frozen), same_memory_confidence=.95, **self.decision)
        else:
            return super().call(module, payload)
        return dict(output=output, usage={"synthetic_units": 1})


class SceneFactTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = Application(Path(tmp.name))
        self.agent = self.app.register_agent("synthetic-facts")["agent_id"]
        self.app.grant_source(self.agent, "demo")
        self.provider = FactProvider()
        self.provider.route = dict(memory_type="stable_fact", claim_scope="user_fact")

    def say(self, day, *turns):
        events = [dict(app="demo", channel="main", source_event_id=f"{day}-{i}", version=1,
                       role=role, text=text, occurred_at=f"2001-01-{day:02}T12:{i:02}:00Z")
                  for i, (role, text) in enumerate(turns)]
        self.app.import_history(self.agent, "demo", events)

    def tick(self):
        result = self.app.maintenance_tick(self.agent, "demo", provider=self.provider)
        self.assertEqual([], result["errors"])
        return result

    def cards(self):
        return self.app.memory(self.agent, "demo")

    def test_new_fact_on_existing_card_is_visible_in_chat(self):
        old = "共享日程会自动生成。"
        new = "共享日程会自动生成，访客看不到变更记录。"
        self.say(1, ("user", old))
        self.tick()
        card = self.cards()[0]
        self.say(2, ("user", new))
        self.provider.action = "drop"
        self.provider.decision = dict(fact_relation="adds_detail", new_quote="访客看不到变更记录")
        self.tick()
        self.tick()
        current, = self.cards()
        self.assertEqual((card["id"], card["current_revision_id"], old),
                         (current["id"], current["current_revision_id"], current["content"]))
        extension, = current["extensions"]
        self.assertEqual(new, extension["content"])
        evidence = self.app.evidence(self.agent, "demo", card["id"])
        self.assertEqual([new], [json.loads(e["source_ref"])["quote"] for e in evidence
                                 if e.get("extension_id") == extension["extension_id"]])
        recall = self.app.recall_for_turn(self.agent, "demo", "访客看不到变更记录")
        self.assertTrue(any(m.get("extension_id") == extension["extension_id"] for m in recall["memories"]))
        self.assertIn(new, recall["rendered"])

    def test_only_user_can_accept_a_standing_agreement(self):
        user = "今天不想去取件。"
        assistant = "以后每周都不用你去取件。"
        self.say(1, ("user", user), ("assistant", assistant))
        self.provider.route = dict(memory_type="agreement", claim_scope="shared")
        self.provider.decision = dict(content=assistant, user_rule_quote=assistant)
        result = self.tick()
        self.assertEqual([dict(role="user", quote=user), dict(role="assistant", quote=assistant)],
                         self.provider.input["candidate"]["source_quotes"])
        self.assertEqual("pending", result["candidates"][0]["status"])
        self.assertEqual([], self.cards())
        self.provider.decision["user_rule_quote"] = user
        decision, _ = core.decide(self.provider.input["candidate"], [], provider=lambda _: {
            "action": "new", "long_term_value": True, "importance": 6, **self.provider.decision})
        self.assertEqual("uncertain", decision["action"])
        confirmed = "以后休息日都不用我去取件。"
        self.say(2, ("user", confirmed))
        self.provider.decision = dict(content=confirmed, user_rule_quote=confirmed)
        self.tick()
        card, = self.cards()
        self.assertEqual(confirmed, card["content"])

    def test_changed_decision_revises_existing_card_and_chat_reads_new_text(self):
        old = "展台的木柜决定定制。"
        new = "展台的木柜改为买成品，型号还没有定。"
        self.say(1, ("user", old))
        self.tick()
        original, = self.cards()
        self.say(2, ("user", new))
        self.provider.action = "drop"
        self.provider.decision = dict(fact_relation="changed_decision", old_quote=old, new_quote=new)
        self.tick()
        current, = self.cards()
        self.assertEqual(original["id"], current["id"])
        self.assertNotEqual(original["current_revision_id"], current["current_revision_id"])
        self.assertEqual(new, current["content"])
        rendered = self.app.recall_for_turn(self.agent, "demo", "展台的木柜怎么选")["rendered"]
        self.assertIn(new, rendered)
        self.assertNotIn(old, rendered)

    def test_duplicate_unsupported_drop_and_manual_target(self):
        quote = "项目使用蓝色标签。"
        candidate = dict(content=quote, memory_type="stable_fact", importance=6,
                         source_quotes=[dict(role="user", quote=quote)])
        frozen = dict(card_id=2, current_revision_id=1, content=quote)
        def decide(card, **fields):
            raw = dict(action="drop", same_memory=True, target_card_id=2, **fields)
            return core.decide(candidate, [card], provider=lambda _: raw)[0]["action"]
        self.assertEqual("drop", decide(frozen, fact_relation="covered", old_quote=quote))
        self.assertEqual("uncertain", decide(frozen))
        self.assertEqual("uncertain", decide(frozen, fact_relation="covered", old_quote="橙色标签"))
        self.assertEqual("uncertain", decide(frozen, fact_relation="adds_detail", new_quote="橙色标签"))
        self.assertEqual("drop", decide(dict(frozen, read_only=True), fact_relation="adds_detail", new_quote=quote))
        candidate["content"] = "项目使用蓝色标签，归档时停用。"
        self.assertEqual("uncertain", decide(frozen, fact_relation="covered", old_quote=quote))
