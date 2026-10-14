# -*- coding: utf-8 -*-
"""Pure, read-only semantic verification for Water and Memory lineage graphs.

The verifier deliberately accepts already-loaded mappings.  It does not open a
database, write an artifact, or repair invalid input.  R4 uses ``verify_artifact``
to reject a structurally inconsistent reviewed artifact before materialization;
R5 can reuse ``verify_water_lineage`` for its compiled, in-memory graph.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any


LINEAGE_TYPES = {"merged_into", "split_into", "superseded_by"}
RELATION_TYPES = {"related", "converges_into"}
IDENTITY_JUDGMENTS = {"same_memory", "related_distinct", "uncertain"}


class WaterVerificationError(ValueError):
    """Raised when a caller requires a valid Water/Lineage graph."""

    def __init__(self, report: Mapping[str, Any]):
        self.report = dict(report)
        errors = self.report.get("errors") or []
        detail = errors[0].get("message") if errors else "unknown verification error"
        super().__init__(f"Water/Lineage semantic verification failed: {detail}")


def title_key(value: Any) -> str:
    """Normalized comparison key for active Water sibling titles."""
    return " ".join(str(value or "").split()).casefold()


def _active(item: Mapping[str, Any]) -> bool:
    return item.get("retired_at") is None and item.get("status") != "retired"


def _card_id_set(values: Iterable[Any]) -> set[int]:
    result: set[int] = set()
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("card_id")
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _find_cycle(graph: Mapping[Any, set[Any]]) -> list[Any] | None:
    visiting: set[Any] = set()
    visited: set[Any] = set()
    path: list[Any] = []

    def visit(node: Any) -> list[Any] | None:
        if node in visiting:
            start = path.index(node)
            return path[start:] + [node]
        if node in visited:
            return None
        visiting.add(node)
        path.append(node)
        for target in sorted(graph.get(node, set()), key=str):
            cycle = visit(target)
            if cycle:
                return cycle
        path.pop()
        visiting.remove(node)
        visited.add(node)
        return None

    for node in sorted(graph, key=str):
        cycle = visit(node)
        if cycle:
            return cycle
    return None


def _evidence_key(ref: Mapping[str, Any]) -> tuple[Any, ...]:
    if ref.get("evidence_id") is not None:
        return ("evidence_id", ref.get("evidence_id"))
    return (
        ref.get("source_kind"),
        ref.get("source_ref"),
        ref.get("raw_event_start_id"),
        ref.get("raw_event_end_id"),
        ref.get("char_start"),
        ref.get("char_end"),
        ref.get("quote_hash"),
    )


def _exact_evidence(refs: Iterable[Any]) -> set[tuple[Any, ...]]:
    result = set()
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        source_kind = str(ref.get("source_kind") or "")
        if source_kind in {"user_text_exact", "raw_event_exact", "raw_event_span"} or (
            ref.get("raw_event_start_id") is not None
        ):
            result.add(_evidence_key(ref))
    return result


def _artifact_lineage(artifact: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("lineage", "memory_card_lineage"):
        value = artifact.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
    return [
        group["proposed_lineage"]
        for group in artifact.get("identity_groups") or []
        if isinstance(group, Mapping)
        and isinstance(group.get("proposed_lineage"), Mapping)
    ]


def _artifact_canonical_ids(artifact: Mapping[str, Any]) -> set[int]:
    proposed = artifact.get("proposed_canonical_set")
    if isinstance(proposed, Mapping) and isinstance(proposed.get("representatives"), list):
        return _card_id_set(proposed["representatives"])

    card_ids = _card_id_set(
        (proposal.get("current_ref") or {}).get("card_id")
        for proposal in artifact.get("card_proposals") or []
        if isinstance(proposal, Mapping)
    )
    edges = _artifact_lineage(artifact)
    return (card_ids - _card_id_set(edge.get("from_card_id") for edge in edges)) | _card_id_set(
        edge.get("to_card_id") for edge in edges
    )


def _verify_split_evidence(artifact: Mapping[str, Any], add_error: Any) -> None:
    for index, proposal in enumerate(artifact.get("card_proposals") or []):
        if not isinstance(proposal, Mapping) or proposal.get("proposed_action") != "split":
            continue
        source = _exact_evidence(proposal.get("evidence_refs") or [])
        if not source:
            add_error(
                "lineage.split_evidence_missing",
                f"card_proposals[{index}]",
                "split predecessor must retain exact evidence for allocation",
            )
        successors = proposal.get("proposed_successors") or []
        assigned: set[tuple[Any, ...]] = set()
        for successor_index, successor in enumerate(successors):
            if not isinstance(successor, Mapping):
                continue
            evidence = _exact_evidence(successor.get("evidence_refs") or [])
            unknown = evidence - source
            if unknown:
                add_error(
                    "lineage.split_evidence_not_source",
                    f"card_proposals[{index}].proposed_successors[{successor_index}]",
                    "split successor uses exact evidence absent from its predecessor",
                )
            overlap = assigned & evidence
            if overlap:
                add_error(
                    "lineage.split_evidence_overlap",
                    f"card_proposals[{index}].proposed_successors[{successor_index}]",
                    "exact evidence is assigned to more than one split successor",
                )
            assigned.update(evidence)
        if assigned != source:
            add_error(
                "lineage.split_evidence_incomplete",
                f"card_proposals[{index}]",
                "split successors must partition all predecessor exact evidence",
            )


def _verify_identity_groups(artifact: Mapping[str, Any], add_error: Any) -> None:
    for index, group in enumerate(artifact.get("identity_groups") or []):
        if not isinstance(group, Mapping):
            continue
        path = f"identity_groups[{index}]"
        members = _card_id_set(group.get("member_card_ids") or [])
        lineage = group.get("proposed_lineage")
        if isinstance(lineage, Mapping):
            if group.get("judgment") != "same_memory":
                add_error(
                    "lineage.group_judgment_conflict",
                    path,
                    "only a same_memory group may propose identity lineage",
                )
            endpoints = _card_id_set(
                (lineage.get("from_card_id"), lineage.get("to_card_id"))
            )
            if len(endpoints) != 2 or not endpoints <= members:
                add_error(
                    "lineage.group_endpoint_invalid",
                    path,
                    "lineage endpoints must belong to the identity group",
                )
            preferred = group.get("preferred_survivor_card_id")
            if preferred is not None and preferred != lineage.get("to_card_id"):
                add_error(
                    "lineage.group_survivor_mismatch",
                    path,
                    "preferred survivor must match the lineage target",
                )
        review = group.get("review")
        edited = review.get("edited") if isinstance(review, Mapping) else None
        if isinstance(edited, Mapping):
            judgment = edited.get("judgment")
            if judgment is not None and judgment not in IDENTITY_JUDGMENTS:
                add_error(
                    "lineage.edited_judgment_invalid",
                    f"{path}.review.edited",
                    "edited identity judgment is invalid",
                )
            survivor = edited.get("preferred_survivor_card_id")
            if survivor is not None and survivor not in members:
                add_error(
                    "lineage.edited_survivor_invalid",
                    f"{path}.review.edited",
                    "edited preferred survivor must belong to the identity group",
                )


def verify_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Verify an R3/R4 proposal artifact without performing any I/O."""

    water_plan = artifact.get("water_plan")
    if not isinstance(water_plan, Mapping):
        return {
            "ok": False,
            "errors": [{
                "code": "water_plan.missing",
                "path": "water_plan",
                "message": "Water plan must be an object",
            }],
            "summary": {"error_count": 1},
        }
    return verify_water_lineage(
        water_plan,
        _artifact_lineage(artifact),
        canonical_card_ids=_artifact_canonical_ids(artifact),
        artifact=artifact,
    )


