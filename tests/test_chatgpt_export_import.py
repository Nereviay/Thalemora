"""用临时官方导出形状的合成文件，走真实 CLI 与 Archive。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from thalemora.app import Application
from thalemora.identity import OwnerPrincipal


ROOT = Path(__file__).resolve().parents[1]


def node(role="user", text="合成正文", **message):
    return {"message": {"author": {"role": role}, "content": {"content_type": "text", "parts": [text]},
                        **message}}


class ChatGPTExportTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.export = self.root / "export.json"
        self.data = self.root / "state"

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tools/import_chatgpt_export.py"),
                                 "--export", str(self.export), *map(str, args)], cwd=self.root,
                                capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn("合成", result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_preview_numbered_zip_skips_hidden_media_and_unrelated_files(self):
        self.export = self.root / "export.zip"
        mapping = {
            "visible": node(), "root": {"message": None},
            "hidden": node(metadata={"is_visually_hidden_from_conversation": True}),
            "analysis": node("assistant", channel="analysis"),
            "tool-call": node("assistant", recipient="tool"),
            "tool-log": node("tool"), "system": node("system"),
            "media": node(content={"content_type": "multimodal_text", "parts": ["合成说明"]}),
            "attachment": node(metadata={"attachments": [{"name": "合成附件"}]}),
            "mixed": node(content={"content_type": "text", "parts": ["合成说明", {"image": 1}]}),
            "blank": node(text="  "), "": node(), "bad-time": node(create_time="unknown"),
        }
        with ZipFile(self.export, "w") as archive:
            archive.writestr("folder/conversations-001.json", json.dumps([{"id": "c1", "mapping": mapping}]))
            archive.writestr("folder/conversations_2.json", json.dumps([{"id": "c2", "mapping": {"ok": node()}},
                                                                         {"mapping": {"orphan": node()}}]))
            archive.writestr("other.json", "invalid json")
            archive.writestr("conversations_metadata.json", "invalid json")
        self.assertEqual(self.cli(), dict(files=2, conversations=3, eligible_nodes=2, skipped_nodes_no_message=1,
                         skipped_nodes_hidden_or_tool=3, skipped_nodes_role=2, skipped_nodes_attachment_or_nontext=2,
                         skipped_nodes_mixed_or_invalid_parts=1, skipped_nodes_empty_text=1, skipped_nodes_missing_event_id=1,
                         skipped_nodes_invalid_timestamp=1, skipped_conversations_missing_id=1,
                         skipped_nodes_missing_conversation_id=1))
        self.assertEqual(list(self.root.iterdir()), [self.export])

    def test_import_branches_isolation_replay_omission_and_conflict(self):
        service = Application(self.data)
        agent, other = [service.register_agent(name)["agent_id"] for name in ("合成甲", "合成乙")]
        service.grant_source(agent, "chatgpt")
        service.grant_source(other, "chatgpt")
        original = " 合成\n正文  "
        mapping = {
            "root": dict(node(content={"content_type": "text", "parts": [" 合成", "\n正文  "]},
                              id="user-id", create_time=0), children=["a", "b"]),
            "a": dict(node("assistant", text="合成旧分支"), parent="root"),
            "b": dict(node("assistant", text="合成编辑分支"), parent="root"),
        }
        conversation = {"conversation_id": "conv", "current_node": "b", "mapping": mapping}
        branch_copy = {"id": "conv-copy", "mapping": {"root": node(id="user-id")}}
        def export():
            self.export.write_text(json.dumps([conversation, branch_copy]), encoding="utf-8")

        export()
        target = ("--data", self.data, "--agent-id", agent)
        self.assertEqual(self.cli(*target)["inserted"], 4)
        self.assertEqual(self.cli(*target)["unchanged"], 4)
        history = {tuple(json.loads(e["source_event_id"])): e for e in service.history(agent, "chatgpt")}
        self.assertEqual(set(history), {("conv", "user-id"), ("conv", "a"), ("conv", "b"), ("conv-copy", "user-id")})
        event = history["conv", "user-id"]
        self.assertEqual((event["text"], event["occurred_at"], event["content_hash"]),
                         (original, "1970-01-01T00:00:00+00:00", hashlib.sha256(original.encode()).hexdigest()))
        self.assertEqual((event["app"], event["channel"], event["conversation_id"], event["version"], event["role"],
                          event["epoch_id"], event["raw_event_id"], event["hash_verification"], event["deleted"]),
                         ("chatgpt", "main", "conv", 1, "user", None, None, "legacy_unverified", False))
        self.assertIsNone(history["conv", "a"]["occurred_at"])
        self.assertEqual(history["conv", "a"]["role"], "assistant")
        self.assertEqual(service.memory(agent, "chatgpt"), [])
        self.assertEqual(service.history(other, "chatgpt"), [])
        del mapping["a"]
        export()
        self.assertEqual(self.cli(*target)["unchanged"], 3)
        mapping["b"] = node("assistant", text="合成冲突")
        export()
        self.assertEqual(self.cli(*target, code=1)["version_conflict"], 1)
        self.assertEqual(service.history(agent, "chatgpt", include_deleted=True), list(history.values()))
        self.assertEqual(self.cli("--data", self.data, "--agent-id", other)["inserted"], 3)

    def test_zip_import_across_batch_boundary(self):
        self.export = self.root / "export.zip"
        with ZipFile(self.export, "w") as archive:
            for name, size in (("conversations.json", 100), ("conversations-002.json", 1)):
                mapping = {str(i): node() for i in range(size)}
                archive.writestr(name, json.dumps([{"id": name, "mapping": mapping}]))
        service = Application(self.data)
        agent = service.register_agent("合成助手")["agent_id"]
        service.grant_source(agent, "chatgpt")
        args = ("--data", self.data, "--agent-id", agent)
        self.assertEqual(self.cli(*args)["inserted"], 101)
        self.assertEqual(self.cli(*args)["unchanged"], 101)
        self.assertEqual(len(service.history(agent, "chatgpt", offset=100)), 1)

    def test_custom_gpt_is_separate_without_explicit_allowlist(self):
        conversations = [
            {"id": "regular", "mapping": {"a": node(text="合成普通聊天")}},
            {"id": "other-gpt", "gizmo_id": "gpt-external",
             "mapping": {"b": node(text="合成其他代理")}},
        ]
        self.export.write_text(json.dumps(conversations), encoding="utf-8")
        preview = self.cli()
        self.assertEqual(preview["eligible_nodes"], 1)
        self.assertEqual(preview["skipped_conversations_other_gpt"], 1)
        self.assertEqual(preview["skipped_nodes_other_gpt"], 1)
        self.assertEqual(self.cli("--include-gizmo-id", "gpt-external")["eligible_nodes"], 2)
        service = Application(self.data)
        agent = service.register_agent("合成 Kiko")["agent_id"]
        service.grant_source(agent, "chatgpt")
        target = ("--data", self.data, "--agent-id", agent)
        self.assertEqual(self.cli(*target)["inserted"], 1)
        self.assertEqual(self.cli(*target, "--include-gizmo-id", "gpt-external")["inserted"], 1)
        self.assertEqual(len(service.history(agent, "chatgpt")), 2)

    def test_existing_agent_and_explicit_chatgpt_main_grant_required(self):
        self.export.write_text('[{"id":"c","mapping":{}}]')
        self.cli("--data", self.data, "--agent-id", "unknown", code=1)
        self.assertFalse(self.data.exists())
        service = Application(self.data)
        agent = service.register_agent("合成助手")["agent_id"]
        service.grant_source(agent, "chatgpt", "private")
        service.grant_source(agent, "outside", "main")
        foreign = service.registry.register_agent(OwnerPrincipal("other-owner"), "合成外部助手")
        for denied in (agent, foreign.agent_id):
            self.assertEqual(self.cli("--data", self.data, "--agent-id", denied, code=1)["error_AccessDenied"], 1)


if __name__ == "__main__":
    unittest.main()
