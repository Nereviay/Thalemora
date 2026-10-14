"""来源沉底评分；仅真实激活及有语义的修订影响时间。"""
from __future__ import annotations

import datetime as dt
import math
from typing import Mapping, Iterable
from .activation import parse_time as _parse


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _authority_blocked(card: Mapping) -> bool:
    return (
        bool(int(card.get("pinned") or 0))
        or str(card.get("memory_kind") or "") == "timeline"
        or str(card.get("source") or "") == "manual"
        or str(card.get("revision_origin") or "") == "manual_ui"
    )


def _reference_time(
    card: Mapping,
    *,
    revision_created_at: str = "",
    activation_last_at: str = "",
) -> dt.datetime | None:
    candidates = [
        _parse(card.get("created_at")),
        _parse(revision_created_at),
        _parse(activation_last_at),
    ]
    values = [item for item in candidates if item is not None]
    return max(values) if values else None


def dormancy_score(
    card: Mapping,
    *,
    now: dt.datetime,
    archive_days: float,
    revision_created_at: str = "",
    activation_weight: float = 0.0,
    activation_last_at: str = "",
) -> dict:
    now = _parse(now.isoformat())
    ref = _reference_time(
        card,
        revision_created_at=revision_created_at,
        activation_last_at=activation_last_at,
    ) or now
    age_days = max(0.0, (now - ref).total_seconds() / 86400.0)
    importance = max(
        1.0, min(10.0, _safe_float(card.get("importance"), 5.0) or 5.0)
    )
    arousal = max(
        0.0, min(1.0, _safe_float(card.get("arousal"), 0.3) or 0.3)
    )
    genuine_activity = max(0.0, float(activation_weight or 0.0))

    decay_lambda = math.log(2.0) / max(float(archive_days), 1.0)
    time_weight = math.exp(-decay_lambda * age_days)
    emotion_weight = 0.6 + arousal * 0.8
    if age_days <= 3.0:
        combined_weight = time_weight * 0.7 + emotion_weight * 0.3
    else:
        combined_weight = emotion_weight * 0.7 + time_weight * 0.3

    # 保留来源评分曲线；只有真实激活计入，不使用检索展示次数。
    activation_factor = math.sqrt(1.0 + genuine_activity)
    score = importance * activation_factor * time_weight * combined_weight
    return {
        "score": score,
        "days": age_days,
        "importance": importance,
        "genuine_activation_weight": genuine_activity,
        "time_weight": time_weight,
        "emotion_weight": emotion_weight,
        "combined_weight": combined_weight,
        "reference_time": ref.isoformat(sep=" ", timespec="seconds"),
    }


def summarize(candidates: Iterable[Mapping]) -> dict:
    rows = list(candidates)
    return {
        "candidate_count": len(rows),
        "would_archive_count": sum(
            1 for item in rows if bool(item.get("would_archive"))
        ),
        "card_ids": [int(item["id"]) for item in rows],
    }
