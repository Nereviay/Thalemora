"""按用途显式调用 OpenAI-compatible HTTP；没有后台调用或凭空估算的用量。"""

from contextlib import closing
import json
import math
import os
import sqlite3
from urllib.request import Request, urlopen
from urllib.parse import urlparse


MODULES = ("embedding", "rerank", "writer", "gatekeeper", "reflection")


class ProviderUnavailable(RuntimeError):
    pass


class Providers:
    def __init__(self, root):
        self.path = root / "providers.sqlite"
        self.local_models = {}
        self.local_errors = {}
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config(module TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls(
                    id INTEGER PRIMARY KEY, module TEXT NOT NULL, status TEXT NOT NULL,
                    tokens INTEGER, usage TEXT, error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX IF NOT EXISTS calls_module ON calls(module,status);
            """)

    def connect(self):
        return closing(sqlite3.connect(self.path))

    @staticmethod
    def module(module):
        if module not in MODULES:
            raise ValueError("未知模型用途")

    def configure(self, module, *, enabled=False, base_url="", model="", key_env="",
                  capability="chat", call_budget=0, token_budget=0, thinking_enabled=None, mode=None):
        self.module(module)
        if type(enabled) is not bool or capability not in ("chat", "embedding", "rerank", "unsupported"):
            raise ValueError("模型开关/能力无效")
        if mode is None:
            mode = "api" if enabled else "disabled"
        if mode not in ("local", "api", "disabled"):
            raise ValueError("模型方式须为 local / api / disabled")
        if module not in ("embedding", "rerank") and mode == "local":
            raise ValueError("此用途不支持本地模型")
        if module in ("embedding", "rerank"):
            enabled = mode != "disabled"
            capability = module
        self.local_errors.pop(module, None)
        if thinking_enabled is not None and type(thinking_enabled) is not bool:
            raise ValueError("thinking_enabled 须为布尔或未指定")
        if any(type(x) is not int or x < 0 for x in (call_budget, token_budget)):
            raise ValueError("预算必须为非负整数")
        if base_url and urlparse(base_url).scheme not in ("http", "https"):
            raise ValueError("base_url 必须是 HTTP(S)")
        config = dict(mode=mode, enabled=enabled, base_url=base_url.rstrip("/"), model=model,
                      key_env=key_env, capability=capability, call_budget=call_budget,
                      token_budget=token_budget, thinking_enabled=thinking_enabled)
        with self.connect() as db, db:
            db.execute("INSERT INTO config VALUES(?,?) ON CONFLICT(module) DO UPDATE SET value=excluded.value",
                       (module, json.dumps(config)))
        return self.status(module)

    def _config(self, db, module):
        row = db.execute("SELECT value FROM config WHERE module=?", (module,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _state(self, module, config):
        if config.get("mode") == "disabled":
            return "disabled"
        if config.get("mode") == "local":
            from importlib.util import find_spec
            local = self.local_models.get(module)
            if local and local[0] == config.get("model"):
                return "error" if module in self.local_errors else "ready"
            if not config.get("model") or not os.path.isdir(config["model"]) or not find_spec("sentence_transformers"):
                return "unconfigured"
            return "error" if module in self.local_errors else "ready"
        if not config.get("base_url") or not config.get("model") or not os.environ.get(config.get("key_env", "")):
            return "unconfigured"
        if not config.get("enabled"):
            return "disabled"
        if config.get("capability") != (module if module in ("embedding", "rerank") else "chat"):
            return "unsupported"
        return "ready"

    def status(self, module):
        self.module(module)
        with self.connect() as db:
            config = self._config(db, module)
            state = self._state(module, config)
            last = db.execute("SELECT status FROM calls WHERE module=? AND status IN ('success','error') "
                              "ORDER BY id DESC LIMIT 1", (module,)).fetchone()
            if state == "ready" and config.get("mode") != "local" and last and last[0] == "error":
                state = "error"
            count, tokens = db.execute("SELECT COUNT(*),COALESCE(SUM(tokens),0) FROM calls "
                                      "WHERE module=? AND status IN ('pending','success','error')", (module,)).fetchone()
            unknown = db.execute("SELECT COUNT(*) FROM calls WHERE module=? AND tokens IS NULL "
                                 "AND status IN ('pending','success','error')", (module,)).fetchone()[0]
            if state == "ready" and config.get("mode") != "local" and (count >= config["call_budget"] or tokens >= config["token_budget"]):
                state = "budget_exceeded"
        return dict(module=module, state=state, config=config, calls=count,
                    actual_tokens=None if unknown else tokens, known_tokens=tokens, unknown_usage_calls=unknown,
                    actual_cost=None, automatic_domain_tasks=False)

    def audit(self, *, limit=20, offset=0):
        from .service import Application
        Application.page(limit, offset)
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM calls ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))]

    def call(self, module, text, *, max_tokens=128, messages=None, json_output=False, documents=None):
        self.module(module)
        if module == "rerank" and (not isinstance(documents, list) or not 1 <= len(documents) <= 12
                                   or any(not isinstance(d, str) or not d or len(d) > 16000 for d in documents)):
            raise ValueError("rerank 需要 1..12 条候选正文")
        if messages is not None:
            if module in ("embedding", "rerank") or not isinstance(messages, list) or not messages:
                raise ValueError("chat messages 必须为非空列表")
            if any(not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant")
                   or not isinstance(m.get("content"), str) for m in messages):
                raise ValueError("chat messages 的角色或正文无效")
            text = json.dumps(messages, ensure_ascii=False)
        if not isinstance(text, str) or not 1 <= len(text) <= 16000:
            raise ValueError("输入长度须为 1..16000 字符")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
            raise ValueError("max_tokens 须为 1..4096")
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            config = self._config(db, module)
            state = self._state(module, config)
            count, tokens = db.execute("SELECT COUNT(*),COALESCE(SUM(tokens),0) FROM calls "
                                      "WHERE module=? AND status IN ('pending','success','error')", (module,)).fetchone()
            if config.get("mode") == "local":
                state = "local_only"
            if state == "ready" and config.get("mode") != "local" and (count >= config["call_budget"] or tokens >= config["token_budget"]):
                state = "budget_exceeded"
            call_id = db.execute("INSERT INTO calls(module,status,error) VALUES(?,?,?)",
                                 (module, "pending" if state == "ready" else "blocked", None if state == "ready" else state)).lastrowid
        if state != "ready":
            raise ProviderUnavailable(state)
        usage = None
        tokens = None
        try:
            if module == "embedding":
                endpoint = "/embeddings"
                body = dict(model=config["model"], input=text)
            elif module == "rerank":
                endpoint = "/rerank"
                body = dict(model=config["model"], query=text, documents=documents)
            else:
                endpoint = "/chat/completions"
                body = dict(model=config["model"], messages=[dict(role="user", content=text)], max_tokens=max_tokens)
                if messages is not None:
                    body["messages"] = messages
                if json_output:
                    body["response_format"] = {"type": "json_object"}
                if config.get("thinking_enabled") is not None:
                    body["thinking"] = {"type": "enabled" if config["thinking_enabled"] else "disabled"}
            request = Request(config["base_url"] + endpoint, data=json.dumps(body).encode(),
                              headers={"Authorization": "Bearer " + os.environ[config["key_env"]],
                                       "Content-Type": "application/json"})
            with urlopen(request, timeout={"embedding": 5, "rerank": 3}.get(module, 30)) as response:
                result = json.load(response)
            usage = result.get("usage")
            if usage is not None:
                actual = usage.get("total_tokens")
                if type(actual) is not int or actual < 0:
                    raise ValueError("provider usage 缺少有效 total_tokens")
                tokens = actual
            if module == "embedding":
                output = result["data"][0]["embedding"]
                if (not isinstance(output, list) or not output
                        or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in output)):
                    raise ValueError("embedding 必须为非空有限数值向量")
            elif module == "rerank":
                output = result["results"]
                from thalemora.retrieval.models import rerank_scores
                rerank_scores(output, len(documents))
            else:
                output = result["choices"][0]["message"]["content"]
            # 无用量则本次失败且用量为未知，不估算实际 token。
            if usage is None:
                raise ValueError("provider 未返回实际 usage；本次成本未知，需核查")
        except Exception as exc:
            with self.connect() as db, db:
                db.execute("UPDATE calls SET status='error',tokens=?,usage=?,error=? WHERE id=?",
                           (tokens, json.dumps(usage), type(exc).__name__, call_id))
            raise ProviderUnavailable(f"provider error: {type(exc).__name__}") from exc
        with self.connect() as db, db:
            db.execute("UPDATE calls SET status='success',tokens=?,usage=? WHERE id=?",
                       (tokens, json.dumps(usage), call_id))
        return dict(output=output, usage=usage, actual_cost=None, call_id=call_id)
