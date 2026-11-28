"""Policy helpers for semantic whisper recall.

``consumed`` is a legacy direct-injection field and never controls recall eligibility.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

DEFAULT_SCORE_FLOOR = 0.60
DEFAULT_TOP_N = 1
MOOD_PREFIX = "（情绪评估）"


def eligible_whisper(row: Mapping[str, Any]) -> bool:
    content = str(row.get("content") or "").strip()
    return bool(content and not content.startswith(MOOD_PREFIX))


def select_whisper_recall(
    candidates: Iterable[Mapping[str, Any]],
    *,
    score_floor: float = DEFAULT_SCORE_FLOOR,
    top_n: int = DEFAULT_TOP_N,
    cooled_ids: Iterable[int] = (),
) -> list[dict[str, Any]]:
    cooled = {int(x) for x in cooled_ids or () if str(x).isdigit()}
    accepted: list[dict[str, Any]] = []
    for raw in candidates or ():
        row = dict(raw)
        if not eligible_whisper(row):
            continue
        try:
            wid = int(row.get("id") or 0)
            score = float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            continue
        if wid <= 0 or wid in cooled or score < float(score_floor):
            continue
        row["id"] = wid
        row["score"] = score
        accepted.append(row)
    accepted.sort(key=lambda item: (-item["score"], -item["id"]))
    return accepted[: max(0, int(top_n))]


def render_whisper_recall(items: Iterable[Mapping[str, Any]], *, actor="助手") -> str:
    rows = [dict(item) for item in items or () if eligible_whisper(item)]
    if not rows:
        return ""
    lines = []
    for row in rows:
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        created_at = str(row.get("created_at") or "").strip()
        date = created_at[:10] if len(created_at) >= 10 else ""
        lines.append(f"- {date} 写下：{content}" if date else f"- {content}")
    return (
        "【过去写下的想说的话】\n"
        f"这是{actor}过去写下但当时没有直接说出口的内容，只作为相关语境下的一点内部回想。"
        "其中‘今天/现在/等会儿’等时间词属于写下当时，不代表当前事实或待办；"
        "不要求提起，也不要逐字复述。\n"
        + "\n".join(lines)
    )


def cooled_ids(ports, now=None):
    WHISPER_RECALL_COOLDOWN = ports['WHISPER_RECALL_COOLDOWN']
    _whisper_recall_history = ports['_whisper_recall_history']
    time = ports['time']
    now = time.time() if now is None else float(now)
    expired = [
        wid for wid, ts in list(_whisper_recall_history.items())
        if now - float(ts or 0) >= WHISPER_RECALL_COOLDOWN
    ]
    for wid in expired:
        _whisper_recall_history.pop(wid, None)
    return set(_whisper_recall_history)


def mark_injected(ports, ids, now=None):
    _whisper_recall_history = ports['_whisper_recall_history']
    time = ports['time']
    now = time.time() if now is None else float(now)
    for item in ids or ():
        try:
            wid = int(item)
        except (TypeError, ValueError):
            continue
        if wid > 0:
            _whisper_recall_history[wid] = now
