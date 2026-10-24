"""PRE6-4: only synthetic events/providers in TemporaryDirectory, no network."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from thalemora.app import Application
from thalemora.app.maintenance import setup
from thalemora.history.archive import VersionConflict
from thalemora.identity import AccessDenied
from thalemora.memory import field_sources, revision_store


class SyntheticProvider:
    synthetic = True

    def __init__(self):
        self.calls = []
        self.hook = None
        self.fail = None
        self.action = "new"
        self.empty = False

    def call(self, module, payload):
        self.calls.append((module, payload))
        if self.hook:
            self.hook(module, payload)
        if self.fail == module:
            self.fail = None
            raise RuntimeError("synthetic transient failure")
        if module == "writer":
            source = payload["sources"][0]
            output = {"candidates": [] if self.empty else [{
                "title": "合成长期项目约定", "content": source["text"], "memory_type": "event",
                "claim_scope": "user_fact", "importance": 7,
                "evidence": [{"source_event_id": source["source_event_id"], "version": source["version"],
                              "quote": source["text"]}]}]}
        elif module == "gatekeeper":
            frozen = json.loads(payload["messages"][-1]["content"])["frozen_cards"]
            output = {"action": self.action, "long_term_value": True, "importance": 7,
                      "reason": "synthetic 30-day value judgment", "same_memory": True,
                      "same_memory_confidence": 0.99,
                      "target_card_id": frozen[0]["card_id"] if frozen else 999}
        else:
            output = {"text": "合成建议：后续核对该确切版本的项目约定。"}
        return {"output": output, "usage": {"synthetic_units": 3}}


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="pre6-maintenance-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.app = Application(self.root)
        self.agent = self.app.register_agent("synthetic-agent")["agent_id"]
        self.app.grant_source(self.agent, "synthetic-app")
        self.provider = SyntheticProvider()
        self.insert()

    def insert(self, **changes):
        event = dict(app="synthetic-app", channel="main", source_event_id="synthetic-event",
                     version=1, role="user", text="合成天文项目长期采用紫色符号。",
                     occurred_at="2001-01-01T00:00:00+00:00", deleted=False,
                     hash_verification="legacy_unverified")
        event.update(changes)
        return self.app.import_history(self.agent, event["app"], [event], event["channel"])

    def tick(self, **kw):
        return self.app.maintenance_tick(self.agent, "synthetic-app", provider=self.provider, **kw)

    def cards(self):
        return self.app.memory(self.agent, "synthetic-app")

    def test_success_evidence_usage_default_and_restart(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            blocked = self.app.maintenance_tick(self.agent, "synthetic-app")
            self.assertEqual("blocked", blocked["jobs"][0]["status"])
            self.assertEqual([], blocked["calls"])
            self.assertEqual([], self.cards())
            result = self.tick()
        self.assertEqual("done", result["jobs"][0]["status"])
        self.assertEqual("applied", result["candidates"][0]["status"])
        card = self.cards()[0]
        evidence = self.app.evidence(self.agent, "synthetic-app", card["id"])[0]
        self.assertEqual("derived", evidence["source_kind"])
        self.assertFalse(evidence["verified"])
        self.assertEqual("current", evidence["resolution"])
        ref = json.loads(evidence["source_ref"])
        self.assertEqual(1, ref["version"])
        self.assertEqual("legacy_unverified", ref["hash_verification"])
        self.assertTrue(all(c["synthetic"] == 1 and c["actual_tokens"] is None for c in result["calls"]))
        self.assertEqual({"writer", "gatekeeper", "reflection"}, {c["module"] for c in result["calls"]})
        self.assertEqual("done", result["reflections"][0]["status"])
        proposal = self.app.inner_state(self.agent, "synthetic-app", "reflection")[0]
        self.assertEqual("suggestion_only", proposal["payload"]["effect"])
        self.assertEqual(card["current_revision_id"], proposal["payload"]["target_version"])
        count = len(self.provider.calls)
        self.app = Application(self.root)
        self.insert()
        self.tick()
        self.assertEqual(count, len(self.provider.calls))
        self.assertEqual(1, len(self.cards()))

    def test_writer_failure_retry_and_gate_failure_retry(self):
        self.provider.fail = "writer"
        self.assertEqual("error", self.tick()["jobs"][0]["status"])
        self.assertEqual([], self.cards())
        self.provider.fail = "gatekeeper"
        result = self.tick()
        self.assertEqual("error", result["jobs"][0]["status"])
        self.assertIn("synthetic transient", result["candidates"][0]["last_error"])
        self.app = Application(self.root)
        self.assertEqual("done", self.tick()["jobs"][0]["status"])
        self.assertEqual(1, len(self.cards()))
        self.assertEqual(2, sum(m == "writer" for m, _ in self.provider.calls))

    def test_crash_after_canonical_commit_recovers_without_duplicate(self):
        with patch("thalemora.app.maintenance.ops.mark_candidate_applied", side_effect=RuntimeError("synthetic crash")):
            self.assertEqual("error", self.tick()["jobs"][0]["status"])
        self.assertEqual(1, len(self.cards()))
        self.app = Application(self.root)
        count = len(self.provider.calls)
        result = self.tick()
        self.assertEqual("done", result["jobs"][0]["status"])
        self.assertEqual(count, len(self.provider.calls))
        self.assertEqual(1, len(self.cards()))
        self.assertEqual(1, len(self.app.inner_state(self.agent, "synthetic-app", "reflection")))

    def test_real_subprocess_restart_does_not_call_models_or_duplicate(self):
        self.tick()
        code = """
