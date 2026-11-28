"""Cheap canonical recall for previously delivered proactive contacts.

This is a relevance picker, not a prompt/history renderer. It never calls a
model and only returns contacts that the caller already proved were delivered.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

_STRONG_RECENT_CUES = (
    "刚才", "刚刚", "你刚", "刚还", "刚不是", "不是刚",
)
_REFERENCE_CUES = (
    "之前你说", "你之前说", "你不是说", "不是说", "你说过",
    "你还说", "你跟我说", "你主动说", "那条消息", "那句话",
)
_HISTORY_CUES = ("主动消息", "主动联系", "之前主动", "以前主动")
# Generic chat words are too weak to justify resurrecting an old proactive line by
# themselves. They may still participate when the user explicitly refers to past speech.
_WEAK_OVERLAP_BIGRAMS = frozenset({
    "会儿", "手机", "现在", "回来", "回家", "看看", "一下", "什么", "怎么",
    "这个", "那个", "今天", "晚上", "早上", "已经", "没有", "不是", "真的",
    "就是", "可以", "还是", "时候", "然后", "一会", "一会儿",
    "不知道", "知道", "不知", "说什么",
})


def _text(value: Any, limit: int = 1200) -> str:
    return str(value or "").strip()[:limit]


def _compact(value: Any) -> str:
    text = _text(value).lower()
    return "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _grams(text: str, n: int) -> set[str]:
    if len(text) < n:
        return set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def _strip_weak_overlap_terms(value: Any) -> str:
    text = _compact(value)
    for term in sorted(_WEAK_OVERLAP_BIGRAMS, key=len, reverse=True):
        text = text.replace(term, "")
    return text


def _parse_time(value: Any) -> datetime | None:
    text = _text(value, 100)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _delivered_at(item: Mapping[str, Any]) -> datetime | None:
    delivery = item.get("proactive_delivery")
    if isinstance(delivery, Mapping):
        stamp = _parse_time(delivery.get("delivered_at") or delivery.get("last_delivered_at"))
        if stamp is not None:
            return stamp
    return _parse_time(item.get("created_at"))


def _lexical_score(query: str, content: str) -> float:
    q = _compact(query)
    c = _compact(content)
    if not q or not c:
        return 0.0
    q2, c2 = _grams(q, 2), _grams(c, 2)
    q3, c3 = _grams(q, 3), _grams(c, 3)
    overlap2 = len(q2 & c2)
    overlap3 = len(q3 & c3)
    score = overlap2 * 2.0 + overlap3 * 3.0
    if len(q) >= 2 and q in c:
        score += 6.0
    # ASCII words are useful for product/site/song names without adding a tokenizer.
    q_words = {w for w in re.findall(r"[a-z0-9_+-]{2,}", query.lower())}
    c_words = {w for w in re.findall(r"[a-z0-9_+-]{2,}", content.lower())}
    score += 4.0 * len(q_words & c_words)
    return score


def select_relevant_proactive_contacts(
    query: str,
    candidates: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    limit: int = 2,
    exclude_event_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Return at most two delivered proactive contacts relevant to this turn.

    Lexical overlap is the normal path. Strong "you just said..." references may
    fall back to the latest recent contact, because pronoun-only follow-ups often
    repeat none of the original nouns. Generic chat gets no fallback.
    """
    query = _text(query, 600)
    if not query:
        return []
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    excluded = {str(x or "").strip() for x in exclude_event_ids if str(x or "").strip()}
    strong_recent = any(cue in query for cue in _STRONG_RECENT_CUES)
    referenced = strong_recent or any(cue in query for cue in _REFERENCE_CUES)
    asks_history = any(cue in query for cue in _HISTORY_CUES)

    ranked: list[tuple[float, float, dict[str, Any]]] = []
    recent_fallback: list[tuple[float, dict[str, Any]]] = []
    for raw in candidates:
        item = dict(raw)
        event_id = _text(item.get("event_id"), 180)
        if event_id and event_id in excluded:
            continue
        content = _text(item.get("content"), 900)
        if not content:
            continue
        delivered = _delivered_at(item)
        if delivered is None:
            continue
        age_hours = max(0.0, (now - delivered).total_seconds() / 3600.0)
        lexical = _lexical_score(query, content)
        generic_query = _strip_weak_overlap_terms(query)
        generic_content = _strip_weak_overlap_terms(content)
        generic_lexical = _lexical_score(generic_query, generic_content)
        # Generic chat must retain distinctive overlap after everyday phrases are
        # removed. Short noun-like follow-ups such as “蛋饼呢” may match on one
        # meaningful bigram; longer chat needs stronger overlap.
        short_anchor = generic_query.rstrip("呢吗呀啊嘛呐吧哦啦了")
        short_exact_anchor = bool(
            2 <= len(short_anchor) <= 6
            and short_anchor in generic_content
        )
        strong_generic_overlap = bool(
            generic_lexical >= 6.0 or short_exact_anchor
        )
        # Recency breaks ties; it never makes an unrelated message relevant.
        recency = max(0.0, 2.0 - math.log2(1.0 + age_hours) * 0.35)
        reference_overlap = generic_lexical >= 2.0
        if lexical >= 2.0 and (
            strong_generic_overlap or (referenced and reference_overlap)
        ):
            ranked.append((lexical + recency, -age_hours, item))
        if age_hours <= (6.0 if strong_recent else 24.0):
            recent_fallback.append((-age_hours, item))

    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    out = [row[2] for row in ranked[: max(1, min(int(limit or 2), 2))]]
    if out:
        return out

    recent_fallback.sort(key=lambda row: row[0], reverse=True)
    if asks_history:
        return [row[1] for row in recent_fallback[: max(1, min(int(limit or 2), 2))]]
    if referenced and recent_fallback:
        return [recent_fallback[0][1]]
    return []


def render_proactive_recall(items: Iterable[Mapping[str, Any]]) -> str:
    selected = list(items)[:2]
    if not selected:
        return ""
    lines = ["【相关的既往主动联系（事实回忆，不是回复示例）】"]
    total = 0
    for item in selected:
        content = _text(item.get("content"), 360).replace("\n", " ")
        if not content:
            continue
        if total + len(content) > 620:
            content = content[: max(0, 620 - total)]
        if not content:
            break
        lines.append("- " + content)
        total += len(content)
        if total >= 620:
            break
    return "\n".join(lines) if len(lines) > 1 else ""
