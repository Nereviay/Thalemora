"""人工 Water 提案与当前卡版本的只读核对。"""
from __future__ import annotations
import datetime as dt
import json
from typing import Any
from . import verifier as water_verifier
from .review import ReviewError
from .rebuild_manifest import normalize_water_plan

def _decision(entry: dict[str, Any] | None) -> str:
    return str((entry or {}).get("decision") or "pending")

def _pending_ids(
    proposals: list[dict[str, Any]],
    decisions: dict[str, Any],
) -> list[int]:
    return sorted(
        int(item["card_id"])
        for item in proposals
        if _decision(decisions.get(str(item["card_id"]))) == "pending"
    )

def _card_proposal_mutates(proposal: dict[str, Any]) -> bool:
    if proposal.get("proposed_action") != "keep":
        return True
    tag_plan = proposal.get("legacy_category_tag_handling") or {}
    return bool(
        tag_plan.get("proposal") == "remove_legacy_auto"
        and tag_plan.get("detected")
    )

def build_report(review, inspect_current_state, active_water_path) -> dict[str, Any]:
    """Build the current report without mutating any source or database."""
    payload = review.build_payload()
    artifact = payload["artifact"]
    state = payload["review_state"]
    summary = payload["summary"]
    active = payload.get("coarse_water_preview")
    if active is None:
        raise ReviewError("active coarse Water proposal is missing")

    strict = inspect_current_state(
        water_artifact_path=active_water_path
    )
    manifest = strict["manifest"]
    preflight = strict["preflight"]
    decisions = state.get("decisions") or {}
    proposal_by_id = {
        str(item["proposal_id"]): item for item in artifact.get("card_proposals") or []
    }
    deleted_card_ids = {
        int(proposal_by_id[proposal_id]["current_ref"]["card_id"])
        for proposal_id, entry in (decisions.get("card") or {}).items()
        if proposal_id in proposal_by_id and _decision(entry) == "delete"
    }

    card_mutation_pending = sorted(
        int(item["current_ref"]["card_id"])
        for item in artifact.get("card_proposals") or []
        if _card_proposal_mutates(item)
        and _decision(
            (decisions.get("card") or {}).get(str(item["proposal_id"]))
        ) == "pending"
    )
    identity_mutation_pending = sorted(
        str(item["group_id"])
        for item in artifact.get("identity_groups") or []
        if item.get("judgment") in {"same_memory", "uncertain"}
        and _decision(
            (decisions.get("identity") or {}).get(str(item["group_id"]))
        ) == "pending"
    )
    keyword_pending = _pending_ids(
        payload.get("keyword_proposals") or [], decisions.get("keyword") or {}
    )
    keyword_pending = [card_id for card_id in keyword_pending if card_id not in deleted_card_ids]
    cleanup_pending = _pending_ids(
        payload.get("content_cleanup_proposals") or [],
        decisions.get("content_cleanup") or {},
    )
    cleanup_pending = [card_id for card_id in cleanup_pending if card_id not in deleted_card_ids]

    statuses: dict[str, list[int]] = {}
    for item in active.get("memberships") or []:
        status = str(item.get("review_status") or "unknown")
        statuses.setdefault(status, []).append(int(item["card_id"]))
    for values in statuses.values():
        values.sort()

    water_summary = summary["water_proposals"]["active"]
    water_conflicts = list(water_summary.get("decision_conflict_card_ids") or [])
    review_blockers: list[dict[str, Any]] = []

    def block(code: str, **details: Any) -> None:
        review_blockers.append({"code": code, **details})

    if water_summary["water_plan"].get("pending"):
        block("ACTIVE_WATER_PLAN_PENDING", count=1)
    if water_summary["water_node"].get("pending"):
        block(
            "ACTIVE_WATER_NODES_PENDING",
            count=int(water_summary["water_node"]["pending"]),
        )
    if water_summary["membership"].get("pending"):
        block(
            "ACTIVE_WATER_MEMBERSHIPS_PENDING",
            count=int(water_summary["membership"]["pending"]),
        )
    if water_conflicts:
        block(
            "ACTIVE_WATER_MANUAL_DECISION_CONFLICT",
            count=len(water_conflicts),
            card_ids=water_conflicts,
            reason="manual decisions reference legacy Water nodes",
        )
    needs_review = statuses.get("needs_review", [])
    if needs_review:
        block(
            "ACTIVE_WATER_PLACEMENT_NEEDS_REVIEW",
            count=len(needs_review),
            card_ids=needs_review,
        )
    identity_placement = sorted(
        statuses.get("identity_evidence_only", [])
        + statuses.get("identity_merge_pending", [])
    )
    accepted_identity_losers = {
        int((item.get("proposed_lineage") or {})["from_card_id"])
        for item in artifact.get("identity_groups") or []
        if item.get("judgment") == "same_memory"
        and (item.get("proposed_lineage") or {}).get("from_card_id") is not None
        and _decision(
            (decisions.get("identity") or {}).get(str(item["group_id"]))
        ) in {"accept", "edit"}
    }
    active_membership_ids = {
        int(item["card_id"])
        for item in active.get("memberships") or []
        if item.get("card_id") is not None
    }
    deterministic_identity_placements = sorted(
        active_membership_ids & accepted_identity_losers
    )
    unresolved_identity_placements = sorted(
        set(identity_placement) - accepted_identity_losers
    )
    if unresolved_identity_placements:
        block(
            "ACTIVE_WATER_IDENTITY_COLLAPSE_UNRESOLVED",
            count=len(unresolved_identity_placements),
            card_ids=unresolved_identity_placements,
        )
    if card_mutation_pending:
        block(
            "CARD_MUTATIONS_PENDING",
            count=len(card_mutation_pending),
            card_ids=card_mutation_pending,
        )
    if identity_mutation_pending:
        block(
            "IDENTITY_MUTATIONS_PENDING",
            count=len(identity_mutation_pending),
            group_ids=identity_mutation_pending,
        )
    if keyword_pending:
        block(
            "KEYWORD_DECISIONS_PENDING",
            count=len(keyword_pending),
            card_ids=keyword_pending,
        )
    if cleanup_pending:
        block(
            "CONTENT_CLEANUP_DECISIONS_PENDING",
            count=len(cleanup_pending),
            card_ids=cleanup_pending,
        )

    keyword_applied = (
        int(summary["keyword"].get("use_proposed", 0))
        + int(summary["keyword"].get("manual_edit", 0))
    )
    cleanup_applied = (
        int(summary["content_cleanup"].get("use_proposed", 0))
        + int(summary["content_cleanup"].get("manual_edit", 0))
    )
    deterministic_cleanup = int(
        (payload.get("automatic_content_cleanup") or {}).get("count") or 0
    )
    normalized_water = normalize_water_plan(active)
    normalized_water["memberships"] = [
        item
        for item in normalized_water.get("memberships") or []
        if int(item["card_id"]) not in accepted_identity_losers
    ]
    normalized_water["unassigned_card_ids"] = [
        card_id
        for card_id in normalized_water.get("unassigned_card_ids") or []
        if int(card_id) not in accepted_identity_losers
    ]
    proposal_card_ids = {
        int(item["current_ref"]["card_id"])
        for item in artifact.get("card_proposals") or []
    }
    semantic_verification = water_verifier.verify_water_lineage(
        normalized_water,
        manifest["mutations"]["lineage"],
        canonical_card_ids=proposal_card_ids - accepted_identity_losers,
    )

    strict_blockers = [dict(item) for item in preflight.get("stale_reasons") or []]
    if not semantic_verification["ok"]:
        strict_blockers.append(
            {
                "code": "SEMANTIC_VERIFICATION_FAILED",
                "errors": semantic_verification["errors"],
            }
        )
    if not manifest.get("commit_path_available"):
        strict_blockers.append({"code": "R5_COMMIT_PATH_UNAVAILABLE"})

    card_counts = summary["card"]
    identity_counts = summary["identity"]
    return {
        "report_kind": "r5_read_only_preflight",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "status": "R5_BLOCKED_READ_ONLY",
        "r5_cutover_authorized": False,
        "canonical_writes": 0,
        "active_water": {
            "role": "sole_active_proposal",
            **water_summary,
            "unresolved_placement_statuses": {
                key: statuses.get(key, [])
                for key in (
                    "manual_conflict", "needs_review",
                    "identity_evidence_only", "identity_merge_pending",
                )
            },
        },
        "legacy_water": summary["water_proposals"]["legacy_reference"],
        "preserved_manual_decisions": {
            "card_delete": int(card_counts.get("delete", 0)),
            "content_cleanup": cleanup_applied,
            "keyword": keyword_applied,
            "identity_accept": int(identity_counts.get("accept", 0)),
            "water_membership_edit": len(decisions.get("membership") or {}),
            "water_membership_active_compatible": (
                int(water_summary["membership"].get("edit", 0))
            ),
            "water_membership_conflict": int(
                water_summary["membership"].get("conflict", 0)
            ),
        },
        "deterministic_proposals": {
            "legacy_category_tag_removals": sum(
                1
                for item in artifact.get("card_proposals") or []
                if (item.get("legacy_category_tag_handling") or {}).get("proposal")
                == "remove_legacy_auto"
                and (item.get("legacy_category_tag_handling") or {}).get("detected")
                and _decision(
                    (decisions.get("card") or {}).get(str(item["proposal_id"]))
                ) == "pending"
            ),
            "exact_content_suffix_removals": deterministic_cleanup,
            "accepted_identity_placement_collapses": {
                "count": len(deterministic_identity_placements),
                "card_ids": deterministic_identity_placements,
            },
        },
        "review_counts": {
            "cards": card_counts,
            "card_mutation_pending": len(card_mutation_pending),
            "clean_keep_pending_non_mutating": (
                int(card_counts.get("pending", 0)) - len(card_mutation_pending)
            ),
            "identity": identity_counts,
            "identity_mutation_pending": len(identity_mutation_pending),
            "keyword": summary["keyword"],
            "keyword_actionable_pending": len(keyword_pending),
            "content_cleanup": summary["content_cleanup"],
            "content_cleanup_actionable_pending": len(cleanup_pending),
        },
        "compiled_mutation_counts": {
            key: len(value) for key, value in manifest["mutations"].items()
        },
        "supplemental_noops": manifest.get("supplemental_noops") or [],
        "review_blockers": review_blockers,
        "strict_preflight": {
            "status": (
                preflight["status"]
                if semantic_verification["ok"]
                else "SEMANTIC_REVIEW_INVALID"
            ),
            "ok": preflight["ok"] and semantic_verification["ok"],
            "blockers": strict_blockers,
            "checks": preflight["checks"],
            "semantic_candidate": semantic_verification,
        },
        "read_only_proof": {
            "canonical_writes": preflight["canonical_writes"],
            "commit_path_available": preflight["commit_path_available"],
            "inputs_changed_during_preflight": preflight["checks"]
            ["input_hashes"]["changed_during_preflight"],
        },
    }
