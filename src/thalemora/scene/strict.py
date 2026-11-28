"""场景候选的来源、事实归属、元数据和路由校验。"""
import hashlib
import json
import math
import re
from thalemora.water._keywords import is_useful_keyword

MEMORY_CAP = 5


ROUTES = {"memory", "plan", "drop"}


EVENT_STATES = ("ongoing", "update", "closed")


MEMORY_TYPES = {
    "stable_fact",
    "preference",
    "agreement",
    "significant_episode",
    "shared_project",
    "plan",
}


CLAIM_SCOPES = {"user_fact", "assistant_self", "shared", "derived"}


SPEAKER_ROLE_CLAIM_SCOPES = {"assistant", "assistant_fact", "user"}


OUTPUT_FIELDS = (
    "route",
    "identity",
    "memory_type",
    "title",
    "content",
    "keywords",
    "importance",
    "valence",
    "arousal",
    "claim_scope",
    "evidence",
    "reason",
)


GENERIC_KEYWORDS = {
    "我",
    "我们",
    "用户",
    "助手",
    "user",
    "assistant",
    "关系",
    "日子",
    "生活",
    "记忆",
    "事情",
    "事件",
    "日程",
    "经历",
    "聊天",
    "对话",
    "互动",
    "情绪",
    "感受",
    "重要",
}

class StrictAdmissionError(RuntimeError):
    """Base error for the isolated strict-admission runner."""


class StrictContractConflict(StrictAdmissionError):
    """An output or run id conflicts with its immutable contract."""


class StrictSourceError(StrictAdmissionError):
    """A source receipt or frozen window is absent or inconsistent."""


class StrictSchemaError(StrictAdmissionError):
    """The output is not a compatible strict-admission receipt database."""


def _stable_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(value):
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()

def _strip_json_noise(raw):
    if not isinstance(raw, str):
        return raw
    value = raw.strip()
    value = re.sub(r"(?is)<think>.*?</think>", "", value).strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", value, re.S | re.I)
    return match.group(1).strip() if match else value


def _parse_json_array(raw):
    if isinstance(raw, list):
        return raw
    cleaned = _strip_json_noise(raw)
    try:
        value = json.loads(cleaned)
    except (TypeError, json.JSONDecodeError):
        match = re.search(r"\[.*\]", cleaned or "", re.S)
        if match is None:
            return []
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []

