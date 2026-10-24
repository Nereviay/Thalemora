"""F1：临时数据、禁联网，来源失效到最终召回的组合回归。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import thalemora
from thalemora.app import Application
from thalemora.memory.revision_store import create_card, create_card_in_transaction


class SourceValidityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="source-validity-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.app = Application(self.root)
        self.agent = self.app.register_agent("合成来源测试")["agent_id"]
        self.app.grant_source(self.agent, "demo")
        self.net = patch("thalemora.app.providers.urlopen", side_effect=AssertionError("禁止联网"))
        self.net.start()
        self.addCleanup(self.net.stop)

    def event(self, event_id="home", text="合成甲城住所", version=1, deleted=False,
              app_id="demo", channel="main", agent=None):
        self.app.import_history(agent or self.agent, app_id, [dict(
            app=app_id, channel=channel, source_event_id=event_id, version=version,
            text=text, deleted=deleted, role="user", occurred_at="2001-01-01T00:00:00Z",
            hash_verification="unknown")], channel)

    def ref(self, event_id="home", version=1, **changes):
        return dict(kind="archive_quote", agent_id=self.agent, app="demo", channel="main",
                    source_event_id=event_id, version=version, **changes)

    def card(self, refs=None, *, content="合成甲城住所", source="auto", origin="auto_writer"):
        path = self.app.memory_path(self.agent, "demo", "main")
        result = create_card(path, content=content, source=source, origin=origin,
                             evidence_rows=[dict(source_kind="derived", source_ref=json.dumps(r))
                                            for r in (refs if refs is not None else [self.ref()])])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS archive_card_scopes(card_id INTEGER PRIMARY KEY,app TEXT,channel TEXT)")
            db.execute("INSERT INTO archive_card_scopes VALUES(?,?,?)", (result["card_id"], "demo", "main"))
        return result

    def recall(self, query="甲城"):
        return self.app.recall_for_turn(self.agent, "demo", query)

    def test_main_tombstone_and_private_copy_stay_isolated(self):
        self.event()
        self.app.remember(self.agent, "demo", "home")
        self.app.grant_source(self.agent, "demo", "private")
        self.event(text=None, version=2, deleted=True)
        self.event(text="合成私密甲城住所", channel="private")
        self.app.remember(self.agent, "demo", "home", "private")
        self.app = Application(self.root)
        self.assertEqual(self.recall()["rendered"], "")
        self.assertIn("私密", self.app.recall_for_turn(self.agent, "demo", "甲城", "private")["rendered"])

    def test_synthetic_writer_pipeline_then_source_revocation(self):
        from test_pre6_maintenance import SyntheticProvider
        self.event()
        result = self.app.maintenance_tick(self.agent, "demo", provider=SyntheticProvider())
        self.assertEqual(result["jobs"][0]["status"], "done")
        self.assertTrue(self.recall()["memories"])
        self.event(text=None, version=2, deleted=True)
        self.app = Application(self.root)
        self.assertEqual(self.recall()["rendered"], "")
        self.assertTrue(self.app.memory(self.agent, "demo"))

    def test_remember_reads_updated_source_and_rejects_deleted_source(self):
        self.event()
        self.event(text="合成乙城新版住所", version=2)
        card = self.app.remember(self.agent, "demo", "home")
        self.assertEqual(json.loads(self.app.evidence(self.agent, "demo", card["card_id"])[0]["source_ref"])["version"], 2)
        self.event(text=None, version=3, deleted=True)
        with self.assertRaises(KeyError):
            self.app.remember(self.agent, "demo", "home")
        self.assertEqual(len(self.app.memory(self.agent, "demo")), 1)

    def test_raw_candidate_deleted_during_rerank_is_not_injected(self):
        self.event()
        def rerank(query, docs):
            self.event(text=None, version=2, deleted=True)
            return [1] * len(docs)
        self.app.providers.local_models["rerank"] = ("synthetic", rerank)
        self.app.providers.configure("rerank", mode="local", model="synthetic")
        self.assertEqual(self.recall()["rendered"], "")

    def test_automatic_revision_cannot_claim_manual_confirmation(self):
        self.card([dict(kind="mcp_explicit_manual_revision", app="demo", channel="main")])
        self.assertEqual(self.recall()["rendered"], "")

    def test_imports_this_worktree(self):
        self.assertEqual(Path(thalemora.__file__).resolve().parents[2], Path(__file__).resolve().parents[1])

    def test_changed_deleted_restart_and_new_version_words(self):
        for kind in ("remember", "derived"):
            with self.subTest(kind=kind):
                event_id = kind
                self.event(event_id)
                card = (self.app.remember(self.agent, "demo", event_id) if kind == "remember"
                        else self.card([self.ref(event_id)]))
                self.assertTrue(self.recall()["memories"])
                self.event(event_id, "合成乙城新版住所", version=2)
                self.app = Application(self.root)
                self.assertEqual(self.recall()["memories"], [])
                new = self.recall("乙城")
                self.assertEqual([(r["id"], r["version"]) for r in new["archive"]], [(event_id, 2)])
                self.assertEqual(self.app.evidence(self.agent, "demo", card["card_id"])[0]["resolution"], "source_changed")
                self.event(event_id, None, version=3, deleted=True)
                self.app = Application(self.root)
                self.assertEqual(self.recall()["rendered"], "")
                self.assertEqual(self.app.evidence(self.agent, "demo", card["card_id"])[0]["resolution"], "deleted")
                # 失效只阻止自动使用，不抹除正式卡和旧证据。
                self.assertTrue(any(r["id"] == card["card_id"] for r in self.app.memory(self.agent, "demo")))

    def test_missing_source_does_not_recall(self):
        self.card()
        self.assertEqual(self.recall()["rendered"], "")
        self.assertEqual(self.app.evidence(self.agent, "demo", 1)[0]["resolution"], "missing")

    def test_all_current_evidence_required(self):
        self.event("one")
        self.event("two", "合成佐证")
        card = self.card([self.ref("one"), self.ref("two")])
        self.assertEqual(self.recall()["memories"][0]["id"], card["card_id"])
        self.event("two", "合成变更", version=2)
        self.assertEqual(self.recall()["memories"], [])
        self.assertEqual([e["resolution"] for e in self.app.evidence(self.agent, "demo", card["card_id"])],
                         ["current", "source_changed"])

    def test_no_or_unusable_evidence_is_not_current_fact(self):
        for refs in ([], [None], ["free text"], [{"kind": "other"}],
                     [dict(self.ref(), version=True)], [dict(self.ref(), version="1")]):
            with self.subTest(refs=refs):
                card = self.card(refs)
                self.assertEqual(self.recall()["memories"], [])
                with self.assertRaises(PermissionError):
                    self.app.evidence(self.agent, "demo", card["card_id"])
                evidence = self.app.evidence(self.agent, None, card["card_id"], scope="owner-global")
                if evidence:
                    self.assertIn(evidence[0]["resolution"], {"invalid_reference", "unsupported"})

    def test_malformed_manual_evidence_is_visible_not_exception(self):
        path = self.app.memory_path(self.agent, "demo", "main")
        card = create_card(path, content="合成手工历史", source="manual", origin="manual_ui",
                           evidence_source_kind="manual", evidence_source_ref="not json")
        with self.assertRaises(PermissionError):
            self.app.evidence(self.agent, "demo", card["card_id"])
        evidence = self.app.evidence(self.agent, None, card["card_id"], scope="owner-global")
        self.assertEqual(evidence[0]["resolution"], "invalid_reference")
        self.assertFalse(evidence[0]["verified"])

    def test_explicit_manual_revision_survives_source_deletion(self):
        self.event()
        card = self.app.remember(self.agent, "demo", "home")
        self.event(text=None, version=2, deleted=True)
        self.assertEqual(self.recall()["rendered"], "")
        revised = self.app.revise_memory(self.agent, "demo", card["card_id"], "人工确认甲城知识",
                                        card["current_revision_id"])
        self.app = Application(self.root)
        result = self.recall()
        self.assertEqual(result["memories"][0]["revision_id"], revised["current_revision_id"])
        self.assertIn("人工确认", result["rendered"])
        self.assertEqual(self.app.evidence(self.agent, "demo", card["card_id"])[-1]["resolution"], "manual_revision")

    def test_current_revision_not_obsolete_evidence_controls_eligibility(self):
        from thalemora.memory.revision_store import update_card
        self.event()
        card = self.card()
        self.event(text="合成乙城新版住所", version=2)
        update_card(self.app.memory_path(self.agent, "demo", "main"), card_id=card["card_id"],
                    content="合成乙城新版住所", expected_current_revision_id=card["current_revision_id"],
                    origin="auto_writer", change_type="correct",
                    evidence_rows=[dict(source_kind="derived", source_ref=json.dumps(self.ref(version=2)))])
        self.assertTrue(self.recall("乙城")["memories"])
        self.assertEqual(self.recall("甲城")["rendered"], "")

    def test_retired_card_only_suppresses_its_referenced_version(self):
        self.event()
        card = self.app.remember(self.agent, "demo", "home")
        with closing(sqlite3.connect(self.app.memory_path(self.agent, "demo", "main"))) as db, db:
            db.execute("UPDATE cards SET status='trashed' WHERE id=?", (card["card_id"],))
        self.assertEqual(self.recall()["rendered"], "")
        self.event(text="合成乙城新版住所", version=2)
        self.assertEqual(self.recall("乙城")["archive"][0]["version"], 2)

    def test_scope_mismatch_neither_recalls_nor_suppresses_raw(self):
        other = self.app.register_agent("合成其他 Agent")["agent_id"]
        self.app.grant_source(other, "demo")
        self.app.grant_source(self.agent, "elsewhere")
        self.app.grant_source(self.agent, "demo", "private")
        self.event(text="合成乙城新版住所")
        for change in ({"agent_id": other}, {"app": "elsewhere"}, {"channel": "private"}):
            with self.subTest(change=change):
                card = self.card([dict(self.ref(), **change)])
                self.assertEqual(self.recall()["rendered"], "")
                self.assertEqual(self.recall("乙城")["archive"][0]["id"], "home")
                with self.assertRaises(PermissionError):
                    self.app.evidence(self.agent, "demo", card["card_id"])
                owner = self.app.memory(self.agent, scope="owner-global")
                self.assertIn(next(c for c in owner if c["id"] == card["card_id"])["ownership"]["state"], {"unknown", "conflict"})
        self.assertEqual(self.app.recall_for_turn(other, "demo", "甲城")["rendered"], "")
        self.assertEqual(self.app.recall_for_turn(self.agent, "elsewhere", "甲城")["rendered"], "")
        self.assertEqual(self.app.recall_for_turn(self.agent, "demo", "甲城", "private")["rendered"], "")

    def test_remember_holds_source_lock_through_card_commit(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                self.event(version=1 if not deleted else 3)
                archive = self.app.archive(self.agent, "demo", "main")
                incoming = dict(app="demo", source_event_id="home", version=2 if not deleted else 4,
                                text="合成乙城新版住所", deleted=deleted)
                original = create_card_in_transaction
                def competing_write():
                    with closing(sqlite3.connect(archive.path, timeout=0.05)) as db, db:
                        try:
                            db.execute("BEGIN IMMEDIATE")
                        except sqlite3.OperationalError as exc:
                            self.assertIn("locked", str(exc))
                            return "locked"
                        archive._upsert(incoming, db)
                        return "updated"
                def checked_create(*args, **kwargs):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        self.assertEqual(pool.submit(competing_write).result(timeout=2), "locked")
                    return original(*args, **kwargs)
                with patch("thalemora.app.service.create_card_in_transaction", checked_create):
                    card = self.app.remember(self.agent, "demo", "home")
                self.assertEqual(self.app.evidence(self.agent, "demo", card["card_id"])[0]["resolution"], "current")
                archive.upsert(incoming)
                self.assertEqual(self.recall()["memories"], [])

    def test_update_during_rerank_cannot_inject_stale_memory(self):
        self.event()
        self.app.remember(self.agent, "demo", "home")
        def rerank(query, docs):
            self.event(text=None, version=2, deleted=True)
            return [1] * len(docs)
        self.app.providers.local_models["rerank"] = ("synthetic", rerank)
        self.app.providers.configure("rerank", mode="local", model="synthetic")
        self.assertEqual(self.recall()["rendered"], "")


if __name__ == "__main__":
    unittest.main()
