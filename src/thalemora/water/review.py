"""Water 提案的人工复核、决定及物化。所有路径和读取权威由调用方显式提供。"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from thalemora.gatekeeper import keywords as keyword_policy
from . import verifier as water_verifier
def _membership_conflicts_with_active_water(
    entry: dict[str, Any], active: dict[str, Any]
) -> bool:
    if str(entry.get("decision") or "pending") != "edit":
        return False
    edited = entry.get("edited") or {}
    lake_key = edited.get("lake_key")
    stream_key = edited.get("stream_key")
    branch_key = edited.get("branch_key")
    draft = edited.get("new_stream")
    if lake_key in (None, "", "SEA"):
        return any(
            value not in (None, "") for value in (stream_key, branch_key)
        ) or bool(draft)
    lakes = {str(item.get("key") or "") for item in active.get("lakes") or []}
    streams = {str(item.get("key") or "") for item in active.get("streams") or []}
    branches = {str(item.get("key") or "") for item in active.get("branches") or []}
    if str(lake_key) not in lakes:
        return True
    if isinstance(draft, dict) and draft:
        return str(draft.get("parent_lake_key") or "") != str(lake_key)
    if str(stream_key or "") not in streams:
        return True
    return branch_key not in (None, "") and str(branch_key) not in branches

DECISIONS = {"pending", "accept", "reject", "edit", "defer", "delete"}

KEYWORD_DECISIONS = {"pending", "keep_current", "use_proposed", "manual_edit"}

CONTENT_CLEANUP_DECISIONS = {"pending", "keep_current", "use_proposed", "manual_edit"}

DETERMINISTIC_CLEANUP_FLAG = "automatic_deterministic_editorial_suffix_cleanup"

KINDS = {
    "card", "identity", "water_node", "membership", "water_plan", "keyword",
    "content_cleanup",
}

_ISSUE_REASON_ZH = {
    "empty_keywords": "当前没有关键词，需要补充稳定的检索词",
    "generic_alias": "包含过于宽泛的词，检索区分度不足",
    "date_only_alias": "仅日期的词缺少可检索的具体含义",
    "tag_duplicate": "关键词与现有标签重复",
    "phrase_like_alias": "关键词像完整句子，不适合作为稳定检索词",
    "punctuation_split": "一段文字被标点拆成了多个句子片段",
    "sentence_fragment": "包含不完整的句子片段",
    "sentence_prefix_fragment": "包含带句首连接词的句子片段",
    "sentence_marker_fragment": "包含陈述式句子片段",
    "sentence_embedded_time_fragment": "句子片段里混入了时间信息",
    "malformed_numeric_pair": "时间或数字被错误拆分",
    "cleanup_would_empty_keywords": "清理问题词后将没有可用关键词",
    "duplicate_alias": "存在重复关键词",
    "no_alnum_alias": "关键词没有可检索文字",
    "numeric_fragment": "包含缺少语义的数字片段",
    "overly_long_phrase": "关键词过长，像摘要而不是检索词",
    "punctuation_fragment": "关键词包含句子标点",
    "stray_latin_fragment": "包含零散的拉丁字母片段",
    "title_duplicate": "关键词与标题重复",
    "too_many_aliases": "关键词数量过多",
    "too_short_alias": "关键词过短，检索区分度不足",
}

class ReviewError(ValueError):
    pass

def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")

def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def _keyword_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [
        item.strip()
        for item in re.split(r"[,，、;；\n|｜]+", str(value or ""))
        if item.strip()
    ]

def _validate_keyword_selection(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ReviewError("keywords must be an array")
    if any(not isinstance(item, str) for item in value):
        raise ReviewError("every keyword must be a string")
    keywords = [str(item).strip() for item in value]
    if not 1 <= len(keywords) <= keyword_policy.MAX_KEYWORDS:
        raise ReviewError("keywords must contain 1-6 items")
    if any(not item for item in keywords):
        raise ReviewError("keywords must not be empty")
    if len({item.casefold() for item in keywords}) != len(keywords):
        raise ReviewError("keywords must be unique")
    normalized = keyword_policy.normalize_keywords(keywords)
    if normalized != keywords:
        raise ReviewError("keywords do not pass keyword quality policy")
    return keywords

def _snapshot_for_edit(proposal: dict[str, Any]) -> dict[str, Any]:
    existing = proposal.get("proposed_snapshot")
    if isinstance(existing, dict):
        return copy.deepcopy(existing)
    current = proposal.get("current_snapshot") or {}
    return {
        "title": str(current.get("title") or ""),
        "content": str(current.get("content") or ""),
        "keywords": _keyword_list(current.get("keywords")),
        "importance": current.get("importance"),
        "valence": current.get("valence"),
        "arousal": current.get("arousal"),
        "happened_at": current.get("happened_at"),
    }

def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(rendered, encoding="utf-8")
    os.replace(tmp, path)

def _empty_state(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_id": artifact["artifact_id"],
        "source_stage": artifact["stage"],
        "created_at": _now(),
        "updated_at": _now(),
        "decisions": {
            "card": {},
            "identity": {},
            "water_node": {},
            "membership": {},
            "water_plan": {},
            "keyword": {},
            "content_cleanup": {},
        },
        "history": [],
    }

def _sanitize_edited(kind: str, edited: Any) -> dict[str, Any] | None:
    if edited is None:
        return None
    if not isinstance(edited, dict):
        raise ReviewError("edited must be an object")
    # R4 is proposal metadata only. Keep the edited payload bounded and JSON-only.
    rendered = json.dumps(edited, ensure_ascii=False)
    if len(rendered) > 30000:
        raise ReviewError("edited payload too large")
    if kind == "keyword":
        allowed = {"keywords"}
    elif kind == "content_cleanup":
        allowed = {"content"}
    elif kind == "card":
        allowed = {
            "title", "content", "keywords", "importance", "valence", "arousal",
            "happened_at", "water_placement",
        }
    elif kind == "identity":
        allowed = {"judgment", "preferred_survivor_card_id", "notes"}
    elif kind == "water_node":
        allowed = {"title", "description", "parent_key", "notes"}
    elif kind == "membership":
        allowed = {"lake_key", "stream_key", "branch_key", "new_stream", "notes"}
    else:
        allowed = {"notes"}
    return {k: copy.deepcopy(v) for k, v in edited.items() if k in allowed}

def _draft_stream_key(lake_key: str, title: str) -> str:
    normalized = re.sub(r"\s+", "", str(title or "").strip()).casefold()
    digest = hashlib.sha256(
        f"{lake_key}\n{normalized}".encode("utf-8")
    ).hexdigest()[:12]
    return f"draft-stream-{lake_key}-{digest}"


def validate_artifact_schema(artifact, schema_path):
    try:
        import jsonschema
    except ImportError:
        return {"available": False, "ok": None}
    try:
        jsonschema.validate(artifact, _load_json(schema_path))
    except (jsonschema.exceptions.ValidationError, jsonschema.exceptions.SchemaError, OSError, ValueError) as exc:
        return {"available": True, "ok": False, "error": str(exc)}
    return {"available": True, "ok": True}


class WaterReview:
    def __init__(self, *, r3_artifact, state_path, r4_artifact, keyword_audit_artifact, semantic_keyword_artifact, content_cleanup_artifact, coarse_water_path, read_current_cards, validate_schema):
        self.r3_artifact = r3_artifact
        self.state_path = state_path
        self.r4_artifact = r4_artifact
        self.keyword_audit_artifact = keyword_audit_artifact
        self.semantic_keyword_artifact = semantic_keyword_artifact
        self.content_cleanup_artifact = content_cleanup_artifact
        self.coarse_water_path = coarse_water_path
        self.read_current_cards = read_current_cards
        self.validate_schema = validate_schema

    def load_coarse_water_preview(self) -> dict[str, Any] | None:
        if not self.coarse_water_path.exists():
            return None
        raw = _load_json(self.coarse_water_path)
        skeleton = raw.get("skeleton") or {}
        placements = raw.get("placements") or []
        lakes = []
        streams = []
        for lake in skeleton.get("lakes") or []:
            lk = str(lake.get("key") or "")
            lake_members = [p for p in placements if p.get("new_lake") == lk]
            lakes.append({
                "key": lk,
                "title": lake.get("title") or lk,
                "description": lake.get("purpose") or lake.get("description") or "",
                "size": len(lake_members),
                "needs_review": any(p.get("status") in {"needs_review", "manual_conflict"} for p in lake_members),
            })
            for stream in lake.get("streams") or []:
                sk = str(stream.get("key") or "")
                members = [p for p in placements if p.get("new_stream") == sk]
                streams.append({
                    "key": sk,
                    "parent_lake_key": lk,
                    "title": stream.get("title") or sk,
                    "description": stream.get("purpose") or stream.get("description") or "",
                    "size": len(members),
                    "needs_review": any(p.get("status") in {"needs_review", "manual_conflict"} for p in members),
                })
        memberships = [{
            "card_id": int(p["card_id"]),
            "lake_key": p.get("new_lake"),
            "stream_key": p.get("new_stream"),
            "branch_key": None,
            "confidence": None,
            "review_status": p.get("status"),
            "review_reason": p.get("reason"),
            "manual_conflict": p.get("status") == "manual_conflict",
        } for p in placements]
        unassigned = sorted(
            int(p["card_id"]) for p in placements
            if not p.get("new_lake") or not p.get("new_stream")
        )
        return {
            "kind": "r4_coarse_preview",
            "proposal_role": "active",
            "proposal_label": f"{len(lakes)} Lakes / {len(streams)} Streams",
            "canonical_writes": 0,
            "lakes": lakes,
            "streams": streams,
            "branches": [],
            "memberships": memberships,
            "unassigned_card_ids": unassigned,
            "branch_policy": raw.get("branch_policy") or {},
            "summary": raw.get("summary") or {},
        }

    def _water_index(self,
        artifact: dict[str, Any],
        coarse: dict[str, Any] | None = None,
    ) -> tuple[dict[str, set[str]], str]:
        """Return the sole active Water index and its source role.

        Once the coarse proposal exists, the R3 Water plan remains available only
        as historical comparison data. Review state is interpreted at read time;
        this function never migrates or rewrites decisions.
        """
        active = coarse or self.load_coarse_water_preview()
        if active is not None:
            node_ids = {
                str(item.get("key") or "")
                for key in ("lakes", "streams", "branches")
                for item in active.get(key) or []
                if str(item.get("key") or "")
            }
            membership_ids = {
                str(item["card_id"])
                for item in active.get("memberships") or []
                if item.get("card_id") is not None
            }
            return {
                "water_node": node_ids,
                "membership": membership_ids,
                "water_plan": {"water_plan"},
            }, "active_coarse"

        plan = artifact.get("water_plan") or {}
        node_ids = {
            str(item.get("key") or "")
            for key in ("lakes", "streams", "branches")
            for item in plan.get(key) or []
            if str(item.get("key") or "")
        }
        membership_ids = {
            str(item["card_id"])
            for item in plan.get("memberships") or []
            if item.get("card_id") is not None
        }
        return {
            "water_node": node_ids,
            "membership": membership_ids,
            "water_plan": {"water_plan"},
        }, "legacy_only"

    def load_keyword_proposals(self,
        artifact: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Load the completed keyword work as revision-bound R4 proposals."""
        artifact = artifact or self.load_artifact()
        audit = _load_json(self.keyword_audit_artifact)
        semantic = _load_json(self.semantic_keyword_artifact)
        if (
            audit.get("stage") != "R1_KEYWORD_QUALITY_AUDIT"
            or audit.get("proposal_only") is not True
            or audit.get("production_canonical_writes") != 0
            or audit.get("all_inputs_stable") is not True
        ):
            raise ReviewError("keyword audit artifact is not a stable proposal-only input")
        if (
            semantic.get("stage") != "R1_SEMANTIC_KEYWORD_PROPOSAL"
            or semantic.get("proposal_only") is not True
            or semantic.get("automatic_apply_allowed") is not False
            or semantic.get("production_canonical_writes") != 0
            or semantic.get("all_inputs_stable") is not True
        ):
            raise ReviewError("semantic keyword artifact is not a stable proposal-only input")

        r3_cards = {
            int(item["current_ref"]["card_id"]): item
            for item in artifact.get("card_proposals") or []
        }
        proposals: list[dict[str, Any]] = []
        for item in audit.get("card_audits") or []:
            if item.get("proposal_status") != "deterministic_fix":
                continue
            proposals.append({
                "proposal_kind": "deterministic_fix",
                "source_artifact_id": audit.get("artifact_id"),
                "card_id": int(item["card_id"]),
                "based_on_revision_id": int(item["based_on_revision_id"]),
                "current_snapshot": {
                    "title": item.get("current_title") or "",
                    "keywords": item.get("current_keywords") or "",
                },
                "proposed_keywords": _validate_keyword_selection(item.get("proposed_keywords")),
                "issue_codes": list(item.get("issue_codes") or []),
                "manual_authority_protected": False,
            })
        for item in semantic.get("proposals") or []:
            proposals.append({
                "proposal_kind": "semantic_proposal",
                "source_artifact_id": semantic.get("artifact_id"),
                "card_id": int(item["card_id"]),
                "based_on_revision_id": int(item["based_on_revision_id"]),
                "current_snapshot": copy.deepcopy(item.get("current_snapshot") or {}),
                "proposed_keywords": _validate_keyword_selection(item.get("proposed_keywords")),
                "issue_codes": list(item.get("source_issue_codes") or []),
                "manual_authority_protected": bool(item.get("manual_authority_protected")),
            })

        seen: set[int] = set()
        for proposal in proposals:
            card_id = proposal["card_id"]
            revision_id = proposal["based_on_revision_id"]
            if card_id in seen:
                raise ReviewError(f"duplicate keyword proposal for card {card_id}")
            seen.add(card_id)
            proposal["proposal_id"] = f"keyword-card-{card_id}-rev-{revision_id}"
            proposal["current_keywords"] = _keyword_list(
                proposal["current_snapshot"].get("keywords")
            )
            proposal["issue_reason_zh"] = "；".join(
                _ISSUE_REASON_ZH.get(code, f"关键词质量问题：{code}")
                for code in proposal["issue_codes"]
            )
            r3_card = r3_cards.get(card_id)
            proposal["r4_card_proposal_id"] = (
                str(r3_card.get("proposal_id")) if r3_card else None
            )
            if r3_card:
                proposal["manual_authority_protected"] = bool(
                    proposal["manual_authority_protected"]
                    or (r3_card.get("protection") or {}).get("user_explicit_authority")
                )
            proposal["stale"] = bool(
                r3_card
                and (
                    int(r3_card["current_ref"]["based_on_revision_id"]) != revision_id
                    or _keyword_list((r3_card.get("current_snapshot") or {}).get("keywords"))
                    != proposal["current_keywords"]
                )
            )
        return sorted(proposals, key=lambda item: item["card_id"])

    def _live_card_content_refs(self) -> dict[int, tuple[int, str]]:
        """Read live revision/content identities without opening a write path."""

        refs: dict[int, tuple[int, str]] = {}
        with self.read_current_cards() as conn:
            for row in conn.execute(
                """
                SELECT id,current_revision_id,content
                FROM cards
                WHERE memory_kind='episodic' AND status!='trashed'
                """
            ):
                refs[int(row["id"])] = (
                    int(row["current_revision_id"]),
                    hashlib.sha256(str(row["content"] or "").encode("utf-8")).hexdigest(),
                )
        return refs

    def _load_all_content_cleanup_proposals(self,
        artifact: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Load every cleanup candidate and bind it to live content fail-closed."""
        artifact = artifact or self.load_artifact()
        source = _load_json(self.content_cleanup_artifact)
        if (
            source.get("stage") != "R1_CONTENT_CLEANUP_AUDIT"
            or source.get("proposal_only") is not True
            or source.get("automatic_apply_allowed") is not False
            or source.get("production_canonical_writes") != 0
        ):
            raise ReviewError("content cleanup artifact is not proposal-only")
        r3_cards = {
            int(item["current_ref"]["card_id"]): item
            for item in artifact.get("card_proposals") or []
        }
        live = self._live_card_content_refs()
        proposals = []
        for raw in source.get("proposals") or []:
            item = copy.deepcopy(raw)
            if not source.get("artifact_id"):
                raise ReviewError("content cleanup artifact_id is required")
            item["source_artifact_id"] = source["artifact_id"]
            card_id = int(item["card_id"])
            r3 = r3_cards.get(card_id)
            revision_id = int(item["based_on_revision_id"])
            current_snapshot = (
                (r3.get("current_snapshot") or {})
                if r3 is not None
                else (item.get("current_snapshot") or {})
            )
            current_content = str(current_snapshot.get("content") or "")
            current_hash = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
            live_ref = live.get(card_id)
            item["stale"] = bool(
                (
                    r3 is not None
                    and int(r3["current_ref"]["based_on_revision_id"]) != revision_id
                )
                or current_hash != item.get("current_content_sha256")
                or live_ref != (revision_id, item.get("current_content_sha256"))
            )
            item["r4_card_proposal_id"] = str(r3["proposal_id"]) if r3 else None
            proposals.append(item)
        if len({int(item["card_id"]) for item in proposals}) != len(proposals):
            raise ReviewError("duplicate content cleanup proposal")
        return sorted(proposals, key=lambda item: int(item["card_id"]))

    def load_content_cleanup_proposals(self,
        artifact: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Load only ambiguous cleanups which still require an R4 decision."""
        return [
            item
            for item in self._load_all_content_cleanup_proposals(artifact)
            if item.get("cleanup_method") == "semantic_reviewed_rewrite"
        ]

    def project_deterministic_content_cleanups(self,
        artifact: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Project exact suffix removals into an R4 copy without writing state."""
        artifact = artifact or self.load_artifact()
        projected = copy.deepcopy(artifact)
        cards = {
            int(item["current_ref"]["card_id"]): item
            for item in projected.get("card_proposals") or []
        }
        applied = []
        for cleanup in self._load_all_content_cleanup_proposals(artifact):
            if cleanup.get("cleanup_method") != "deterministic_editorial_suffix":
                continue
            card_id = int(cleanup["card_id"])
            proposal = cards.get(card_id)
            if proposal is None:
                raise ReviewError(
                    f"deterministic content cleanup has no R4 card projection: {card_id}"
                )
            if cleanup.get("stale"):
                raise ReviewError(f"stale deterministic content cleanup: {card_id}")
            current_content = str(
                (proposal.get("current_snapshot") or {}).get("content") or ""
            )
            details = cleanup.get("deterministic_cleanup") or {}
            removed = str(details.get("removed_editorial_text") or "")
            proposed_content = str(cleanup.get("proposed_content") or "")
            if (
                details.get("historical_text_preservation") != "byte_exact_prefix"
                or not proposed_content
                or current_content != proposed_content + removed
            ):
                raise ReviewError(
                    f"deterministic content cleanup is not a byte-exact suffix: {card_id}"
                )
            snapshot = _snapshot_for_edit(proposal)
            existing_content = str(snapshot.get("content") or "")
            if existing_content not in {current_content, proposed_content}:
                raise ReviewError(
                    f"deterministic cleanup conflicts with existing proposed content: {card_id}"
                )
            snapshot["content"] = proposed_content
            proposal["proposed_snapshot"] = snapshot
            proposal["proposed_action"] = "edit"
            flags = list(proposal.get("audit_flags") or [])
            if DETERMINISTIC_CLEANUP_FLAG not in flags:
                flags.append(DETERMINISTIC_CLEANUP_FLAG)
            proposal["audit_flags"] = flags
            applied.append(cleanup)
        return projected, applied

    def load_artifact(self) -> dict[str, Any]:
        artifact = _load_json(self.r3_artifact)
        if artifact.get("stage") not in {"R3_WATER_PLAN", "R4_REVIEW"}:
            raise ReviewError("review source artifact is not an R3/R4 proposal")
        return artifact

    def _index(self, artifact: dict[str, Any]) -> dict[str, set[str]]:
        cards = {
            str(p["proposal_id"])
            for p in artifact.get("card_proposals") or []
        }
        identities = {
            str(g["group_id"])
            for g in artifact.get("identity_groups") or []
        }
        water_index, _ = self._water_index(artifact)
        keywords = {
            str(proposal["card_id"])
            for proposal in self.load_keyword_proposals(artifact)
            if not proposal.get("stale")
        }
        content_cleanups = {
            str(proposal["card_id"])
            for proposal in self.load_content_cleanup_proposals(artifact)
            if not proposal.get("stale")
        }
        return {
            "card": cards,
            "identity": identities,
            "water_node": water_index["water_node"],
            "membership": water_index["membership"],
            "water_plan": water_index["water_plan"],
            "keyword": keywords,
            "content_cleanup": content_cleanups,
        }

    def load_state(self, artifact: dict[str, Any] | None = None) -> dict[str, Any]:
        artifact = artifact or self.load_artifact()
        if not self.state_path.exists():
            return _empty_state(artifact)
        try:
            state = _load_json(self.state_path)
        except Exception:
            return _empty_state(artifact)
        if state.get("artifact_id") != artifact.get("artifact_id"):
            # Never silently carry review decisions to a regenerated proposal.
            return _empty_state(artifact)
        decisions = state.get("decisions")
        if not isinstance(decisions, dict):
            return _empty_state(artifact)
        for kind in KINDS:
            decisions.setdefault(kind, {})
        state.setdefault("history", [])
        return state

    def save_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        artifact = self.load_artifact()
        state = self.load_state(artifact)
        if str(payload.get("artifact_id") or "") != str(artifact["artifact_id"]):
            raise ReviewError("stale review artifact")
        kind = str(payload.get("kind") or "")
        if kind not in KINDS:
            raise ReviewError("invalid review kind")
        item_id = str(payload.get("id") or "")
        if item_id not in self._index(artifact)[kind]:
            raise ReviewError("review item not found")
        decision = str(payload.get("decision") or "")
        if kind == "keyword":
            allowed_decisions = KEYWORD_DECISIONS
        elif kind == "content_cleanup":
            allowed_decisions = CONTENT_CLEANUP_DECISIONS
        else:
            allowed_decisions = DECISIONS
        if decision not in allowed_decisions:
            raise ReviewError("invalid review decision")
        if decision == "delete" and kind != "card":
            raise ReviewError("delete decision is only valid for Memory cards")
        notes = str(payload.get("notes") or "").strip()
        if len(notes) > 5000:
            raise ReviewError("review notes too long")
        edited = _sanitize_edited(kind, payload.get("edited"))
        if decision in {"edit", "manual_edit"} and not edited:
            raise ReviewError("edit decision requires edited payload")

        keyword_proposal = None
        if kind == "keyword":
            keyword_proposal = next(
                (
                    item for item in self.load_keyword_proposals(artifact)
                    if str(item["card_id"]) == item_id
                ),
                None,
            )
            if keyword_proposal is None:
                raise ReviewError("keyword proposal not found")
            if keyword_proposal.get("stale"):
                raise ReviewError("stale keyword proposal")
            if str(payload.get("proposal_id") or "") != keyword_proposal["proposal_id"]:
                raise ReviewError("stale keyword proposal")
            try:
                requested_revision = int(payload.get("based_on_revision_id"))
            except (TypeError, ValueError):
                raise ReviewError("keyword decision requires based_on_revision_id")
            if requested_revision != keyword_proposal["based_on_revision_id"]:
                raise ReviewError("stale keyword proposal revision")
            if decision == "manual_edit":
                edited = {"keywords": _validate_keyword_selection(edited.get("keywords"))}
            elif edited is not None:
                raise ReviewError("only manual keyword edits may include edited keywords")

        cleanup_proposal = None
        if kind == "content_cleanup":
            cleanup_proposal = next(
                (
                    item for item in self.load_content_cleanup_proposals(artifact)
                    if str(item["card_id"]) == item_id
                ),
                None,
            )
            if cleanup_proposal is None:
                raise ReviewError("content cleanup proposal not found")
            if cleanup_proposal.get("stale"):
                raise ReviewError("stale content cleanup proposal")
            if str(payload.get("proposal_id") or "") != cleanup_proposal["proposal_id"]:
                raise ReviewError("stale content cleanup proposal")
            try:
                requested_revision = int(payload.get("based_on_revision_id"))
            except (TypeError, ValueError):
                raise ReviewError("content cleanup decision requires based_on_revision_id")
            if requested_revision != cleanup_proposal["based_on_revision_id"]:
                raise ReviewError("stale content cleanup proposal revision")
            if str(payload.get("current_content_sha256") or "") != cleanup_proposal["current_content_sha256"]:
                raise ReviewError("stale content cleanup content hash")
            if decision == "manual_edit":
                content = str((edited or {}).get("content") or "").strip()
                if not content:
                    raise ReviewError("manual content cleanup must not be empty")
                edited = {"content": content}
            elif edited is not None:
                raise ReviewError("only manual content cleanup may include edited content")

        if kind == "identity" and decision == "edit":
            group = next(
                (
                    item
                    for item in artifact.get("identity_groups") or []
                    if str(item.get("group_id") or "") == item_id
                ),
                None,
            )
            if group is None:
                raise ReviewError("identity group not found")
            judgment = edited.get("judgment")
            if judgment is not None and judgment not in water_verifier.IDENTITY_JUDGMENTS:
                raise ReviewError("invalid identity judgment")
            survivor = edited.get("preferred_survivor_card_id")
            if survivor is not None:
                try:
                    survivor_id = int(survivor)
                except (TypeError, ValueError):
                    raise ReviewError("preferred survivor must be a card id")
                members = {int(member) for member in group.get("member_card_ids") or []}
                if survivor_id not in members:
                    raise ReviewError("preferred survivor must belong to the identity group")

        if kind == "membership" and decision == "edit":
            plan = artifact.get("water_plan") or {}
            coarse = self.load_coarse_water_preview() or {}
            lakes = {str(x.get("key") or ""): x for x in (plan.get("lakes") or []) + (coarse.get("lakes") or [])}
            streams = {str(x.get("key") or ""): x for x in (plan.get("streams") or []) + (coarse.get("streams") or [])}
            branches = {str(x.get("key") or ""): x for x in (plan.get("branches") or []) + (coarse.get("branches") or [])}
            lake_key = edited.get("lake_key")
            stream_key = edited.get("stream_key")
            branch_key = edited.get("branch_key")
            new_stream = edited.get("new_stream")
            if lake_key in {"", "SEA"}:
                lake_key = None
            if stream_key == "":
                stream_key = None
            if branch_key == "":
                branch_key = None
            if new_stream is not None and not isinstance(new_stream, dict):
                raise ReviewError("new_stream must be an object")
            if lake_key is None:
                if stream_key is not None or branch_key is not None or new_stream:
                    raise ReviewError("Sea membership cannot keep stream/branch")
                new_stream = None
            else:
                if lake_key not in lakes:
                    raise ReviewError("unknown lake")
                if new_stream:
                    title = str(new_stream.get("title") or "").strip()
                    description = str(new_stream.get("description") or "").strip()
                    if not title:
                        raise ReviewError("new Stream title is required")
                    if len(title) > 40:
                        raise ReviewError("new Stream title is too long")
                    if len(description) > 500:
                        raise ReviewError("new Stream description is too long")
                    normalized_title = water_verifier.title_key(title)
                    sibling_titles = {
                        water_verifier.title_key(node.get("title")): str(node.get("key") or "")
                        for node in (plan.get("streams") or []) + (coarse.get("streams") or [])
                        if str(node.get("parent_lake_key") or "") == str(lake_key)
                        and node.get("retired_at") is None
                        and node.get("status") != "retired"
                    }
                    if normalized_title in sibling_titles:
                        raise ReviewError(
                            "lake already has an active Stream with the same title: "
                            + sibling_titles[normalized_title]
                        )
                    stream_key = _draft_stream_key(str(lake_key), title)
                    branch_key = None
                    new_stream = {
                        "key": stream_key,
                        "parent_lake_key": str(lake_key),
                        "title": title,
                        "description": description,
                        "status": "r4_draft",
                    }
                else:
                    if stream_key is None:
                        raise ReviewError("Memory cannot be placed directly in a Lake; choose or create a Stream, or use Sea")
                    stream = streams.get(str(stream_key))
                    if stream is None:
                        known_draft = None
                        for membership_entry in (
                            state.get("decisions", {}).get("membership", {}).values()
                        ):
                            draft = (membership_entry.get("edited") or {}).get("new_stream")
                            if (
                                isinstance(draft, dict)
                                and str(draft.get("key") or "") == str(stream_key)
                            ):
                                known_draft = draft
                                break
                        if (
                            known_draft is None
                            or str(known_draft.get("parent_lake_key") or "")
                            != str(lake_key)
                        ):
                            raise ReviewError("stream does not belong to lake")
                    elif str(stream.get("parent_lake_key") or "") != str(lake_key):
                        raise ReviewError("stream does not belong to lake")
                    if branch_key is not None:
                        branch = branches.get(str(branch_key))
                        if branch is None or str(branch.get("parent_stream_key") or "") != str(stream_key or ""):
                            raise ReviewError("branch does not belong to stream")
            edited["lake_key"] = lake_key
            edited["stream_key"] = stream_key
            edited["branch_key"] = branch_key
            edited["new_stream"] = new_stream

        previous = copy.deepcopy(state["decisions"][kind].get(item_id))
        entry = {
            "decision": decision,
            "notes": notes,
            "edited": edited,
            "reviewed_at": _now(),
        }
        if keyword_proposal is not None:
            entry.update({
                "proposal_id": keyword_proposal["proposal_id"],
                "source_artifact_id": keyword_proposal["source_artifact_id"],
                "based_on_revision_id": keyword_proposal["based_on_revision_id"],
            })
        if cleanup_proposal is not None:
            entry.update({
                "proposal_id": cleanup_proposal["proposal_id"],
                "source_artifact_id": cleanup_proposal["source_artifact_id"],
                "based_on_revision_id": cleanup_proposal["based_on_revision_id"],
                "current_content_sha256": cleanup_proposal["current_content_sha256"],
            })
        state["decisions"][kind][item_id] = entry
        state["updated_at"] = _now()
        state["history"].append({
            "at": state["updated_at"],
            "kind": kind,
            "id": item_id,
            "previous": previous,
            "next": entry,
        })
        state["history"] = state["history"][-2000:]
        _atomic_json(self.state_path, state)
        return self.review_summary(artifact, state)

    def safe_bulk_accept(self, payload: dict[str, Any]) -> dict[str, Any]:
        artifact = self.load_artifact()
        state = self.load_state(artifact)
        if str(payload.get("artifact_id") or "") != str(artifact["artifact_id"]):
            raise ReviewError("stale review artifact")
        scope = str(payload.get("scope") or "clean_keep")

        accepted = []
        if scope == "clean_keep":
            for proposal in artifact.get("card_proposals") or []:
                if proposal.get("proposed_action") != "keep":
                    continue
                proposal_id = str(proposal["proposal_id"])
                current = state["decisions"]["card"].get(proposal_id)
                if current and current.get("decision") != "pending":
                    continue
                state["decisions"]["card"][proposal_id] = {
                    "decision": "accept",
                    "notes": "bulk_safe_accept_clean_keep",
                    "edited": None,
                    "reviewed_at": _now(),
                }
                accepted.append(proposal_id)
        elif scope == "safe_water_nodes":
            plan = self.load_coarse_water_preview() or (artifact.get("water_plan") or {})
            for key in ("lakes", "streams", "branches"):
                for node in plan.get(key) or []:
                    if bool(node.get("needs_review")):
                        continue
                    node_id = str(node.get("key") or "")
                    if not node_id:
                        continue
                    current = state["decisions"]["water_node"].get(node_id)
                    if current and current.get("decision") != "pending":
                        continue
                    state["decisions"]["water_node"][node_id] = {
                        "decision": "accept",
                        "notes": "bulk_safe_accept_nonflagged_water_node",
                        "edited": None,
                        "reviewed_at": _now(),
                    }
                    accepted.append(node_id)
        else:
            raise ReviewError("unsupported bulk scope")

        state["updated_at"] = _now()
        state["history"].append({
            "at": state["updated_at"],
            "kind": "bulk",
            "id": scope,
            "previous": None,
            "next": {"accepted_count": len(accepted)},
        })
        state["history"] = state["history"][-2000:]
        _atomic_json(self.state_path, state)
        result = self.review_summary(artifact, state)
        result["bulk_accepted_count"] = len(accepted)
        return result

    def review_summary(self,
        artifact: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact = artifact or self.load_artifact()
        state = state or self.load_state(artifact)
        index = self._index(artifact)
        coarse = self.load_coarse_water_preview()
        keyword_by_id = {
            str(item["card_id"]): item for item in self.load_keyword_proposals(artifact)
        }
        cleanup_by_id = {
            str(item["card_id"]): item for item in self.load_content_cleanup_proposals(artifact)
        }
        summary: dict[str, Any] = {}
        effective_decisions: dict[str, dict[str, str]] = {}
        for kind in KINDS:
            if kind == "keyword":
                valid_decisions = KEYWORD_DECISIONS
            elif kind == "content_cleanup":
                valid_decisions = CONTENT_CLEANUP_DECISIONS
            else:
                valid_decisions = DECISIONS
            counts = {key: 0 for key in valid_decisions}
            decisions = state["decisions"].get(kind) or {}
            effective_decisions[kind] = {}
            for item_id in index[kind]:
                entry = decisions.get(item_id) or {"decision": "pending"}
                decision = str(entry.get("decision") or "pending")
                if (
                    kind == "membership"
                    and coarse is not None
                    and _membership_conflicts_with_active_water(entry, coarse)
                ):
                    decision = "conflict"
                if kind == "keyword" and decision != "pending":
                    proposal = keyword_by_id.get(item_id)
                    if (
                        proposal is None
                        or entry.get("proposal_id") != proposal["proposal_id"]
                        or entry.get("based_on_revision_id")
                        != proposal["based_on_revision_id"]
                    ):
                        decision = "pending"
                if kind == "content_cleanup" and decision != "pending":
                    proposal = cleanup_by_id.get(item_id)
                    if (
                        proposal is None
                        or proposal.get("stale")
                        or entry.get("proposal_id") != proposal["proposal_id"]
                        or entry.get("based_on_revision_id") != proposal["based_on_revision_id"]
                        or entry.get("current_content_sha256")
                        != proposal["current_content_sha256"]
                    ):
                        decision = "pending"
                if decision == "conflict":
                    counts["conflict"] = counts.get("conflict", 0) + 1
                    effective_decisions[kind][item_id] = decision
                    continue
                if decision not in counts:
                    decision = "pending"
                counts[decision] += 1
                effective_decisions[kind][item_id] = decision
            summary[kind] = {
                "total": len(index[kind]),
                **counts,
            }

        legacy_plan = artifact.get("water_plan") or {}
        legacy_index = {
            "water_node": {
                str(item.get("key") or "")
                for key in ("lakes", "streams", "branches")
                for item in legacy_plan.get(key) or []
                if str(item.get("key") or "")
            },
            "membership": {
                str(item["card_id"])
                for item in legacy_plan.get("memberships") or []
                if item.get("card_id") is not None
            },
            "water_plan": {"water_plan"},
        }
        legacy_summary: dict[str, Any] = {}
        for kind in ("water_plan", "water_node", "membership"):
            counts = {key: 0 for key in DECISIONS}
            for item_id in legacy_index[kind]:
                decision = str(
                    ((state["decisions"].get(kind) or {}).get(item_id) or {}).get(
                        "decision"
                    )
                    or "pending"
                )
                counts[decision if decision in counts else "pending"] += 1
            legacy_summary[kind] = {"total": len(legacy_index[kind]), **counts}

        active_water = {
            "role": "active",
            "source": "coarse_5_lakes_18_streams" if coarse is not None else "legacy_only",
            "excluded_from_gates": False,
            "water_plan": copy.deepcopy(summary["water_plan"]),
            "water_node": copy.deepcopy(summary["water_node"]),
            "membership": copy.deepcopy(summary["membership"]),
        }
        if coarse is not None:
            active_water["shape"] = {
                "lakes": len(coarse.get("lakes") or []),
                "streams": len(coarse.get("streams") or []),
                "branches": len(coarse.get("branches") or []),
                "memberships": len(coarse.get("memberships") or []),
            }
            active_water["placement_status_counts"] = copy.deepcopy(
                (coarse.get("summary") or {}).get("status_counts") or {}
            )
            active_water["decision_conflict_card_ids"] = sorted(
                int(item_id)
                for item_id in index["membership"]
                if _membership_conflicts_with_active_water(
                    (state["decisions"].get("membership") or {}).get(item_id) or {},
                    coarse,
                )
            )
        summary["water_proposals"] = {
            "active": active_water,
            "legacy_reference": {
                "role": "historical_reference",
                "source": "r3_water_plan",
                "excluded_from_pending": coarse is not None,
                "excluded_from_gates": coarse is not None,
                **legacy_summary,
            },
        }

        card_proposals = artifact.get("card_proposals") or []
        active_plan = coarse or legacy_plan
        unresolved_card_ids = {
            int(proposal["current_ref"]["card_id"])
            for proposal in card_proposals
            if effective_decisions["card"].get(str(proposal["proposal_id"]), "pending")
            in {"pending", "defer"}
            and (
                proposal.get("proposed_action") != "keep"
                or "content_change_requires_human_review"
                in (proposal.get("reasons") or [])
                or bool(
                    (proposal.get("protection") or {}).get("user_explicit_authority")
                )
                or bool((proposal.get("protection") or {}).get("requires_user_review"))
                or not (proposal.get("water_placement") or {}).get("lake")
                or float(
                    (proposal.get("water_placement") or {}).get("confidence") or 0
                )
                < 0.75
            )
        }
        unresolved_keyword_ids = {
            int(item_id)
            for item_id, decision in effective_decisions["keyword"].items()
            if decision == "pending"
        }
        unresolved_cleanup_ids = {
            int(item_id)
            for item_id, decision in effective_decisions["content_cleanup"].items()
            if decision == "pending"
        }
        unresolved_attention_ids = (
            unresolved_card_ids | unresolved_keyword_ids | unresolved_cleanup_ids
        )
        summary["attention"] = {
            "unresolved_item_count": len(unresolved_attention_ids),
            "unresolved_card_ids": sorted(unresolved_attention_ids),
            "unresolved_substantive_card_decisions": len(unresolved_card_ids),
            "unresolved_keyword_decisions": len(unresolved_keyword_ids),
            "unresolved_content_cleanup_decisions": len(unresolved_cleanup_ids),
            "card_edits": sum(p.get("proposed_action") == "edit" for p in card_proposals),
            "card_content_changes": sum(
                "content_change_requires_human_review" in (p.get("reasons") or [])
                for p in card_proposals
            ),
            "user_explicit_card_edits": sum(
                p.get("proposed_action") in {"edit", "split"}
                and bool((p.get("protection") or {}).get("user_explicit_authority"))
                for p in card_proposals
            ),
            "same_memory_merges": sum(
                g.get("judgment") == "same_memory"
                for g in artifact.get("identity_groups") or []
            ),
            "water_nodes_needing_review": sum(
                bool(n.get("needs_review"))
                for key in ("streams", "branches")
                for n in active_plan.get(key, [])
            ),
            "sea_cards": len(active_plan.get("unassigned_card_ids") or []),
            "delete_requests": sum(
                (state["decisions"].get("card", {}).get(str(p["proposal_id"])) or {}).get("decision") == "delete"
                for p in card_proposals
            ),
            "low_confidence_memberships": (
                0
                if coarse is not None
                else sum(
                    float(m.get("confidence") or 0) < 0.75
                    for m in active_plan.get("memberships") or []
                )
            ),
            "active_water_review_placements": sum(
                str(m.get("review_status") or "")
                in {
                    "candidate", "manual_conflict", "needs_review",
                    "identity_evidence_only", "identity_merge_pending",
                }
                for m in active_plan.get("memberships") or []
            ),
            "automatic_deterministic_content_cleanups": sum(
                DETERMINISTIC_CLEANUP_FLAG in (p.get("audit_flags") or [])
                for p in card_proposals
            ),
        }
        summary["artifact_id"] = artifact["artifact_id"]
        summary["state_updated_at"] = state.get("updated_at")
        return summary

    def build_payload(self) -> dict[str, Any]:
        source_artifact = self.load_artifact()
        state = self.load_state(source_artifact)
        artifact, deterministic_cleanups = self.project_deterministic_content_cleanups(
            source_artifact
        )
        keyword_proposals = self.load_keyword_proposals(source_artifact)
        return {
            "ok": True,
            "mode": "r4_review_only",
            "canonical_memory_writes": 0,
            "artifact": artifact,
            "coarse_water_preview": self.load_coarse_water_preview(),
            "keyword_proposals": keyword_proposals,
            "content_cleanup_proposals": self.load_content_cleanup_proposals(source_artifact),
            "automatic_content_cleanup": {
                "cleanup_method": "deterministic_editorial_suffix",
                "count": len(deterministic_cleanups),
                "card_ids": [int(item["card_id"]) for item in deterministic_cleanups],
                "review_state_writes": 0,
            },
            "review_state": state,
            "summary": self.review_summary(artifact, state),
        }

    def materialize_reviewed_artifact(self) -> dict[str, Any]:
        source_artifact = self.load_artifact()
        artifact, deterministic_cleanups = self.project_deterministic_content_cleanups(
            source_artifact
        )
        state = self.load_state(source_artifact)
        artifact["stage"] = "R4_REVIEW"
        artifact["r4_review_state"] = copy.deepcopy(state)
        artifact["supplemental_content_cleanup"] = {
            "source_artifact": str(self.content_cleanup_artifact),
            "automatic_projection": {
                "cleanup_method": "deterministic_editorial_suffix",
                "count": len(deterministic_cleanups),
                "card_ids": [int(item["card_id"]) for item in deterministic_cleanups],
                "review_state_writes": 0,
            },
            "decisions": copy.deepcopy(state["decisions"].get("content_cleanup") or {}),
        }
        artifact["r4_review_summary"] = self.review_summary(artifact, state)
        artifact["r4_materialized_at"] = _now()

        card_decisions = state["decisions"].get("card") or {}
        for proposal in artifact.get("card_proposals") or []:
            proposal_id = str(proposal["proposal_id"])
            entry = card_decisions.get(proposal_id) or {
                "decision": "pending", "notes": "", "edited": None, "reviewed_at": None
            }
            proposal["review"] = copy.deepcopy(entry)

        identity_decisions = state["decisions"].get("identity") or {}
        for group in artifact.get("identity_groups") or []:
            group_id = str(group["group_id"])
            group["review"] = copy.deepcopy(
                identity_decisions.get(group_id)
                or {"decision": "pending", "notes": "", "edited": None, "reviewed_at": None}
            )

        # Never inherit the R3 validation receipt: R4 has just changed the artifact.
        # Both checks are pure/read-only and run before the output path is replaced.
        artifact["schema_validation"] = self.validate_schema(artifact)
        if artifact["schema_validation"].get("ok") is not True:
            raise ReviewError(
                "materialized R4 artifact failed schema validation: "
                + str(artifact["schema_validation"].get("error") or "validator unavailable")
            )
        artifact["semantic_validation"] = water_verifier.verify_artifact(artifact)
        if artifact["semantic_validation"].get("ok") is not True:
            first = (artifact["semantic_validation"].get("errors") or [{}])[0]
            raise ReviewError(
                "materialized R4 artifact failed semantic validation: "
                + str(first.get("message") or "unknown semantic error")
            )

        _atomic_json(self.r4_artifact, artifact)
        return {
            "ok": True,
            "path": str(self.r4_artifact),
            "summary": artifact["r4_review_summary"],
            "schema_validation": artifact["schema_validation"],
            "semantic_validation": artifact["semantic_validation"],
        }