def _evidence(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            result.append({"event_id": None, "quote": ""})
            continue
        try:
            event_id = int(item.get("event_id"))
        except (TypeError, ValueError):
            event_id = None
        quote = item.get("quote")
        result.append(
            {"event_id": event_id, "quote": quote if isinstance(quote, str) else ""}
        )
    return result


def _metadata_warning(field, code, **details):
    return {"field": field, "code": code, **details}


def _normalized_number(value, low, high, default, field):
    if isinstance(value, bool):
        return default, [
            _metadata_warning(field, "defaulted", input=value, output=default)
        ]
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default, [
            _metadata_warning(field, "defaulted", input=value, output=default)
        ]
    if not math.isfinite(number):
        return default, [
            _metadata_warning(field, "defaulted", input=value, output=default)
        ]
    clamped = min(high, max(low, number))
    if clamped != number:
        return clamped, [_metadata_warning(field, "clamped", input=number, output=clamped)]
    return clamped, []


def _clean_keywords(value):
    if isinstance(value, str):
        value = value.split(",")
    source = value if isinstance(value, list) else []
    cleaned = []
    removed_invalid = []
    removed_empty = 0
    removed_duplicates = []
    for item in source:
        if item is None:
            removed_empty += 1
            continue
        keyword = str(item).strip()
        if not keyword:
            removed_empty += 1
            continue
        if _is_generic_keyword(keyword) or not is_useful_keyword(keyword):
            removed_invalid.append(keyword)
        elif keyword in cleaned:
            removed_duplicates.append(keyword)
        else:
            cleaned.append(keyword)
    warnings = []
    if removed_invalid:
        warnings.append(
            _metadata_warning("keywords", "removed_invalid", values=removed_invalid)
        )
    if removed_empty:
        warnings.append(
            _metadata_warning("keywords", "removed_empty", count=removed_empty)
        )
    if removed_duplicates:
        warnings.append(
            _metadata_warning(
                "keywords", "removed_duplicates", values=removed_duplicates
            )
        )
    if len(cleaned) > 4:
        warnings.append(
            _metadata_warning(
                "keywords", "truncated", input_count=len(cleaned), output_count=4
            )
        )
    return cleaned[:4], warnings


def _merge_warnings(target, warnings):
    for warning in warnings:
        if warning not in target:
            target.append(warning)


def normalize_routes(raw):
    """Normalize derived metadata without turning dirty values into route failures."""
    routes = []
    for item in _parse_json_array(raw):
        if not isinstance(item, dict):
            continue
        warnings = []
        keywords, keyword_warnings = _clean_keywords(item.get("keywords"))
        importance, importance_warnings = _normalized_number(
            item.get("importance"), 1.0, 10.0, 5.0, "importance"
        )
        valence, valence_warnings = _normalized_number(
            item.get("valence"), 0.0, 1.0, 0.5, "valence"
        )
        arousal, arousal_warnings = _normalized_number(
            item.get("arousal"), 0.0, 1.0, 0.5, "arousal"
        )
        for field_warnings in (
            keyword_warnings,
            importance_warnings,
            valence_warnings,
            arousal_warnings,
        ):
            _merge_warnings(warnings, field_warnings)
        route = {
            "route": str(item.get("route") or "").strip().lower(),
            "identity": str(item.get("identity") or "").strip(),
            "memory_type": str(item.get("memory_type") or "").strip().lower(),
            "title": str(item.get("title") or "").strip(),
            "content": str(item.get("content") or "").strip(),
            "keywords": keywords,
            "importance": importance,
            "valence": valence,
            "arousal": arousal,
            "claim_scope": str(item.get("claim_scope") or "").strip(),
            "evidence": _evidence(item.get("evidence")),
            "reason": str(item.get("reason") or "").strip(),
            "metadata_warnings": warnings,
        }
        if (route["route"] == "memory" and route["claim_scope"] == "user_fact"
                and item.get("event_state") in EVENT_STATES):
            route["event_state"] = item["event_state"]
        routes.append(route)
    return routes


def _is_generic_keyword(keyword):
    compact = re.sub(r"\s+", "", keyword).lower()
    if compact in GENERIC_KEYWORDS:
        return True
    return bool(
        re.fullmatch(
            r"(?:19|20)\d{2}(?:[-/.年]\d{1,2})?(?:[-/.月]\d{1,2}日?)?", compact
        )
    )


def _grounded_anchor_candidates(route, materials=()):
    values = [
        route.get("identity", ""),
        route.get("title", ""),
        route.get("content", ""),
    ]
    for material in materials:
        values.extend((material.get("identity", ""), material.get("material", "")))
    candidates = []
    for value in values:
        for fragment in re.split(r"[，。；;！？!?\n]+", str(value)):
            anchor = fragment.strip()
            compact = re.sub(r"\s+", "", anchor)
            if (
                2 <= len(compact) <= 24
                and not _is_generic_keyword(anchor)
                and anchor not in candidates
            ):
                candidates.append(anchor)
    return candidates


def finalize_derived_metadata(route, materials=()):
    """Keep underfilled keywords underfilled rather than fabricating sentence fragments."""
    if route["route"] not in {"memory", "plan"}:
        return route
    if len(route["keywords"]) < 2:
        _merge_warnings(
            route["metadata_warnings"],
            [
                _metadata_warning(
                    "keywords", "fewer_than_two_anchors", count=len(route["keywords"])
                )
            ],
        )
    return route


def _has_report_voice(item):
    """Return whether rendering uses explicit user/assistant report labels."""
    text = f"{item['title']}\n{item['content']}"
    return any(term in text.lower() for term in ("用户", "助手", "user", "assistant"))


def _fail_soft_claim_scope(item, has_user_quote, has_assistant_quote):
    """Downgrade uncertain ownership without promoting a claim to a fact."""
    if item["route"] == "drop":
        if item["claim_scope"] in CLAIM_SCOPES:
            return False
        scope = item["claim_scope"]
        item["claim_scope"] = "derived"
        _merge_warnings(
            item["metadata_warnings"],
            [
                _metadata_warning(
                    "claim_scope",
                    "normalized_to_derived",
                    input=scope,
                    reason="drop_scope_not_applicable",
                )
            ],
        )
        return True
    if item["route"] not in {"memory", "plan"}:
        return False
    has_grounded_evidence = has_user_quote or has_assistant_quote
    if not has_grounded_evidence:
        return False
    scope = item["claim_scope"]
    reason = None
    if scope in SPEAKER_ROLE_CLAIM_SCOPES:
        reason = "illegal_speaker_role_scope"
    elif scope == "user_fact" and not has_user_quote:
        reason = "user_evidence_unavailable"
    elif scope == "assistant_self" and not has_assistant_quote:
        reason = "assistant_evidence_unavailable"
    elif scope == "shared" and not (has_user_quote and has_assistant_quote):
        reason = "bilateral_evidence_unavailable"
    if reason is None:
        return False
    item["claim_scope"] = "derived"
    item.pop("event_state", None)
    _merge_warnings(
        item["metadata_warnings"],
        [
            _metadata_warning(
                "claim_scope",
                "normalized_to_derived",
                input=scope,
                reason=reason,
            )
        ],
    )
    return True


def evaluate_routes(routes, events):
    event_by_id = {int(event["event_id"]): event for event in events}
    validity = []
    exact_evidence_invalid = 0
    claim_scope_source_role_invalid = 0
    claim_scope_invalid = 0
    memory_count = sum(item["route"] == "memory" for item in routes)
    for index, item in enumerate(routes):
        item.setdefault("metadata_warnings", [])
        errors = []
        if item["route"] not in ROUTES:
            errors.append("invalid_route")
        if not item["identity"]:
            errors.append("missing_identity")
        if item["memory_type"] not in MEMORY_TYPES:
            errors.append("invalid_memory_type")
        if not item["reason"]:
            errors.append("missing_reason")
        if item["route"] in {"memory", "plan"}:
            if not item["title"]:
                errors.append("missing_title")
            if not item["content"]:
                errors.append("missing_content")
        if item["route"] == "memory" and item["memory_type"] == "plan":
            errors.append("plan_type_routed_to_memory")
        if item["route"] == "plan" and item["memory_type"] != "plan":
            errors.append("plan_route_requires_plan_type")
        if memory_count > MEMORY_CAP and item["route"] == "memory":
            _merge_warnings(
                item["metadata_warnings"],
                [
                    _metadata_warning(
                        "routing",
                        "memory_cap_exceeded",
                        memory_count=memory_count,
                        cap=MEMORY_CAP,
                    )
                ],
            )

        evidence_checks = []
        has_user_quote = False
        has_assistant_quote = False
        for evidence_index, evidence in enumerate(item["evidence"]):
            evidence_errors = []
            event = event_by_id.get(evidence["event_id"])
            if event is None:
                evidence_errors.append("event_not_in_window")
            if not evidence["quote"]:
                evidence_errors.append("empty_quote")
            elif event is not None and evidence["quote"] not in event["quotable_text"]:
                evidence_errors.append("quote_not_exact")
            if not evidence_errors and event["role"] == "user":
                has_user_quote = True
            if not evidence_errors and event["role"] == "assistant":
                has_assistant_quote = True
            if evidence_errors:
                exact_evidence_invalid += 1
            evidence_checks.append(
                {
                    "evidence_index": evidence_index,
                    "event_id": evidence["event_id"],
                    "valid": not evidence_errors,
                    "errors": evidence_errors,
                }
            )
        _fail_soft_claim_scope(item, has_user_quote, has_assistant_quote)
        if item["route"] in {"memory", "plan"} and item["claim_scope"] not in CLAIM_SCOPES:
            errors.append("invalid_claim_scope")
            claim_scope_invalid += 1
        if (
            item["route"] in {"memory", "plan"}
            and item["claim_scope"] == "user_fact"
            and not has_user_quote
        ):
            errors.append("user_fact_requires_user_evidence")
            claim_scope_source_role_invalid += 1
        if (
            item["route"] in {"memory", "plan"}
            and item["claim_scope"] == "assistant_self"
            and not has_assistant_quote
        ):
            errors.append("assistant_self_requires_assistant_evidence")
            claim_scope_source_role_invalid += 1
        if (
            item["route"] in {"memory", "plan"}
            and item["claim_scope"] == "shared"
            and not (has_user_quote and has_assistant_quote)
        ):
            errors.append("shared_requires_user_and_assistant_evidence")
            claim_scope_source_role_invalid += 1
        if (
            item["route"] in {"memory", "plan"}
            and item["claim_scope"] == "derived"
            and not (has_user_quote or has_assistant_quote)
        ):
            errors.append("derived_requires_grounded_evidence")
            claim_scope_source_role_invalid += 1
        validity.append(
            {
                "route_index": index,
                "valid": not errors and all(check["valid"] for check in evidence_checks),
                "contract_errors": errors,
                "evidence": evidence_checks,
            }
        )

    admitted = [item for item in routes if item["route"] in {"memory", "plan"}]
    render_warning_count = 0
    for item in admitted:
        if _has_report_voice(item):
            warning = _metadata_warning("rendering", "explicit_report_role")
            _merge_warnings(item["metadata_warnings"], [warning])
            render_warning_count += 1
    metadata_warning_count = sum(len(item["metadata_warnings"]) for item in routes)
    scope_downgrade_count = sum(
        warning.get("field") == "claim_scope"
        and warning.get("code") == "normalized_to_derived"
        for item in routes
        for warning in item["metadata_warnings"]
    )
    metrics = {
        "memory_count": memory_count,
        "plan_count": sum(item["route"] == "plan" for item in routes),
        "drop_count": sum(item["route"] == "drop" for item in routes),
        # Compatibility aggregate retained for existing receipt readers. New
        # readers should use the two counters below to distinguish exact quote
        # failures from claim-scope/source-role contract failures.
        "evidence_invalid": (
            exact_evidence_invalid + claim_scope_source_role_invalid
        ),
        "exact_evidence_invalid": exact_evidence_invalid,
        "claim_scope_source_role_invalid": claim_scope_source_role_invalid,
        "claim_scope_invalid": claim_scope_invalid,
        "claim_scope_downgrade_count": scope_downgrade_count,
        "claim_scope_downgrade_routes": sum(
            any(
                warning.get("field") == "claim_scope"
                and warning.get("code") == "normalized_to_derived"
                for warning in item["metadata_warnings"]
            )
            for item in routes
        ),
        "generic_keyword_hits": sum(
            len(warning.get("values", []))
            for item in admitted
            for warning in item["metadata_warnings"]
            if warning.get("field") == "keywords"
            and warning.get("code") == "removed_invalid"
        ),
        "perspective_violations": render_warning_count,
        "metadata_warning_count": metadata_warning_count,
        "metadata_warning_routes": sum(bool(item["metadata_warnings"]) for item in routes),
        "render_warning_count": render_warning_count,
        "memory_count_over_cap": max(0, memory_count - MEMORY_CAP),
        "memory_cap_warning_routes": sum(
            any(
                warning.get("field") == "routing"
                and warning.get("code") == "memory_cap_exceeded"
                for warning in item["metadata_warnings"]
            )
            for item in routes
        ),
    }
    return validity, metrics


def _empty_metrics():
    return {
        "memory_count": 0,
        "plan_count": 0,
        "drop_count": 0,
        "evidence_invalid": 0,
        "exact_evidence_invalid": 0,
        "claim_scope_source_role_invalid": 0,
        "claim_scope_invalid": 0,
        "claim_scope_downgrade_count": 0,
        "claim_scope_downgrade_routes": 0,
        "generic_keyword_hits": 0,
        "perspective_violations": 0,
        "metadata_warning_count": 0,
        "metadata_warning_routes": 0,
        "render_warning_count": 0,
        "memory_count_over_cap": 0,
        "memory_cap_warning_routes": 0,
    }
