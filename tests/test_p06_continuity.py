"""06 第一段：隔离 Archive/Scene/Memory 真写入；模型语义由可控合成输出替代。"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from thalemora.app import Application
from thalemora.app.maintenance import reflection_biases, setup


class ContinuityProvider:
    synthetic = True

    def __init__(self):
        self.calls = []
        self.routes = {}
        self.actions = {}
        self.reflection_signal = None

    def call(self, module, payload):
        self.calls.append((module, payload))
        if module == "writer":
            candidates = []
            for source in payload["sources"]:
                content, action = self.routes[source["source_event_id"]]
                self.actions[content] = action
                candidates.append(dict(title="合成角色A", content=content, keywords=["合成角色A"],
                    memory_type="event", claim_scope="user_fact", importance=7,
                    evidence=[dict(source_event_id=source["source_event_id"],
                                   version=source["version"], quote=source["text"])]))
            output = dict(candidates=candidates)
        elif module == "gatekeeper":
            context = json.loads(payload["messages"][-1]["content"])
            candidate, frozen = context["candidate"], context["frozen_cards"]
            output = dict(action=self.actions[candidate["content"]], content=candidate["content"],
                title="合成角色A", keywords="合成角色A", long_term_value=True, importance=7,
                same_memory=True, same_memory_confidence=0.99, reason="合成可控同一事实判断",
                target_card_id=frozen[0]["card_id"] if frozen else None)
        else:
            output = dict(text="合成修订审阅建议")
            if self.reflection_signal is not None:
                output.update(self.reflection_signal)
        return dict(output=output, usage=dict(synthetic_units=1))


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory(prefix="p06-continuity-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.app = Application(self.root)
        self.agent = self.app.register_agent("synthetic-continuity")["agent_id"]
        self.app.grant_source(self.agent, "synthetic-app")
        self.provider = ContinuityProvider()
        self.sequence = 0
        network = patch("urllib.request.urlopen", side_effect=AssertionError("no network"))
        network.start()
        self.addCleanup(network.stop)

    def source(self, text, content, action, role="user", app_id="synthetic-app", channel="main"):
        self.sequence += 1
        event_id = f"synthetic-{self.sequence}"
        self.provider.routes[event_id] = content, action
        self.app.import_history(self.agent, app_id, [dict(app=app_id, channel=channel,
            source_event_id=event_id, version=1, role=role, text=text,
            occurred_at=f"2001-01-{self.sequence:02d}T00:00:00+00:00", deleted=False,
            hash_verification="legacy_unverified")], channel)
        return event_id

    def tick(self, app_id="synthetic-app", channel="main"):
        return self.app.maintenance_tick(self.agent, app_id, channel, provider=self.provider)

    def cards(self):
        return self.app.memory(self.agent, "synthetic-app")

    def revisions(self):
        with closing(sqlite3.connect(self.app.memory_path(self.agent, "synthetic-app", "main"))) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM card_revisions ORDER BY id")]

    def seed(self, content="合成角色A居住地成都"):
        event = self.source(content, content, "new")
        result = self.tick()
        self.assertEqual([], result["errors"])
        self.assertEqual(1, len(self.cards()))
        return self.cards()[0], event

    def test_correction_current_history_evidence_and_restart(self):
        before, old_source = self.seed()
        new_source = self.source("我之前记错，角色A现在居住地重庆", "合成角色A居住地重庆", "correct")
        result = self.tick()
        self.assertEqual([], result["errors"])
        after, = self.cards()
        self.assertEqual(before["id"], after["id"])
        self.assertEqual("合成角色A居住地重庆", after["content"])
        revisions = self.revisions()
        self.assertEqual(["合成角色A居住地成都", "合成角色A居住地重庆"], [r["content"] for r in revisions])
        evidence = self.app.evidence(self.agent, "synthetic-app", after["id"])
        self.assertEqual({old_source, new_source}, {json.loads(e["source_ref"])["source_event_id"] for e in evidence})
        self.assertTrue(all(e["resolution"] == "current" for e in evidence))
        gates = [json.loads(p["messages"][-1]["content"]) for m, p in self.provider.calls if m == "gatekeeper"]
        self.assertEqual(before["current_revision_id"], gates[-1]["frozen_cards"][0]["current_revision_id"])
        writers = [p for m, p in self.provider.calls if m == "writer"]
        self.assertEqual(2, len({p["scene_id"] for p in writers}))
        normal = self.app.recall_for_turn(self.agent, "synthetic-app", "合成角色A居住地")
        history = self.app.recall_for_turn(self.agent, "synthetic-app", "合成角色A以前居住地成都，后来怎么更正的？")
        self.assertIn('重庆', normal['rendered'])
        self.assertNotIn('成都', normal['rendered'])
        self.assertIn('重庆', history['rendered'])
        self.assertIn('修订', history['rendered'])
        calls = len(self.provider.calls)
        self.tick()
        self.app = Application(self.root)
        self.tick()
        self.assertEqual(calls, len(self.provider.calls))
        self.assertEqual(revisions, self.revisions())

    def test_uncertain_then_real_confirmation(self):
        before, _ = self.seed()
        self.source("或许/假设A住重庆，两种说法尚未确认", "合成角色A或许居住地重庆", "uncertain")
        self.tick()
        self.assertEqual([before], self.cards())
        self.assertEqual(1, len(self.revisions()))
        self.assertTrue(any(c['last_error'] == 'uncertain'
                            for c in self.tick()['candidates']))
        calls = len(self.provider.calls)
        self.tick()
        self.assertEqual(calls, len(self.provider.calls))
        self.source("我确认之前记错了，角色A现在居住地重庆", "合成角色A居住地重庆", "correct")
        self.tick()
        self.assertEqual(1, len(self.cards()))
        self.assertEqual("合成角色A居住地重庆", self.cards()[0]["content"])
        self.assertEqual(2, len(self.revisions()))
        self.app = Application(self.root)
        self.tick()
        self.assertEqual(2, len(self.revisions()))

    def test_assistant_is_not_user_confirmation(self):
        before, _ = self.seed()
        self.source("我确认角色A现在住重庆", "合成角色A居住地重庆", "correct", role="assistant")
        result = self.tick()
        self.assertIn("user_fact requires a user Archive quote", str(result["errors"]))
        self.assertEqual([before], self.cards())
        self.assertEqual(1, len(self.revisions()))

    def test_manual_card_cannot_be_corrected(self):
        before, _ = self.seed()
        self.app.revise_memory(self.agent, "synthetic-app", before["id"], "人工确认合成角色A居住地成都", before["current_revision_id"])
        manual = self.cards()
        self.source("我之前记错，角色A现在住重庆", "合成角色A居住地重庆", "correct")
        self.assertEqual([], self.tick()["errors"])
        self.assertEqual(manual, self.cards())
        self.assertEqual(2, len(self.revisions()))

    def test_project_plan_supersedes_same_card(self):
        before, _ = self.seed("合成角色A项目计划采用方案甲")
        self.source("项目计划已确认调整为方案乙", "合成角色A项目计划采用方案乙", "supersede")
        self.assertEqual([], self.tick()["errors"])
        after, = self.cards()
        self.assertEqual(before["id"], after["id"])
        self.assertEqual("合成角色A项目计划采用方案乙", after["content"])
        self.assertEqual(2, len(self.revisions()))

    def test_reflection_affinity_and_bias_are_suggestions_only(self):
        self.provider.reflection_signal = dict(ambient_affinity=.20, salience_vote=-.5)
        before, _ = self.seed()
        signal = reflection_biases(self.app, self.agent, 'synthetic-app')
        self.assertIn(before['id'], signal)
        self.assertEqual(signal[before['id']]['affinity'], .20)
        self.assertAlmostEqual(signal[before['id']]['bias'], -.056)
        self.assertEqual('合成角色A居住地成都', self.cards()[0]['content'])
        self.assertEqual(1, len(self.app.inner_state(self.agent, 'synthetic-app', 'reflection')))
        calls = len(self.provider.calls)
        self.tick()
        self.app = Application(self.root)
        self.tick()
        self.assertEqual(calls, len(self.provider.calls))
        self.assertEqual(signal, reflection_biases(self.app, self.agent, 'synthetic-app'))
        self.app.revise_memory(self.agent, 'synthetic-app', before['id'],
            '人工确认合成角色A居住地成都', before['current_revision_id'])
        self.assertEqual({}, reflection_biases(self.app, self.agent, 'synthetic-app'))

    def test_reflection_latest_revision_signal_replaces_previous_bias(self):
        self.provider.reflection_signal = dict(ambient_affinity=.2, salience_vote=-.5)
        first, _ = self.seed()
        self.assertEqual(.2, reflection_biases(self.app, self.agent, 'synthetic-app')[first['id']]['affinity'])
        self.provider.reflection_signal = dict(ambient_affinity=.9, salience_vote=.5)
        self.source('确认角色A居住地已变成重庆', '合成角色A居住地重庆', 'correct')
        self.assertEqual([], self.tick()['errors'])
        scores = reflection_biases(self.app, self.agent, 'synthetic-app')
        self.assertEqual(.9, scores[first['id']]['affinity'])
        self.assertAlmostEqual(.068, scores[first['id']]['bias'])
        _, path, _ = setup(self.app, self.agent, 'synthetic-app', 'main')
        with closing(sqlite3.connect(path)) as db:
            count = db.execute('SELECT COUNT(*) FROM maintenance_reflection_signals').fetchone()[0]
        self.assertEqual(2, count)
        self.tick()
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(count, db.execute('SELECT COUNT(*) FROM maintenance_reflection_signals').fetchone()[0])

    def test_reflection_unspecified_or_invalid_no_fake_signal(self):
        first, _ = self.seed()
        self.assertEqual({}, reflection_biases(self.app, self.agent, 'synthetic-app'))
        self.provider.reflection_signal = dict(ambient_affinity=2.0, salience_vote=0)
        self.source('我更正合成角色A居住地重庆', '合成角色A居住地重庆', 'correct')
        result = self.tick()
        self.assertTrue(any('invalid reflection ambient_affinity' in e for e in result['errors']))
        self.assertEqual({}, reflection_biases(self.app, self.agent, 'synthetic-app'))
        self.assertEqual(1, len(self.cards()))
        self.assertEqual('合成角色A居住地重庆', self.cards()[0]['content'])
        # 修改事实由 Gatekeeper 写入，反思出错不回退它，也不能写污染的评分。
        self.assertEqual(2, len(self.revisions()))

    def test_other_scope_cannot_target_current_card(self):
        before, _ = self.seed()
        self.app.grant_source(self.agent, "synthetic-app", "private")
        self.source("我之前记错，角色A现在住重庆", "合成角色A居住地重庆", "correct", channel="private")
        result = self.tick(channel="private")
        self.assertIn("target_outside_frozen_map", str(result["errors"]))
        gate = [json.loads(p["messages"][-1]["content"]) for m, p in self.provider.calls if m == "gatekeeper"][-1]
        self.assertEqual([], gate["frozen_cards"])
        self.assertEqual([before], self.cards())
        self.assertEqual([], self.app.memory(self.agent, "synthetic-app", "private"))


if __name__ == "__main__":
    unittest.main()
