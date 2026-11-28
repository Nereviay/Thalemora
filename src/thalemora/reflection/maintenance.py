from __future__ import annotations


from copy import deepcopy


import hashlib


import json


import os


import sqlite3


import uuid


from typing import Any, Iterable, Mapping, Sequence
from thalemora.scene import strict
from thalemora.scene.admission import SCENE_ADMISSION_SYSTEM_PROMPT
SCHEMA_VERSION = 1


ACTIONS = {
    "leave_unchanged",
    "keep_scene_only",
    "promote_memory",
    "revise_memory",
    "merge_memories",
    "demote_to_scene",
    "link_only",
}


STRUCTURAL_ACTIONS = {
    "promote_memory",
    "revise_memory",
    "merge_memories",
    "demote_to_scene",
    "link_only",
}


FACTUAL_CANDIDATE_ACTIONS = {
    "promote_memory",
    "revise_memory",
    "merge_memories",
}


REFLECTIVE_MEMORY_SYSTEM_TASK = """根据已有真实 Agent 上下文、Scene 与逐字证据进行内部记忆反思维护。只输出 JSON 对象。
action 只能为 leave_unchanged、keep_scene_only、promote_memory、revise_memory、merge_memories、demote_to_scene、link_only。
返回 target_memory_ids、relation、reason、reflection、memory_candidates。修改事实须有逐字来源及对应旧修订；解释与事实分开。
promote/revise/merge 需要一条合格的 memory 候选。revise 指向单卡，merge 需要至少两张。其他动作候选为空。
reflection 可含 why_it_matters_to_me、future_relevance、ambient_affinity(0..1)、salience_vote。建议不改变正式重要度。
不确定时保留旧记忆。人工记忆及原文受保护。不得冒充过去或真实的 Agent 行动。"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS reflective_memory_schema (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    schema_version INTEGER NOT NULL,
    authority_note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reflective_memory_receipts (
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    scene_revision_no INTEGER NOT NULL,
    context_sha256 TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    action TEXT NOT NULL,
    target_memory_ids_json TEXT NOT NULL,
    target_revision_refs_json TEXT NOT NULL DEFAULT '[]',
    relation TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    reflection_json TEXT NOT NULL,
    normalized_candidates_json TEXT NOT NULL,
    route_validity_json TEXT NOT NULL,
    contract_hash TEXT NOT NULL,
    model_output_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, scene_id, scene_revision_no, evidence_fingerprint)
);

CREATE TABLE IF NOT EXISTS reflective_memory_signals (
    receipt_id TEXT NOT NULL,
    memory_id INTEGER,
    scene_id TEXT NOT NULL,
    ambient_affinity REAL NOT NULL,
    salience_vote REAL NOT NULL,
    why_it_matters TEXT NOT NULL,
    future_relevance TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(receipt_id) REFERENCES reflective_memory_receipts(receipt_id)
);

CREATE TABLE IF NOT EXISTS reflective_memory_apply_outcomes (
    receipt_id TEXT PRIMARY KEY,
    outcome_json TEXT NOT NULL,
    FOREIGN KEY(receipt_id) REFERENCES reflective_memory_receipts(receipt_id)
);
"""


class ReflectiveMaintenanceError(RuntimeError):
    pass


