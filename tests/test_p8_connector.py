"""P8-3：临时合成数据与 ASGI 接口，不访问真实网络或模型。"""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

from thalemora.app import Application
from thalemora.app.http import create_http
from thalemora.app.providers import ProviderUnavailable


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="p8-connector-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = Application(self.root)
        self.agent = self.service.register_agent("合成 Agent")["agent_id"]
        self.service.grant_source(self.agent, "demo")
        self.client = TestClient(create_http(self.service, "synthetic-admin"))
        self.addCleanup(self.client.close)
        self.admin = {"Authorization": "Bearer synthetic-admin"}
        self.network = patch("thalemora.app.providers.urlopen", side_effect=AssertionError("禁止联网"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.credential = self.issue()
        self.headers = {"Authorization": "Bearer " + self.credential["token"]}

    def issue(self, **updates):
        response = self.client.post("/api/issue_recall_token", headers=self.admin,
                                    json=dict(agent_id=self.agent, app_id="demo", **updates))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def insert(self, event_id, text, *, agent=None, app="demo", channel="main"):
        self.service.import_history(agent or self.agent, app, [dict(
            app=app, channel=channel, source_event_id=event_id, version=1, role="user", text=text,
            occurred_at="2026-10-16T01:00:00Z", conversation_id="synthetic", epoch_id=None,
            deleted=False, content_hash=None, hash_verification="unknown", raw_event_id=None)], channel)

    def recall(self, **updates):
        return self.client.post("/api/recall", headers=self.headers,
                                json={"message": "以前喜欢什么茶？", **updates})

    def test_admin_issue_revoke_persistence_and_hash_only(self):
        for headers in ({}, self.headers):
            for op, args in (("issue_recall_token", dict(agent_id=self.agent, app_id="demo")),
                             ("revoke_recall_token", dict(token_id=self.credential["token_id"]))):
                self.assertEqual(self.client.post("/api/" + op, headers=headers, json=args).status_code, 401)
        with closing(sqlite3.connect(self.service.registry.registry_path)) as db:
            row = db.execute("SELECT * FROM recall_tokens").fetchone()
        self.assertNotIn(self.credential["token"], row)
        restarted = TestClient(create_http(Application(self.root), "synthetic-admin"))
        with restarted:
            self.assertEqual(restarted.post("/api/recall", headers=self.headers,
                                            json={"message": ""}).status_code, 200)
            response = self.client.post("/api/revoke_recall_token", headers=self.admin,
                                        json={"token_id": self.credential["token_id"]})
            self.assertEqual(response.json(), {"result": {"revoked": True}})
            self.assertEqual(restarted.post("/api/recall", headers=self.headers,
                                            json={"message": ""}).status_code, 401)

    def test_credentials_scope_and_source_checked_each_request(self):
        for headers in ({}, self.admin, {"Authorization": "Bearer wrong"}):
            self.assertEqual(self.client.post("/api/recall", headers=headers,
                                              json={"message": ""}).status_code, 401)
        other = self.service.register_agent("另一合成 Agent")["agent_id"]
        self.service.grant_source(other, "demo")
        self.service.grant_source(self.agent, "other")
        self.service.grant_source(self.agent, "demo", "private")
        self.insert("tea", "我喜欢乌龙茶")
        self.insert("secret", "我喜欢秘密茶", agent=other)
        self.insert("secret", "我喜欢隐藏茶", app="other")
        self.insert("secret", "我喜欢私有茶", channel="private")
        result = self.recall().json()
        self.assertIn("乌龙茶", result["rendered"])
        for text in ("秘密", "隐藏", "私有"):
            self.assertNotIn(text, result["rendered"])
        for key, value in (("agent_id", other), ("app_id", "other"), ("channel", "private"),
                           ("source", "other")):
            self.assertEqual(self.recall(**{key: value}).status_code, 400)
        denied = self.client.post("/api/issue_recall_token", headers=self.admin,
                                  json=dict(agent_id=other, app_id="ungranted"))
        self.assertEqual(denied.status_code, 403)
        with closing(sqlite3.connect(self.service.registry.registry_path)) as db, db:
            db.execute("DELETE FROM sources WHERE agent_id=? AND app_id='demo' AND channel='main'",
                       (self.agent,))
        self.assertEqual(self.recall(message="").status_code, 403)

    def test_related_unrelated_visible_and_minimal_response(self):
        self.insert("tea", "我喜欢乌龙茶")
        card = self.service.remember(self.agent, "demo", "tea")
        self.insert("raw", "上次买的乌龙茶来自合成茶店")
        response = self.recall()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["empty"])
        self.assertEqual(set(data), {"rendered", "memories", "archive", "empty", "errors"})
        self.assertEqual(set(data["memories"][0]), {"id", "revision_id"})
        self.assertEqual(data["archive"], [{"id": "raw", "version": 1}])
        for visible in (dict(visible_memory_ids=[card["card_id"]], visible_archive_ids=["raw"]),
                        dict(visible_archive_ids=["tea", "raw"]),
                        dict(visible_texts=["我喜欢乌龙茶", "上次买的乌龙茶来自合成茶店"])):
            self.assertTrue(self.recall(**visible).json()["empty"])
        self.assertTrue(self.recall(message="讲讲二分查找").json()["empty"])
        self.assertEqual(self.service.providers.audit(), [])

    def test_http_validation_and_failures_are_explicit_and_redacted(self):
        for body in ([], {}, {"message": None}, {"message": "x" * 201},
                     {"message": "", "visible_memory_ids": [True]},
                     {"message": "", "visible_archive_ids": "x"},
                     {"message": "", "visible_texts": [1]}):
            self.assertEqual(self.client.post("/api/recall", headers=self.headers, json=body).status_code, 400)
        self.assertEqual(self.client.post("/api/recall", headers=self.headers, content=b"{").status_code, 400)
        self.assertEqual(self.client.post("/api/recall", headers=self.headers,
                                          content=b"x" * (1024 * 1024 + 1)).status_code, 413)
        self.assertEqual(self.client.get("/api/recall", headers=self.headers).status_code, 405)
        for exception, status in ((RuntimeError("synthetic-admin private-text"), 500),
                                  (ProviderUnavailable("private-text"), 503),
                                  (TimeoutError("private-text"), 504)):
            with patch.object(self.service, "recall_for_turn", side_effect=exception):
                response = self.recall()
            self.assertEqual(response.status_code, status)
            self.assertNotIn("private-text", response.text)
            self.assertNotIn("synthetic-admin", response.text)
        with patch.object(self.service, "recall_for_turn", return_value={"errors": ["private-text"]}):
            self.assertEqual(self.recall().json(), {"error": "recall_unavailable"})
        # Rerank 只是可选层；失败时保留基础召回并告知降级，不泄漏异常正文。
        fallback = dict(rendered="[记忆 1] 合成旧事", memories=[dict(id=1, revision_id=1)],
                        archive=[], errors=[dict(module="rerank", error="private-text")])
        with patch.object(self.service, "recall_for_turn", return_value=fallback):
            response = self.recall()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["errors"], ["rerank_unavailable"])
        self.assertIn("合成旧事", response.json()["rendered"])
        self.assertNotIn("private-text", response.text)
        with patch("thalemora.app.http.RECALL_TIMEOUT_SECONDS", 0.01), patch.object(
                self.service, "recall_for_turn", side_effect=lambda **kw: time.sleep(0.08)):
            self.assertEqual(self.recall().status_code, 504)
        self.assertEqual(self.client.get("/api/list_agents", headers=self.admin).status_code, 200)

    def test_embedding_outage_keeps_real_application_lexical_recall_and_empty_warning(self):
        self.insert('tea','我喜欢乌龙茶')
        self.service.remember(self.agent,'demo','tea')
        self.insert('raw','乌龙茶在合成店铺有售')
        class Broken:
            def encode_many(self,texts,**kw): raise ProviderUnavailable('private-model-detail')
            def encode_one(self,text): raise AssertionError('document failure must not retry query')
        self.service.providers.local_models['embedding']=('synthetic',Broken())
        self.service.providers.configure('embedding',mode='local',model='synthetic')
        response=self.recall(message='乌龙茶')
        self.assertEqual(response.status_code,200)
        data=response.json()
        self.assertEqual(data['errors'],['embedding_unavailable'])
        self.assertTrue(data['memories'])
        self.assertEqual(data['archive'],[{'id':'raw','version':1}])
        self.assertNotIn('private-model-detail',response.text)
        empty=self.recall(message='量子计算')
        self.assertEqual(empty.status_code,200)
        self.assertTrue(empty.json()['empty'])
        self.assertEqual(empty.json()['errors'],['embedding_unavailable'])

    def test_documented_host_inserts_before_fake_upstream_and_continues_on_failure(self):
        doc = Path(__file__).resolve().parents[1] / "docs/RECALL_API.md"
        namespace = {}
        exec(compile(doc.read_text().split("## Python 发送前示例", 1)[1].split("```python\n", 1)[1].split("```", 1)[0], str(doc), "exec"), namespace)
        self.insert("tea", "我喜欢乌龙茶")
        events, errors = [], []
        test = self

        class SyntheticClient:
            async def post(self, url, **kwargs):
                events.append("recall")
                test.assertEqual(kwargs.pop("timeout"), 2.0)
                return test.client.post(url, **kwargs)

        async def upstream(context):
            events.append("upstream")
            return context

        original = [{"role": "user", "content": "以前喜欢什么茶？"}]

        def send(client):
            return asyncio.run(namespace["before_send"](
                original[0]["content"], original, recall_url="/api/recall",
                recall_token=self.credential["token"], client=client,
                send_upstream=upstream, report_error=errors.append))

        context = send(SyntheticClient())
        self.assertEqual(events, ["recall", "upstream"])
        self.assertIn("乌龙茶", context[-1]["content"])
        self.assertEqual(len(original), 1)
        self.assertEqual(errors, [])
        with patch.object(self.service, "recall_for_turn", side_effect=RuntimeError("synthetic")):
            self.assertEqual(send(SyntheticClient()), original)

        class TimeoutClient:
            async def post(self, *args, **kwargs):
                raise httpx.ReadTimeout("synthetic")

        self.assertEqual(send(TimeoutClient()), original)
        self.assertEqual(len(errors), 2)
        self.assertEqual(events[-1], "upstream")


if __name__ == "__main__":
    unittest.main()
