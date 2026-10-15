"""PRE6-2：官方客户端、临时 loopback 服务、合成档案；不调用模型。"""
import asyncio
from contextlib import asynccontextmanager, closing
from datetime import timedelta
import json
import multiprocessing
import os
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from thalemora.app import Application
from thalemora.app.http import create_http
from thalemora.identity import OwnerPrincipal
from thalemora.memory.field_sources import get, mark


TOKEN = "synthetic-pre6-owner-token"
TOOLS = {"list_agents", "search", "history", "inner_state", "memory", "evidence",
         "import_history", "remember", "revise_memory"}


def run_http(root, listener):
    app = create_http(Application(root), TOKEN)
    uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on")).run(sockets=[listener])


@asynccontextmanager
async def serve(service):
    # SDK 依赖的 sse-starlette 持有进程级 shutdown 状态；每次真实重开用新进程。
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    process = multiprocessing.get_context("spawn").Process(
        target=run_http, args=(service.registry.root, listener))
    process.start()
    async def started():
        async with httpx.AsyncClient(timeout=1) as client:
            while process.is_alive():
                try:
                    if (await client.get(url + "/health")).status_code == 200:
                        return
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(0.01)
            raise RuntimeError(f"临时服务器退出：{process.exitcode}")
    try:
        await asyncio.wait_for(started(), 10)
        yield url
    finally:
        process.terminate()
        try:
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join)
                raise RuntimeError("临时服务器未正常关闭")
        finally:
            listener.close()
            process.close()


@asynccontextmanager
async def connect(url):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10) as client:
        async with streamable_http_client(url + "/mcp", http_client=client) as (read, write, session_id):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as session:
                await session.initialize()
                yield session, session_id


