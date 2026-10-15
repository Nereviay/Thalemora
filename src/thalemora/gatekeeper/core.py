"""同一候选 Gatekeeper；Memory 写入只经 revision_store。

来源：gatekeeper.py 指定职责的净化提取。Provider 接收 messages，返回 dict/JSON。
无生产默认 provider、召回器、Prompt、路径；冻结候选和模型 profile 由调用方提供。
"""
import hashlib
import json
import re
import sqlite3
from functools import partial

from thalemora.memory import revision_store, evidence_resolver
from . import keywords as keyword_policy
from . import ops as ops_store

# 仅公共决策协议；产品叙述视角由调用方 provider 负责，不携带私人 Prompt。
SYSTEM = "按候选事实及冻结卡片决定 new/extend/correct/supersede/drop/uncertain。只返回 JSON。更新须同一记忆且置信度至少 0.85；extend 仅附加候选原文，不改主卡。字段为 action,target_card_id,same_memory,same_memory_confidence,long_term_value,content,title,keywords,category,importance,reason。保留来源、否定、时间及不确定性，不编造事实。"
SCENE_NEW_CARD_RULES = "Scene 新卡须明确通过三十天长期价值判断 long_term_value；用户明确偏好及约定须保留。"
RELATION_RECHECK_SYSTEM = "只复核明确纠正候选与冻结旧卡是否同一记忆，返回相同决策协议 JSON。"
MAINTENANCE_SYSTEM = "只依据原文提出整理稿及缺失元数据、现有 Stream 路由；遵守输入作者与叙述者，不编造事实或行动。原文始终是唯一事实权威。返回 JSON 字段 organized_content,title,keywords,importance,tags,water_stream_id,water_reason。fill_fields 之外的元数据返回 null；needs_organized_content=false 时整理稿返回 null；needs_water=false 时路由返回 null。importance 为 1–10，tags 仅选我们/你/我/项目/规则/生活/心情/日子/未分类；Stream 仅从输入 active_streams 选择，无合适位置返回 null。"


def _provider_required(messages):
    raise GatekeeperUnavailable("provider_required")


def _frozen_map_required(content):
    raise GatekeeperUnavailable("frozen_map_required")


def _profile_fingerprint(profile):
    return _stable_hash({"model": profile["model"],
                         "thinking_enabled": profile["thinking_enabled"],
                         "reasoning_effort": (profile["reasoning_effort"]
                                              if profile["thinking_enabled"] else None)})

GATE_POLICY_VERSION = "p3c1-gate-v10-memory-first-person-quality"

SCENE_NEW_CARD_POLICY_VERSION = "p3c1-gate-v11-durable-new-card-admission"

VALID_ACTIONS = {"new", "extend", "supersede", "correct", "drop", "uncertain"}

UPDATE_ACTIONS = {"extend", "supersede", "correct"}

PRESET_CATEGORIES = {"我们", "你", "我", "项目", "规则", "生活", "心情", "日子", "未分类"}

SAME_MEMORY_CONFIDENCE_MIN = 0.85

CORRECTION_SIGNAL_TERMS = (
    "纠正", "更正", "改正", "记错", "说错", "搞错", "弄错",
    "不是……而是", "不是...而是", "其实不是", "并不是",
)

FUTURE_PLAN_TERMS = (
    "演唱会", "考试", "旅行", "航班", "车票", "机票", "预约", "面试",
    "比赛", "就医", "手术", "聚会", "约会", "开学", "毕业", "搬家",
)

FUTURE_PLAN_MARKERS = (
    "准备", "打算", "计划", "要去", "还要", "会去", "将要", "预约",
    "报名", "购票", "抢票", "明天", "后天", "下周", "下个月", "星期", "周五", "周六", "周日",
)

class GatekeeperUnavailable(Exception):
    pass

class InvalidGateDecision(GatekeeperUnavailable):
    pass