def require_valid_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    report = verify_artifact(artifact)
    if not report["ok"]:
        raise WaterVerificationError(report)
    return report


def verify_water_lineage(
    water_plan: Mapping[str, Any],
    lineage_edges: Iterable[Mapping[str, Any]] = (),
    *,
    canonical_card_ids: Iterable[int] | None = None,
    artifact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic report for locked Water/Lineage invariants."""

    errors: list[dict[str, str]] = []

    def add_error(code: str, path: str, message: str) -> None:
        errors.append({"code": code, "path": path, "message": message})

    sea = water_plan.get("sea")
    if sea is not None and (
        not isinstance(sea, Mapping)
        or sea.get("conceptual_root") is not True
        or sea.get("persisted_node") is not False
    ):
        add_error(
            "water.sea_not_conceptual",
            "water_plan.sea",
            "Sea must be a non-persisted conceptual root",
        )

    node_groups = {
        "lake": list(water_plan.get("lakes") or []),
        "stream": list(water_plan.get("streams") or []),
        "branch": list(water_plan.get("branches") or []),
    }
    nodes: dict[str, tuple[str, Mapping[str, Any]]] = {}
    sibling_titles: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for kind, rows in node_groups.items():
        for index, row in enumerate(rows):
            path = f"water_plan.{kind}s[{index}]"
            if not isinstance(row, Mapping):
                add_error("water.node_not_object", path, "Water node must be an object")
                continue
            key = str(row.get("key") or "")
            if not key:
                add_error("water.node_key_missing", path, "Water node key is required")
                continue
            if key in nodes:
                add_error("water.node_key_duplicate", path, f"duplicate Water node key: {key}")
            else:
                nodes[key] = (kind, row)
            if _active(row):
                parent = "ROOT"
                if kind == "stream":
                    parent = str(row.get("parent_lake_key") or "")
                elif kind == "branch":
                    parent = str(row.get("parent_stream_key") or "")
                node_title_key = title_key(row.get("title"))
                title_scope = (kind, parent)
                if not node_title_key:
                    add_error("water.node_title_missing", path, "active Water node title is required")
                elif node_title_key in sibling_titles[title_scope]:
                    add_error(
                        "water.sibling_title_duplicate",
                        path,
                        f"active sibling title duplicates {sibling_titles[title_scope][node_title_key]}",
                    )
                else:
                    sibling_titles[title_scope][node_title_key] = key

    for key, (kind, row) in nodes.items():
        if kind == "lake":
            if row.get("parent_key") not in (None, ""):
                add_error("water.lake_has_parent", f"water_plan.lakes[{key}]", "Lake cannot have a parent")
            continue
        parent_field = "parent_lake_key" if kind == "stream" else "parent_stream_key"
        expected = "lake" if kind == "stream" else "stream"
        parent_key = str(row.get(parent_field) or "")
        parent = nodes.get(parent_key)
        if parent is None or parent[0] != expected:
            add_error(
                "water.parent_invalid",
                f"water_plan.{kind}s[{key}]",
                f"{kind.title()} must have an existing {expected.title()} parent",
            )
        elif _active(row) and not _active(parent[1]):
            add_error(
                "water.active_child_retired_parent",
                f"water_plan.{kind}s[{key}]",
                "active Water child cannot reference a retired parent",
            )

    placed_ids: set[int] = set()
    sea_membership_ids: set[int] = set()
    all_membership_ids: set[int] = set()
    for index, membership in enumerate(water_plan.get("memberships") or []):
        path = f"water_plan.memberships[{index}]"
        if not isinstance(membership, Mapping):
            add_error("water.membership_not_object", path, "membership must be an object")
            continue
        try:
            card_id = int(membership.get("card_id"))
        except (TypeError, ValueError):
            add_error("water.membership_card_invalid", path, "membership card_id must be an integer")
            continue
        if card_id in all_membership_ids:
            add_error("water.membership_duplicate", path, f"card {card_id} has multiple memberships")
        all_membership_ids.add(card_id)
        lake_key = membership.get("lake_key")
        stream_key = membership.get("stream_key")
        branch_key = membership.get("branch_key")
        is_sea = membership.get("status") == "sea" or (
            lake_key is None and stream_key is None and branch_key is None
        )
        if is_sea:
            sea_membership_ids.add(card_id)
            if any(value is not None for value in (lake_key, stream_key, branch_key)):
                add_error("water.sea_has_container", path, "Sea card cannot reference a Water node")
            continue
        placed_ids.add(card_id)
        lake = nodes.get(str(lake_key or ""))
        stream = nodes.get(str(stream_key or ""))
        if lake is None or lake[0] != "lake" or not _active(lake[1]):
            add_error("water.membership_lake_invalid", path, "membership Lake must be active")
        if stream is None or stream[0] != "stream" or not _active(stream[1]):
            add_error("water.membership_stream_invalid", path, "membership Stream must be active")
        elif str(stream[1].get("parent_lake_key") or "") != str(lake_key or ""):
            add_error("water.membership_ancestry_invalid", path, "Stream does not belong to membership Lake")
        if branch_key is not None:
            branch = nodes.get(str(branch_key))
            if branch is None or branch[0] != "branch" or not _active(branch[1]):
                add_error("water.membership_branch_invalid", path, "membership Branch must be active")
            elif str(branch[1].get("parent_stream_key") or "") != str(stream_key or ""):
                add_error("water.membership_ancestry_invalid", path, "Branch does not belong to membership Stream")

    unassigned = _card_id_set(water_plan.get("unassigned_card_ids") or [])
    # Proposal artifacts may carry explicit status=sea placement rows; a
    # materialized DB graph correctly represents Sea by the absence of a
    # membership row.  When pseudo-memberships are present they must agree
    # exactly with the explicit unassigned set.
    if sea_membership_ids and unassigned != sea_membership_ids:
        add_error(
            "water.sea_set_mismatch",
            "water_plan.unassigned_card_ids",
            "unassigned card IDs must exactly match Sea memberships",
        )
    if placed_ids & unassigned:
        add_error("water.sea_membership_overlap", "water_plan", "a card cannot be both placed and in Sea")

    relation_keys: set[tuple[str, str, str]] = set()
    convergence: dict[str, set[str]] = defaultdict(set)
    for index, relation in enumerate(water_plan.get("relations") or []):
        path = f"water_plan.relations[{index}]"
        if not isinstance(relation, Mapping):
            add_error("water.relation_not_object", path, "Water relation must be an object")
            continue
        relation_type = str(relation.get("relation_type") or "")
        if relation_type not in RELATION_TYPES:
            add_error("water.relation_type_invalid", path, "unknown Water relation type")
            continue
        if "from_stream_key" in relation or "to_stream_key" in relation:
            source = str(relation.get("from_stream_key") or "")
            target = str(relation.get("to_stream_key") or "")
        elif "from_lake_key" in relation or "to_lake_key" in relation:
            source = str(relation.get("from_lake_key") or "")
            target = str(relation.get("to_lake_key") or "")
        else:
            source = str(relation.get("from_node_key") or "")
            target = str(relation.get("to_node_key") or "")
        source_node = nodes.get(source)
        target_node = nodes.get(target)
        if (
            source_node is None
            or target_node is None
            or source_node[0] != target_node[0]
            or source_node[0] not in {"lake", "stream"}
            or not _active(source_node[1])
            or not _active(target_node[1])
        ):
            add_error("water.relation_endpoint_invalid", path, "relation endpoints must be active Lakes or active Streams of the same type")
            continue
        if source == target:
            add_error("water.relation_self_edge", path, "Water relation cannot be a self-edge")
            continue
        if relation_type == "related":
            normalized = tuple(sorted((source, target)))
            if (source, target) != normalized:
                add_error("water.related_not_normalized", path, "related endpoints must be stored in sorted order")
            key = (relation_type, normalized[0], normalized[1])
        else:
            key = (relation_type, source, target)
            convergence[source].add(target)
        if key in relation_keys:
            add_error("water.relation_duplicate", path, "duplicate Water relation")
        relation_keys.add(key)

    convergence_cycle = _find_cycle(convergence)
    if convergence_cycle:
        add_error(
            "water.convergence_cycle",
            "water_plan.relations",
            "converges_into graph contains a cycle: " + " -> ".join(convergence_cycle),
        )

    edges = [edge for edge in lineage_edges if isinstance(edge, Mapping)]
    lineage_graph: dict[int, set[int]] = defaultdict(set)
    source_types: dict[int, set[str]] = defaultdict(set)
    source_targets: dict[int, set[int]] = defaultdict(set)
    target_types: dict[int, set[str]] = defaultdict(set)
    edge_keys: set[tuple[int, int, str]] = set()
    for index, edge in enumerate(edges):
        path = f"lineage[{index}]"
        try:
            source = int(edge.get("from_card_id"))
            target = int(edge.get("to_card_id"))
        except (TypeError, ValueError):
            add_error("lineage.card_invalid", path, "lineage endpoints must be card IDs")
            continue
        relation_type = str(edge.get("relation_type") or "")
        if relation_type not in LINEAGE_TYPES:
            add_error("lineage.type_invalid", path, "unknown lineage transition type")
            continue
        if source == target:
            add_error("lineage.self_edge", path, "lineage cannot be a self-edge")
        key = (source, target, relation_type)
        if key in edge_keys:
            add_error("lineage.edge_duplicate", path, "duplicate lineage edge")
        edge_keys.add(key)
        lineage_graph[source].add(target)
        source_types[source].add(relation_type)
        source_targets[source].add(target)
        target_types[target].add(relation_type)

    for source, types in sorted(source_types.items()):
        if len(types) > 1:
            add_error("lineage.source_type_conflict", f"lineage.source[{source}]", "a predecessor cannot use multiple active transition types")
        relation_type = next(iter(types))
        if relation_type in {"merged_into", "superseded_by"} and len(source_targets[source]) != 1:
            add_error("lineage.target_cardinality", f"lineage.source[{source}]", f"{relation_type} must have exactly one target")
        if relation_type == "split_into" and len(source_targets[source]) < 2:
            add_error("lineage.target_cardinality", f"lineage.source[{source}]", "split_into must have at least two targets")
    for target, types in sorted(target_types.items()):
        if len(types) > 1:
            add_error("lineage.incoming_type_conflict", f"lineage.target[{target}]", "a successor cannot mix incoming transition types")

    lineage_cycle = _find_cycle(lineage_graph)
    if lineage_cycle:
        add_error(
            "lineage.cycle",
            "lineage",
            "lineage graph contains a cycle: " + " -> ".join(map(str, lineage_cycle)),
        )

    canonical = set(int(value) for value in canonical_card_ids) if canonical_card_ids is not None else None
    if canonical is not None:
        observed = placed_ids | unassigned
        if observed != canonical:
            add_error(
                "water.canonical_coverage_mismatch",
                "water_plan",
                "placed memberships plus Sea must exactly equal the final canonical card set",
            )
        predecessors = set(lineage_graph)
        leaked = predecessors & observed
        if leaked:
            add_error(
                "lineage.predecessor_is_canonical",
                "lineage",
                "lineage predecessors cannot retain final Water membership: " + ", ".join(map(str, sorted(leaked))),
            )
        if not lineage_cycle:
            for predecessor in sorted(predecessors):
                stack = [predecessor]
                terminals: set[int] = set()
                visited: set[int] = set()
                while stack:
                    node = stack.pop()
                    if node in visited:
                        continue
                    visited.add(node)
                    targets = lineage_graph.get(node, set())
                    if targets:
                        stack.extend(targets)
                    else:
                        terminals.add(node)
                if not terminals or not terminals <= canonical:
                    add_error(
                        "lineage.sink_not_canonical",
                        f"lineage.source[{predecessor}]",
                        "every lineage path must end at a final canonical card",
                    )

    if artifact is not None:
        _verify_identity_groups(artifact, add_error)
        _verify_split_evidence(artifact, add_error)

    errors.sort(key=lambda item: (item["code"], item["path"], item["message"]))
    return {
        "ok": not errors,
        "errors": errors,
        "summary": {
            "error_count": len(errors),
            "lake_count": len(node_groups["lake"]),
            "stream_count": len(node_groups["stream"]),
            "branch_count": len(node_groups["branch"]),
            "placed_membership_count": len(placed_ids),
            "sea_card_count": len(unassigned),
            "relation_count": len(relation_keys),
            "lineage_edge_count": len(edge_keys),
        },
    }
