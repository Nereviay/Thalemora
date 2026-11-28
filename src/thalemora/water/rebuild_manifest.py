"""Water 人工复核清单的编译与只读核对。"""
from __future__ import annotations
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping
COMPILER_VERSION = "r5-dryrun-2"

SCHEMA_VERSION = 2

INPUT_HASH_KEYS = {
    "memory": "memory_sha256",
    "raw": "raw_sha256",
    "retrieval": "retrieval_sha256",
    "scene": "scene_sha256",
}

REVIEW_DECISIONS = {"pending", "accept", "reject", "edit", "defer", "delete"}

class CompileError(ValueError):
    """The supplied review inputs cannot be compiled safely."""

def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical JSON representation used for all R5 IDs."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompileError(f"cannot load {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise CompileError(f"{label} must be a JSON object")
    return value

def _operation(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    body = {"kind": kind, **dict(payload)}
    return {
        "operation_id": f"r5op:{canonical_sha256(body)[:24]}",
        **body,
    }

def _review_entry(
    decisions: Mapping[str, Any], kind: str, item_id: str
) -> dict[str, Any]:
    entry = (decisions.get(kind) or {}).get(item_id) or {}
    decision = str(entry.get("decision") or "pending")
    if decision not in REVIEW_DECISIONS:
        raise CompileError(f"invalid {kind} decision for {item_id}: {decision}")
    return dict(entry)

def _compile_card_operations(
    r3: Mapping[str, Any], state: Mapping[str, Any]
) -> list[dict[str, Any]]:
    decisions = state.get("decisions") or {}
    operations: list[dict[str, Any]] = []
    proposals = sorted(
        r3.get("card_proposals") or [],
        key=lambda item: (int(item["current_ref"]["card_id"]), str(item["proposal_id"])),
    )
    for proposal in proposals:
        proposal_id = str(proposal["proposal_id"])
        tag_plan = proposal.get("legacy_category_tag_handling") or {}
        if (
            tag_plan.get("proposal") == "remove_legacy_auto"
            and proposal.get("manual_authority_protected")
        ):
            raise CompileError(
                f"automatic legacy tag removal targets manual authority: {proposal_id}"
            )
        review = _review_entry(decisions, "card", proposal_id)
        decision = str(review.get("decision") or "pending")
        ref = proposal["current_ref"]
        common = {
            "proposal_id": proposal_id,
            "card_id": int(ref["card_id"]),
            "based_on_revision_id": int(ref["based_on_revision_id"]),
        }
        if decision == "delete":
            operations.append(_operation("card_status_trashed", common))
            continue
        if decision == "edit":
            edited = review.get("edited")
            if not isinstance(edited, dict) or not edited:
                raise CompileError(f"card edit has no edited payload: {proposal_id}")
            operations.append(
                _operation(
                    "card_revision",
                    {**common, "origin": "manual_ui", "snapshot": edited},
                )
            )
            continue
        if decision == "pending":
            if (
                str(proposal.get("proposed_action") or "keep") == "keep"
                and tag_plan.get("proposal") == "remove_legacy_auto"
            ):
                tags = sorted({str(tag) for tag in tag_plan.get("detected") or []})
                if tags:
                    operations.append(
                        _operation(
                            "legacy_tag_remove",
                            {
                                **common,
                                "tags": tags,
                                "authority": "deterministic_legacy_category_migration",
                            },
                        )
                    )
            continue
        if decision != "accept":
            continue

        action = str(proposal.get("proposed_action") or "keep")
        if action == "edit":
            snapshot = proposal.get("proposed_snapshot")
            if not isinstance(snapshot, dict) or not snapshot:
                raise CompileError(f"accepted edit has no proposed snapshot: {proposal_id}")
            operations.append(
                _operation(
                    "card_revision",
                    {**common, "origin": "manual_ui", "snapshot": snapshot},
                )
            )
        elif action == "keep":
            tag_plan = proposal.get("legacy_category_tag_handling") or {}
            # "review" means preserve manual/manual_ui authority.  The explicit
            # card accept authorizes only an already-proposed automatic removal.
            if tag_plan.get("proposal") == "remove_legacy_auto":
                tags = sorted({str(tag) for tag in tag_plan.get("detected") or []})
                if tags:
                    operations.append(
                        _operation(
                            "legacy_tag_remove",
                            {**common, "tags": tags, "authority": "user_review"},
                        )
                    )
        else:
            raise CompileError(f"unsupported card action {action!r}: {proposal_id}")
    return operations

def _compile_supplemental_operations(
    r3: Mapping[str, Any],
    state: Mapping[str, Any],
    keyword_audit: Mapping[str, Any] | None,
    semantic_keywords: Mapping[str, Any] | None,
    content_cleanup: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compile reviewed patches and exact deterministic suffix removals.

    A card-level delete is the user's dominant decision. Supplemental choices
    for that card become explicit no-ops instead of new review requirements.
    """
    proposals = {
        int(item["current_ref"]["card_id"]): item
        for item in r3.get("card_proposals") or []
    }
    decisions = state.get("decisions") or {}
    deleted_cards = {
        int(item["current_ref"]["card_id"])
        for item in proposals.values()
        if _review_entry(decisions, "card", str(item["proposal_id"])).get("decision")
        == "delete"
    }
    operations: list[dict[str, Any]] = []
    noops: list[dict[str, Any]] = []

    keyword_proposals: dict[int, dict[str, Any]] = {}
    if keyword_audit is not None:
        for item in keyword_audit.get("card_audits") or []:
            if item.get("proposal_status") == "deterministic_fix":
                keyword_proposals[int(item["card_id"])] = dict(item)
    if semantic_keywords is not None:
        for item in semantic_keywords.get("proposals") or []:
            card_id = int(item["card_id"])
            if card_id in keyword_proposals:
                raise CompileError(f"duplicate supplemental keyword proposal: {card_id}")
            keyword_proposals[card_id] = dict(item)

    for item_id, review in sorted((decisions.get("keyword") or {}).items()):
        decision = str((review or {}).get("decision") or "pending")
        if decision not in {"use_proposed", "manual_edit"}:
            continue
        card_id = int(item_id)
        proposal = keyword_proposals.get(card_id)
        if proposal is None:
            raise CompileError(f"reviewed keyword proposal is missing: {card_id}")
        revision_id = int(proposal["based_on_revision_id"])
        proposal_id = f"keyword-card-{card_id}-rev-{revision_id}"
        if str((review or {}).get("proposal_id") or "") != proposal_id:
            raise CompileError(f"stale supplemental keyword proposal: {card_id}")
        if card_id in deleted_cards:
            noops.append({"kind": "keyword", "card_id": card_id, "reason": "card_delete"})
            continue
        if card_id in proposals and revision_id != int(
            proposals[card_id]["current_ref"]["based_on_revision_id"]
        ):
            raise CompileError(f"stale supplemental keyword revision: {card_id}")
        keywords = (
            ((review or {}).get("edited") or {}).get("keywords")
            if decision == "manual_edit"
            else proposal.get("proposed_keywords")
        )
        if not isinstance(keywords, list) or not keywords or any(
            not isinstance(value, str) or not value.strip() for value in keywords
        ):
            raise CompileError(f"invalid supplemental keyword payload: {card_id}")
        operations.append(
            _operation(
                "card_revision_patch",
                {
                    "card_id": card_id,
                    "based_on_revision_id": revision_id,
                    "fields": {"keywords": keywords},
                    "authority": "user_review",
                    "proposal_id": proposal_id,
                },
            )
        )

    recorded_keyword_noops = {
        item["card_id"] for item in noops if item["kind"] == "keyword"
    }
    for card_id in sorted(deleted_cards & set(keyword_proposals)):
        if card_id not in recorded_keyword_noops:
            noops.append({"kind": "keyword", "card_id": card_id, "reason": "card_delete"})

    if content_cleanup is not None:
        for proposal in sorted(
            content_cleanup.get("proposals") or [], key=lambda item: int(item["card_id"])
        ):
            card_id = int(proposal["card_id"])
            revision_id = int(proposal["based_on_revision_id"])
            method = str(proposal.get("cleanup_method") or "")
            review = (decisions.get("content_cleanup") or {}).get(str(card_id)) or {}
            decision = str(review.get("decision") or "pending")
            automatic = method == "deterministic_editorial_suffix"
            if card_id in deleted_cards:
                noops.append(
                    {"kind": "content_cleanup", "card_id": card_id, "reason": "card_delete"}
                )
                continue
            if not automatic and decision not in {"use_proposed", "manual_edit"}:
                continue
            r3_proposal = proposals.get(card_id)
            if r3_proposal is not None and revision_id != int(
                r3_proposal["current_ref"]["based_on_revision_id"]
            ):
                raise CompileError(f"stale content cleanup revision: {card_id}")
            current_content = str(
                (
                    (r3_proposal.get("current_snapshot") or {})
                    if r3_proposal is not None
                    else (proposal.get("current_snapshot") or {})
                ).get("content")
                or ""
            )
            if hashlib.sha256(current_content.encode("utf-8")).hexdigest() != str(
                proposal.get("current_content_sha256") or ""
            ):
                raise CompileError(f"stale content cleanup hash: {card_id}")
            if automatic:
                details = proposal.get("deterministic_cleanup") or {}
                removed = str(details.get("removed_editorial_text") or "")
                proposed_content = str(proposal.get("proposed_content") or "")
                if (
                    not removed
                    or details.get("historical_text_preservation") != "byte_exact_prefix"
                    or current_content != proposed_content + removed
                ):
                    raise CompileError(
                        f"deterministic cleanup is not a byte-exact suffix: {card_id}"
                    )
                authority = "deterministic_exact_suffix"
            else:
                proposal_id = str(proposal.get("proposal_id") or "")
                if str(review.get("proposal_id") or "") != proposal_id:
                    raise CompileError(f"stale reviewed content cleanup: {card_id}")
                proposed_content = (
                    str((review.get("edited") or {}).get("content") or "").strip()
                    if decision == "manual_edit"
                    else str(proposal.get("proposed_content") or "")
                )
                authority = "user_review"
            if not proposed_content:
                raise CompileError(f"empty content cleanup payload: {card_id}")
            operations.append(
                _operation(
                    "card_revision_patch",
                    {
                        "card_id": card_id,
                        "based_on_revision_id": revision_id,
                        "fields": {"content": proposed_content},
                        "authority": authority,
                        "proposal_id": str(proposal.get("proposal_id") or ""),
                    },
                )
            )
    return operations, noops

def _compile_lineage_operations(
    r3: Mapping[str, Any], state: Mapping[str, Any]
) -> list[dict[str, Any]]:
    decisions = state.get("decisions") or {}
    operations: list[dict[str, Any]] = []
    for group in sorted(r3.get("identity_groups") or [], key=lambda item: str(item["group_id"])):
        group_id = str(group["group_id"])
        review = _review_entry(decisions, "identity", group_id)
        decision = str(review.get("decision") or "pending")
        if decision not in {"accept", "edit"}:
            continue
        final = dict(group)
        if decision == "edit":
            edited = review.get("edited")
            if not isinstance(edited, dict) or not edited:
                raise CompileError(f"identity edit has no edited payload: {group_id}")
            final.update(edited)
        if final.get("judgment") != "same_memory":
            continue
        lineage = final.get("proposed_lineage")
        if not isinstance(lineage, dict):
            raise CompileError(f"same_memory identity has no lineage: {group_id}")
        operations.append(
            _operation(
                "card_lineage",
                {
                    "group_id": group_id,
                    "from_card_id": int(lineage["from_card_id"]),
                    "to_card_id": int(lineage["to_card_id"]),
                    "relation_type": str(lineage["relation_type"]),
                },
            )
        )
    return operations

def normalize_water_plan(water: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize an explicit R3 plan or the active coarse R4 proposal.

    The coarse artifact is a review projection: Streams are nested below Lakes
    and card placements use ``new_lake`` / ``new_stream``.  R5 freezes that
    exact proposal into the same in-memory shape as an R3 ``water_plan``.  It
    does not resolve review conflicts or invent a placement.
    """
    embedded = water.get("water_plan")
    if isinstance(embedded, Mapping):
        return dict(embedded)
    if any(key in water for key in ("lakes", "streams", "memberships")):
        return dict(water)

    skeleton = water.get("skeleton")
    placements = water.get("placements")
    if not isinstance(skeleton, Mapping) or not isinstance(placements, list):
        raise CompileError("selected Water artifact has no supported plan shape")

    lakes: list[dict[str, Any]] = []
    streams: list[dict[str, Any]] = []
    node_keys: set[str] = set()
    stream_parents: dict[str, str] = {}
    for raw_lake in skeleton.get("lakes") or []:
        if not isinstance(raw_lake, Mapping):
            raise CompileError("coarse Water Lake must be an object")
        lake_key = str(raw_lake.get("key") or "")
        if not lake_key or lake_key in node_keys:
            raise CompileError(f"invalid or duplicate coarse Water node: {lake_key!r}")
        node_keys.add(lake_key)
        lakes.append(
            {
                "key": lake_key,
                "title": str(raw_lake.get("title") or lake_key),
                "description": str(
                    raw_lake.get("purpose") or raw_lake.get("description") or ""
                ),
                "status": "proposed",
            }
        )
        for raw_stream in raw_lake.get("streams") or []:
            if not isinstance(raw_stream, Mapping):
                raise CompileError("coarse Water Stream must be an object")
            stream_key = str(raw_stream.get("key") or "")
            if not stream_key or stream_key in node_keys:
                raise CompileError(
                    f"invalid or duplicate coarse Water node: {stream_key!r}"
                )
            node_keys.add(stream_key)
            stream_parents[stream_key] = lake_key
            streams.append(
                {
                    "key": stream_key,
                    "parent_lake_key": lake_key,
                    "title": str(raw_stream.get("title") or stream_key),
                    "description": str(
                        raw_stream.get("purpose")
                        or raw_stream.get("description")
                        or ""
                    ),
                    "status": "proposed",
                }
            )

    memberships: list[dict[str, Any]] = []
    unassigned_card_ids: list[int] = []
    seen_cards: set[int] = set()
    for placement in placements:
        if not isinstance(placement, Mapping):
            raise CompileError("coarse Water placement must be an object")
        try:
            card_id = int(placement["card_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CompileError("coarse Water placement has invalid card_id") from exc
        if card_id in seen_cards:
            raise CompileError(f"duplicate coarse Water placement: {card_id}")
        seen_cards.add(card_id)
        lake_key = placement.get("new_lake")
        stream_key = placement.get("new_stream")
        if lake_key in (None, "") and stream_key in (None, ""):
            unassigned_card_ids.append(card_id)
            continue
        if not lake_key or not stream_key:
            raise CompileError(f"partial coarse Water placement: {card_id}")
        if stream_parents.get(str(stream_key)) != str(lake_key):
            raise CompileError(f"invalid coarse Water ancestry: {card_id}")
        memberships.append(
            {
                "card_id": card_id,
                "lake_key": str(lake_key),
                "stream_key": str(stream_key),
                "branch_key": None,
                "status": str(placement.get("status") or "proposed"),
            }
        )

    return {
        "lakes": lakes,
        "streams": streams,
        "branches": [],
        "memberships": memberships,
        "unassigned_card_ids": sorted(unassigned_card_ids),
        "relations": [],
    }

def _compile_water_operations(
    water: Mapping[str, Any] | None,
    state: Mapping[str, Any],
    *,
    lineage_predecessors: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Compile only individually reviewed Water items from an explicit source.

    A pending Water plan is always an empty mutation set.  This intentionally
    does not infer whether the R3 or coarse plan should win.  Approved nodes,
    membership-edit draft Streams, and their dependency closure are the only
    Water mutations that may be compiled.
    """
    if water is None:
        return []
    decisions = state.get("decisions") or {}
    plan_review = _review_entry(decisions, "water_plan", "water_plan")
    if str(plan_review.get("decision") or "pending") not in {"accept", "edit"}:
        return []
    plan = normalize_water_plan(water)
    predecessors = lineage_predecessors or set()
    operations: list[dict[str, Any]] = []
    approved_nodes: dict[str, str] = {}
    compiled_nodes: dict[str, dict[str, Any]] = {}
    for node_type, collection in (
        ("lake", "lakes"),
        ("stream", "streams"),
        ("branch", "branches"),
    ):
        for node in sorted(plan.get(collection) or [], key=lambda item: str(item.get("key") or "")):
            key = str(node.get("key") or "")
            if not key:
                raise CompileError(f"selected Water {node_type} has no key")
            review = _review_entry(decisions, "water_node", key)
            decision = str(review.get("decision") or "pending")
            if decision not in {"accept", "edit"}:
                continue
            final = dict(node)
            if decision == "edit":
                edited = review.get("edited")
                if not isinstance(edited, dict) or not edited:
                    raise CompileError(f"Water node edit has no payload: {key}")
                final.update(edited)
            operations.append(
                _operation("water_node", {"node_type": node_type, "node": final})
            )
            approved_nodes[key] = node_type
            compiled_nodes[key] = final
    membership_by_card = {
        str(item["card_id"]): item for item in plan.get("memberships") or []
    }
    membership_operations: list[tuple[str, dict[str, Any]]] = []
    for card_id, review in sorted((decisions.get("membership") or {}).items()):
        decision = str((review or {}).get("decision") or "pending")
        if decision not in {"accept", "edit"}:
            continue
        if int(card_id) in predecessors:
            # An accepted identity transition makes the source non-canonical.
            # Its Water position is inherited by the survivor; it must never
            # be materialized as a second membership.
            continue
        final = dict(membership_by_card.get(str(card_id)) or {})
        if decision == "edit":
            edited = (review or {}).get("edited")
            if not isinstance(edited, dict) or not edited:
                raise CompileError(f"membership edit has no payload: {card_id}")
            final.update(edited)
            draft = edited.get("new_stream")
            if isinstance(draft, dict) and draft:
                draft_key = str(draft.get("key") or "")
                if not draft_key:
                    raise CompileError(
                        f"membership edit draft Stream has no key: {card_id}"
                    )
                # A draft Stream authored by a membership edit is authorized by
                # the same review receipt and must be written as an explicit
                # node operation, never implicit side data.
                existing = compiled_nodes.get(draft_key)
                if existing is not None and existing != dict(draft):
                    raise CompileError(
                        f"conflicting Water node payload for draft Stream: {draft_key}"
                    )
                if existing is None:
                    operations.append(
                        _operation(
                            "water_node",
                            {"node_type": "stream", "node": dict(draft)},
                        )
                    )
                    approved_nodes[draft_key] = "stream"
                    compiled_nodes[draft_key] = dict(draft)
        if not final:
            raise CompileError(f"reviewed membership is absent from selected Water: {card_id}")
        final["card_id"] = int(card_id)
        membership_operations.append((str(card_id), final))

    parent_fields = {
        "stream": ("parent_lake_key", "lake"),
        "branch": ("parent_stream_key", "stream"),
    }
    for operation in [item for item in operations if item["kind"] == "water_node"]:
        node_type = str(operation["node_type"])
        parent_field = parent_fields.get(node_type)
        if parent_field is None:
            continue
        field, expected_type = parent_field
        parent_key = str(operation["node"].get(field) or "")
        if parent_key and approved_nodes.get(parent_key) != expected_type:
            raise CompileError(
                f"Water {node_type} references unapproved {expected_type} parent: {parent_key}"
            )
    for card_id, final in membership_operations:
        for field, expected_type in (
            ("lake_key", "lake"),
            ("stream_key", "stream"),
            ("branch_key", "branch"),
        ):
            value = final.get(field)
            if value in (None, ""):
                continue
            if approved_nodes.get(str(value)) != expected_type:
                raise CompileError(
                    f"membership {card_id} references unapproved Water {expected_type}: {value}"
                )
        operations.append(_operation("water_membership", {"membership": final}))
    return operations

def compile_manifest(
    *,
    r3_artifact_path: Path,
    review_state_path: Path,
    reviewed_artifact_path: Path,
    water_artifact_path: Path | None,
    keyword_audit_path: Path | None = None,
    semantic_keyword_path: Path | None = None,
    content_cleanup_path: Path | None = None,
) -> dict[str, Any]:
    """Freeze review inputs without reading or writing canonical databases."""
    r3_path = Path(r3_artifact_path)
    state_path = Path(review_state_path)
    reviewed_path = Path(reviewed_artifact_path)
    water_path = Path(water_artifact_path) if water_artifact_path is not None else None
    r3 = _load_object(r3_path, "R3 artifact")
    state = _load_object(state_path, "review state")
    reviewed = _load_object(reviewed_path, "reviewed artifact")
    water = _load_object(water_path, "selected Water artifact") if water_path else None
    keyword_audit = (
        _load_object(Path(keyword_audit_path), "keyword audit")
        if keyword_audit_path is not None
        else None
    )
    semantic_keywords = (
        _load_object(Path(semantic_keyword_path), "semantic keyword proposals")
        if semantic_keyword_path is not None
        else None
    )
    content_cleanup = (
        _load_object(Path(content_cleanup_path), "content cleanup proposals")
        if content_cleanup_path is not None
        else None
    )

    artifact_id = str(r3.get("artifact_id") or "")
    if not artifact_id:
        raise CompileError("R3 artifact_id is required")
    if state.get("artifact_id") != artifact_id:
        raise CompileError("review state artifact_id does not match R3")
    if reviewed.get("artifact_id") != artifact_id:
        raise CompileError("reviewed artifact_id does not match R3")

    based_on = r3.get("based_on_manifest")
    if not isinstance(based_on, dict):
        raise CompileError("R3 based_on_manifest is required")
    input_hashes: dict[str, str] = {}
    for logical_name, source_key in INPUT_HASH_KEYS.items():
        value = str(based_on.get(source_key) or "")
        if len(value) != 64:
            raise CompileError(f"R3 is missing {source_key}")
        input_hashes[logical_name] = value

    expected_revisions: list[dict[str, Any]] = []
    seen_cards: set[int] = set()
    for proposal in sorted(
        r3.get("card_proposals") or [], key=lambda item: int(item["current_ref"]["card_id"])
    ):
        ref = proposal.get("current_ref") or {}
        card_id = int(ref["card_id"])
        if card_id in seen_cards:
            raise CompileError(f"duplicate card proposal: {card_id}")
        seen_cards.add(card_id)
        snapshot = proposal.get("current_snapshot") or {}
        expected_revisions.append(
            {
                "card_id": card_id,
                "based_on_revision_id": int(ref["based_on_revision_id"]),
                "source": str(snapshot.get("source") or ""),
                "revision_origin": str(snapshot.get("revision_origin") or ""),
            }
        )

    embedded_state = reviewed.get("r4_review_state")
    embedded_hash = canonical_sha256(embedded_state) if isinstance(embedded_state, dict) else None
    state_content_hash = canonical_sha256(state)
    compile_issues: list[dict[str, Any]] = []
    if embedded_hash != state_content_hash:
        compile_issues.append(
            {
                "code": "REVIEW_STATE_NOT_MATERIALIZED",
                "review_state_sha256": state_content_hash,
                "materialized_state_sha256": embedded_hash,
            }
        )
    if water_path is None:
        compile_issues.append({"code": "WATER_ARTIFACT_NOT_SELECTED"})

    source_files = {
        "r3_artifact": sha256_file(r3_path),
        "review_state": sha256_file(state_path),
        "reviewed_artifact": sha256_file(reviewed_path),
        "water_artifact": sha256_file(water_path) if water_path else None,
        "keyword_audit": sha256_file(Path(keyword_audit_path)) if keyword_audit_path else None,
        "semantic_keywords": (
            sha256_file(Path(semantic_keyword_path)) if semantic_keyword_path else None
        ),
        "content_cleanup": (
            sha256_file(Path(content_cleanup_path)) if content_cleanup_path else None
        ),
    }
    supplemental, supplemental_noops = _compile_supplemental_operations(
        r3, state, keyword_audit, semantic_keywords, content_cleanup
    )
    lineage = _compile_lineage_operations(r3, state)
    lineage_predecessors = {
        int(operation["from_card_id"]) for operation in lineage
    }
    mutations = {
        "cards": _compile_card_operations(r3, state),
        "lineage": lineage,
        "water": _compile_water_operations(
            water,
            state,
            lineage_predecessors=lineage_predecessors,
        ),
        "supplemental": supplemental,
    }
    manifest_without_id: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "compiler_version": COMPILER_VERSION,
        "artifact_id": artifact_id,
        "source_files": source_files,
        "review_binding": {
            "review_state_content_sha256": state_content_hash,
            "materialized_state_sha256": embedded_hash,
        },
        "water_selection": {
            "selected": water_path is not None,
            "artifact_sha256": source_files["water_artifact"],
        },
        "input_hashes": input_hashes,
        "expected_card_revisions": expected_revisions,
        "mutations": mutations,
        "supplemental_noops": supplemental_noops,
        "compile_issues": compile_issues,
        "canonical_writes": 0,
        "commit_path_available": False,
    }
    return {
        **manifest_without_id,
        "cutover_id": f"r5:{canonical_sha256(manifest_without_id)}",
    }

def _read_card_state(memory_path: Path) -> dict[int, dict[str, Any]]:
    resolved = memory_path.resolve(strict=True)
    uri = f"{resolved.as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        required = {"cards", "card_revisions"}
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = sorted(required - tables)
        if missing:
            raise CompileError(f"memory copy missing tables: {missing}")
        rows = connection.execute(
            """
            SELECT c.id, c.current_revision_id, c.source,
                   r.card_id AS revision_card_id, r.origin AS revision_origin
            FROM cards c
            LEFT JOIN card_revisions r ON r.id = c.current_revision_id
            WHERE c.memory_kind = 'episodic' AND c.status != 'trashed'
            ORDER BY c.id
            """
        ).fetchall()
    return {
        int(row["id"]): {
            "current_revision_id": (
                int(row["current_revision_id"])
                if row["current_revision_id"] is not None
                else None
            ),
            "source": str(row["source"] or ""),
            "revision_card_id": (
                int(row["revision_card_id"])
                if row["revision_card_id"] is not None
                else None
            ),
            "revision_origin": str(row["revision_origin"] or ""),
        }
        for row in rows
    }

def preflight(
    manifest: Mapping[str, Any],
    *,
    input_paths: Mapping[str, Path],
    source_paths: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """Run strict read-only hash/card/revision guards and return a report."""
    if int(manifest.get("schema_version") or 0) != SCHEMA_VERSION:
        raise CompileError("unsupported cutover manifest schema_version")
    missing_inputs = sorted(set(INPUT_HASH_KEYS) - set(input_paths))
    if missing_inputs:
        raise CompileError(f"missing preflight inputs: {missing_inputs}")

    reasons: list[dict[str, Any]] = [dict(item) for item in manifest.get("compile_issues") or []]
    before_hashes = {
        name: sha256_file(Path(input_paths[name])) for name in sorted(INPUT_HASH_KEYS)
    }
    expected_hashes = manifest.get("input_hashes") or {}
    hash_mismatches = [
        {
            "input": name,
            "expected_sha256": expected_hashes.get(name),
            "actual_sha256": before_hashes[name],
        }
        for name in sorted(INPUT_HASH_KEYS)
        if before_hashes[name] != expected_hashes.get(name)
    ]
    if hash_mismatches:
        reasons.append({"code": "INPUT_HASH_MISMATCH", "inputs": hash_mismatches})

    source_mismatches: list[dict[str, Any]] = []
    if source_paths is not None:
        expected_sources = manifest.get("source_files") or {}
        for name, path in sorted(source_paths.items()):
            actual = sha256_file(Path(path))
            if actual != expected_sources.get(name):
                source_mismatches.append(
                    {
                        "source": name,
                        "expected_sha256": expected_sources.get(name),
                        "actual_sha256": actual,
                    }
                )
        if source_mismatches:
            reasons.append(
                {"code": "COMPILED_SOURCE_HASH_MISMATCH", "sources": source_mismatches}
            )

    current = _read_card_state(Path(input_paths["memory"]))
    expected_entries = manifest.get("expected_card_revisions") or []
    expected = {int(item["card_id"]): item for item in expected_entries}
    expected_ids = set(expected)
    current_ids = set(current)
    added = sorted(current_ids - expected_ids)
    removed = sorted(expected_ids - current_ids)
    if added or removed:
        reasons.append({"code": "CARD_SET_MISMATCH", "added": added, "removed": removed})

    revision_changes = []
    revision_set_changes = {
        "unexpected": [
            {
                "card_id": card_id,
                "actual_revision_id": current[card_id]["current_revision_id"],
            }
            for card_id in added
        ],
        "missing": [
            {
                "card_id": card_id,
                "expected_revision_id": int(expected[card_id]["based_on_revision_id"]),
            }
            for card_id in removed
        ],
    }
    ownership_errors = []
    authority_changes = []
    for card_id in sorted(expected_ids & current_ids):
        wanted = expected[card_id]
        actual = current[card_id]
        expected_revision = int(wanted["based_on_revision_id"])
        if actual["current_revision_id"] != expected_revision:
            revision_changes.append(
                {
                    "card_id": card_id,
                    "expected_revision_id": expected_revision,
                    "actual_revision_id": actual["current_revision_id"],
                }
            )
        if actual["revision_card_id"] != card_id:
            ownership_errors.append(
                {
                    "card_id": card_id,
                    "revision_id": actual["current_revision_id"],
                    "revision_card_id": actual["revision_card_id"],
                }
            )
        expected_source = str(wanted.get("source") or "")
        expected_origin = str(wanted.get("revision_origin") or "")
        if (
            expected_source and actual["source"] != expected_source
        ) or (
            expected_origin and actual["revision_origin"] != expected_origin
        ):
            authority_changes.append(
                {
                    "card_id": card_id,
                    "expected_source": expected_source,
                    "actual_source": actual["source"],
                    "expected_revision_origin": expected_origin,
                    "actual_revision_origin": actual["revision_origin"],
                }
            )
    if revision_changes:
        reasons.append({"code": "REVISION_MISMATCH", "cards": revision_changes})
    if revision_set_changes["unexpected"] or revision_set_changes["missing"]:
        reasons.append({"code": "REVISION_SET_MISMATCH", **revision_set_changes})
    if ownership_errors:
        reasons.append({"code": "REVISION_OWNERSHIP_MISMATCH", "cards": ownership_errors})
    if authority_changes:
        reasons.append({"code": "MANUAL_AUTHORITY_CHANGED", "cards": authority_changes})

    after_hashes = {
        name: sha256_file(Path(input_paths[name])) for name in sorted(INPUT_HASH_KEYS)
    }
    changed_during_preflight = [
        name for name in sorted(INPUT_HASH_KEYS) if before_hashes[name] != after_hashes[name]
    ]
    if changed_during_preflight:
        reasons.append(
            {
                "code": "INPUT_CHANGED_DURING_PREFLIGHT",
                "inputs": changed_during_preflight,
            }
        )

    status = "STALE_REVIEW" if reasons else "PREFLIGHT_OK"
    return {
        "status": status,
        "ok": not reasons,
        "cutover_id": manifest.get("cutover_id"),
        "canonical_writes": 0,
        "commit_path_available": False,
        "stale_reasons": reasons,
        "checks": {
            "input_hashes": {
                "ok": not hash_mismatches and not changed_during_preflight,
                "before": before_hashes,
                "after": after_hashes,
                "mismatches": hash_mismatches,
                "changed_during_preflight": changed_during_preflight,
            },
            "compiled_sources": {
                "ok": not source_mismatches,
                "mismatches": source_mismatches,
            },
            "card_set": {
                "ok": not added and not removed,
                "expected_count": len(expected_ids),
                "actual_count": len(current_ids),
                "added": added,
                "removed": removed,
            },
            "revisions": {
                "ok": not revision_changes
                and not revision_set_changes["unexpected"]
                and not revision_set_changes["missing"],
                "changed": revision_changes,
                **revision_set_changes,
            },
            "revision_ownership": {
                "ok": not ownership_errors,
                "errors": ownership_errors,
            },
            "manual_authority": {
                "ok": not authority_changes,
                "changed": authority_changes,
            },
        },
    }