def _stable_hash(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def freeze_candidate_map_from_card_ids(card_ids, *, memory_db_path):
    """Freeze canonical Memory projections for an already-derived candidate set.

    This is decision-only input preparation. It never mutates Memory and keeps
    the same manual/timeline authority exclusions as the legacy recall path.
    """
    memory_path = memory_db_path
    ordered_ids = []
    seen = set()
    for value in card_ids or ():
        try:
            card_id = int(value)
        except (TypeError, ValueError):
            continue
        if card_id <= 0 or card_id in seen:
            continue
        seen.add(card_id)
        ordered_ids.append(card_id)
    if not ordered_ids:
        return []

    connection = sqlite3.connect(memory_path)
    connection.row_factory = sqlite3.Row
    try:
        frozen = []
        for card_id in ordered_ids:
            row = connection.execute(
                "SELECT c.*, cr.origin AS revision_origin "
                "FROM cards c LEFT JOIN card_revisions cr "
                "ON cr.id=c.current_revision_id WHERE c.id=?",
                (card_id,),
            ).fetchone()
            if row is None or not row["current_revision_id"]:
                continue
            item = dict(row)
            if str(item.get("memory_kind") or "episodic") == "timeline":
                continue
            if str(item.get("source") or "") == "manual":
                continue
            if str(item.get("revision_origin") or "") == "manual_ui":
                continue
            frozen.append({
                "card_id": card_id,
                "current_revision_id": int(item["current_revision_id"]),
                "title": item.get("title") or "",
                "content": item.get("content") or "",
                "tags": item.get("tags") or "",
                "keywords": item.get("keywords") or "",
                "importance": float(item.get("importance") or 0),
                "memory_kind": item.get("memory_kind") or "episodic",
            })
        return frozen
    finally:
        connection.close()

def gate_input_hash(core, frozen_map, profile):
    ordered = sorted(frozen_map, key=lambda item: int(item["card_id"]))
    map_for_hash = [
        {
            "card_id": item["card_id"],
            "current_revision_id": item["current_revision_id"],
            "title": item.get("title") or "",
            "content": item.get("content") or "",
        }
        for item in ordered
    ]
    return _stable_hash({
        "candidate_core": core,
        "frozen_map": map_for_hash,
        "model_profile": _profile_fingerprint(profile),
        "policy_version": (
            SCENE_NEW_CARD_POLICY_VERSION if "memory_type" in core
            else GATE_POLICY_VERSION
        ),
    })

def _future_plan_terms(core):
    text = f"{core.get('title') or ''}\n{core.get('content') or ''}"
    terms = [term for term in FUTURE_PLAN_TERMS if term in text]
    has_time = bool(re.search(r"(?<!\d)\d{1,2}号(?!\d)|\d{4}-\d{1,2}-\d{1,2}", text))
    has_future = has_time or any(marker in text for marker in FUTURE_PLAN_MARKERS)
    return terms if has_future else []

def _guard_future_plan_decision(decision, core, frozen_map):
    """未来计划先保住 extractor 已验证正文；不让 gate 把它揉进无关通用卡。"""
    terms = _future_plan_terms(core)
    if not terms or decision["action"] in {"drop", "uncertain"}:
        return decision
    guarded = dict(decision)
    if guarded["action"] == "new":
        guarded["content"] = str(core.get("content") or "").strip()
        return guarded
    if guarded["action"] in UPDATE_ACTIONS:
        target = next(
            (item for item in frozen_map if int(item["card_id"]) == guarded["target_card_id"]),
            None,
        )
        target_text = (
            f"{target.get('title') or ''}\n{target.get('content') or ''}" if target else ""
        )
        if not any(term in target_text for term in terms):
            guarded["action"] = "new"
            guarded["target_card_id"] = None
            guarded["content"] = str(core.get("content") or "").strip()
            if core.get("claim_scope") == "user_fact":
                guarded["category"] = "你"
            guarded["reason"] = "future_plan_kept_separate"
    return guarded

def _parse_decision(raw, frozen_map, fallback_content, *, fallback_importance=None):
    try:
        decision = raw if isinstance(raw, dict) else json.loads(raw)
    except Exception as exc:
        raise InvalidGateDecision("invalid_json") from exc
    if not isinstance(decision, dict):
        raise InvalidGateDecision("decision_not_object")
    action = decision.get("action")
    if action not in VALID_ACTIONS:
        raise InvalidGateDecision("invalid_action")

    frozen_by_id = {int(item["card_id"]): item for item in frozen_map}
    target_id = decision.get("target_card_id")
    if target_id is None:
        target_id = decision.get("card_id")
    if target_id is not None:
        try:
            target_id = int(target_id)
        except (TypeError, ValueError) as exc:
            raise InvalidGateDecision("invalid_target") from exc
    if action in UPDATE_ACTIONS and target_id not in frozen_by_id:
        raise InvalidGateDecision("target_outside_frozen_map")
    if action not in UPDATE_ACTIONS:
        target_id = None

    content = str(decision.get("content") or fallback_content).strip()
    if action not in {"drop", "uncertain"} and not content:
        raise InvalidGateDecision("empty_content")
    try:
        score = decision.get("importance", fallback_importance)
        importance = None if score is None else max(1.0, min(10.0, float(score)))
        if action not in {"drop","uncertain"} and importance is None:
            raise InvalidGateDecision("missing_importance")
    except (TypeError, ValueError) as exc:
        raise InvalidGateDecision("invalid_importance") from exc
    category = str(decision.get("category") or "未分类").strip()
    if category not in PRESET_CATEGORIES:
        category = "未分类"
    same_memory = decision.get("same_memory") is True
    try:
        same_memory_confidence = max(
            0.0, min(1.0, float(decision.get("same_memory_confidence", 0.0)))
        )
    except (TypeError, ValueError):
        same_memory_confidence = 0.0
    return {
        "action": action,
        "target_card_id": target_id,
        "same_memory": same_memory,
        "same_memory_confidence": same_memory_confidence,
        "long_term_value": (
            decision.get("long_term_value")
            if isinstance(decision.get("long_term_value"), bool) else None
        ),
        "content": content,
        "title": str(decision.get("title") or "").strip()[:80],
        "keywords": keyword_policy.normalize_csv(
            decision.get("keywords") or "",
            title=str(decision.get("title") or "").strip(),
            tags=category,
            limit=4,
        ),
        # Accept the legacy provider field during rollout. Commit never trusts
        # either field as the extension payload.
        "extension": str(
            decision.get("extension") or decision.get("ring") or ""
        ).strip(),
        "category": category,
        "importance": importance,
        "reason": str(decision.get("reason") or "").strip(),
    }

def _guard_same_memory_update(decision, core):
    """Fail closed on destructive reconciliation: related is not identical."""
    if decision["action"] not in UPDATE_ACTIONS:
        return decision
    if (
        decision.get("same_memory") is True
        and float(decision.get("same_memory_confidence") or 0.0)
        >= SAME_MEMORY_CONFIDENCE_MIN
    ):
        return decision
    guarded = dict(decision)
    guarded["action"] = "new"
    guarded["target_card_id"] = None
    guarded["content"] = str(core.get("content") or "").strip()
    guarded["extension"] = ""
    guarded["reason"] = "same_memory_boundary_kept_separate"
    return guarded

def _has_explicit_correction_signal(core):
    text = f"{core.get('title') or ''}\n{core.get('content') or ''}"
    if any(term in text for term in CORRECTION_SIGNAL_TERMS):
        return True
    return bool(re.search(r"不是.{0,40}(?:而是|只是)", text))

def _relation_recheck(core, frozen_map, provider, *, fallback_importance=None):
    prompt = json.dumps(
        {"candidate": core, "frozen_cards": frozen_map},
        ensure_ascii=False,
        sort_keys=True,
    )
    raw = (provider or _provider_required)([
        {"role": "system", "content": RELATION_RECHECK_SYSTEM},
        {"role": "user", "content": prompt},
    ])
    decision = _parse_decision(
        raw, frozen_map, core.get("content") or "",
        fallback_importance=fallback_importance,
    )
    decision = _guard_same_memory_update(decision, core)
    if decision["action"] in {"extend", "correct", "supersede"}:
        return decision
    return None

def decide(core, frozen_map=None, provider=None):
    try:
        frozen = (
            list(frozen_map)
            if frozen_map is not None
            else _frozen_map_required(core.get("content") or "")
        )
    except GatekeeperUnavailable:
        raise
    except Exception as exc:
        raise GatekeeperUnavailable(str(exc)) from exc
    ordered = sorted(frozen, key=lambda item: int(item["card_id"]))
    prompt = json.dumps(
        {"candidate": core, "frozen_cards": ordered},
        ensure_ascii=False,
        sort_keys=True,
    )
    try:
        raw = (provider or _provider_required)([
            {"role": "system", "content": (
                SYSTEM + SCENE_NEW_CARD_RULES if "memory_type" in core
                else SYSTEM
            )},
            {"role": "user", "content": prompt},
        ])
    except GatekeeperUnavailable:
        raise
    except Exception as exc:
        raise GatekeeperUnavailable(str(exc)) from exc
    decision = _parse_decision(raw, ordered, core.get("content") or "")
    protected_user_boundary = (
        "memory_type" in core
        and core.get("memory_type") in {"preference", "agreement"}
        and "user_text_exact" in core.get("source_roles", ())
    )
    if protected_user_boundary and decision["action"] == "drop" and not ordered:
        decision = {**decision, "action": "uncertain",
                    "reason": "scene_user_boundary_direct_drop_uncertain"}
    initial_action = decision["action"]
    decision = _guard_same_memory_update(decision, core)
    decision = _guard_future_plan_decision(decision, core, ordered)
    # Only new Scene-first candidates enter this stricter durable-card lane.
    # The older direct/legacy Gate contract remains unchanged.
    if "memory_type" in core and decision["action"] == "new":
        if decision["long_term_value"] is False:
            user_boundary = (
                core.get("memory_type") in {"preference", "agreement"}
                and "user_text_exact" in core.get("source_roles", ())
            )
            decision = {
                **decision,
                "action": "uncertain" if user_boundary else "drop",
                "target_card_id": None,
                "reason": (
                    "scene_explicit_user_boundary_needs_preservation_check"
                    if user_boundary else "scene_new_failed_30_day_test"
                ),
            }
        elif decision["long_term_value"] is not True:
            # A failed same-memory update must never silently turn into an
            # unreviewed new card; preserve the candidate for safe retry.
            decision = {**decision, "action": "uncertain",
                        "target_card_id": None,
                        "reason": "scene_update_rejected_new_value_unconfirmed"}
    if (
        decision["action"] == "new"
        and ordered
        and _has_explicit_correction_signal(core)
    ):
        reviewed = _relation_recheck(
            core, ordered, provider, fallback_importance=decision["importance"]
        )
        if reviewed is not None:
            decision = _guard_future_plan_decision(reviewed, core, ordered)
    return decision, ordered

def _csv_union(old_value, new_value, *, title="", tags=""):
    return keyword_policy.merge_keywords(
        old_value, new_value, title=title, tags=tags
    )

def _commit(decision, core, frozen_map, *, memory_db_path, candidate_id,
            source, origin, evidence_source_kind, evidence_source_ref,
            evidence_rows=None, embedding_provider=None, embedding_model=None,
            connection=None):
    create_card = (partial(revision_store.create_card_in_transaction, connection)
                   if connection is not None else partial(revision_store.create_card, memory_db_path))
    update_card = (partial(revision_store.update_card_in_transaction, connection)
                   if connection is not None else partial(revision_store.update_card, memory_db_path))
    create_extension = (partial(revision_store.create_extension_in_transaction, connection)
                        if connection is not None else partial(revision_store.create_extension, memory_db_path))
    action = decision["action"]
    if action == "extend":
        target = {
            int(item["card_id"]): item for item in frozen_map
        }[decision["target_card_id"]]
        return create_extension(
            card_id=target["card_id"],
            expected_base_revision_id=target["current_revision_id"],
            content=str(core.get("content") or "").strip(),
            keywords=keyword_policy.normalize_csv(
                core.get("keywords") or [],
                title=str(core.get("title") or "").strip(),
                tags=decision["category"],
            ),
            importance=max(
                0.0, min(10.0, float(core["importance"] if core.get("importance") is not None else decision["importance"]))
            ),
            happened_at=core.get("happened_at"),
            origin=origin,
            source=source,
            same_memory_confidence=decision["same_memory_confidence"],
            evidence_source_kind=evidence_source_kind,
            evidence_source_ref=evidence_source_ref,
            evidence_rows=evidence_rows,
            candidate_id=candidate_id,
        )

    embedding = embedding_provider(decision["content"]) if embedding_provider else None
    if action == "new":
        title = str(core.get("title") or decision.get("title") or "").strip()
        keywords = keyword_policy.normalize_csv(
            core.get("keywords") or decision.get("keywords") or [],
            title=title,
            tags=decision["category"],
            limit=4,
        )
        sources = {
            "title": ("agent_explicit" if source == "agent" and core.get("title")
                      else "derived" if title else "pending"),
            "keywords": ("agent_explicit" if source == "agent" and core.get("keywords")
                         else "derived" if keywords else "pending"),
            "importance": "derived",
            "tags": "derived",
        }
        return create_card(
            content=decision["content"],
            title=title,
            tags=decision["category"],
            keywords=keywords,
            importance=(decision["importance"] if core.get("importance") is None
                        else max(float(core["importance"]), decision["importance"])),
            field_sources=sources,
            valence=core.get("valence"),
            arousal=core.get("arousal"),
            happened_at=core.get("happened_at"),
            embedding=embedding,
            embedding_model=embedding_model,
            source=source,
            origin=origin,
            evidence_source_kind=evidence_source_kind,
            evidence_source_ref=evidence_source_ref,
            evidence_rows=evidence_rows,
            candidate_id=candidate_id,
        )

    target = {
        int(item["card_id"]): item for item in frozen_map
    }[decision["target_card_id"]]
    title = target.get("title") or str(core.get("title") or "").strip()
    return update_card(
        card_id=target["card_id"],
        expected_current_revision_id=target["current_revision_id"],
        content=decision["content"],
        title=title,
        tags=target.get("tags") or "",
        keywords=_csv_union(
            target.get("keywords") or "",
            core.get("keywords") or [],
            title=title,
            tags=target.get("tags") or "",
        ),
        importance=max(
            float(target.get("importance") or 0),
            float(core.get("importance") or 0),
            decision["importance"],
        ),
        valence=core.get("valence"),
        arousal=core.get("arousal"),
        embedding=embedding,
        embedding_model=embedding_model,
        change_type=action,
        origin=origin,
        evidence_source_kind=evidence_source_kind,
        evidence_source_ref=evidence_source_ref,
        evidence_rows=evidence_rows,
        candidate_id=candidate_id,
        forbidden_memory_kind="timeline",
    )

def _core(payload):
    core = {
        key: payload.get(key)
        for key in (
            "title", "content", "keywords", "importance", "valence",
            "arousal", "happened_at", "claim_scope",
        )
    }
    # Only Scene-first candidates carry strict-admission metadata. Other
    # writer and direct Agent inputs keep their existing core/hash shape.
    if (
        isinstance(payload.get("scene_admission"), dict)
        and payload.get("memory_type") and payload.get("memory_identity")
    ):
        core["memory_type"] = str(payload.get("memory_type") or "")[:32]
        core["memory_identity"] = str(payload.get("memory_identity") or "")[:120]
        core["admission_reason"] = str(payload.get("admission_reason") or "")[:160]
        verified = payload.get("verified_evidence") or []
        roles = {
            str(item.get("source_kind") or "")
            for item in verified if isinstance(item, dict)
        }
        core["source_roles"] = sorted(
            r for r in roles if r in {"user_text_exact", "assistant_text_exact"}
        )
    return core

def _user_fact_has_exact_evidence(payload):
    if payload.get("claim_scope") != "user_fact":
        return True
    return any(
        item.get("source_kind") == "user_text_exact"
        for item in payload.get("verified_evidence") or []
    )

def _scene_admission_retrieval_hints(payload, memory_db_path):
    """Transfer Scene-retrieved card IDs to the FINAL Gate, not its decision.

    These IDs are discovery hints, not revision authority. We intentionally
    re-freeze current canonical revisions and re-run the normal Gate decision,
    preserving manual/timeline exclusions and commit-time CAS. A card edited
    after Scene preview must not stall an unrelated durable new fact forever.
    """
    scene = payload.get("scene_admission")
    if scene is None:
        return None
    if not isinstance(scene, dict):
        raise InvalidGateDecision("invalid_scene_admission_metadata")
    try:
        revision_no = int(scene.get("scene_revision_no") or 0)
    except (TypeError, ValueError) as exc:
        raise InvalidGateDecision("invalid_scene_admission_metadata") from exc
    if (
        not scene.get("receipt_id")
        or not scene.get("scene_id")
        or not scene.get("source_fingerprint")
        or revision_no <= 0
        or scene.get("gate_action") not in VALID_ACTIONS
    ):
        raise InvalidGateDecision("invalid_scene_admission_metadata")
    refs = scene.get("frozen_memory_refs")
    if not isinstance(refs, list) or len(refs) > 8:
        raise InvalidGateDecision("invalid_scene_frozen_refs")
    ids = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise InvalidGateDecision("invalid_scene_frozen_ref")
        try:
            card_id = int(ref["card_id"])
            revision = int(ref["revision_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidGateDecision("invalid_scene_frozen_ref") from exc
        if card_id <= 0 or revision <= 0 or card_id in ids:
            raise InvalidGateDecision("duplicate_or_invalid_scene_frozen_ref")
        ids.append(card_id)
    # Historical derived index or Scene refs may outlive retired cards.
    # This remains a lookup hint, never permission to revise inactive cards.
    active_ids = []
    with sqlite3.connect(
        f"file:{memory_db_path}?mode=ro", uri=True
    ) as con:
        has_lineage = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='memory_card_lineage'"
        ).fetchone()
        lineage_filter = (
            " AND NOT EXISTS (SELECT 1 FROM memory_card_lineage l "
            "WHERE l.from_card_id=c.id AND l.retired_at IS NULL)"
            if has_lineage else ""
        )
        for card_id in ids:
            active = con.execute(
                "SELECT 1 FROM cards c WHERE c.id=? "
                "AND c.status!='trashed' AND c.memory_kind='episodic'"
                + lineage_filter,
                (card_id,),
            ).fetchone()
            if active:
                active_ids.append(card_id)
    return freeze_candidate_map_from_card_ids(
        active_ids, memory_db_path=memory_db_path
    )

def _scene_gate_input_hash(core, frozen, payload, profile):
    """Hash the evidence provenance of a Scene candidate along with Gate input.

    Legacy candidates retain their existing idempotence contract.
    """
    base = gate_input_hash(core, frozen, profile)
    meta = payload.get("scene_admission")
    if not isinstance(meta, dict):
        return base
    return _stable_hash({
        "base": base,
        "receipt_id": meta.get("receipt_id"),
        "source_fingerprint": meta.get("source_fingerprint"),
        "scene_revision_no": meta.get("scene_revision_no"),
        "verified_evidence": payload.get("verified_evidence"),
    })

def _freeze_failure_hash(core, profile):
    return _stable_hash({
        "candidate_core": core,
        "stage": "freeze_candidate_map",
        "model_profile": _profile_fingerprint(profile),
        "policy_version": GATE_POLICY_VERSION,
    })

def _transient_failure(candidate_id, input_hash, exc, ops_db_path):
    ops_store.record_gate_result(
        candidate_id,
        input_hash,
        f"gatekeeper_unavailable:{type(exc).__name__}:{exc}",
        increment=True,
        db_path=ops_db_path,
    )
    refreshed = ops_store.get_candidate(candidate_id, ops_db_path)
    exhausted = int(refreshed.get("gate_attempts") or 0) >= 3
    return {
        "ok": exhausted,
        "status": "pending",
        "retryable": not exhausted,
        "error": "retry_exhausted" if exhausted else "gatekeeper_unavailable",
    }, 200 if exhausted else 503

def _extension_unavailable_hash(input_hash):
    return _stable_hash({
        "gate_input_hash": input_hash,
        "extension_storage": "unavailable",
    })

def _extension_storage_unavailable(candidate_id, input_hash, ops_db_path):
    unavailable_hash = _extension_unavailable_hash(input_hash)
    ops_store.record_gate_result(
        candidate_id,
        unavailable_hash,
        "extension_storage_unavailable",
        db_path=ops_db_path,
    )
    return {
        "ok": True,
        "status": "pending",
        "retryable": False,
        "action": "extend",
        "error": "extension_storage_unavailable",
    }, 200

def apply_candidate(candidate_id, *, ops_db_path, memory_db_path, raw_db_path, profile,
                    frozen_map=None, provider=None, embedding_provider=None, embedding_model=None):
    memory_path = memory_db_path
    candidate = ops_store.get_candidate(candidate_id, ops_db_path)
    if candidate is None:
        return {"ok": False, "error": "candidate_not_found"}, 404
    if candidate["status"] == "dropped":
        return {
            "ok": True,
            "status": "dropped",
            "card_id": candidate["resulting_card_id"],
            "current_revision_id": candidate["resulting_revision_id"],
            "idempotent": True,
        }, 200

    recovered = revision_store.find_candidate_mutation(memory_path, candidate_id)
    if candidate["status"] == "applied":
        result = {
            "ok": True,
            "status": "applied",
            "card_id": candidate["resulting_card_id"],
            "current_revision_id": candidate["resulting_revision_id"],
            "idempotent": True,
        }
        if recovered and recovered["kind"] == "extension":
            result.update(
                action="extend", extension_id=recovered["extension_id"]
            )
        return result, 200

    if recovered:
        ops_store.mark_candidate_applied(
            candidate_id,
            recovered["card_id"],
            recovered["current_revision_id"],
            ops_db_path,
        )
        result = {
            "ok": True, "status": "applied",
            "action": "extend" if recovered["kind"] == "extension" else "recovered",
            "card_id": recovered["card_id"],
            "current_revision_id": recovered["current_revision_id"],
            "idempotent": True,
        }
        if recovered["kind"] == "extension":
            result["extension_id"] = recovered["extension_id"]
        return result, 200

    try:
        payload = json.loads(candidate["payload_json"])
    except (ValueError, TypeError):
        ops_store.mark_candidate_failed(candidate_id, "invalid_candidate_payload", ops_db_path)
        return {"ok": False, "status": "pending", "retryable": False,
                "error": "invalid_candidate_payload"}, 422
    evidence_error = _candidate_evidence_error(payload, raw_db_path)
    if evidence_error:
        ops_store.mark_candidate_failed(candidate_id, evidence_error, ops_db_path)
        return {"ok": False, "status": "pending", "retryable": False,
                "error": evidence_error}, 422
    if not _user_fact_has_exact_evidence(payload):
        ops_store.update_candidate_payload(
            candidate_id, payload, "user_fact_requires_user_text_exact", ops_db_path
        )
        return {"ok": False, "status": "pending", "retryable": False,
                "error": "user_fact_requires_user_text_exact"}, 422

    core = _core(payload)
    if frozen_map is None:
        try:
            frozen_map = _scene_admission_retrieval_hints(payload, memory_path)
        except InvalidGateDecision as exc:
            # Malformed provenance is not a reason to create a new card.
            ops_store.record_gate_result(
                candidate_id, _freeze_failure_hash(core, profile),
                f"invalid_scene_hints:{type(exc).__name__}", db_path=ops_db_path
            )
            return {"ok": True, "status": "pending", "retryable": False,
                    "error": "invalid_scene_retrieval_hints"}, 200
        except sqlite3.Error as exc:
            return _transient_failure(
                candidate_id, _freeze_failure_hash(core, profile), exc, ops_db_path
            )
    freeze_hash = _freeze_failure_hash(core, profile)
    if (
        candidate.get("last_gate_input_hash") == freeze_hash
        and int(candidate.get("gate_attempts") or 0) >= 3
    ):
        return {"ok": True, "status": "pending", "retryable": False,
                "error": "retry_exhausted"}, 200
    try:
        frozen = (
            list(frozen_map)
            if frozen_map is not None
            else _frozen_map_required(core.get("content") or "")
        )
    except Exception as exc:
        return _transient_failure(
            candidate_id, freeze_hash, exc, ops_db_path
        )
    input_hash = _scene_gate_input_hash(core, frozen, payload, profile)
    extension_available = revision_store.extension_storage_available(memory_path)
    effective_input_hash = (
        input_hash
        if extension_available
        else _extension_unavailable_hash(input_hash)
    )
    if candidate.get("last_gate_input_hash") == effective_input_hash:
        if candidate.get("last_error") == "uncertain":
            return {"ok": True, "status": "pending", "retryable": False,
                    "action": "uncertain"}, 200
        if candidate.get("last_error") == "extension_storage_unavailable":
            return {"ok": True, "status": "pending", "retryable": False,
                    "action": "extend",
                    "error": "extension_storage_unavailable"}, 200
        if int(candidate.get("gate_attempts") or 0) >= 3:
            return {"ok": True, "status": "pending", "retryable": False,
                    "error": "retry_exhausted"}, 200

    try:
        decision, frozen = decide(core, frozen, provider)
    except Exception as exc:
        return _transient_failure(
            candidate_id, input_hash, exc, ops_db_path
        )

    if decision["action"] == "uncertain":
        ops_store.record_gate_result(
            candidate_id, input_hash, "uncertain", db_path=ops_db_path
        )
        return {"ok": True, "status": "pending", "retryable": False,
                "action": "uncertain", "reason": decision["reason"]}, 200
    if decision["action"] == "drop":
        ops_store.mark_candidate_dropped(candidate_id, ops_db_path)
        return {"ok": True, "status": "dropped", "action": "drop",
                "reason": decision["reason"]}, 200
    if decision["action"] == "extend" and not extension_available:
        return _extension_storage_unavailable(
            candidate_id, input_hash, ops_db_path
        )

    try:
        committed = _commit(
            decision, core, frozen, memory_db_path=memory_path,
            candidate_id=candidate_id, source="auto", origin="auto_writer",
            evidence_source_kind=None,
            evidence_source_ref=None,
            evidence_rows=payload["verified_evidence"],
            embedding_provider=embedding_provider, embedding_model=embedding_model,
        )
    except (
        revision_store.InvalidRevisionState,
        revision_store.RevisionConflict,
    ):
        ops_store.record_gate_result(
            candidate_id, input_hash, "stale_revision", db_path=ops_db_path
        )
        return {"ok": False, "status": "pending", "retryable": True,
                "error": "stale_revision"}, 503
    except revision_store.ExtensionStorageUnavailable:
        return _extension_storage_unavailable(
            candidate_id, input_hash, ops_db_path
        )
    except Exception as exc:
        ops_store.mark_candidate_failed(candidate_id, f"commit_failed:{type(exc).__name__}:{exc}", ops_db_path)
        raise

    ops_store.mark_candidate_applied(
        candidate_id, committed["card_id"], committed["current_revision_id"],
        ops_db_path,
    )
    result = {
        "ok": True, "status": "applied", "action": decision["action"],
        "card_id": committed["card_id"],
        "current_revision_id": committed["current_revision_id"],
    }
    if decision["action"] == "extend":
        result["extension_id"] = committed["extension_id"]
    return result, 200

def apply(new_info, title=None, keywords="", *, source,
          frozen_map=None, provider=None, embedding_provider=None,
          memory_db_path, embedding_model=None):
    """Agent 来源协议的显式直写入口；没有默认 Persona。"""
    if source != "agent":
        raise ValueError("use_candidate_apply")
    core = {
        "title": str(title or "").strip(),
        "content": str(new_info or "").strip(),
        "keywords": [item.strip() for item in str(keywords or "").split(",") if item.strip()],
        "importance": None,
        "valence": None,
        "arousal": None,
        "happened_at": None,
        "claim_scope": "assistant_self",
    }
    decision, frozen = decide(core, frozen_map, provider)
    if decision["action"] in {"drop", "uncertain"}:
        return {"ok": True, "status": decision["action"],
                "action": decision["action"], "reason": decision["reason"]}
    evidence_ref = json.dumps(
        {"content_hash": _stable_hash(core["content"]), "source": "agent"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    committed = _commit(
        decision, core, frozen, memory_db_path=memory_db_path,
        candidate_id=None, source="agent", origin="agent_mcp",
        evidence_source_kind="agent_mcp", evidence_source_ref=evidence_ref,
        embedding_provider=embedding_provider, embedding_model=embedding_model,
    )
    result = {
        "ok": True, "status": "applied", "action": decision["action"],
        "card_id": committed["card_id"],
        "current_revision_id": committed["current_revision_id"],
    }
    if decision["action"] == "extend":
        result["extension_id"] = committed["extension_id"]
    return result

def propose_maintenance(card, streams, *, provider=None):
    """Shared Gatekeeper metadata/routing rules; the scheduler has no model policy."""
    author = card["original_author"]
    prompt = {
        "original_author": author,
        "organized_content_narrator": card["organized_content_narrator"],
        "memory": {
            "title": card.get("title") or "",
            "content": card["content"],
            "source": card.get("source") or "",
            "revision_origin": card.get("revision_origin") or "",
            "keywords": card.get("keywords") or "",
            "importance": card.get("importance"),
            "tags": card.get("tags") or "",
        },
        "active_streams": streams if card.get("needs_water", True) else [],
        "fill_fields": card.get("fill_fields", ["title","keywords","importance","tags"]),
        "needs_organized_content": card.get("needs_organized_content", True),
        "needs_water": card.get("needs_water", True),
    }
    raw = (provider or _provider_required)([
        {"role":"system","content":MAINTENANCE_SYSTEM},
        {"role":"user","content":json.dumps(prompt,ensure_ascii=False,sort_keys=True)},
    ])
    try:
        result = raw if isinstance(raw, dict) else json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidGateDecision("invalid_maintenance_json") from exc
    if not isinstance(result, dict):
        raise InvalidGateDecision("maintenance_not_object")
    title = str(result.get("title") or "").strip()[:80]
    keywords = keyword_policy.normalize_csv(
        result.get("keywords") or "", title=title or card.get("title") or "",
        tags=result.get("tags") or card.get("tags") or "", limit=4,
    )
    organized_content = str(result.get("organized_content") or "").strip()
    importance = result.get("importance")
    if importance is not None:
        try:
            importance = float(importance)
        except (TypeError,ValueError) as exc:
            raise InvalidGateDecision("invalid_maintenance_importance") from exc
        if not 1 <= importance <= 10:
            raise InvalidGateDecision("maintenance_importance_out_of_range")
    tag = str(result.get("tags") or "未分类").strip()
    if tag not in PRESET_CATEGORIES:
        tag = "未分类"
    stream_id = str(result.get("water_stream_id") or "").strip() or None
    valid_ids = {str(item["node_id"]) for item in streams}
    if stream_id not in valid_ids:
        stream_id = None
    return {
        "organized_content": organized_content,
        "title": title,
        "keywords": keywords,
        "importance": importance,
        "tags": tag,
        "water_stream_id": stream_id,
        "water_reason": str(result.get("water_reason") or "").strip()[:240],
    }

def maintain(card, streams, *, provider=None):
    """One shared Gatekeeper decision path for nightly metadata + Water routing."""
    return propose_maintenance(card, streams, provider=provider)

def _candidate_evidence_error(payload, raw_db_path):
    if not isinstance(payload, dict):
        return "invalid_candidate_payload"
    rows = payload.get("verified_evidence")
    if not isinstance(rows, list) or not rows:
        return payload.get("evidence_state") or "evidence_required"
    kinds = {"user_text_exact", "assistant_text_exact", "raw_range", "manual",
             "agent_mcp", "bottle_answer", "derived", "legacy_unverified"}
    for row in rows:
        if not isinstance(row, dict) or row.get("source_kind") not in kinds:
            return "evidence_invalid"
        if row["source_kind"] in {"user_text_exact", "assistant_text_exact"}:
            full = {key: row.get(key) for key in (
                "source_kind", "source_ref", "raw_event_start_id", "raw_event_end_id",
                "event_hash", "char_start", "char_end", "quote_hash")}
            resolved = evidence_resolver._resolve_evidence_rows([full], raw_db_path)[0]
            expected_role = "user" if row["source_kind"] == "user_text_exact" else "assistant"
            if not row.get("event_hash") or not resolved["verified"] or resolved["role"] != expected_role:
                return "evidence_invalid:" + resolved["resolution"]
    return None

