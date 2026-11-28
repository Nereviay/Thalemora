from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .tool_prompts import (
    SELF_MODEL_PROVIDER_TOOL_SCHEMAS,
    SELF_MODEL_STATEMENT_GUIDANCE,
)


SURFACE = "self_maintenance"
SCAN_INTERVAL = timedelta(hours=6)
NORMAL_TOPIC_COOLDOWN = timedelta(hours=24)
CONTEMPLATION_TOPIC_COOLDOWN = timedelta(hours=1)
CONTEMPLATION_DEBOUNCE = timedelta(minutes=30)
EVIDENCE_THRESHOLD = 6
REPEAT_THRESHOLD_14D = 3
INDEPENDENCE_THRESHOLD = 2

_ALLOWED_TOOLS = (
    "self_model_review",
    "self_model_observe",
    "self_model_revise",
    "self_model_retire",
)

MAINTENANCE_SYSTEM_TASK = (
    "【自我维护】\n"
    "这是一次独立的内部自我维护，不是在给对话对象发消息。"
    "使用上面的当前正常对话上下文和你已有的自我认识，检查有没有你自己真正想继续观察、修订或退役的内容。"
    "没有需要更新的就不要为了完成任务而修改。"
    "不要把对话对象的偏好、要求或一次情绪直接写成‘你是谁’。"
    + SELF_MODEL_STATEMENT_GUIDANCE
    + "只使用 self_model_review / self_model_observe / self_model_revise / self_model_retire；"
    "最终不需要给对话对象输出消息。"
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class TopicSignal:
    topic_key: str
    new_agent_evidence_count: int = 0
    repeat_count_14d: int = 0
    independence_key_count: int = 0
    contemplation_at: datetime | None = None
    has_contradiction: bool = False
    explicit_invalidation: bool = False
    last_maintenance_at: datetime | None = None

    def __post_init__(self) -> None:
        if not str(self.topic_key or "").strip():
            raise ValueError("topic_key required")
        for field in (
            "new_agent_evidence_count",
            "repeat_count_14d",
            "independence_key_count",
        ):
            if int(getattr(self, field)) < 0:
                raise ValueError(f"{field} must be non-negative")
        _utc(self.contemplation_at)
        _utc(self.last_maintenance_at)


@dataclass(frozen=True)
class TopicEligibility:
    topic_key: str
    eligible: bool
    urgent: bool
    reasons: tuple[str, ...]
    cooldown_until: datetime | None


@dataclass(frozen=True)
class MaintenancePlan:
    scan_due: bool
    should_run: bool
    urgent: bool
    eligible_topics: tuple[TopicEligibility, ...]
    blocked_topics: tuple[TopicEligibility, ...]
    next_scan_at: datetime
    daily_limit_blocked: bool


def _cooldown_ready(
    now: datetime,
    last_maintenance_at: datetime | None,
    cooldown: timedelta,
) -> tuple[bool, datetime | None]:
    last = _utc(last_maintenance_at)
    if last is None:
        return True, None
    until = last + cooldown
    return now >= until, until


def evaluate_topic(signal: TopicSignal, *, now: datetime) -> TopicEligibility:
    now = _utc(now)
    assert now is not None

    urgent_reasons = []
    if signal.has_contradiction:
        urgent_reasons.append("contradiction")
    if signal.explicit_invalidation:
        urgent_reasons.append("explicit_invalidation")
    if urgent_reasons:
        return TopicEligibility(
            topic_key=signal.topic_key,
            eligible=True,
            urgent=True,
            reasons=tuple(urgent_reasons),
            cooldown_until=None,
        )

    reasons: list[str] = []
    contemplation_ready = False
    contemplation = _utc(signal.contemplation_at)
    if contemplation is not None and now >= contemplation + CONTEMPLATION_DEBOUNCE:
        contemplation_ready = True
        reasons.append("contemplation_debounced")

    if signal.new_agent_evidence_count >= EVIDENCE_THRESHOLD:
        reasons.append("new_agent_evidence_threshold")

    if (
        signal.repeat_count_14d >= REPEAT_THRESHOLD_14D
        and signal.independence_key_count >= INDEPENDENCE_THRESHOLD
    ):
        reasons.append("repeated_topic_14d")

    if not reasons:
        return TopicEligibility(
            topic_key=signal.topic_key,
            eligible=False,
            urgent=False,
            reasons=(),
            cooldown_until=None,
        )

    cooldown = (
        CONTEMPLATION_TOPIC_COOLDOWN
        if contemplation_ready
        else NORMAL_TOPIC_COOLDOWN
    )
    ready, cooldown_until = _cooldown_ready(
        now, signal.last_maintenance_at, cooldown
    )
    return TopicEligibility(
        topic_key=signal.topic_key,
        eligible=ready,
        urgent=False,
        reasons=tuple(reasons) if ready else tuple(reasons) + ("topic_cooldown",),
        cooldown_until=None if ready else cooldown_until,
    )


def plan_maintenance(
    signals: Iterable[TopicSignal],
    *,
    now: datetime,
    last_scan_at: datetime | None = None,
    last_real_run_at: datetime | None = None,
) -> MaintenancePlan:
    now = _utc(now)
    assert now is not None
    last_scan = _utc(last_scan_at)
    last_run = _utc(last_real_run_at)

    scan_due = last_scan is None or now >= last_scan + SCAN_INTERVAL
    next_scan_at = (
        now + SCAN_INTERVAL
        if last_scan is None or scan_due
        else last_scan + SCAN_INTERVAL
    )

    evaluated = tuple(evaluate_topic(signal, now=now) for signal in signals)
    eligible = tuple(item for item in evaluated if item.eligible)
    urgent = any(item.urgent for item in eligible)

    daily_limit_blocked = False
    regular_allowed = True
    if last_run is not None and now < last_run + timedelta(hours=24):
        regular_allowed = False
        daily_limit_blocked = any(not item.urgent for item in eligible)

    # Event-qualified topics may start maintenance immediately rather than
    # waiting for the 6h fallback scan.  The scan exists to discover missed
    # eligibility, not to delay a signal already known to be eligible.
    runnable = tuple(
        item
        for item in eligible
        if item.urgent or regular_allowed
    )
    blocked = tuple(
        item
        for item in evaluated
        if (not item.eligible)
        or (item.eligible and not item.urgent and not regular_allowed)
    )

    return MaintenancePlan(
        scan_due=scan_due,
        should_run=bool(runnable),
        urgent=any(item.urgent for item in runnable),
        eligible_topics=runnable,
        blocked_topics=blocked,
        next_scan_at=next_scan_at,
        daily_limit_blocked=daily_limit_blocked,
    )


def maintenance_tool_schemas(schemas=SELF_MODEL_PROVIDER_TOOL_SCHEMAS) -> tuple[dict[str, Any], ...]:
    """Return only the four Self Model capabilities for this internal surface."""
    by_name = {
        str(item.get("function", {}).get("name") or ""): item
        for item in schemas
    }
    return tuple(deepcopy(by_name[name]) for name in _ALLOWED_TOOLS)


def build_maintenance_messages(
    normal_provider_messages: Sequence[Mapping[str, Any]],
    *,
    pending_suggestion_ids: Sequence[str] = (),
    interlocutor: str = "对话对象",
) -> list[dict[str, Any]]:
    """Preserve the complete normal context and append one internal task.

    No synthetic user message is created.  The returned prefix is a deep copy
    that is value-identical to the supplied normal Provider messages.
    """
    if not normal_provider_messages:
        raise ValueError("normal provider context required")
    messages = deepcopy([dict(item) for item in normal_provider_messages])
    pending = tuple(dict.fromkeys(
        str(value or "").strip()
        for value in pending_suggestion_ids
        if str(value or "").strip()
    ))[:10]
    task = MAINTENANCE_SYSTEM_TASK.replace("对话对象", interlocutor)
    if pending:
        task += (
            f"\n有 {len(pending)} 条待复核建议："
            + "、".join(pending)
            + "。先 self_model_review，再决定是否 revise/retire；不要直接接受。"
        )
    messages.append({
        "role": "system",
        "content": task,
    })
    return messages


def validate_maintenance_result(
    *,
    visible_text: str | None,
    tool_calls: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Shadow result contract: text is discarded; only allowed tool intents remain."""
    allowed = set(_ALLOWED_TOOLS)
    names = tuple(
        str(item.get("name") or item.get("function", {}).get("name") or "").strip()
        for item in tool_calls
        if isinstance(item, Mapping)
    )
    forbidden = tuple(name for name in names if name and name not in allowed)
    return {
        "surface": SURFACE,
        "discard_visible_text": bool(str(visible_text or "")),
        "allowed_tool_names": tuple(name for name in names if name in allowed),
        "forbidden_tool_names": forbidden,
        "accepted": not forbidden,
    }
