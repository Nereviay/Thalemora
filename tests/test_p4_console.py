"""真实 Uvicorn + 隔离 Chromium；仅使用合成数据，不需要 Docker 或云端模型。"""
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest

import uvicorn

from thalemora.app import Application
from thalemora.app.http import create_http


@unittest.skipUnless(os.environ.get('PLAYWRIGHT_BROWSERS_PATH'), '显式指定隔离 Chromium 路径后运行浏览器验收')
class P4ConsoleTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.tmp = TemporaryDirectory(dir=os.environ['TMPDIR'], prefix='p4-console-')
        self.addCleanup(self.tmp.cleanup)
        self.service = Application(Path(self.tmp.name) / 'state')
        self.a = self.service.register_agent('合成旅行助手')['agent_id']
        self.service.grant_source(self.a, 'demo-app')
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        self.addCleanup(sock.close)
        self.url = f'http://127.0.0.1:{sock.getsockname()[1]}'
        self.server = uvicorn.Server(uvicorn.Config(create_http(self.service, 'synthetic-browser-token'),
                                                   log_level='error', access_log=False))
        thread = Thread(target=self.server.run, kwargs={'sockets':[sock]}, daemon=True)
        thread.start()
        def stop():
            self.server.should_exit = True
            thread.join(timeout=5)
        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError('隔离 Uvicorn 未启动')
            time.sleep(.01)
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={'width':1280,'height':900})
        self.errors = []
        self.external = []
        self.page.on('pageerror', lambda error:self.errors.append(str(error)))
        self.page.on('request', lambda request:self.external.append(request.url) if not request.url.startswith(self.url) else None)
        self.page.goto(self.url)
        self.page.locator('#token').fill('synthetic-browser-token')
        self.page.get_by_role('button', name='连接本地实例').click()
        self.page.locator('#workspace').wait_for(state='visible')
        self.page.locator('#agent').select_option(self.a)
        self.page.locator('#content .empty').wait_for()

    def view(self, title):
        self.page.locator('nav').get_by_role('button', name=title, exact=True).click()
        self.page.locator('#title').filter(has_text=title).wait_for()
        self.page.wait_for_function("!document.querySelector('#refresh').disabled")

    def screenshot(self, name):
        target = Path(os.environ.get('P4_ARTIFACTS_DIR', str(Path(os.environ['TMPDIR'])/'p4-browser')))
        target.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(target/name), full_page=True)

    def test_real_console_import_search_memory_water_persona(self):
        self.page.locator('input[type=file]').set_input_files(str(Path(__file__).resolve().parents[1]/'docs/examples/p4_history.json'))
        self.page.get_by_role('heading', name='synthetic-001', exact=True).wait_for()
        self.page.get_by_label('历史原文检索（1–200 字符）').fill('旅行计划')
        self.page.get_by_role('button', name='检索', exact=True).click()
        self.page.wait_for_function("document.querySelectorAll('#content .record').length===1")
        self.page.get_by_role('button', name='原文存为人工记忆').click()
        self.page.locator('#detail').wait_for(state='visible')
        self.page.get_by_role('button', name='关闭', exact=True).click()
        self.view('Memory · 长期记忆')
        self.page.get_by_role('button', name='查看来源证据').click()
        self.page.locator('#detailBody').filter(has_text='current').wait_for()
        self.page.get_by_role('button', name='关闭', exact=True).click()
        self.view('Water · 水系')
        self.page.get_by_text('添加湖 / 溪流 / 支流', exact=True).click()
        self.page.get_by_label('节点 ID', exact=True).fill('browser-lake')
        self.page.get_by_label('类型：lake / stream / branch').fill('lake')
        self.page.get_by_label('标题', exact=True).fill('合成旅行湖')
        self.page.get_by_label('上级 ID（根湖填 -）').fill('-')
        self.page.get_by_role('button', name='创建节点').click()
        self.page.get_by_role('heading', name='合成旅行湖').wait_for()
        self.view('Persona · 人格配置')
        self.page.get_by_label('本地管理的人格配置').fill('合成示例：认真整理来源。')
        self.page.get_by_label('配置理由').fill('浏览器合成验收')
        self.page.get_by_role('button', name='保存 managed Persona').click()
        self.page.locator('#content').get_by_text('合成示例：认真整理来源。',exact=True).wait_for()
        self.view('Memory · 长期记忆')
        self.screenshot('console-desktop.png')
        self.assertEqual(self.errors, [])
        self.assertEqual(self.external, [])
        self.assertEqual(len(self.service.memory(self.a,'demo-app')),1)

    def test_mobile_views_registration_and_private_denial(self):
        for width in (320,375,414,768):
            self.page.set_viewport_size({'width':width,'height':900})
            for title in ('历史与原话','Self Model · 自我认识','Emotion · 情绪','Diary · 日记','Dream · 梦境',
                          'Whisper · 低语','Reflection · 反思','Continuity · 连续性','来源授权','模型与用量','备份与恢复'):
                self.view(title)
                dimensions=self.page.evaluate('({page:Math.max(document.documentElement.scrollWidth,document.body.scrollWidth),viewport:innerWidth})')
                self.assertLessEqual(dimensions['page'],dimensions['viewport'],(width,title))
                overflow=self.page.evaluate("""Array.from(document.querySelectorAll('button,input,select')).filter(el=>{
                    if(!el.getClientRects().length) return false;
                    const r=el.getBoundingClientRect();
                    return r.left<0 || r.right>innerWidth+1 || (el.tagName==='BUTTON' && el.scrollWidth>el.clientWidth+1);
                }).map(el=>el.textContent||el.id)""")
                self.assertEqual(overflow,[],(width,title))
                self.assertTrue(self.page.locator('#content').inner_text().strip(),(width,title))
        self.view('历史与原话')
        self.page.locator('#channel').select_option('private')
        self.page.locator('#notice.error').filter(has_text='授权').wait_for()
        self.page.get_by_role('button',name='授权此来源/通道').click()
        self.page.wait_for_function("!document.querySelector('#refresh').disabled")
        self.page.locator('#content .empty').wait_for()
        self.assertEqual(self.service.history(self.a,'demo-app','private'),[])
        self.page.set_viewport_size({'width':375,'height':900})
        self.screenshot('console-mobile.png')
        self.page.locator('#agentName').fill('浏览器注册的第二 Agent')
        self.page.locator('#agentForm button').click()
        self.page.wait_for_function("document.querySelector('#agent').options.length===2")
        self.assertEqual(len(self.service.list_agents()),2)
        self.assertEqual(self.errors,[])
        self.assertEqual(self.external,[])


if __name__ == '__main__':
    unittest.main()
