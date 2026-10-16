"""复用管理台浏览器夹具，验证两个独立模型选择真正保存并影响召回。"""
import os
import unittest

from thalemora.app import Application
import test_p4_console as console_fixture
from test_p8_recall import Embedder


@unittest.skipUnless(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), "需显式指定隔离 Chromium")
class RecallConsoleTests(unittest.TestCase):
    setUp = console_fixture.P4ConsoleTests.setUp
    view = console_fixture.P4ConsoleTests.view

    def test_independent_model_selectors_persist_and_drive_recall(self):
        embedder = Embedder()
        calls = []
        def rerank(query, docs):
            calls.append((query, docs))
            return [1.0] * len(docs)
        self.service.providers.local_models.update(embedding=("fake-e", embedder), rerank=("fake-r", rerank))
        self.service.import_history(self.a, "demo-app", [dict(
            app="demo-app", channel="main", source_event_id="tea", version=1, role="user", text="我喜欢乌龙茶",
            occurred_at="2026-10-16T01:00:00Z", conversation_id="synthetic", epoch_id=None,
            deleted=False, content_hash=None, hash_verification="unknown", raw_event_id=None)])
        self.service.remember(self.a, "demo-app", "tea")
        self.view("模型与用量")
        for title, model, button in (("Embedding · 召回向量", "fake-e", "保存 embedding"),
                                     ("Rerank · 候选重排", "fake-r", "保存 rerank")):
            section = self.page.locator("details").filter(has=self.page.locator("summary", has_text=title))
            section.locator("summary").click()
            section.locator("select").select_option("local")
            section.get_by_label("本地模型标识或目录 / API 模型名", exact=True).fill(model)
            section.get_by_role("button", name=button, exact=True).click()
            self.page.get_by_text("配置已保存", exact=True).wait_for()
            self.page.wait_for_function("!document.querySelector('#refresh').disabled")
        result = self.service.recall_for_turn(self.a, "demo-app", "乌龙茶")
        self.assertTrue(result["rendered"])
        self.assertTrue(embedder.inputs)
        self.assertEqual(len(calls), 1)
        section = self.page.locator("details").filter(has=self.page.locator("summary", has_text="Embedding · 召回向量"))
        section.locator("summary").click()
        self.assertEqual(section.locator("select").input_value(), "local")
        section.locator("select").select_option("disabled")
        section.get_by_role("button", name="保存 embedding", exact=True).click()
        self.page.wait_for_function("!document.querySelector('#refresh').disabled")
        # 等保存完成后从新实例读磁盘，证明不是只改了页面。
        self.page.get_by_text("配置已保存", exact=True).wait_for()
        restarted = Application(self.service.registry.root)
        self.assertEqual(restarted.providers.status("embedding")["config"]["mode"], "disabled")
        self.assertEqual(restarted.providers.status("rerank")["config"]["mode"], "local")
        self.assertEqual(self.errors, [])
        self.assertEqual(self.external, [])