class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="pre6-mcp-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.service = Application(self.root)
        self.agent = self.service.register_agent("合成 Agent")['agent_id']
        self.app = "synthetic-app"
        self.service.grant_source(self.agent, self.app)
        self.domain = dict(agent_id=self.agent, app_id=self.app, channel="main")
        self.event = dict(app=self.app, channel="main", source_event_id="event-1", version=1,
                          role="user", text="合成旅行原文\n保留  空格🙂", occurred_at="2026-10-15T12:00:00+08:00",
                          conversation_id="conversation-1", epoch_id="epoch-1", content_hash="source-proof",
                          hash_verification="legacy_unverified", raw_event_id="raw-1", deleted=False)

    async def call(self, session, name, args):
        reply = await session.call_tool(name, args)
        self.assertFalse(reply.isError, reply.content)
        data = reply.structuredContent
        if data is None:
            values = [json.loads(item.text) for item in reply.content]
            return values[0] if name in {"remember", "revise_memory"} else values
        return data["result"] if set(data) == {"result"} else data

    async def error(self, session, name, args, message):
        reply = await session.call_tool(name, args)
        self.assertTrue(reply.isError, reply)
        self.assertIn(message, " ".join(item.text for item in reply.content))

    async def test_http_handshake_operations_cas_and_persistence(self):
        async with serve(self.service) as url, connect(url) as (session, _):
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            self.assertEqual(set(tools), TOOLS)
            self.assertEqual(tools["list_agents"].inputSchema["properties"], {
                "limit": {"default": 20, "title": "Limit", "type": "integer"},
                "offset": {"default": 0, "title": "Offset", "type": "integer"}})
            for name in ("history", "search", "inner_state"):
                schema = tools[name].inputSchema
                self.assertEqual(schema["required"], ["agent_id", "app_id"] +
                                 (["query"] if name == "search" else ["kind"] if name == "inner_state" else []))
                for key, default in (("channel", "main"), ("limit", 20), ("offset", 0)):
                    self.assertEqual(schema["properties"][key]["default"], default)
            self.assertEqual(set(tools["import_history"].inputSchema["required"]),
                             {"agent_id", "app_id", "channel", "events"})
            self.assertNotIn("origin", tools["revise_memory"].inputSchema["properties"])
            self.assertEqual((await self.call(session, "list_agents", {}))[0]["agent_id"], self.agent)
            args = dict(self.domain, events=[self.event])
            self.assertEqual((await self.call(session, "import_history", args))[0]["status"], "inserted")
            self.assertEqual((await self.call(session, "import_history", args))[0]["status"], "unchanged")
            await self.error(session, "import_history", dict(self.domain, events=[dict(self.event, text="同版本冲突")]), "冲突")
            self.assertEqual(await self.call(session, "history", self.domain), [self.event])
            self.assertEqual((await self.call(session, "search", dict(self.domain, query="旅行")))[0]["text"], self.event["text"])
            self.assertEqual(await self.call(session, "inner_state", dict(self.domain, kind="emotion")), [])
            self.assertEqual(await self.call(session, "memory", self.domain), [])  # 导入不自动提炼
            await self.error(session, "remember", dict(self.domain, source_event_id="missing"), "找不到")
            saved = await self.call(session, "remember", dict(self.domain, source_event_id="event-1"))
            self.assertTrue(saved["changed"])
            path = self.service.memory_path(self.agent, self.app, "main")
            with closing(sqlite3.connect(path)) as db, db:
                mark(db, saved["card_id"], {"title": "user_explicit", "importance": "pending"})
            proof = await self.call(session, "evidence", dict(self.domain, card_id=saved["card_id"]))
            ref = json.loads(proof[0]["source_ref"])
            self.assertEqual(ref, dict(agent_id=self.agent, app=self.app, channel="main", source_event_id="event-1", version=1))
            self.assertFalse(proof[0]["verified"])
            revise = dict(self.domain, card_id=saved["card_id"], content="明确的合成人工修订",
                          expected_current_revision_id=saved["current_revision_id"])
            changed = await self.call(session, "revise_memory", revise)
            self.assertTrue(changed["changed"])
            self.assertNotEqual(changed["current_revision_id"], saved["current_revision_id"])
            await self.error(session, "revise_memory", revise, "expected revision")
            revise["expected_current_revision_id"] = changed["current_revision_id"]
            self.assertEqual(await self.call(session, "revise_memory", revise), dict(changed, changed=False))
            await self.error(session, "revise_memory", dict(revise, content="  "), "不能为空")
            await self.error(session, "revise_memory", dict(revise, card_id=999), "999")
            proof = await self.call(session, "evidence", dict(self.domain, card_id=saved["card_id"]))
            self.assertEqual(len(proof), 2)
            self.assertEqual([row["source_kind"] for row in proof], ["manual", "manual"])
            self.assertEqual(json.loads(proof[1]["source_ref"])["kind"], "mcp_explicit_manual_revision")
            self.assertEqual([row["resolution"] for row in proof], ["current", "manual_revision"])
            self.assertFalse(proof[1]["verified"])
            self.assertIsNone(proof[1]["current_source_text"])
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT origin,change_type FROM card_revisions ORDER BY id").fetchall(),
                                 [("manual_ui", "create"), ("manual_ui", "manual_edit")])
                self.assertEqual(db.execute("SELECT source,title,importance FROM cards").fetchone(), ("manual", "", 5.0))
                self.assertEqual(get(db, saved["card_id"]), {"title": "user_explicit", "importance": "pending"})
            updated = dict(self.event, version=2, text="来源更新")
            await self.call(session, "import_history", dict(self.domain, events=[updated]))
            proof = await self.call(session, "evidence", dict(self.domain, card_id=saved["card_id"]))
            self.assertEqual([row["resolution"] for row in proof],
                             ["source_changed", "manual_revision"])
            self.assertTrue(all(not row["verified"] for row in proof))

        # 新 Application 和新官方会话读取同一 Registry、档案和修订链。
        fresh = Application(self.root)
        async with serve(fresh) as url, connect(url) as (session, _):
            self.assertEqual(await self.call(session, "history", self.domain), [updated])
            cards = await self.call(session, "memory", self.domain)
            self.assertEqual(cards[0]["content"], revise["content"])
            self.assertEqual(cards[0]["current_revision_id"], changed["current_revision_id"])
            tombstone = dict(updated, version=3, deleted=True, text=None)
            await self.call(session, "import_history", dict(self.domain, events=[tombstone]))
            await self.error(session, "remember", dict(self.domain, source_event_id="event-1"), "找不到")
            proof = await self.call(session, "evidence", dict(self.domain, card_id=saved["card_id"]))
            self.assertEqual([row["resolution"] for row in proof], ["deleted", "manual_revision"])
            self.assertTrue(all(row["current_source_text"] is None for row in proof))

    async def test_http_auth_gate_on_every_method_and_existing_session(self):
        for token in (None, ""):
            with self.assertRaises(ValueError):
                create_http(self.service, token)
        async with serve(self.service) as url, connect(url) as (_, session_id):
            async with httpx.AsyncClient(base_url=url, timeout=10) as client:
                for path in ("/mcp", "/mcp/"):
                    for method in ("POST", "GET", "DELETE", "OPTIONS"):
                        for authorization in ("", "Bearer wrong-token"):
                            reply = await client.request(method, path, content=b"invalid-json",
                                                         headers={"Authorization": authorization,
                                                                  "Mcp-Session-Id": session_id()})
                            self.assertEqual(reply.status_code, 401)
                            self.assertEqual(reply.headers["www-authenticate"], "Bearer")
                            self.assertNotIn(TOKEN, reply.text)
                self.assertEqual((await client.get("/health")).status_code, 200)
                self.assertEqual((await client.get("/api/list_agents")).status_code, 401)

    async def test_http_registry_denies_foreign_agent_private_and_ungranted_source(self):
        foreign = self.service.registry.register_agent(OwnerPrincipal("synthetic-other-owner"), "其他 owner")
        self.service.import_history(self.agent, self.app, [self.event])
        saved = self.service.remember(self.agent, self.app, "event-1")
        async with serve(self.service) as url, connect(url) as (session, _):
            for domain in (dict(self.domain, agent_id=foreign.agent_id),
                           dict(self.domain, channel="private"), dict(self.domain, app_id="ungranted")):
                cases = [("history", {}), ("search", {"query": "旅行"}),
                         ("inner_state", {"kind": "emotion"}), ("memory", {}),
                         ("evidence", {"card_id": saved["card_id"]}),
                         ("remember", {"source_event_id": "event-1"}),
                         ("revise_memory", dict(card_id=saved["card_id"], content="越权修订",
                                                expected_current_revision_id=saved["current_revision_id"])),
                         ("import_history", {"events": [dict(self.event, app=domain["app_id"], channel=domain["channel"])]})]
                for name, args in cases:
                    await self.error(session, name, dict(domain, **args), "Agent" if domain["agent_id"] != self.agent else "授权")
            await self.error(session, "import_history", dict(self.domain, events=[dict(self.event, app="other")]), "一致")
            await self.error(session, "import_history", {"agent_id": self.agent, "app_id": self.app, "events": [self.event]}, "channel")
            self.assertEqual(self.service.memory(self.agent, self.app)[0]["current_revision_id"], saved["current_revision_id"])

    async def test_official_stdio_original_tools_and_manual_write_wire(self):
        self.service.import_history(self.agent, self.app, [self.event])
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "thalemora.app", "--data", str(self.root), "mcp"],
                                       env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            self.assertEqual({tool.name for tool in (await session.list_tools()).tools}, TOOLS)
            self.assertEqual((await self.call(session, "list_agents", {}))[0]["agent_id"], self.agent)
            self.assertEqual(await self.call(session, "history", self.domain), [self.event])
            self.assertEqual((await self.call(session, "search", dict(self.domain, query="旅行")))[0]["text"], self.event["text"])
            self.assertEqual(await self.call(session, "inner_state", dict(self.domain, kind="emotion")), [])
            saved = await self.call(session, "remember", dict(self.domain, source_event_id="event-1"))
            changed = await self.call(session, "revise_memory", dict(self.domain, card_id=saved["card_id"],
                                      content="stdio 合成人工修订", expected_current_revision_id=saved["current_revision_id"]))
            self.assertTrue(changed["changed"])
            await self.error(session, "memory", dict(self.domain, channel="private"), "授权")


if __name__ == "__main__":
    unittest.main()
