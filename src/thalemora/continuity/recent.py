"""近期事件的来源、版本选择和文本预算。"""
from .facts import estimate_tokens

MAX_TOKENS = 500

SOURCE_KINDS = {"scene", "memory", "daily", "weekly", "monthly", "handoff"}

HEADER = "【近期持续事件（派生摘要）】"

STATUS_HEADER = "【用户近期自述】"

def render_recent_items(items):
    if not items:
        return ""
    header = STATUS_HEADER if all(item["key"] == "user.recent_status" for item in items) else HEADER
    return header + "\n" + "\n".join("- " + item["text"] for item in items)

def build_recent_summary(updates, *, sources, principal_id, previous=None, max_tokens=MAX_TOKENS):
    """同 key 修订或结束；调用方提供已有来源原句，不做自由摘要。

    sources 由对应 owner 读取，包含 ref/kind/principal_id/visibility/text；
    ref 指向确定的来源版本，若 owner 另给 version/source_refs 则原样保留。
    previous 只能是上版已冻结结果。closed 会移除待办，即使关闭文本超过预算。
    """
    if not 0 <= max_tokens <= MAX_TOKENS:
        raise ValueError("recent summary budget must be <=500")
    if previous and previous.get("principal_id") != principal_id:
        raise ValueError("previous summary belongs to another principal")
    items = {item["key"]: dict(item) for item in reversed((previous or {}).get("items", []))}
    for update in updates:
        key, state, text = update["key"], update["state"], update["text"]
        if not key or state not in {"open", "changed", "closed", "current"} or not text:
            raise ValueError("invalid recent event update")
        refs = update.get("source_refs") or []
        if not refs:
            raise ValueError("recent event requires source refs")
        source_refs = []
        for ref in refs:
            source = sources[ref]
            if (source["ref"] != ref or source["kind"] not in SOURCE_KINDS
                    or source["principal_id"] != principal_id or source["visibility"] != "normal"
                    or source.get("deleted") or text not in source["text"]):
                raise ValueError("recent event evidence/visibility mismatch")
            source_refs.append({"ref": ref, "kind": source["kind"],
                                **{name: source[name] for name in ("version", "source_refs") if name in source}})
        items.pop(key, None)
        if state != "closed":
            items[key] = {"key": key, "state": state, "text": text, "source_refs": source_refs}
    # 新修订排在前面，预算只选择完整事件，不切断正文或来源。
    selected = []
    content = ""
    for item in reversed(list(items.values())):
        candidate = render_recent_items([*selected, item])
        if estimate_tokens(candidate) <= max_tokens:
            content = candidate
            selected.append(item)
    return {"principal_id": principal_id, "content": content, "items": selected,
            "budget": {"max_tokens": max_tokens, "estimated_tokens": estimate_tokens(content),
                       "omitted_count": len(items) - len(selected)}}

def _filtered_text(content, recent, remaining):
    items = (recent or {}).get("items", ())
    if len(remaining) == len(items):
        return content
    summary = recent["content"]
    if not summary or not content.endswith(summary):
        raise ValueError("frozen recent summary text mismatch")
    prefix = content.removesuffix(summary).removesuffix("\n\n")
    return "\n\n".join(part for part in (prefix, render_recent_items(remaining)) if part)

def without_recent_status(content, recent):
    """独立档案无法重核正式对话来源时，移除相应的可撤回内容。"""
    return _filtered_text(content, recent, [item for item in (recent or {}).get("items", ())
                          if item["key"] != "user.recent_status" and not item["key"].startswith("memory-event:")])
