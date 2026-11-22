"""预览官方 ChatGPT 导出的可见文本，或导入已有 Agent 的 chatgpt/main 档案。"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from itertools import islice
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
from zipfile import BadZipFile, ZipFile


def documents(path):
    if path.suffix.lower() == ".json":
        with path.open("rb") as stream:
            yield json.load(stream)
        return
    if path.suffix.lower() != ".zip":
        raise ValueError("export_format")
    with ZipFile(path) as archive:
        parts = [entry for entry in archive.infolist() if not entry.is_dir() and re.fullmatch(
            r"conversations(?:[-_]?\d+)?\.json", PurePosixPath(entry.filename).name)]
        if not parts:
            raise ValueError("missing_conversations_json")
        for entry in parts:
            with archive.open(entry) as stream:
                yield json.load(stream)


def source_id(*values):
    return next((v for v in values if isinstance(v, str) and v.strip()), None)


def convert_node(conversation_id, key, node):
    """返回一个 envelope，或一个固定的跳过原因。mapping 的键也是导出中的节点 ID。"""
    if not isinstance(node, dict) or not isinstance(message := node.get("message"), dict):
        return "no_message"
    event_id = source_id(message.get("id"), node.get("id"), key)
    if not event_id:
        return "missing_event_id"
    author = message.get("author")
    role = author.get("role") if isinstance(author, dict) else None
    if role not in ("user", "assistant"):
        return "role"
    metadata = message.get("metadata", {})
    if not isinstance(metadata, dict):
        return "invalid_metadata"
    if (metadata.get("is_visually_hidden_from_conversation") or metadata.get("is_user_system_message")
            or message.get("channel") not in (None, "final")
            or metadata.get("channel") not in (None, "final")
            or message.get("recipient") not in (None, "all") or metadata.get("tool_calls") or message.get("tool_calls")
            or metadata.get("is_redacted") or metadata.get("is_deleted")):
        return "hidden_or_tool"
    content = message.get("content")
    if (metadata.get("attachments") or not isinstance(content, dict)
            or content.get("content_type") != "text"):
        return "attachment_or_nontext"
    parts = content.get("parts")
    if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
        return "mixed_or_invalid_parts"
    text = "".join(parts)
    if not text.strip():
        return "empty_text"
    timestamp = message.get("create_time")
    occurred_at = None
    if timestamp is not None:
        if type(timestamp) not in (int, float):
            return "invalid_timestamp"
        try:
            occurred_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return "invalid_timestamp"
    epoch, raw_id = message.get("epoch_id"), message.get("raw_event_id")
    if epoch is not None and not isinstance(epoch, str):
        return "invalid_source_reference"
    if raw_id is not None and not isinstance(raw_id, str) and not (
            type(raw_id) is int and -(2**63) <= raw_id < 2**63):
        return "invalid_source_reference"
    # 对话分支或副本可复用消息 ID；用两个原始 ID 组成无歧义的来源键。
    return dict(app="chatgpt", channel="main", conversation_id=conversation_id,
                source_event_id=json.dumps([conversation_id, event_id], ensure_ascii=False, separators=(",", ":")),
                role=role, text=text, occurred_at=occurred_at, epoch_id=epoch,
                raw_event_id=raw_id, version=1, deleted=False,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                hash_verification="legacy_unverified")


def events(path, counts, allowed_gizmo_ids=()):
    for conversations in documents(path):
        if not isinstance(conversations, list):
            raise ValueError("expected_conversation_list")
        counts["files"] += 1
        for conv in conversations:
            counts["conversations"] += 1
            if not isinstance(conv, dict) or not isinstance(mapping := conv.get("mapping"), dict):
                counts["skipped_conversations_invalid_mapping"] += 1
                continue
            # Other custom GPTs are separate Agents unless explicitly allowed.
            gizmo_id = conv.get("gizmo_id")
            if isinstance(gizmo_id, str) and gizmo_id.strip() and gizmo_id not in allowed_gizmo_ids:
                counts["skipped_conversations_other_gpt"] += 1
                counts["skipped_nodes_other_gpt"] += len(mapping)
                continue
            cid = source_id(conv.get("conversation_id"), conv.get("id"))
            if not cid:
                counts["skipped_conversations_missing_id"] += 1
                counts["skipped_nodes_missing_conversation_id"] += len(mapping)
                continue
            for key, node in mapping.items():
                event = convert_node(cid, key, node)
                if isinstance(event, str):
                    counts["skipped_nodes_" + event] += 1
                else:
                    counts["eligible_nodes"] += 1
                    yield event


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--data", type=Path, help="已存在的洄海数据目录，省略时只预览")
    parser.add_argument("--agent-id", help="已获 chatgpt/main 授权的目标 Agent")
    parser.add_argument("--include-gizmo-id", action="append", default=[],
                        help="仅明确授权的自定义 GPT/Gizmo ID 可并入此 Agent；默认跳过其他 GPT")
    args = parser.parse_args(argv)
    if args.data is not None and not args.agent_id:
        parser.error("--data 需要 --agent-id")

    counts = Counter(eligible_nodes=0)
    stream = events(args.export, counts, set(args.include_gizmo_id))
    exit_code = 0
    try:
        if args.data is None:
            for _ in stream:
                pass
        else:
            from thalemora.app import Application
            from thalemora.history import VersionConflict

            if not (args.data / "registry.sqlite").is_file():
                raise ValueError("missing_registry")
            service = Application(args.data)
            service.registry.assert_source(service.owner, args.agent_id, "chatgpt", "main")
            while batch := list(islice(stream, 100)):
                try:
                    receipts = service.import_history(args.agent_id, "chatgpt", batch, "main")
                except VersionConflict:
                    counts["version_conflict"] += 1
                    exit_code = 1
                    break
                counts.update(r["status"] for r in receipts)
    except (ValueError, OSError, BadZipFile, RuntimeError, sqlite3.Error) as exc:
        # 异常消息可能包含文件名、标识符或正文，只公开错误类型及计数。
        counts["error_" + type(exc).__name__] += 1
        exit_code = 1
    finally:
        stream.close()
    print(json.dumps(counts, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