def _stable_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _parse_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str):
        raise ReflectiveMaintenanceError(
            "reflection output must be mapping or JSON string"
        )
    text = raw.strip()
    if text.startswith(chr(96) * 3):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReflectiveMaintenanceError("reflection output is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ReflectiveMaintenanceError("reflection output must be one JSON object")
    return value


def _bounded_number(value: Any, *, low: float, high: float, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ReflectiveMaintenanceError(f"{field} must be numeric") from exc
    if number < low or number > high:
        raise ReflectiveMaintenanceError(
            f"{field} must be within [{low}, {high}]"
        )
    return number


def _normalize_target_ids(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReflectiveMaintenanceError("target_memory_ids must be a list")
    result: list[int] = []
    for item in value:
        try:
            memory_id = int(item)
        except (TypeError, ValueError) as exc:
            raise ReflectiveMaintenanceError(
                "target_memory_ids must contain integers"
            ) from exc
        if memory_id <= 0:
            raise ReflectiveMaintenanceError("target memory ids must be > 0")
        if memory_id not in result:
            result.append(memory_id)
    return result


def _normalize_revision_refs(values: Sequence[Mapping[str, Any]] | None) -> list[dict[str, int]]:
    result: list[dict[str, int]] = []
    seen: set[int] = set()
    for raw in values or ():
        if not isinstance(raw, Mapping):
            raise ReflectiveMaintenanceError("target revision refs must be objects")
        try:
            card_id = int(raw.get("card_id") or raw.get("id"))
            revision_id = int(raw.get("revision_id") or raw.get("current_revision_id"))
        except (TypeError, ValueError) as exc:
            raise ReflectiveMaintenanceError("target revision refs require integer ids") from exc
        if card_id <= 0 or revision_id <= 0:
            raise ReflectiveMaintenanceError("target revision refs require positive ids")
        if card_id in seen:
            raise ReflectiveMaintenanceError("duplicate target revision ref")
        seen.add(card_id)
        result.append({"card_id": card_id, "revision_id": revision_id})
    result.sort(key=lambda item: item["card_id"])
    return result


def normalize_evidence_events(events: Sequence[Mapping[str, Any]]) -> list[dict]:
    normalized = []
    seen = set()
    for event in events:
        try:
            event_id = int(event.get("event_id"))
        except (TypeError, ValueError) as exc:
            raise ReflectiveMaintenanceError("event_id must be an integer") from exc
        if event_id <= 0 or event_id in seen:
            raise ReflectiveMaintenanceError(
                "evidence event ids must be unique positive integers"
            )
        seen.add(event_id)
        role = str(event.get("role") or "").strip()
        if role not in {"user","assistant"}:
            raise ReflectiveMaintenanceError(f"unsupported evidence role: {role}")
        normalized.append({
            "event_id": event_id,
            "role": role,
            "beijing_time": str(event.get("beijing_time") or ""),
            "quotable_text": str(event.get("quotable_text") or ""),
        })
    if not normalized:
        raise ReflectiveMaintenanceError("evidence_events required")
    return normalized


def build_reflective_messages(
    normal_provider_messages: Sequence[Mapping[str, Any]],
    *,
    scene: Mapping[str, Any],
    existing_memories: Sequence[Mapping[str, Any]],
    evidence_events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Preserve normal identity context and append one internal maintenance task.

    This mirrors Self Model's independent maintenance surface: no synthetic
    user instruction replaces the normal context, and no visible reply is asked
    for. The normal prefix is copied value-identically.
    """
    if not normal_provider_messages:
        raise ReflectiveMaintenanceError("normal provider context required")
    scene_id = str(scene.get("scene_id") or "").strip()
    if not scene_id:
        raise ReflectiveMaintenanceError("scene_id required")
    events = normalize_evidence_events(evidence_events)
    memories = []
    for item in existing_memories:
        if not isinstance(item, Mapping):
            continue
        try:
            memory_id = int(item.get("id") or item.get("memory_id"))
        except (TypeError, ValueError):
            continue
        if memory_id <= 0:
            continue
        memories.append({
            "id": memory_id,
            "title": str(item.get("title") or ""),
            "content": str(item.get("content") or item.get("text") or ""),
            "status": str(item.get("status") or ""),
            "importance": item.get("importance"),
            "revision_id": item.get("revision_id"),
            "provenance": item.get("provenance") or {},
        })

    bundle = {
        "scene": {
            "scene_id": scene_id,
            "revision_no": int(scene.get("revision_no") or scene.get("current_revision_no") or 1),
            "state": str(scene.get("state") or "finalized"),
            "title_hint": str(scene.get("title_hint") or ""),
            "topic_hint": str(scene.get("topic_hint") or ""),
        },
        "existing_memories": memories[:12],
        "evidence_events": events,
    }
    messages = deepcopy([dict(item) for item in normal_provider_messages])
    messages.append({
        "role": "system",
        "content": (
            REFLECTIVE_MEMORY_SYSTEM_TASK
            + "\n\n【待维护证据包】\n"
            + _stable_json(bundle)
        ),
    })
    return messages


def validate_reflection(
    evidence_events: Sequence[Mapping[str, Any]],
    raw_output: Any,
    *,
    available_memory_ids: Iterable[int] = (),
) -> dict[str, Any]:
    events = normalize_evidence_events(evidence_events)
    parsed = _parse_object(raw_output)
    action = str(parsed.get("action") or "").strip()
    if action not in ACTIONS:
        raise ReflectiveMaintenanceError(f"invalid action: {action}")

    target_ids = _normalize_target_ids(parsed.get("target_memory_ids"))
    available = {int(value) for value in available_memory_ids}
    unknown = [value for value in target_ids if value not in available]
    if unknown:
        raise ReflectiveMaintenanceError(
            f"target Memory not present in maintenance bundle: {unknown}"
        )

    relation = str(parsed.get("relation") or "").strip()
    reason = str(parsed.get("reason") or "").strip()
    if not reason:
        raise ReflectiveMaintenanceError("reason required")

    reflection = parsed.get("reflection")
    if reflection is None:
        reflection = {}
    if not isinstance(reflection, Mapping):
        raise ReflectiveMaintenanceError("reflection must be an object")
    normalized_reflection = {
        "why_it_matters_to_me": str(
            reflection.get("why_it_matters_to_me") or ""
        ).strip(),
        "future_relevance": str(
            reflection.get("future_relevance") or ""
        ).strip(),
        "ambient_affinity": _bounded_number(
            reflection.get("ambient_affinity", 0.5),
            low=0.0, high=1.0, field="ambient_affinity",
        ),
        "salience_vote": _bounded_number(
            reflection.get("salience_vote", 0.0),
            low=-1.0, high=1.0, field="salience_vote",
        ),
    }

    raw_candidates = parsed.get("memory_candidates")
    if raw_candidates is None:
        raw_candidates = []
    if not isinstance(raw_candidates, list):
        raise ReflectiveMaintenanceError("memory_candidates must be a list")

    candidates = strict.normalize_routes(raw_candidates)
    route_validity, metrics = strict.evaluate_routes(candidates, events)
    invalid = [item for item in route_validity if not item.get("valid")]

    if action in {
        "leave_unchanged",
        "keep_scene_only",
        "demote_to_scene",
        "link_only",
    } and candidates:
        raise ReflectiveMaintenanceError(
            f"{action} must not emit factual Memory candidates"
        )

    if action == "promote_memory":
        if target_ids:
            raise ReflectiveMaintenanceError(
                "promote_memory must not target an existing Memory"
            )
        if len(candidates) != 1 or candidates[0].get("route") != "memory":
            raise ReflectiveMaintenanceError(
                "promote_memory requires exactly one route=memory candidate"
            )

    if action == "revise_memory":
        if len(target_ids) != 1:
            raise ReflectiveMaintenanceError(
                "revise_memory requires exactly one target Memory"
            )
        if len(candidates) != 1 or candidates[0].get("route") != "memory":
            raise ReflectiveMaintenanceError(
                "revise_memory requires exactly one route=memory candidate"
            )

    if action == "merge_memories":
        if len(target_ids) < 2:
            raise ReflectiveMaintenanceError(
                "merge_memories requires at least two target Memories"
            )
        if len(candidates) != 1 or candidates[0].get("route") != "memory":
            raise ReflectiveMaintenanceError(
                "merge_memories requires one evidence-grounded replacement candidate"
            )

    if action == "demote_to_scene" and len(target_ids) != 1:
        raise ReflectiveMaintenanceError(
            "demote_to_scene requires exactly one target Memory"
        )

    if action == "link_only":
        if not target_ids:
            raise ReflectiveMaintenanceError(
                "link_only requires at least one target Memory"
            )
        if not relation:
            raise ReflectiveMaintenanceError("link_only requires relation")

    if invalid:
        raise ReflectiveMaintenanceError(
            "strict-admission factual validation failed: " + _stable_json(invalid)
        )

    return {
        "action": action,
        "target_memory_ids": target_ids,
        "relation": relation,
        "reason": reason,
        "reflection": normalized_reflection,
        "candidates": candidates,
        "route_validity": route_validity,
        "metrics": metrics,
        "model_output": parsed,
        "contract_hash": _sha({
            "reflective_task": REFLECTIVE_MEMORY_SYSTEM_TASK,
            "strict_prompt": SCENE_ADMISSION_SYSTEM_PROMPT,
            "schema_version": SCHEMA_VERSION,
        }),
    }


def plan_reflective_maintenance(
    *,
    scene_finalized: bool,
    admission_disposition: str,
    overlap_memory_ids: Sequence[int] = (),
    contradiction: bool = False,
    correction: bool = False,
    repeated_reactivation_count: int = 0,
    user_marked_important: bool = False,
    regular_runs_today: int = 0,
    max_regular_runs_per_day: int = 2,
) -> dict[str, Any]:
    """Cheap trigger gate; reflection is intentionally low-frequency.

    Personality is asked to arbitrate meaning only when there is something
    genuinely ambiguous/important to maintain, not for every Scene.
    """
    if not scene_finalized:
        return {
            "should_run": False,
            "urgent": False,
            "reasons": ["scene_not_finalized"],
        }

    reasons: list[str] = []
    urgent = bool(contradiction or correction)
    if contradiction:
        reasons.append("contradiction")
    if correction:
        reasons.append("correction")

    overlap_ids = []
    for value in overlap_memory_ids:
        try:
            memory_id = int(value)
        except (TypeError, ValueError):
            continue
        if memory_id > 0 and memory_id not in overlap_ids:
            overlap_ids.append(memory_id)

    if admission_disposition == "promote_memory" and overlap_ids:
        reasons.append("new_vs_update_ambiguity")
    if len(overlap_ids) >= 2:
        reasons.append("merge_or_distinct_ambiguity")
    if int(repeated_reactivation_count or 0) >= 3:
        reasons.append("repeated_reactivation")
    if user_marked_important:
        reasons.append("user_marked_important")

    if not reasons:
        return {
            "should_run": False,
            "urgent": False,
            "reasons": ["no_reflective_maintenance_signal"],
        }

    if not urgent and int(regular_runs_today or 0) >= int(max_regular_runs_per_day):
        return {
            "should_run": False,
            "urgent": False,
            "reasons": reasons + ["daily_regular_budget"],
        }

    return {
        "should_run": True,
        "urgent": urgent,
        "reasons": reasons,
        "overlap_memory_ids": overlap_ids,
    }


class ReflectiveMaintenanceShadowStore:
    def __init__(self, db_path: str, *, receipt_roles=None, role_aliases=None):
        self.receipt_roles = receipt_roles or {}
        self.role_aliases = role_aliases or {}
        self.db_path = os.path.abspath(os.path.expanduser(db_path))
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize(self):
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            receipt_columns = {
                str(item["name"])
                for item in conn.execute("PRAGMA table_info(reflective_memory_receipts)")
            }
            if "target_revision_refs_json" not in receipt_columns:
                conn.execute(
                    "ALTER TABLE reflective_memory_receipts "
                    "ADD COLUMN target_revision_refs_json TEXT NOT NULL DEFAULT '[]'"
                )
            row = conn.execute(
                "SELECT schema_version FROM reflective_memory_schema WHERE singleton=1"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO reflective_memory_schema("
                    "singleton,schema_version,authority_note) VALUES(1,?,?)",
                    (
                        SCHEMA_VERSION,
                        "shadow proposals only; personality has no raw/canonical mutation authority",
                    ),
                )
            elif int(row["schema_version"]) != SCHEMA_VERSION:
                raise ReflectiveMaintenanceError(
                    "reflective maintenance schema mismatch"
                )

    def record(
        self,
        *,
        run_id: str,
        scene_id: str,
        scene_revision_no: int,
        normal_provider_messages: Sequence[Mapping[str, Any]],
        evidence_events: Sequence[Mapping[str, Any]],
        raw_output: Any,
        available_memory_ids: Iterable[int] = (),
        target_revision_refs: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        result = validate_reflection(
            evidence_events,
            raw_output,
            available_memory_ids=available_memory_ids,
        )
        context_sha = _sha([dict(item) for item in normal_provider_messages])
        identity = normalize_evidence_events(evidence_events)
        for row, event in zip(identity, evidence_events):
            original = event.get("receipt_role")
            if original is not None and self.role_aliases.get(original) == row["role"]:
                row["role"] = original
            else:
                row["role"] = self.receipt_roles.get(row["role"], row["role"])
        evidence_fp = _sha(identity)
        revision_refs = _normalize_revision_refs(target_revision_refs)
        revision_refs_json = _stable_json(revision_refs)
        receipt_id = "memory-reflection-" + uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM reflective_memory_receipts WHERE run_id=? ORDER BY rowid LIMIT 1",
                (str(run_id),),
            ).fetchone()
            if existing:
                if (
                    existing["scene_id"] != str(scene_id)
                    or existing["scene_revision_no"] != int(scene_revision_no)
                    or existing["evidence_fingerprint"] != evidence_fp
                    or existing["model_output_json"] != _stable_json(result["model_output"])
                    or existing["target_revision_refs_json"] != revision_refs_json
                ):
                    raise ReflectiveMaintenanceError("reflective retry payload mismatch")
                return self._receipt_result(existing)
            conn.execute(
                "INSERT INTO reflective_memory_receipts("
                "receipt_id,run_id,scene_id,scene_revision_no,context_sha256,"
                "evidence_fingerprint,action,target_memory_ids_json,target_revision_refs_json,relation,reason,"
                "reflection_json,normalized_candidates_json,route_validity_json,"
                "contract_hash,model_output_json"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    str(run_id),
                    str(scene_id),
                    int(scene_revision_no),
                    context_sha,
                    evidence_fp,
                    result["action"],
                    _stable_json(result["target_memory_ids"]),
                    revision_refs_json,
                    result["relation"],
                    result["reason"],
                    _stable_json(result["reflection"]),
                    _stable_json(result["candidates"]),
                    _stable_json(result["route_validity"]),
                    result["contract_hash"],
                    _stable_json(result["model_output"]),
                ),
            )
            targets = result["target_memory_ids"] or [None]
            for memory_id in targets:
                conn.execute(
                    "INSERT INTO reflective_memory_signals("
                    "receipt_id,memory_id,scene_id,ambient_affinity,salience_vote,"
                    "why_it_matters,future_relevance"
                    ") VALUES(?,?,?,?,?,?,?)",
                    (
                        receipt_id,
                        memory_id,
                        str(scene_id),
                        result["reflection"]["ambient_affinity"],
                        result["reflection"]["salience_vote"],
                        result["reflection"]["why_it_matters_to_me"],
                        result["reflection"]["future_relevance"],
                    ),
                )
            stored = conn.execute(
                "SELECT * FROM reflective_memory_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
        return self._receipt_result(stored)

    @staticmethod
    def _receipt_result(receipt: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "receipt_id": receipt["receipt_id"],
            "action": receipt["action"],
            "target_memory_ids": json.loads(receipt["target_memory_ids_json"]),
            "target_revision_refs": json.loads(receipt["target_revision_refs_json"]),
            "candidate_count": len(json.loads(receipt["normalized_candidates_json"])),
            "reflection": json.loads(receipt["reflection_json"]),
            "contract_hash": receipt["contract_hash"],
            "mutated_canonical_memory": False,
            "mutated_raw_events": False,
        }


    def latest_affinity_by_memory(self) -> dict[int, float]:
        """Return the latest explicit suitability for unrelated Ambient surfacing.

        Missing keys mean "not assessed yet", not neutral or approved.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.* FROM reflective_memory_signals s "
                "JOIN reflective_memory_receipts r ON r.receipt_id=s.receipt_id "
                "WHERE s.memory_id IS NOT NULL "
                "ORDER BY r.created_at DESC, s.rowid DESC"
            ).fetchall()
        result: dict[int, float] = {}
        for row in rows:
            memory_id = int(row["memory_id"])
            if memory_id in result:
                continue
            result[memory_id] = max(
                0.0, min(1.0, float(row["ambient_affinity"]))
            )
        return result

    def latest_bias_by_memory(self) -> dict[int, float]:
        """Return a small bounded Ambient bias from the latest signal per Memory.

        Mapping range is [-0.10, +0.10]. It is intentionally much smaller than
        status/importance gates and cannot resurrect excluded candidates.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.* FROM reflective_memory_signals s "
                "JOIN reflective_memory_receipts r ON r.receipt_id=s.receipt_id "
                "WHERE s.memory_id IS NOT NULL "
                "ORDER BY r.created_at DESC, s.rowid DESC"
            ).fetchall()
        result: dict[int, float] = {}
        for row in rows:
            memory_id = int(row["memory_id"])
            if memory_id in result:
                continue
            affinity = float(row["ambient_affinity"])
            salience = float(row["salience_vote"])
            # Center affinity around 0.5, then blend a tiny salience vote.
            bias = ((affinity - 0.5) * 0.12) + (salience * 0.04)
            result[memory_id] = max(-0.10, min(0.10, bias))
        return result