import sys
from thalemora.app import Application
from test_pre6_maintenance import SyntheticProvider
provider = SyntheticProvider()
result = Application(sys.argv[1]).maintenance_tick(sys.argv[2], 'synthetic-app', provider=provider)
assert not provider.calls, provider.calls
assert result['jobs'][0]['status'] == 'done'
"""
        completed = subprocess.run([sys.executable, "-c", code, str(self.root), self.agent],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(1, len(self.cards()))

    def test_reflection_commit_receipt_loss_is_idempotent(self):
        original = self.app.reflect
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic reflection receipt loss")
        with patch.object(self.app, "reflect", side_effect=crash):
            self.assertEqual("error", self.tick()["reflections"][0]["status"])
        self.app = Application(self.root)
        calls = len(self.provider.calls)
        self.assertEqual("done", self.tick()["reflections"][0]["status"])
        self.assertEqual(calls, len(self.provider.calls))
        self.assertEqual(1, len(self.app.inner_state(self.agent, "synthetic-app", "reflection")))

    def test_reflection_model_failure_retries_without_rewriting_memory(self):
        self.provider.fail = "reflection"
        self.assertEqual("error", self.tick()["reflections"][0]["status"])
        self.assertEqual(1, len(self.cards()))
        self.assertEqual("done", self.tick()["reflections"][0]["status"])
        self.assertEqual(1, sum(m == "writer" for m, _ in self.provider.calls))

    def test_nonmanual_cas_race_does_not_overwrite_newer_revision(self):
        self.tick()
        card = self.cards()[0]
        self.insert(version=2, text="合成来源修订约定")
        self.provider.action = "correct"
        def concurrent(module, payload):
            if module == "gatekeeper":
                revision_store.update_card(self.app.memory_path(self.agent, "synthetic-app", "main"),
                    card_id=card["id"], expected_current_revision_id=card["current_revision_id"],
                    content="合成并发的较新修订", origin="auto_writer", change_type="correct")
                self.provider.hook = None
        self.provider.hook = concurrent
        result = self.tick()
        self.assertIn("RevisionConflict", result["jobs"][0]["error"])
        self.assertEqual("合成并发的较新修订", self.cards()[0]["content"])
        self.assertEqual("done", self.tick()["jobs"][0]["status"])
        self.assertEqual(1, len(self.cards()))

    def test_extension_uses_existing_memory_revision_store(self):
        self.tick()
        card = self.cards()[0]
        self.insert(version=2, text="合成项目增加一项长期约定")
        self.provider.action = "extend"
        result = self.tick()
        self.assertEqual("done", result["jobs"][0]["status"])
        after = self.cards()[0]
        self.assertEqual({k: v for k, v in card.items() if k != 'extensions'},
                         {k: v for k, v in after.items() if k != 'extensions'})
        self.assertEqual(len(after['extensions']), 1)
        self.assertEqual(after['extensions'][0]['content'], '合成项目增加一项长期约定')
        self.assertTrue(after['extensions'][0]['eligible'])
        path = self.app.memory_path(self.agent, "synthetic-app", "main")
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM memory_extensions").fetchone()[0])

    def test_receipt_recovery_after_source_deleted_does_not_report_false_drop(self):
        with patch("thalemora.app.maintenance.applied_receipt", side_effect=RuntimeError("synthetic crash before receipts")):
            self.tick()
        self.assertEqual(1, len(self.cards()))
        self.insert(version=2, deleted=True)
        result = self.tick()
        self.assertEqual("applied", result["candidates"][0]["status"])
        evidence = self.app.evidence(self.agent, "synthetic-app", self.cards()[0]["id"])[0]
        self.assertEqual("deleted", evidence["resolution"])
        self.assertIsNone(evidence["current_source_text"])
        self.assertEqual(1, sum(m == "writer" for m, _ in self.provider.calls))

    def test_writer_cannot_cite_foreign_or_invented_source(self):
        original = self.provider.call
        def invalid(module, payload):
            result = original(module, payload)
            if module == "writer":
                result["output"]["candidates"][0]["evidence"][0]["source_event_id"] = "foreign-synthetic-event"
            return result
        with patch.object(self.provider, "call", side_effect=invalid):
            result = self.tick()
        self.assertIn("exact Archive quote", result["jobs"][0]["error"])
        self.assertEqual([], self.cards())
        self.assertEqual(["writer"], [m for m, _ in self.provider.calls])

    def test_same_version_conflict(self):
        with self.assertRaises(VersionConflict):
            self.insert(text="合成不同内容")
        self.tick()
        self.assertEqual("合成天文项目长期采用紫色符号。", self.cards()[0]["content"])

    def test_changed_during_model_cannot_commit_stale_evidence(self):
        def edit(module, payload):
            if module == "gatekeeper":
                self.insert(version=2, text="合成来源已经改成黄色符号。")
                self.provider.hook = None
        self.provider.hook = edit
        result = self.tick()
        self.assertIn("stale_archive_source", result["jobs"][0]["error"])
        self.assertEqual([], self.cards())
        result = self.tick()
        self.assertIn("stale", {j["status"] for j in result["jobs"]})
        self.assertEqual("合成来源已经改成黄色符号。", self.cards()[0]["content"])

    def test_deleted_during_model_and_after_restart(self):
        def delete(module, payload):
            if module == "gatekeeper":
                self.insert(version=2, deleted=True)
                self.provider.hook = None
        self.provider.hook = delete
        self.tick()
        self.assertEqual([], self.cards())
        self.app = Application(self.root)
        self.tick()
        self.assertEqual([], self.cards())
        self.assertEqual(1, sum(m == "writer" for m, _ in self.provider.calls))

    def test_manual_revision_race_cas_and_fields_preserved(self):
        self.tick()
        card = self.cards()[0]
        self.insert(version=2, text="合成项目的新修订约定")
        self.provider.action = "correct"
        def manual(module, payload):
            if module == "gatekeeper":
                self.app.revise_memory(self.agent, "synthetic-app", card["id"], "人工最终约定", card["current_revision_id"])
                self.provider.hook = None
        self.provider.hook = manual
        result = self.tick()
        self.assertEqual("error", result["jobs"][0]["status"])
        self.assertEqual("人工最终约定", self.cards()[0]["content"])
        self.assertEqual(1, len(self.cards()))

    def test_explicit_metadata_protected(self):
        self.tick()
        card = self.cards()[0]
        memory = self.app.memory_path(self.agent, "synthetic-app", "main")
        with closing(sqlite3.connect(memory)) as db, db:
            db.execute("UPDATE cards SET importance=1 WHERE id=?", (card["id"],))
            field_sources.mark(db, card["id"], {"importance": "user_explicit"})
        self.insert(version=2, text="合成项目的新修订约定")
        self.provider.action = "correct"
        result = self.tick()
        self.assertIn("explicit fields", result["jobs"][0]["error"])
        self.assertEqual(card["content"], self.cards()[0]["content"])

    def test_revision_and_drop_and_uncertain(self):
        self.tick()
        card = self.cards()[0]
        self.insert(version=2, text="合成项目修订后的长期约定")
        self.provider.action = "correct"
        self.tick()
        revised = self.cards()[0]
        self.assertEqual(1, len(self.cards()))
        self.assertNotEqual(card["current_revision_id"], revised["current_revision_id"])
        self.assertEqual("合成项目修订后的长期约定", revised["content"])
        self.insert(version=3, text="合成待判断约定")
        self.provider.action = "uncertain"
        self.assertEqual("pending", self.tick()["jobs"][0]["status"])
        self.provider.action = "drop"
        self.assertIn("dropped", {c["status"] for c in self.tick()["candidates"]})
        self.assertEqual(revised, self.cards()[0])

    def test_scope_isolation_and_permissions(self):
        self.tick()
        other_agent = self.app.register_agent("synthetic-other")["agent_id"]
        with self.assertRaises(AccessDenied):
            self.app.maintenance_tick(other_agent, "synthetic-app", provider=self.provider)
        for app_id, channel in (("synthetic-other-app", "main"), ("synthetic-app", "private")):
            self.app.grant_source(self.agent, app_id, channel)
            self.insert(app=app_id, channel=channel)
            provider = SyntheticProvider()
            self.app.maintenance_tick(self.agent, app_id, channel, provider=provider)
            source = next(p["sources"] for m, p in provider.calls if m == "writer")
            self.assertTrue(all(r["app"] == app_id and r["channel"] == channel for r in source))
            frozen = next(json.loads(p["messages"][-1]["content"])["frozen_cards"]
                          for m, p in provider.calls if m == "gatekeeper")
            self.assertEqual([], frozen)
        self.app.grant_source(other_agent, "synthetic-app")
        self.assertEqual([], self.app.maintenance_tick(other_agent, "synthetic-app", provider=self.provider)["jobs"])

    def test_empty_writer_skips_and_live_scene_waits(self):
        self.provider.empty = True
        result = self.tick()
        self.assertEqual("done", result["jobs"][0]["status"])
        self.assertEqual([], self.cards())
        self.assertEqual(["writer"], [m for m, _ in self.provider.calls])
        self.insert(source_event_id="fresh", occurred_at="2099-01-01T00:00:00+00:00")
        self.tick()
        self.assertEqual(["writer"], [m for m, _ in self.provider.calls])

    def test_no_foreground_or_configured_provider_calls(self):
        with patch.object(self.app.providers, "call", side_effect=AssertionError("must not call configured Providers")):
            self.insert()
            self.app.search(self.agent, "synthetic-app", "合成")
            self.app.maintenance_tick(self.agent, "synthetic-app")
        with self.assertRaisesRegex(ValueError, "synthetic"):
            self.app.maintenance_tick(self.agent, "synthetic-app", provider=object())


if __name__ == "__main__":
    unittest.main()
