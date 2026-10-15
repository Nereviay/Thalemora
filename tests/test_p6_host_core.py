"""P6-3 宿主兼容的关键实际契约，不访问生产资产。"""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

from thalemora.adapters.niannian_history import NiannianHistoryBinding
from thalemora.adapters.niannian_retrieval import NiannianRetrievalIndex
from thalemora.history import Archive
from thalemora.identity import OwnerPrincipal, AccessDenied
from thalemora.memory import schema, field_sources, revision_store as revisions, operations


class HostCoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="p6_host_core_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = self.root / "memory.sqlite"
        with sqlite3.connect(self.memory) as db:
            schema.initialize(db)
        self.card = revisions.create_card(self.memory, content="synthetic star memory",
                                         origin="manual_ui", source="manual",
                                         evidence_source_kind="manual",
                                         evidence_source_ref="synthetic")

    def test_keyword_intent_anchor_and_manual_rewrite(self):
        cid, rid = self.card["card_id"], self.card["current_revision_id"]
        changed = operations.apply_keyword_intent(self.memory, card_id=cid,
                                                  action="add", keyword="synthetic-key")
        result = operations.validate_episodic_anchors(self.memory, [{
            "card_id":cid, "anchored_revision_id":rid,"kind":"episodic_anchor"}])
        self.assertTrue(result[0]["valid"])
        revised = revisions.update_card(self.memory, card_id=cid,
            expected_current_revision_id=changed["current_revision_id"], content="synthetic rewrite")
        result = operations.validate_episodic_anchors(self.memory, [{
            "card_id":cid, "anchored_revision_id":rid,"kind":"episodic_anchor"}])
        self.assertFalse(result[0]["valid"])

    def test_scene_gate_manual_relation_is_read_only_and_dropped(self):
        from thalemora.gatekeeper import core as gate
        cid=self.card["card_id"]
        payload={"scene_admission":{"receipt_id":"synthetic-receipt","scene_id":"synthetic-scene",
                  "scene_revision_no":1,"source_fingerprint":"synthetic-fingerprint",
                  "gate_action":"new","frozen_memory_refs":[{"card_id":cid,
                  "revision_id":self.card["current_revision_id"]}]}}
        frozen=gate._scene_admission_retrieval_hints(payload,self.memory,include_read_only=True)
        self.assertTrue(frozen[0]["read_only"])
        decision=gate._parse_decision({"action":"correct","target_card_id":cid,
            "importance":7,"content":"synthetic automatic rewrite"},frozen,"synthetic")
        self.assertEqual("drop",decision["action"])
        self.assertEqual("manual_memory_read_only",decision["reason"])

    def test_legacy_explicit_field_remains_protected(self):
        cid = self.card["card_id"]
        with sqlite3.connect(self.memory) as db:
            field_sources.mark(db, cid, {"title":"chenlin_explicit"})
        with self.assertRaises(revisions.InvalidRevisionState):
            revisions.update_card(self.memory, card_id=cid,
                expected_current_revision_id=self.card["current_revision_id"],
                title="automatic overwrite", origin="auto_writer",change_type="derived_refresh")
        with sqlite3.connect(self.memory) as db:
            self.assertEqual("chenlin_explicit",field_sources.get(db,cid)["title"])

    def test_joint_retrieval_uses_shared_memory_core_and_raw_scope(self):
        raw = self.root / "raw.sqlite"
        with sqlite3.connect(raw) as db:
            db.execute("CREATE TABLE raw_events(id INTEGER PRIMARY KEY,source TEXT,"
                       "source_event_id TEXT,event_hash TEXT,role TEXT,text TEXT,"
                       "created_at TEXT,metadata_json TEXT,deleted INTEGER)")
            db.execute("INSERT INTO raw_events VALUES(1,'app','synthetic','synthetic-hash',"
                       "'user','synthetic star quote','2001-01-01','{}',0)")
            db.execute("INSERT INTO raw_events VALUES(2,'app','private','private-hash',"
                       "'user','synthetic private quote','2001-01-01','{\"channel\":\"private\"}',0)")
        index = NiannianRetrievalIndex(str(self.root / "index.sqlite"),
            memory_db_path=str(self.memory), embedding_model="synthetic", embedder=None)
        index.rebuild_memory()
        index.rebuild_raw(str(raw), None)
        hits = index.search_parallel("synthetic")
        self.assertEqual(1,len(hits["memory_hits"]))
        self.assertEqual([1],[h["source_id"] for h in hits["raw_hits"]])
        self.assertEqual("thalemora.retrieval.core",index._lexical_hits.__module__)
        class Embedder:
            def encode_one(self,text):
                return np.array([1,0],dtype=np.float32)
        result = index.refresh_memory_card(self.card["card_id"],embedder=Embedder())
        self.assertTrue(result["embedded"])
        self.assertIsNone(index.embedder)

    def test_batch_import_retains_prefix_and_rolls_back_failed_event_index(self):
        owner = OwnerPrincipal("synthetic-owner")
        binding = NiannianHistoryBinding(archive_path=self.root/"batch.sqlite",
            owner=owner, agent_id="existing-subject", apps=["niannian"])
        archive = Archive(binding,owner,"existing-subject")
        archive.ensure_short_index()
        events = [{"app":"niannian","channel":"main","source_event_id":str(i),
                   "version":1,"text":"合成批量","hash_verification":"unknown"} for i in range(3)]
        original = archive._update_short_index
        def fail_second(event,db):
            original(event,db)
            if event["source_event_id"]=="1":
                raise RuntimeError("synthetic-index-failure")
        with patch.object(archive,"_update_short_index",side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError,"synthetic-index"):
                archive.import_history(events)
        self.assertEqual(["0"],[r["source_event_id"] for r in archive.list_events("niannian")])
        archive.import_history(events)
        self.assertEqual(3,len(archive.list_events("niannian")))

    def test_legacy_water_proposer_is_preserved(self):
        from thalemora.water import schema as water_schema, store, growth
        cards = [self.card]
        for i in range(2):
            cards.append(revisions.create_card(
                self.memory, content=f"synthetic independent fact {i}",
                origin="manual_ui", source="manual",
                evidence_source_kind="manual", evidence_source_ref=f"synthetic-{i}"))
        with sqlite3.connect(self.memory) as db:
            db.row_factory = sqlite3.Row
            water_schema.initialize(db)
            store.create_node(db, node_id="synthetic-lake", node_type="lake",
                              title="合成湖", origin="synthetic")
            proposal = growth.create_proposal(
                db, "new_stream", "synthetic-lake", "合成支流", "合成依据",
                [card["card_id"] for card in cards], proposer="chenlin")
            row = db.execute(
                "SELECT proposer FROM water_growth_proposals WHERE proposal_id=?",
                (proposal,)).fetchone()
            self.assertEqual("chenlin", row["proposer"])

    def test_existing_non_uuid_subject_can_bind_history_without_new_identity(self):
        owner = OwnerPrincipal("synthetic-owner")
        binding = NiannianHistoryBinding(archive_path=self.root/"archive.sqlite",
            owner=owner, agent_id="existing-runtime-subject", apps=["niannian"])
        archive = Archive(binding,owner,"existing-runtime-subject")
        archive.upsert({"app":"niannian","channel":"main","source_event_id":"source-one",
                        "version":1,"text":"synthetic","hash_verification":"unknown"})
        self.assertEqual(1,len(archive.list_events("niannian")))
        with self.assertRaises(AccessDenied):
            binding.assert_source(owner,"existing-runtime-subject","niannian","private")
        with self.assertRaises(AccessDenied):
            binding.archive_db_path(owner,"other-agent")
        self.assertFalse((self.root/"registry.sqlite").exists())


if __name__=="__main__":
    unittest.main()
