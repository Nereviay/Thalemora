"""真实 HTTP/worker 路径，全部合成数据、本地假模型，无付费调用。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

from thalemora.app import Application
from thalemora.app.maintenance import setup
from thalemora.app.maintenance_provider import MaintenanceProvider


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        module = body["model"]
        payload = json.loads(body["messages"][-1]["content"])
        if module == "writer":
            if self.server.slow:
                self.server.started.set()
                self.server.release.wait(5)
            source = payload["sources"][0]
            output = {"candidates": [{"content": source["text"], "title": "合成长期约定",
                       "memory_type": "event", "claim_scope": "user_fact", "evidence": [{
                           "source_event_id": source["source_event_id"], "version": source["version"],
                           "quote": source["text"]}]}]}
        elif module == "gatekeeper":
            output = {"action": "new", "importance": 7, "long_term_value": True,
                      "reason": "合成长期价值判断"}
        else:
            output = {"text": "合成建议：核对这条约定的准确版本。"}
        content = "invalid JSON" if self.server.bad_json else json.dumps(output)
        response = {"choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


class RealMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(dir="/tmp", prefix="pre6-4b-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.app = Application(self.root)
        self.agent = self.app.register_agent("合成测试 Agent")["agent_id"]
        self.app.grant_source(self.agent, "synthetic-app")
        self.event = dict(app="synthetic-app", source_event_id="synthetic-1", version=1,
                          role="user", text="合成天文项目长期采用紫色符号。",
                          occurred_at="2001-01-01T00:00:00+00:00")
        self.app.import_history(self.agent, "synthetic-app", [self.event])
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        self.server.requests = []
        self.server.bad_json = self.server.slow = False
        self.server.started, self.server.release = Event(), Event()
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.env = patch.dict(os.environ, {"PRE6_4B_TEST_KEY": "synthetic-only"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for module in ("writer", "gatekeeper", "reflection"):
            self.configure(module)
        self.provider = MaintenanceProvider(self.app.providers)

    def close_server(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def configure(self, module="writer", **changes):
        config = dict(enabled=True, base_url=f"http://127.0.0.1:{self.server.server_port}/v1",
                      model=module, key_env="PRE6_4B_TEST_KEY", call_budget=10, token_budget=100, thinking_enabled=False)
        return self.app.providers.configure(module, **dict(config, **changes))

    def tick(self):
        return self.app.maintenance_tick(self.agent, "synthetic-app", provider=self.provider)

    def test_http_pipeline_usage_and_restart_idempotence(self):
        result = self.tick()
        self.assertEqual("done", result["jobs"][0]["status"])
        self.assertEqual("done", result["reflections"][0]["status"])
        self.assertEqual(1, len(self.app.memory(self.agent, "synthetic-app")))
        self.assertEqual(3, len(self.server.requests))
        for row in result["calls"]:
            self.assertEqual(0, row["synthetic"])
            self.assertEqual(8, row["actual_tokens"])
            self.assertIsInstance(row["provider_call_id"], int)
        self.assertTrue(all(r["response_format"] == {"type": "json_object"} for r in self.server.requests))
        self.assertEqual("system", self.server.requests[1]["messages"][0]["role"])
        self.assertTrue(all(r["thinking"] == {"type": "disabled"} for r in self.server.requests))
        self.app = Application(self.root)
        self.provider = MaintenanceProvider(self.app.providers)
        self.tick()
        self.assertEqual(3, len(self.server.requests))
        proposal = self.app.inner_state(self.agent, "synthetic-app", "reflection")[0]
        self.assertEqual("suggestion_only", proposal["payload"]["effect"])

    def test_disabled_unconfigured_and_budgets_no_request(self):
        for changes, error in (({"enabled": False}, "disabled"), ({"model": ""}, "unconfigured"),
                               ({"call_budget": 0}, "budget_exceeded"),
                               ({"token_budget": 0}, "budget_exceeded")):
            with self.subTest(changes=changes):
                self.configure(**changes)
                result = self.tick()
                self.assertEqual("error", result["jobs"][0]["status"])
                self.assertIn(error, result["jobs"][0]["error"])
        self.assertEqual([], self.server.requests)
        self.configure()
        self.assertEqual("done", self.tick()["jobs"][0]["status"])

    def test_bad_json_and_timeout_audited_then_recover(self):
        self.server.bad_json = True
        result = self.tick()
        self.assertIn("JSONDecodeError", result["jobs"][0]["error"])
        self.assertEqual(8, self.app.providers.audit()[0]["tokens"])
        self.server.bad_json = False
        with patch("thalemora.app.providers.urlopen", side_effect=TimeoutError("synthetic timeout")):
            result = self.tick()
        self.assertIn("TimeoutError", result["jobs"][0]["error"])
        self.assertEqual("error", self.app.providers.audit()[0]["status"])
        self.assertIsNone(self.app.providers.audit()[0]["tokens"])
        self.assertEqual([], self.app.memory(self.agent, "synthetic-app"))
        self.assertEqual("done", self.tick()["jobs"][0]["status"])

    def test_slow_model_does_not_hold_foreground(self):
        self.server.slow = True
        with ThreadPoolExecutor(max_workers=2) as pool:
            background = pool.submit(self.tick)
            try:
                self.assertTrue(self.server.started.wait(3))
                def foreground():
                    app = Application(self.root)
                    found = app.search(self.agent, "synthetic-app", "合成")
                    event = dict(self.event, source_event_id="synthetic-2", occurred_at="2026-10-15T11:00:00+00:00")
                    app.import_history(self.agent, "synthetic-app", [event])
                    return found
                self.assertTrue(pool.submit(foreground).result(timeout=1))
                self.assertFalse(background.done())
            finally:
                self.server.release.set()
            self.assertEqual("done", background.result(timeout=5)["jobs"][0]["status"])

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "thalemora.app", "--data", str(self.root), *args],
                              capture_output=True, text=True, timeout=15)

    def test_worker_cli_restart_and_status(self):
        args = ("worker", self.agent, "synthetic-app", "--once")
        first = self.cli(*args)
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual({"done": 1}, json.loads(first.stdout)["jobs"])
        self.assertEqual(0, self.cli(*args).returncode)
        self.assertEqual(3, len(self.server.requests))
        status = self.cli("maintenance-status", self.agent, "synthetic-app")
        self.assertEqual("done", json.loads(status.stdout)["jobs"][0]["status"])
        process = subprocess.Popen([sys.executable, "-m", "thalemora.app", "--data", str(self.root),
                                    "worker", self.agent, "synthetic-app"], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        try:
            self.assertIn('"done"', process.stdout.readline())
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(0, process.returncode, stderr)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_worker_stops_on_error_without_retry_loop(self):
        self.configure(call_budget=0)
        result = self.cli("worker", self.agent, "synthetic-app", "--interval", "1")
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("budget_exceeded", result.stdout)
        self.assertEqual([], self.server.requests)
        self.assertEqual(1, len(self.app.providers.audit()))

    def test_worker_reports_later_reflection_failure_and_preserves_memory(self):
        self.configure("reflection", call_budget=1)
        self.tick()
        self.app.import_history(self.agent, "synthetic-app", [dict(
            self.event, source_event_id="synthetic-2", text="合成航海项目长期采用蓝色徽记。",
            occurred_at="2002-01-01T00:00:00+00:00")])
        result = self.cli("worker", self.agent, "synthetic-app", "--once")
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("budget_exceeded", json.loads(result.stdout)["errors"][0])
        self.assertEqual(2, len(self.app.memory(self.agent, "synthetic-app")))

    def test_upgrade_v050_audit_keeps_existing_rows(self):
        _, path, _ = setup(self.app, self.agent, "synthetic-app", "main")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("DROP TABLE maintenance_calls")
            db.execute("CREATE TABLE maintenance_calls(id INTEGER PRIMARY KEY,job_id TEXT NOT NULL,"
                       "module TEXT NOT NULL,status TEXT NOT NULL,synthetic INTEGER NOT NULL CHECK(synthetic=1),"
                       "usage TEXT,output TEXT,actual_tokens INTEGER CHECK(actual_tokens IS NULL),error TEXT,"
                       "created_at TEXT NOT NULL)")
            db.execute("INSERT INTO maintenance_calls VALUES(1,'old-job','writer','success',1,'{}','{}',NULL,NULL,'old')")
        result = self.tick()
        old = [row for row in result["calls"] if row["job_id"] == "old-job"][0]
        self.assertEqual(1, old["synthetic"])
        self.assertIsNone(old["actual_tokens"])
        self.assertEqual("done", result["jobs"][0]["status"])


if __name__ == "__main__":
    unittest.main()
