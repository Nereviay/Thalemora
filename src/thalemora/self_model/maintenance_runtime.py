"""Shadow-only two-phase runtime for independent Self Model maintenance."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .contract import (
    ContractValidationError,
    InvocationContext,
    OperationReceipt,
    namespace_for_principal,
    validate_model_args,
)
from .evidence import SELF_MODEL_MUTATION_TOOLS
from .core import SelfModelCore
from .evidence import EvidenceResolutionError
import sqlite3
from .maintenance_evidence import (
    MaintenanceAuthoritySource,
    MaintenanceEvidenceResolver,
    MaintenanceReviewedSuggestionResolver,
)
from .maintenance_store import (
    SQLiteSelfModelMaintenanceStore,
)
from .store import SQLiteSelfModelStore


SURFACE = "self_maintenance"


class SelfModelMaintenanceRuntime:
    """Own only maintenance authority and Self Model shadow mutation stores.

    There is intentionally no ConversationStore, generation registry, provider,
    scheduler, or heartbeat dependency in this runtime.
    """

    def __init__(
        self,
        authority_db_path: str | Path,
        self_model_db_path: str | Path,
        *,
        principal_id: str,
        runtime_resolver: Any = None,
    ) -> None:
        self.principal_id = principal_id
        self.namespace = namespace_for_principal(principal_id)
        self.authority_store = SQLiteSelfModelMaintenanceStore(authority_db_path)
        self.connection = sqlite3.connect(self_model_db_path, isolation_level=None, check_same_thread=False)
        self.self_model_store = SQLiteSelfModelStore(self.connection)
        self.self_model_store.initialize()
        self.runtime_resolver = runtime_resolver
        self.source = MaintenanceAuthoritySource(self.authority_store, principal_id=principal_id)
        authority_resolver = MaintenanceEvidenceResolver(self.source)
        self.resolver = (
            MaintenanceReviewedSuggestionResolver(
                authority_resolver,
                self.self_model_store,
                runtime_resolver,
            )
            if runtime_resolver is not None
            else authority_resolver
        )
        self.resolver.principal_id = principal_id
        self.resolver.generation_state = self.source.generation_state
        self.service = _MaintenanceCore(store=self.self_model_store, resolver=self.resolver)

    def close(self) -> None:
        self.connection.close()
        self.authority_store.close()

    def accept_run(
        self,
        *,
        run_id: str,
        context_epoch_id: str,
        trigger_kind: str,
        trigger_key: str,
        normal_context_sha256: str,
        normal_message_count: int,
        context_source_refs: Sequence[Mapping[str, str]],
        started_at: str,
    ):
        return self.authority_store.accept_run(
            run_id=run_id,
            principal_id=self.principal_id,
            context_epoch_id=context_epoch_id,
            trigger_kind=trigger_kind,
            trigger_key=trigger_key,
            normal_context_sha256=normal_context_sha256,
            normal_message_count=normal_message_count,
            context_source_refs=context_source_refs,
            started_at=started_at,
        )

    def handle_tool_call(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        model_args: Mapping[str, Any],
        recorded_at: str,
    ) -> OperationReceipt | dict[str, Any]:
        """Persist validated intent before staging any mutation operation."""
        try:
            args = validate_model_args(tool_name, model_args)
        except ContractValidationError as exc:
            return {
                "ok": False,
                "status": "rejected",
                "code": exc.code,
                "message": str(exc),
            }
        result_status = "read_only" if tool_name == "self_model_review" else "validated"
        result_code = "review_read_only" if tool_name == "self_model_review" else "validated_intent"
        call = self.authority_store.append_tool_call(
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args=args,
            result_status=result_status,
            result_code=result_code,
            recorded_at=recorded_at,
        )
        if tool_name == "self_model_review":
            return self.review(
                args,
                operation_id=call.durable_ref,
                recorded_at=call.recorded_at,
            )
        return self.service.stage(tool_name, args, self._context_for(call))

    def finish_run(
        self,
        run_id: str,
        *,
        state: str,
        recorded_at: str,
        provider_visible_text_sha256: str | None = None,
        reason_code: str | None = None,
    ) -> tuple[OperationReceipt, ...]:
        self.authority_store.append_lifecycle_event(
            run_id=run_id,
            state=state,
            recorded_at=recorded_at,
            provider_visible_text_sha256=provider_visible_text_sha256,
            reason_code=reason_code,
        )
        return self.reconcile_run(run_id)

    def reconcile_run(self, run_id: str) -> tuple[OperationReceipt, ...]:
        """Replay only durable maintenance calls; never scan canonical chat."""
        run = self.authority_store.run(run_id)
        if run is None or run.principal_id != self.principal_id:
            return ()
        state = self.authority_store.current_state(run_id)
        calls = self.authority_store.tool_calls(run_id)
        mutation_calls = [
            call for call in calls if call.tool_name in SELF_MODEL_MUTATION_TOOLS
        ]
        receipts: list[OperationReceipt] = []
        for call in mutation_calls:
            staged = self.service.stage(call.tool_name, call.args, self._context_for(call))
            if state == "accepted":
                receipts.append(staged)
            else:
                receipts.append(self.service.finalize(staged.operation_id))
        if state == "completed":
            self._reconcile_review_events(calls, tuple(mutation_calls), tuple(receipts))
        return tuple(receipts)

    def _reconcile_review_events(
        self,
        calls,
        mutation_calls,
        receipts: tuple[OperationReceipt, ...],
    ) -> None:
        """Close viewed suggestions only after the maintenance run completes."""
        reviewed = {}
        for call in calls:
            if call.tool_name != "self_model_review":
                continue
            suggestion_id = str(call.args.get("review_suggestion_id") or "").strip()
            if suggestion_id:
                reviewed[suggestion_id] = call
        if not reviewed:
            return

        acted = {}
        for call, receipt in zip(mutation_calls, receipts):
            suggestion_id = str(call.args.get("review_suggestion_id") or "").strip()
            if not suggestion_id:
                continue
            if (
                receipt.phase == "finalized"
                and receipt.status in {"applied", "applied_downgraded", "no_op"}
            ):
                acted[suggestion_id] = call

        for suggestion_id, review_call in reviewed.items():
            action_call = acted.get(suggestion_id)
            if action_call is not None:
                action = "acted"
                operation_id = action_call.durable_ref
                recorded_at = action_call.recorded_at
            else:
                action = "dismissed"
                operation_id = review_call.durable_ref
                recorded_at = review_call.recorded_at
            self.self_model_store.append_review_event(
                suggestion_id=suggestion_id,
                operation_id=operation_id,
                action=action,
                recorded_at=recorded_at,
            )

    def reconcile_pending(self) -> tuple[OperationReceipt, ...]:
        """Recover missing/staged maintenance operations without chat backfill."""
        receipts: list[OperationReceipt] = []
        operations = self.self_model_store.operations()
        existing_roots = {
            (request.generation_id, request.tool_call_event_id)
            for request in operations
            if request.surface == SURFACE
        }
        run_ids = {
            request.generation_id
            for request in self.self_model_store.pending_operations()
            if request.surface == SURFACE
        }
        for run_id in self.authority_store.run_ids():
            if any(
                call.tool_name in SELF_MODEL_MUTATION_TOOLS
                and (run_id, call.tool_call_root_id) not in existing_roots
                for call in self.authority_store.tool_calls(run_id)
            ):
                run_ids.add(run_id)
            if self.authority_store.current_state(run_id) == "completed" and any(
                call.tool_name == "self_model_review"
                for call in self.authority_store.tool_calls(run_id)
            ):
                run_ids.add(run_id)
        for run_id in sorted(run_ids):
            receipts.extend(self.reconcile_run(run_id))
        return tuple(receipts)

    def review(
        self,
        args: Mapping[str, Any],
        *,
        operation_id: str | None = None,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        """Read claims; detailed suggestion views append only a review event."""
        suggestion_id = str(args.get("review_suggestion_id") or "").strip()
        if suggestion_id:
            suggestion = self.self_model_store.review_suggestion(suggestion_id)
            if suggestion is None or suggestion["namespace"] != self.namespace:
                return {
                    "ok": False,
                    "status": "rejected",
                    "code": "suggestion_not_found",
                }
            claim = self.self_model_store.p3c_latest(
                str(suggestion.get("claim_id") or "")
            )
            if claim is None or claim.principal_id != self.principal_id:
                return {
                    "ok": False,
                    "status": "rejected",
                    "code": "suggestion_not_found",
                }
            if operation_id and recorded_at:
                self.self_model_store.append_review_event(
                    suggestion_id=suggestion_id,
                    operation_id=operation_id,
                    action="viewed",
                    recorded_at=recorded_at,
                )
            evidence = []
            for evidence_id in suggestion["evidence_ids"]:
                item = self.self_model_store.evidence(evidence_id)
                if item is None:
                    continue
                evidence.append({
                    "evidence_id": item.evidence_id,
                    "durable_ref": item.durable_ref,
                    "source_type": item.source_type,
                    "evidence_kind": item.evidence_kind,
                    "actor": item.actor,
                    "relation": item.relation,
                    "independence_key": item.independence_key,
                    "occurred_at": item.occurred_at,
                    "canonical_state": item.canonical_state,
                    "outcome": item.outcome,
                })
            return {
                "ok": True,
                "status": "ok",
                "code": "suggestion_reviewed",
                "suggestion": {
                    "suggestion_id": suggestion["suggestion_id"],
                    "claim_id": suggestion["claim_id"],
                    "target_claim_version": suggestion["target_claim_version"],
                    "current_claim_version": claim.version,
                    "suggestion_type": suggestion["suggestion_type"],
                    "summary": suggestion["summary"],
                    "created_at": suggestion["created_at"],
                    "evidence": evidence,
                },
            }
        view = str(args.get("view") or "active")
        limit = int(args.get("limit") or 10)
        claim_id = str(args.get("claim_id") or "").strip()
        candidates = (
            (self.self_model_store.p3c_latest(claim_id),)
            if claim_id
            else self.self_model_store.p3c_latest_claims()
        )
        claims = [
            claim
            for claim in candidates
            if claim is not None
            and claim.namespace == self.namespace
            and (
                view == "all"
                or (view == "active" and claim.maturity != "retired")
                or claim.maturity == view
            )
        ][:limit]
        pending = self.self_model_store.pending_review_suggestions(
            namespace=self.namespace,
            limit=10,
        )
        return {
            "ok": True,
            "status": "ok",
            "code": "review_read_only",
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "version": claim.version,
                    "statement": claim.statement,
                    "maturity": claim.maturity,
                    "domain": claim.domain,
                    "topic_key": claim.topic_key,
                    "scope": claim.scope,
                    "support_count": len(claim.support_independence_keys),
                }
                for claim in claims
            ],
            "pending_suggestions": [
                {
                    "suggestion_id": item["suggestion_id"],
                    "claim_id": item["claim_id"],
                    "suggestion_type": item["suggestion_type"],
                    "summary": item["summary"],
                    "created_at": item["created_at"],
                }
                for item in pending
            ],
        }

    def _context_for(self, call) -> InvocationContext:
        run = self.authority_store.run(call.run_id)
        if run is None:
            raise ValueError("run_not_found")
        return InvocationContext(
            principal_id=run.principal_id,
            generation_id=run.run_id,
            canonical_tool_call_event_id=call.tool_call_root_id,
            canonical_branch_id=run.run_id,
            context_epoch_id=run.context_epoch_id,
            surface=SURFACE,
            invoked_at=call.recorded_at,
        )

class _MaintenanceCore(SelfModelCore):
    def _suggestion(self, request, intent):
        # 此类审核的 durable_ref 属于本次维护运行的正式工具调用。
        # resolve 已核验同一运行内的 review、目标版本和当前有效证据。
        if intent.args.get("review_suggestion_id") and not isinstance(self.resolver, MaintenanceReviewedSuggestionResolver):
            raise EvidenceResolutionError("suggestion_not_reviewed", "a verified runtime evidence source is required")
        return None
