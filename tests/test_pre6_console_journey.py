"""PRE6-1：从空实例以真实浏览器完成首次使用、重启与持久回读；全程合成。"""
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest

from playwright.sync_api import sync_playwright
import uvicorn

from thalemora.app import Application
from thalemora.app.http import create_http


class FirstUseJourney(unittest.TestCase):
    def serve(self):
        self.service = Application(self.state)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(
            create_http(self.service, "synthetic-pre6-token"), log_level="error", access_log=False
        ))
        thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() >= deadline:
                raise RuntimeError("isolated Uvicorn did not start")
            time.sleep(.02)
        self.active = (server, thread, sock)
        return f"http://127.0.0.1:{sock.getsockname()[1]}"

    def stop_server(self):
        if getattr(self, "active", None):
            server, thread, sock = self.active
            server.should_exit = True
            thread.join(timeout=5)
            sock.close()
            self.assertFalse(thread.is_alive(), "isolated server did not stop")
            self.active = None

    def open_console(self, url):
        self.page.goto(url)
        self.page.locator("#token").fill("synthetic-pre6-token")
        self.page.get_by_role("button", name="连接本地实例").click()
        self.page.locator("#workspace").wait_for(state="visible")
        self.page.wait_for_function("!document.querySelector('#refresh').disabled")

    def view(self, title):
        self.page.locator("nav").get_by_role("button", name=title, exact=True).click()
        self.page.locator("#title").filter(has_text=title).wait_for()
        self.page.wait_for_function("!document.querySelector('#refresh').disabled")

    def screenshot(self, filename):
        target = Path(os.environ["PRE6_ARTIFACTS_DIR"])
        target.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(target / filename), full_page=True)

    def test_first_use_restart_and_rescope(self):
        base = Path(__file__).resolve().parents[1]
        (base / "tests/.tmp").mkdir(exist_ok=True, parents=True)
        with TemporaryDirectory(dir=base / "tests/.tmp", prefix="pre6-state-") as temp:
            self.state = Path(temp) / "state"
            self.active = None
            self.addCleanup(self.stop_server)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    self.page = browser.new_page(viewport={"width": 1280, "height": 900})
                    errors = []
                    self.page.on("pageerror", lambda err: errors.append(str(err)))
                    first_url = self.serve()
                    self.page.goto(first_url)
                    self.page.locator("#token").fill("incorrect-token")
                    self.page.get_by_role("button", name="连接本地实例").click()
                    self.page.locator("#notice.error").filter(has_text="认证失败").wait_for()
                    self.assertTrue(self.page.locator("#workspace").is_hidden())
                    self.open_console(first_url)
                    self.page.get_by_text("先在左侧注册一个 Agent").wait_for()
                    self.screenshot("01-empty-desktop.png")

                    self.page.locator("#agentName").fill("合成的旅行助手")
                    self.page.locator("#agentForm button").click()
                    self.page.locator("#notice").filter(has_text="Agent 已注册").wait_for()
                    agent = self.page.locator("#agent").input_value()
                    self.assertTrue(agent)
                    self.page.get_by_role("button", name="授权此来源/通道").click()
                    self.page.locator("#notice").filter(has_text="已授权").wait_for()
                    self.view("历史与原话")
                    self.page.locator('input[type="file"]').set_input_files(
                        str(base / "docs/examples/p4_history.json")
                    )
                    self.page.get_by_role("heading", name="synthetic-001", exact=True).wait_for()
                    self.page.get_by_label("历史原文检索（1–200 字符）").fill("旅行计划")
                    self.page.get_by_role("button", name="检索", exact=True).click()
                    self.page.wait_for_function("document.querySelectorAll('#content .record').length===1")
                    self.page.get_by_role("button", name="原文存为人工记忆").click()
                    self.page.locator("#detailBody").filter(has_text="card_id").wait_for()
                    self.page.get_by_role("button", name="关闭", exact=True).click()

                    self.view("Memory · 长期记忆")
                    self.page.locator("#content").get_by_text("纯合成示例：我想整理一次旅行计划。").wait_for()
                    self.page.get_by_role("button", name="查看来源证据").click()
                    self.page.locator("#detailBody").filter(has_text="current_source_text").wait_for()
                    self.page.locator("#detailBody").filter(has_text="纯合成示例").wait_for()
                    self.page.get_by_role("button", name="关闭", exact=True).click()
                    card = self.service.memory(agent, "demo-app")[0]

                    self.view("Water · 水系")
                    self.page.get_by_text("添加湖 / 溪流 / 支流", exact=True).click()
                    self.page.get_by_label("节点 ID", exact=True).fill("synthetic-lake")
                    self.page.get_by_label("类型：lake / stream / branch").fill("lake")
                    self.page.get_by_label("标题", exact=True).fill("合成旅行湖")
                    self.page.get_by_label("上级 ID（根湖填 -）").fill("-")
                    self.page.get_by_role("button", name="创建节点").click()
                    self.page.get_by_role("heading", name="合成旅行湖").wait_for()
                    self.page.get_by_text("添加湖 / 溪流 / 支流", exact=True).click()
                    self.page.get_by_label("节点 ID", exact=True).fill("synthetic-stream")
                    self.page.get_by_label("类型：lake / stream / branch").fill("stream")
                    self.page.get_by_label("标题", exact=True).fill("合成行程溪")
                    self.page.get_by_label("上级 ID（根湖填 -）").fill("synthetic-lake")
                    self.page.get_by_role("button", name="创建节点").click()
                    self.page.get_by_role("heading", name="合成行程溪").wait_for()
                    self.page.get_by_text("添加湖 / 溪流 / 支流", exact=True).click()
                    self.page.get_by_label("Memory ID").fill(str(card["id"]))
                    self.page.get_by_label("目标溪流/支流 ID（海填 -）").fill("synthetic-stream")
                    self.page.get_by_label("调整理由").fill("合成体验归属")
                    self.page.get_by_role("button", name="调整归属").click()
                    self.page.locator("#content").get_by_text("synthetic-stream", exact=True).wait_for()
                    self.screenshot("02-water-desktop.png")
                    self.page.set_viewport_size({"width": 375, "height": 820})
                    self.screenshot("03-water-mobile.png")
                    dims = self.page.evaluate("({width:Math.max(document.body.scrollWidth,document.documentElement.scrollWidth), viewport:innerWidth})")
                    self.assertLessEqual(dims["width"], dims["viewport"])

                    self.view("历史与原话")
                    self.page.locator("#channel").select_option("private")
                    self.page.locator("#notice.error").filter(has_text="授权").wait_for()
                    self.page.locator("#channel").select_option("main")
                    self.page.get_by_role("heading", name="synthetic-001", exact=True).wait_for()
                    self.stop_server()

                    second_url = self.serve()
                    self.open_console(second_url)
                    self.assertEqual(self.page.locator("#agent").input_value(), agent)
                    self.page.get_by_role("heading", name="synthetic-001", exact=True).wait_for()
                    self.view("Memory · 长期记忆")
                    self.page.get_by_role("button", name="查看来源证据").click()
                    self.page.locator("#detailBody").filter(has_text="current_source_text").wait_for()
                    self.page.get_by_role("button", name="关闭", exact=True).click()
                    self.view("Water · 水系")
                    self.page.get_by_role("heading", name="合成行程溪").wait_for()
                    self.page.locator("#content").get_by_text("synthetic-stream", exact=True).wait_for()
                    self.page.locator("#count").filter(has_text="2 个节点 · 1 条归属").wait_for()
                    self.screenshot("04-reopened-mobile.png")
                    self.assertEqual(self.service.water_memberships(agent, "demo-app")[0]["container_node_id"], "synthetic-stream")
                    self.assertEqual(len(self.service.history(agent, "demo-app")), 2)
                    self.assertEqual(len(self.service.memory(agent, "demo-app")), 1)
                    # 合成归属数超过节点数与单页容量时，第二页仍须可见。
                    for _ in range(20):
                        extra = self.service.remember(agent, "demo-app", "synthetic-001")
                        self.service.water_move(agent, "demo-app", extra["card_id"],
                                                "synthetic-stream", "合成分页验证")
                    self.page.locator("#refresh").click()
                    self.page.locator("#count").filter(has_text="20 条归属").wait_for()
                    self.assertTrue(self.page.locator("#next").is_enabled())
                    self.page.locator("#next").click()
                    self.page.locator("#count").filter(has_text="0 个节点 · 1 条归属").wait_for()
                    self.assertEqual(self.page.locator("#content .record").count(), 1)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()
                    self.stop_server()


if __name__ == "__main__":
    unittest.main()
