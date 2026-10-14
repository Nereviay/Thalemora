"""Self Model 确定性领域 Gate；无 Provider 或 Runtime。"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable

from .contract import (
    OperationReceipt,
    OperationRequest,
    SemanticIntent,
    normalize_statement,
)
from .evidence import (
    EvidenceBundle,
)
from .model import (
    P3CClaimVersion,
    SelfModelStore,
)


GATE_VERSION = "p3c-gate-v2"


def _claim_id_for(request: OperationRequest) -> str:
    digest = hashlib.sha256(
        f"{request.namespace}\x1f{request.operation_id}".encode("utf-8")
    ).hexdigest()
    return f"self-trait:{digest[:32]}"


def _decision_fingerprint(
    *,
    request: OperationRequest,
    claim_id: str,
    expected_version: int,
    statement: str,
    requested_maturity: str,
    support_keys: Iterable[str],
    contradiction_ids: Iterable[str],
) -> str:
    canonical = json.dumps(
        {
            "claim_id": claim_id,
            "expected_version": expected_version,
            "gate_version": GATE_VERSION,
            "namespace": request.namespace,
            "requested_maturity": requested_maturity,
            "statement": normalize_statement(statement),
            "support_independence_keys": sorted(set(support_keys)),
            "contradiction_evidence_ids": sorted(set(contradiction_ids)),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DeterministicSelfModelGate:
    """Apply validated intent using resolver output only; never call an LLM."""

    def __init__(self, store: SelfModelStore):
        self.store = store

    def decide(
        self,
        request: OperationRequest,
        intent: SemanticIntent,
        resolved: EvidenceBundle,
    ) -> OperationReceipt:
        if not intent.is_mutation:
            return OperationReceipt(
                request.operation_id,
                "finalized",
                "ok",
                "review_read_only",
            )

        current_agent = tuple(
            record
            for record in resolved.mutation_authority
            if record.generation_id == request.generation_id
        )
        if not current_agent:
            return self._reject(request, "no_agent_origin_evidence")

        if intent.tool_name == "self_model_observe":
            return self._observe(request, intent, resolved)
        claim_id = str(intent.args["claim_id"])
        latest = self.store.p3c_latest(claim_id)
        if latest is None or latest.namespace != request.namespace:
            return self._reject(request, "claim_not_found")
        expected = int(intent.args["expected_version"])
        if expected != latest.version:
            return self._reject(request, "version_conflict", claim=latest)
        if latest.maturity == "retired":
            code = "already_retired" if intent.tool_name == "self_model_retire" else "lineage_retired"
            return self._reject(request, code, claim=latest)
        if intent.tool_name == "self_model_retire":
            return self._retire(request, latest, resolved)
        return self._revise(request, intent, latest, resolved)

    def _observe(
        self,
        request: OperationRequest,
        intent: SemanticIntent,
        resolved: EvidenceBundle,
    ) -> OperationReceipt:
        args = intent.args
        statement = normalize_statement(str(args["statement"]))
        latest = self.store.find_active_semantic_duplicate(
            namespace=request.namespace,
            domain=str(args["domain"]),
            topic_key=str(args["topic_key"]),
            scope=str(args["scope"]),
            statement=statement,
        )
        claim_id = latest.claim_id if latest else _claim_id_for(request)
        return self._apply_active(
            request=request,
            latest=latest,
            claim_id=claim_id,
            statement=statement,
            domain=str(args["domain"]),
            topic_key=str(args["topic_key"]),
            scope=str(args["scope"]),
            requested_maturity=str(args.get("requested_maturity", "observing")),
            resolved=resolved,
            new_statement=False,
        )

    def _revise(
        self,
        request: OperationRequest,
        intent: SemanticIntent,
        latest: P3CClaimVersion,
        resolved: EvidenceBundle,
    ) -> OperationReceipt:
        statement = normalize_statement(str(intent.args.get("statement", latest.statement)))
        requested = str(intent.args.get("requested_maturity", latest.maturity))
        return self._apply_active(
            request=request,
            latest=latest,
            claim_id=latest.claim_id,
            statement=statement,
            domain=latest.domain,
            topic_key=latest.topic_key,
            scope=latest.scope,
            requested_maturity=requested,
            resolved=resolved,
            new_statement=statement != latest.statement,
        )

    def _apply_active(
        self,
        *,
        request: OperationRequest,
        latest: P3CClaimVersion | None,
        claim_id: str,
        statement: str,
        domain: str,
        topic_key: str,
        scope: str,
        requested_maturity: str,
        resolved: EvidenceBundle,
        new_statement: bool,
    ) -> OperationReceipt:
        if latest is not None and latest.maturity == "retired":
            return self._reject(request, "lineage_retired", claim=latest)

        if latest is not None and not new_statement:
            all_records = tuple(resolved.records)
        else:
            # A restated claim does not inherit old semantic support.  Reviewed
            # suggestion roots, if explicitly adopted, are already in resolved.
            all_records = tuple(resolved.records)

        # Proposal authority and maturity support are deliberately distinct.
        # Recompute carried support from the actual evidence records under the
        # current policy so legacy maintenance-only keys cannot leak forward.
        support_records = tuple(
            record for record in all_records if record.is_maturity_support
        )
        support_ids = {record.evidence_id for record in support_records}
        support_keys = {record.independence_key for record in support_records}
        contradiction_ids = {
            record.evidence_id
            for record in all_records
            if record.is_active_contradiction
        }

        actual_maturity = requested_maturity
        downgrade_code: str | None = None
        if requested_maturity == "established" and contradiction_ids:
            actual_maturity = "observing"
            downgrade_code = "downgraded_active_contradiction"
        elif requested_maturity == "established" and len(support_keys) < 2:
            actual_maturity = "observing"
            downgrade_code = "downgraded_insufficient_independent_support"

        if latest is not None and (
            latest.statement == statement
            and latest.maturity == actual_maturity
            and set(latest.support_independence_keys) == support_keys
            and set(latest.contradiction_evidence_ids) == contradiction_ids
        ):
            return OperationReceipt(
                request.operation_id,
                "finalized",
                "no_op",
                "no_op_duplicate_evidence_set",
                latest.claim_id,
                latest.version,
                latest.maturity,
                eligible_for_epoch="next_boundary_only",
            )

        expected_version = latest.version if latest else 0
        fingerprint = _decision_fingerprint(
            request=request,
            claim_id=claim_id,
            expected_version=expected_version,
            statement=statement,
            requested_maturity=requested_maturity,
            support_keys=support_keys,
            contradiction_ids=contradiction_ids,
        )
        try:
            claim = self.store.append_p3c_version(
                request=request,
                claim_id=claim_id,
                expected_version=latest.version if latest else None,
                statement=statement,
                domain=domain,
                topic_key=topic_key,
                scope=scope,
                maturity=actual_maturity,
                evidence=all_records,
                support_evidence_ids=support_ids,
                support_independence_keys=support_keys,
                contradiction_evidence_ids=contradiction_ids,
                decision_fingerprint=fingerprint,
            )
        except ValueError as exc:
            code = str(exc)
            if code not in {"version_conflict", "lineage_retired", "immutable_claim_identity"}:
                code = "storage_conflict"
            return self._reject(request, code, claim=latest)

        if downgrade_code:
            status = "applied_downgraded"
            code = downgrade_code
        else:
            status = "applied"
            if claim.version == 1:
                code = f"created_{claim.maturity}"
            else:
                code = f"appended_{claim.maturity}"
        return OperationReceipt(
            request.operation_id,
            "finalized",
            status,
            code,
            claim.claim_id,
            claim.version,
            claim.maturity,
            eligible_for_epoch="next_boundary_only",
        )

    def _retire(
        self,
        request: OperationRequest,
        latest: P3CClaimVersion,
        resolved: EvidenceBundle,
    ) -> OperationReceipt:
        records = tuple(resolved.records)
        support_records = tuple(
            record for record in records if record.is_maturity_support
        )
        support_ids = {record.evidence_id for record in support_records}
        support_keys = {record.independence_key for record in support_records}
        contradictions = {
            record.evidence_id
            for record in records
            if record.is_active_contradiction
        }
        fingerprint = _decision_fingerprint(
            request=request,
            claim_id=latest.claim_id,
            expected_version=latest.version,
            statement=latest.statement,
            requested_maturity="retired",
            support_keys=support_keys,
            contradiction_ids=contradictions,
        )
        try:
            claim = self.store.append_p3c_version(
                request=request,
                claim_id=latest.claim_id,
                expected_version=latest.version,
                statement=latest.statement,
                domain=latest.domain,
                topic_key=latest.topic_key,
                scope=latest.scope,
                maturity="retired",
                evidence=records,
                support_evidence_ids=support_ids,
                support_independence_keys=support_keys,
                contradiction_evidence_ids=contradictions,
                decision_fingerprint=fingerprint,
            )
        except ValueError as exc:
            return self._reject(request, str(exc), claim=latest)
        return OperationReceipt(
            request.operation_id,
            "finalized",
            "applied",
            "retired",
            claim.claim_id,
            claim.version,
            claim.maturity,
            eligible_for_epoch="next_boundary_only",
        )

    def mechanical_revalidate(
        self,
        request: OperationRequest,
        latest: P3CClaimVersion,
        resolved: EvidenceBundle,
    ) -> OperationReceipt:
        """Append the sole permitted system-authored semantic-neutral downgrade."""
        support_ids = {record.evidence_id for record in resolved.maturity_support}
        support_keys = set(resolved.support_independence_keys)
        contradictions = {record.evidence_id for record in resolved.contradictions}
        if latest.maturity != "established" or (len(support_keys) >= 2 and not contradictions):
            return OperationReceipt(
                request.operation_id,
                "finalized",
                "no_op",
                "integrity_state_unchanged",
                latest.claim_id,
                latest.version,
                latest.maturity,
            )
        fingerprint = _decision_fingerprint(
            request=request,
            claim_id=latest.claim_id,
            expected_version=latest.version,
            statement=latest.statement,
            requested_maturity="mechanical_observing",
            support_keys=support_keys,
            contradiction_ids=contradictions,
        )
        claim = self.store.append_p3c_version(
            request=request,
            claim_id=latest.claim_id,
            expected_version=latest.version,
            statement=latest.statement,
            domain=latest.domain,
            topic_key=latest.topic_key,
            scope=latest.scope,
            maturity="observing",
            evidence=resolved.records,
            support_evidence_ids=support_ids,
            support_independence_keys=support_keys,
            contradiction_evidence_ids=contradictions,
            decision_fingerprint=fingerprint,
            mechanical=True,
        )
        return OperationReceipt(
            request.operation_id,
            "finalized",
            "applied_downgraded",
            "mechanical_evidence_invalidation_downgrade",
            claim.claim_id,
            claim.version,
            claim.maturity,
            eligible_for_epoch="next_boundary_only",
        )

    @staticmethod
    def _reject(
        request: OperationRequest,
        code: str,
        *,
        claim: P3CClaimVersion | None = None,
    ) -> OperationReceipt:
        return OperationReceipt(
            request.operation_id,
            "rejected",
            "rejected",
            code,
            claim.claim_id if claim else None,
            claim.version if claim else None,
            claim.maturity if claim else None,
        )


