"""P8-2 隔离合成验收；模型/API 全部使用替身。"""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from thalemora.app import Application
from thalemora.app.providers import ProviderUnavailable
from thalemora.identity import AccessDenied


class Embedder:
    def __init__(self, dim=2):
        self.dim = dim
        self.inputs = []

    def encode_one(self, text):
        self.inputs.append(text)
        return [1.0] + [0.0] * (self.dim - 1)

    def encode_many(self, texts, *, batch_size=16):
        return [self.encode_one(t) for t in texts]


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="p8-recall-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.app = Application(self.root)
        self.agent = self.app.register_agent("合成 Agent")["agent_id"]
        self.other = self.app.register_agent("其他 Agent")["agent_id"]
        for agent in (self.agent, self.other):
            self.app.grant_source(agent, "demo")

    def event(self, event_id, text, *, app_id="demo", channel="main", version=1, deleted=False):
        return dict(app=app_id, channel=channel, source_event_id=event_id, version=version,
                    role="user", text=text, occurred_at="2026-10-16T01:00:00Z",
                    conversation_id="synthetic", epoch_id=None, deleted=deleted,
                    content_hash=None, hash_verification="unknown", raw_event_id=None)

    def insert(self, event_id, text, *, agent=None, app_id="demo", channel="main", **kwargs):
        self.app.import_history(agent or self.agent, app_id,
                                [self.event(event_id, text, app_id=app_id, channel=channel, **kwargs)], channel)

    def card(self, event_id, text, **kwargs):
        self.insert(event_id, text, **kwargs)
        return self.app.remember(kwargs.get("agent", self.agent), kwargs.get("app_id", "demo"),
                                 event_id, kwargs.get("channel", "main"))

    def recall(self, query="以前喜欢什么茶？", **kwargs):
        return self.app.recall_for_turn(self.agent, "demo", query, **kwargs)

    def test_lexical_related_unrelated_low_information_and_visible(self):
        card = self.card("tea", "我喜欢乌龙茶")
        self.insert("raw", "上次买的乌龙茶来自合成茶店")
        with patch("thalemora.app.providers.urlopen", side_effect=AssertionError("禁止联网")):
            result = self.recall()
            self.assertIn("乌龙茶", result["rendered"])
            self.assertEqual(len(result["memories"]), 1)
            self.assertEqual(result["archive"][0]["id"], "raw")
            self.assertLessEqual(len(result["rendered"]), 700)
            with patch("thalemora.retrieval.recall.RetrievalIndex._replace_lane",
                       side_effect=AssertionError("unchanged turn must not rebuild index")):
                self.assertTrue(self.recall()["rendered"])
            for query in ("", " ", "好的", "嗯", "谢谢", "继续", "我想问一下", "讲讲二分查找"):
                self.assertEqual(self.recall(query)["rendered"], "", query)
            self.assertEqual(self.recall(visible_memory_ids=[card["card_id"]],
                                         visible_archive_ids=["raw"])["rendered"], "")
            self.assertEqual(self.recall(visible_archive_ids=["tea", "raw"])["rendered"], "")
            self.assertEqual(self.recall(visible_texts=["我喜欢乌龙茶", "上次买的乌龙茶来自合成茶店"])["rendered"], "")
            self.assertEqual(self.app.providers.audit(), [])

    def test_archive_neighbors_roles_and_visible_suppression(self):
        for event_id, text in (("before", "先把杯子摆好"), ("anchor", "我喜欢乌龙茶"),
                               ("after", "好的我去烧水")):
            self.insert(event_id, text)
        with closing(sqlite3.connect(self.app.archive(self.agent, "demo", "main").path)) as db, db:
            for event_id, role, minute in (("before", "assistant", "00"),
                                            ("anchor", "user", "01"), ("after", "assistant", "02")):
                db.execute("UPDATE history_events SET epoch_id='epoch', role=?, occurred_at=? "
                           "WHERE source_event_id=?", (role, f"2026-10-16T01:{minute}:00Z", event_id))
        result = self.recall()
        self.assertEqual([item["id"] for item in result["archive"]], ["anchor", "before", "after"])
        self.assertIn("[历史原话 anchor] 我喜欢乌龙茶", result["rendered"])
        self.assertIn("[原话 anchor (user) 的前文 before v1 (assistant)]", result["rendered"])
        self.assertIn("[原话 anchor (user) 的后文 after v1 (assistant)]", result["rendered"])
        hidden = self.recall(visible_archive_ids=["before"], visible_texts=["好的我去烧水"])
        self.assertEqual([item["id"] for item in hidden["archive"]], ["anchor"])
        self.assertNotIn("先把杯子摆好", hidden["rendered"])
        self.assertNotIn("好的我去烧水", hidden["rendered"])
        self.assertEqual(self.recall(visible_archive_ids=["anchor"])["rendered"], "")

    def test_archive_neighbor_boundaries_and_tombstone_position(self):
        for event_id, text in (("far", "更早的位置"), ("before", "先把杯子摆好"),
                               ("anchor", "我喜欢乌龙茶"), ("after", "好的我去烧水")):
            self.insert(event_id, text)
        path = self.app.archive(self.agent, "demo", "main").path
        # 每次恢复同一组合成窗口，只改变边界条件。
        for boundary in ("conversation", "epoch", "deleted", "duplicate", "empty"):
            with self.subTest(boundary=boundary):
                with closing(sqlite3.connect(path)) as db, db:
                    for event_id, minute in (("far", "00"), ("before", "01"),
                                              ("anchor", "02"), ("after", "03")):
                        db.execute("UPDATE history_events SET conversation_id='synthetic', epoch_id='epoch', "
                                   "deleted=0, text=?, occurred_at=? WHERE source_event_id=?",
                                   ({"far": "更早的位置", "before": "先把杯子摆好",
                                     "anchor": "我喜欢乌龙茶", "after": "好的我去烧水"}[event_id],
                                    f"2026-10-16T01:{minute}:00Z", event_id))
                    if boundary == "conversation":
                        db.execute("UPDATE history_events SET conversation_id='other' WHERE source_event_id!='anchor'")
                    elif boundary == "epoch":
                        db.execute("UPDATE history_events SET epoch_id='other' WHERE source_event_id!='anchor'")
                    elif boundary == "deleted":
                        db.execute("UPDATE history_events SET deleted=1, text=NULL "
                                   "WHERE source_event_id IN ('before','after')")
                    elif boundary == "duplicate":
                        db.execute("UPDATE history_events SET occurred_at='2026-10-16T01:02:00Z' "
                                   "WHERE source_event_id='before'")
                    else:
                        db.execute("UPDATE history_events SET epoch_id='' ")
                result = self.recall()
                self.assertEqual([item["id"] for item in result["archive"]], ["anchor"])
                self.assertIn("我喜欢乌龙茶", result["rendered"])

    def test_archive_neighbor_time_gap_and_render_budget(self):
        for event_id, text in (("before", "先把杯子摆好"), ("anchor", "我喜欢乌龙茶"),
                               ("after", "好的我去烧水")):
            self.insert(event_id, text)
        path = self.app.archive(self.agent, "demo", "main").path
        with closing(sqlite3.connect(path)) as db, db:
            for event_id, instant in (("before", "2026-10-15T23:59:00Z"),
                                       ("anchor", "2026-10-16T01:00:00Z"),
                                       ("after", "2026-10-16T01:30:00Z")):
                db.execute("UPDATE history_events SET epoch_id='epoch', occurred_at=? "
                           "WHERE source_event_id=?", (instant, event_id))
        result = self.recall()
        self.assertEqual([item["id"] for item in result["archive"]], ["anchor", "after"])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE history_events SET occurred_at='2026-10-16T01:30:01Z' "
                       "WHERE source_event_id='after'")
        self.assertEqual([item["id"] for item in self.recall()["archive"]], ["anchor"])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE history_events SET occurred_at='2026-10-16T00:59:00Z', text=? "
                       "WHERE source_event_id='before'", ("杯" * 400,))
            db.execute("UPDATE history_events SET occurred_at='2026-10-16T01:01:00Z', text=? "
                       "WHERE source_event_id='after'", ("水" * 400,))
        result = self.recall()
        self.assertLessEqual(len(result["rendered"]), 700)
        self.assertEqual([item["id"] for item in result["archive"]], ["anchor", "before"])
        self.assertIn("杯" * 400, result["rendered"])
        self.assertNotIn("水" * 400, result["rendered"])

    def test_revision_refresh_tombstone_and_lineage(self):
        card = self.card("home", "我住甲城")
        self.assertIn("甲城", self.recall("我现在住哪？")["rendered"])
        self.app.revise_memory(self.agent, "demo", card["card_id"], "我住乙城", card["current_revision_id"])
        result = self.recall("我现在住哪？")
        self.assertIn("乙城", result["rendered"])
        self.assertNotIn("甲城", result["rendered"])
        path = self.app.memory_path(self.agent, "demo", "main")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE cards SET status='trashed' WHERE id=?", (card["card_id"],))
        self.assertEqual(self.recall("我现在住哪？")["rendered"], "")
        other = self.card("home2", "我住丙城")
        target = self.card("replacement", "合成占位")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO memory_card_lineage(from_card_id,to_card_id,from_revision_id,to_revision_id,"
                       "relation_type,origin,created_at) VALUES(?,?,?,?,'superseded_by','synthetic','2026-10-16')",
                       (other["card_id"], target["card_id"], other["current_revision_id"], target["current_revision_id"]))
        self.assertEqual(self.recall("我现在住哪？")["rendered"], "")

    def test_agent_app_channel_scope_before_model_and_empty_authorization(self):
        self.card("a", "我喜欢乌龙茶")
        self.card("b", "其他 Agent 喜欢红茶", agent=self.other)
        self.app.grant_source(self.agent, "other-app")
        self.card("x", "别的来源喜欢秘密茶", app_id="other-app")
        self.app.grant_source(self.agent, "demo", "private")
        self.card("p", "私密通道喜欢隐藏茶", channel="private")
        embedder = Embedder()
        self.app.providers.local_models["embedding"] = ("fake", embedder)
        self.app.providers.configure("embedding", mode="local", model="fake")
        text = self.recall()["rendered"]
        self.assertIn("乌龙茶", text)
        self.assertNotIn("秘密", text)
        self.assertNotIn("隐藏", text)
        self.assertFalse(any("秘密" in t or "隐藏" in t or "红茶" in t for t in embedder.inputs))
        with self.assertRaises(AccessDenied):
            self.app.recall_for_turn(self.other, "demo", "", "private")
        with self.assertRaises(AccessDenied):
            self.app.recall_for_turn(self.agent, "ungranted", "")

    def test_independent_local_modes_model_change_and_disable(self):
        self.card("tea", "我喜欢乌龙茶")
        self.insert("raw", "乌龙茶在合成店铺有售")
        embedder = Embedder()
        calls = []
        def rerank(query, docs):
            calls.append((query, docs))
            return list(range(len(docs)))
        local = Application(self.root, embedder=("e1", embedder), reranker=("r1", rerank))
        local.providers.configure("embedding", mode="local", model="e1")
        local.providers.configure("rerank", mode="local", model="r1")
        result = local.recall_for_turn(self.agent, "demo", "喜欢什么茶？")
        self.assertTrue(result["rendered"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(local.providers.status("embedding")["state"], "ready")
        self.assertEqual(local.providers.status("rerank")["state"], "ready")
        first_count = len(embedder.inputs)
        self.assertEqual(local.recall_for_turn(self.agent, "demo", "我想问一下")["rendered"], "")
        self.assertEqual(len(embedder.inputs), first_count)
        local.recall_for_turn(self.agent, "demo", "喜欢什么茶？")
        self.assertEqual(len(embedder.inputs), first_count + 1)  # 只新增 query，复用卡片向量
        newer = Embedder(dim=3)
        local.providers.local_models["embedding"] = ("e2", newer)
        local.providers.configure("embedding", mode="local", model="e2")
        local.recall_for_turn(self.agent, "demo", "喜欢什么茶？")
        self.assertTrue(any("乌龙茶" in t for t in newer.inputs))
        with closing(sqlite3.connect(local.memory_path(self.agent, "demo", "main").with_name("recall_1.sqlite"))) as db:
            self.assertEqual(db.execute("SELECT DISTINCT vector_dim FROM chunks").fetchall(), [(3,)])
        local.providers.configure("embedding", mode="disabled", model="e2")
        before = len(newer.inputs)
        self.assertTrue(local.recall_for_turn(self.agent, "demo", "乌龙茶")["rendered"])
        self.assertEqual(len(newer.inputs), before)
        self.assertEqual(local.providers.status("embedding")["state"], "disabled")
        restarted = Application(self.root)
        self.assertEqual(restarted.providers.status("embedding")["config"]["mode"], "disabled")
        local.providers.configure("rerank", mode="disabled", model="r1")
        count = len(calls)
        local.recall_for_turn(self.agent, "demo", "乌龙茶")
        self.assertEqual(len(calls), count)

    def test_api_embedding_rerank_endpoints_budget_and_errors(self):
        self.card("tea", "我喜欢乌龙茶")
        requests = []
        def urlopen(request, timeout):
            body = json.loads(request.data)
            requests.append((request.full_url, body))
            if request.full_url.endswith("/embeddings"):
                output = {"data": [{"embedding": [1.0, 0.0]}]}
            else:
                self.assertTrue(request.full_url.endswith("/rerank"))
                output = {"results": [{"index": i, "relevance_score": 0.9} for i in range(len(body["documents"]))]}
            output["usage"] = {"total_tokens": 8}
            return io.BytesIO(json.dumps(output).encode())
        with patch.dict(os.environ, {"P8_FAKE_KEY": "synthetic-only"}), patch("thalemora.app.providers.urlopen", urlopen):
            for module in ("embedding", "rerank"):
                self.app.providers.configure(module, mode="api", model="fake", base_url="https://synthetic.invalid/v1",
                                             key_env="P8_FAKE_KEY", call_budget=10, token_budget=1000)
            result = self.recall()
            self.assertTrue(result["rendered"])
            self.assertEqual(len(requests), 3)  # 卡片、query、rerank
            self.assertEqual([r[0].split("/")[-1] for r in requests], ["embeddings", "embeddings", "rerank"])
            self.assertEqual(self.app.providers.status("embedding")["known_tokens"], 16)
            self.app.providers.configure("rerank", mode="api", model="fake", base_url="https://synthetic.invalid/v1",
                                         key_env="P8_FAKE_KEY", call_budget=1, token_budget=1000)
            result = self.recall()
            self.assertTrue(result["errors"])
            self.assertTrue(result["rendered"])
            self.assertEqual(self.app.providers.status("rerank")["state"], "budget_exceeded")
            self.app.providers.configure("embedding", mode="disabled")
            self.app.providers.configure("rerank", mode="api", model="fake", base_url="https://synthetic.invalid/v1",
                                         key_env="P8_FAKE_KEY", call_budget=10, token_budget=1000)
            with patch("thalemora.app.providers.urlopen", side_effect=TimeoutError("synthetic timeout")):
                result = self.recall()
            self.assertTrue(result["errors"])
            self.assertEqual(self.app.providers.status("rerank")["state"], "error")
            self.assertEqual(self.app.providers.audit()[0]["status"], "error")
            with patch("thalemora.app.providers.urlopen", return_value=io.BytesIO(b'{"results":[]}')):
                self.assertTrue(self.recall()["errors"])

    def test_unconfigured_local_and_duplicate_text(self):
        self.card("tea", "我喜欢乌龙茶")
        self.insert("duplicate", "我喜欢乌龙茶")
        result = self.recall()
        self.assertEqual(len(result["memories"]) + len(result["archive"]), 1)
        self.assertEqual(self.app.providers.configure("embedding", mode="local", model="missing-local-model")["state"], "unconfigured")
        unavailable = self.recall()
        self.assertTrue(unavailable["rendered"])
        self.assertEqual(unavailable["errors"][0]["code"], "embedding_unavailable")
        self.app.providers.configure("embedding", mode="disabled")
        def broken(query, docs):
            raise RuntimeError("synthetic local failure")
        self.app.providers.local_models["rerank"] = ("broken", broken)
        self.app.providers.configure("rerank", mode="local", model="broken")
        result = self.recall()
        self.assertTrue(result["rendered"])
        self.assertTrue(result["errors"])
        self.assertEqual(self.app.providers.status("rerank")["state"], "error")


if __name__ == "__main__":
    unittest.main()
