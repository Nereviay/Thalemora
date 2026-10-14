"""领域操作入口：Agent intent、证据核验、Gate 与账本事务。

不执行模型、调度或 Runtime mutation。context 和 source 的作者真实性由
调用方核验；这里守护 principal、独立支持、审核和版本边界。
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json

from .contract import (
    InvocationContext, OperationReceipt, OperationRequest, SemanticIntent,
    build_semantic_intent, make_operation_request, normalize_statement,
)
from .evidence import EvidenceBundle, EvidenceResolutionError, EvidenceResolver
from .gate import DeterministicSelfModelGate
from .store import SQLiteSelfModelStore


class SelfModelCore:
    def __init__(self, *, store: SQLiteSelfModelStore, resolver: EvidenceResolver):
        self.store = store
        self.resolver = resolver
        self.gate = DeterministicSelfModelGate(store)

    def stage(self, tool_name: str, model_args: dict, context: InvocationContext) -> OperationReceipt:
        if context.principal_id != self.resolver.principal_id:
            raise EvidenceResolutionError("principal_mismatch", "operation belongs to another principal")
        intent = build_semantic_intent(tool_name, model_args)
        request = make_operation_request(intent, context)
        with self.store.stage_transaction():
            state, original = self.store.stage_operation(request)
            if state == "conflict":
                raise ValueError("idempotency_key_reuse")
            previous = self.store.latest_receipt(original.operation_id)
            if state == "replay" and previous is not None:
                return previous
            return self.store.append_receipt(OperationReceipt(
                request.operation_id, "accepted", "accepted", "accepted_pending_canonicalization",
            ))

    @staticmethod
    def _context(request: OperationRequest) -> InvocationContext:
        return InvocationContext(
            request.principal_id, request.generation_id, request.tool_call_event_id,
            request.canonical_branch_id, request.context_epoch_id, request.surface,
            request.invoked_at,
        )

    def _latest_for(self, request: OperationRequest, intent: SemanticIntent):
        args = intent.args
        if intent.tool_name == "self_model_observe":
            return self.store.find_active_semantic_duplicate(
                namespace=request.namespace, domain=args["domain"],
                topic_key=args["topic_key"], scope=args["scope"],
                statement=normalize_statement(args["statement"]),
            )
        claim_id = args.get("claim_id", "")
        if intent.tool_name == "self_model_review" and not claim_id and args.get("review_suggestion_id"):
            suggestion = self.store.review_suggestion(str(args["review_suggestion_id"]))
            claim_id = suggestion["claim_id"] if suggestion else ""
        claim = self.store.p3c_latest(str(claim_id or ""))
        return claim if claim and claim.namespace == request.namespace else None

    def _suggestion(self, request: OperationRequest, intent: SemanticIntent):
        suggestion_id = intent.args.get("review_suggestion_id")
        if not suggestion_id:
            return None
        suggestion = self.store.review_suggestion(str(suggestion_id))
        latest = self._latest_for(request, intent)
        if (suggestion is None or suggestion["namespace"] != request.namespace
                or (suggestion["claim_id"] and (latest is None or latest.claim_id != suggestion["claim_id"]))):
            raise EvidenceResolutionError("suggestion_not_found", "suggestion does not match claim and principal")
        if suggestion["claim_id"] and suggestion["target_claim_version"] != latest.version:
            raise EvidenceResolutionError("suggestion_version_conflict", "suggestion targets another claim version")
        if intent.is_mutation:
            event = suggestion["latest_event"]
            operation = self.store.operation(event["operation_id"]) if event else None
            receipt = self.store.latest_receipt(operation.operation_id) if operation else None
            if (not event or event["action"] != "viewed" or operation is None
                    or operation.tool_name != "self_model_review"
                    or operation.namespace != request.namespace or receipt is None
                    or receipt.code != "review_read_only"
                    or (latest and receipt.claim_version != latest.version)):
                raise EvidenceResolutionError("suggestion_not_reviewed", "Agent review of this version is required")
            # 审核本身也必须仍有可核验的 canonical Agent 来源。
            self.resolver.resolve(self._context(operation), SemanticIntent(operation.tool_name, operation.replay_intent))
        return suggestion

    def finalize(self, operation_id: str) -> OperationReceipt:
        request = self.store.operation(operation_id)
        if request is None:
            return OperationReceipt(operation_id, "rejected", "rejected", "operation_not_found")
        if request.principal_id != self.resolver.principal_id:
            raise EvidenceResolutionError("principal_mismatch", "operation belongs to another principal")
        with self.store.finalize_transaction():
            previous = self.store.latest_receipt(operation_id)
            if previous and previous.phase in {"finalized", "rejected", "aborted"}:
                return previous
            generation = self.resolver.generation_state(request.generation_id)
            if generation and generation.pending:
                return self.store.append_receipt(OperationReceipt(
                    operation_id, "pending", "pending", "pending_canonicalization",
                ))
            if generation and not generation.finalizable:
                return self.store.append_receipt(OperationReceipt(
                    operation_id, "aborted", "aborted", "aborted_noncanonical_generation",
                ))
            intent = SemanticIntent(request.tool_name, request.replay_intent)
            try:
                resolved = self.resolver.resolve(self._context(request), intent)
                suggestion = self._suggestion(request, intent)
                latest = self._latest_for(request, intent)
                refs = []
                # 原陈述的累积支持在每次采用前重新解析，改述不继承旧支持。
                if (intent.is_mutation and latest
                        and normalize_statement(str(intent.args.get("statement", latest.statement))) == latest.statement):
                    refs.extend(self.store.evidence(key).durable_ref for key in latest.evidence_ids)
                if intent.is_mutation and suggestion:
                    refs.extend(self.store.evidence(key).durable_ref for key in suggestion["evidence_ids"])
                if refs:
                    refreshed = self.resolver.resolve_refs(refs)
                    records = {item.durable_ref: item for item in (*refreshed.records, *resolved.records)}
                    resolved = EvidenceBundle(tuple(sorted(records.values(), key=lambda item: item.durable_ref)))
                if not intent.is_mutation and suggestion and not any(
                        item.generation_id == request.generation_id for item in resolved.mutation_authority):
                    raise EvidenceResolutionError("no_agent_origin_evidence", "review needs Agent authority")
            except EvidenceResolutionError as exc:
                return self.store.append_receipt(OperationReceipt(
                    operation_id, "rejected", "rejected", exc.code, detail=str(exc),
                ))
            receipt = self.gate.decide(request, intent, resolved)
            if not intent.is_mutation:
                receipt = replace(receipt, claim_id=latest.claim_id if latest else None,
                                  claim_version=latest.version if latest else None)
            self.store.append_receipt(receipt)
            if suggestion and receipt.phase == "finalized":
                self.store.append_review_event(
                    suggestion_id=suggestion["suggestion_id"], operation_id=operation_id,
                    action="acted" if intent.is_mutation else "viewed", recorded_at=request.invoked_at,
                )
            return receipt

    def review_view(self, operation_id: str) -> dict:
        request = self.store.operation(operation_id)
        receipt = self.store.latest_receipt(operation_id)
        if (request is None or request.principal_id != self.resolver.principal_id
                or request.tool_name != "self_model_review" or receipt is None
                or receipt.code != "review_read_only"):
            raise ValueError("review_not_finalized")
        args = request.replay_intent
        claims = [claim for claim in self.store.p3c_latest_claims() if claim.namespace == request.namespace]
        if args.get("claim_id"):
            claims = [claim for claim in claims if claim.claim_id == args["claim_id"]]
        view = args.get("view", "active")
        claims = [claim for claim in claims if view == "all" or
                  (view == "active" and claim.maturity != "retired") or claim.maturity == view]
        claims = claims[:int(args.get("limit", 10))]
        return {
            "claims": tuple(claims),
            "history": {c.claim_id: self.store.p3c_history(c.claim_id) for c in claims} if args.get("include_history") else {},
            "evidence": {c.claim_id: tuple(self.store.evidence(key) for key in c.evidence_ids) for c in claims}
            if args.get("include_evidence_summary", True) else {},
            "suggestion": self.store.review_suggestion(args["review_suggestion_id"]) if args.get("review_suggestion_id") else None,
            "provider_visible": False,
        }

    def revalidate_claim(self, claim_id: str, *, context_epoch_id: str, now: str) -> OperationReceipt:
        """仅机械降级，绝不由维护器写新陈述或自动升格。"""
        with self.store.finalize_transaction():
            latest = self.store.p3c_latest(claim_id)
            if latest is None or latest.principal_id != self.resolver.principal_id:
                raise ValueError("claim_not_found")
            resolved = self.resolver.resolve_refs(self.store.evidence(key).durable_ref for key in latest.evidence_ids)
            watermark = json.dumps([vars(item) for item in resolved.records], sort_keys=True, ensure_ascii=False)
            digest = hashlib.sha256(f"{claim_id}\x1f{latest.version}\x1f{watermark}".encode()).hexdigest()
            request = OperationRequest(
                operation_id="self-model-integrity:" + digest[:32], idempotency_key="sha256:" + digest,
                tool_name="self_model_integrity_revalidate", principal_id=latest.principal_id,
                namespace=latest.namespace, generation_id="system-integrity", tool_call_event_id="system-integrity",
                canonical_branch_id="system-integrity", context_epoch_id=context_epoch_id,
                surface=latest.surface, invoked_at=now, intent_json=json.dumps({"claim_id": claim_id}), args_hash="sha256:" + digest,
            )
            _, request = self.store.stage_operation(request)
            previous = self.store.latest_receipt(request.operation_id)
            if previous is not None:
                return previous
            receipt = self.gate.mechanical_revalidate(request, latest, resolved)
            return self.store.append_receipt(receipt)
