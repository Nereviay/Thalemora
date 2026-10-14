# -*- coding: utf-8 -*-
"""Bounded retrieval-keyword policy for Memory V2.

Keywords are retrieval aliases, not a second summary/tag pile.  Keep them small,
specific and stable so the high BM25 keyword weight cannot amplify generic noise.
"""
from __future__ import annotations

import re

MAX_KEYWORDS = 6
MAX_KEYWORD_CHARS = 18
_SPLIT_RE = re.compile(r"[,，、;；\n|｜]+")
_SENTENCE_PUNCTUATION_RE = re.compile(r"[，。；;！？!?：:\n]")
_SENTENCE_PREFIXES = (
    "我已", "我会", "我承认", "我明确", "她会",
    "若只是", "如果", "并", "而", "但", "因为", "当时",
)
_SENTENCE_MARKERS = (
    "表示", "觉得", "认为", "答应", "承诺", "并计划", "而非", "不再这样",
)
GENERIC_KEYWORDS = {
    "我", "我们", "双方", "两人",
    "关系", "日子", "生活", "记忆", "事情", "事件", "经历",
    "聊天", "对话", "互动", "情绪", "感受", "重要", "长期记忆",
}
_DATE_ONLY_RE = re.compile(
    r"^(?:20\d{2}[-/.年])?\d{1,2}(?:[-/.月])\d{1,2}(?:日|号)?$"
)


def _parts(value):
    """Split keyword-ish text into candidate aliases.

    Historical values used ASCII or Chinese commas; a few legacy rows also used
    ``、``/``；``/``|`` as visual separators. Treating those as separators is a
    bounded normalization: previously such a whole string was kept or rejected
    as one junk token. ``/`` is intentionally *not* a separator (aliases such
    as ``演/用户标签`` are meaningful).
    """
    if isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        items = [str(value or "")]
    parts: list[str] = []
    for item in items:
        parts.extend(piece.strip() for piece in _SPLIT_RE.split(item))
    return parts


def _tag_set(tags):
    return {item.casefold() for item in _parts(tags) if item}


def _useful(keyword, *, title="", tags=""):
    return not keyword_issues(keyword, title=title, tags=tags)


def keyword_issues(value, *, title="", tags=""):
    """Return machine-readable issue codes for one candidate keyword.

    An empty list means the keyword passes ``is_useful_keyword``. The codes
    mirror the checks made by ``_useful`` so proposal-only audits can explain
    a rejection without duplicating the live keyword policy:

    ``empty``, ``generic``, ``too_long``, ``too_short``,
    ``sentence_punctuation``, ``sentence_prefix``, ``sentence_marker``,
    ``date_only``, ``title_duplicate``, ``tag_duplicate``, ``no_alnum``.
    """
    item = str(value or "").strip()
    issues: list[str] = []
    if not item:
        return ["empty"]
    if item in GENERIC_KEYWORDS:
        issues.append("generic")
    if len(item) > MAX_KEYWORD_CHARS:
        issues.append("too_long")
    if len(item) < 2:
        issues.append("too_short")
    if _SENTENCE_PUNCTUATION_RE.search(item):
        issues.append("sentence_punctuation")
    if item.startswith(_SENTENCE_PREFIXES):
        issues.append("sentence_prefix")
    if any(marker in item for marker in _SENTENCE_MARKERS):
        issues.append("sentence_marker")
    if _DATE_ONLY_RE.match(item):
        issues.append("date_only")
    if title and item.casefold() == str(title).strip().casefold():
        issues.append("title_duplicate")
    if item.casefold() in _tag_set(tags):
        issues.append("tag_duplicate")
    if not any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in item):
        issues.append("no_alnum")
    return issues


def is_useful_keyword(value, *, title="", tags=""):
    return _useful(value, title=title, tags=tags)


def normalize_keywords(value, *, title="", tags="", limit=MAX_KEYWORDS):
    result = []
    seen = set()
    for raw in _parts(value):
        item = raw.strip()
        key = item.casefold()
        if key in seen or not _useful(item, title=title, tags=tags):
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= max(0, int(limit)):
            break
    return result
