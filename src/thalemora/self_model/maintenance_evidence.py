"""Fail-closed evidence adapter for independent maintenance authority."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .contract import InvocationContext, SemanticIntent
from .evidence import (
    AuthoritativeRecord,
    EvidenceBundle,
    EvidenceRecord,
    EvidenceResolutionError,
    GenerationState,
    SELF_MODEL_MUTATION_TOOLS,
)
from .maintenance_store import (
    MaintenanceStoreError,
    SQLiteSelfModelMaintenanceStore,
)

from .store import SQLiteSelfModelStore


MAINTENANCE_RESOLVER_VERSION = "p3d-5b-maintenance-evidence-v1"


class MaintenanceAuthoritySource:
    """Read verified run/tool lineage without consulting canonical chat."""

    def __init__(self, store: SQLiteSelfModelMaintenanceStore, *, principal_id=None):
        self.store = store
        self.principal_id = principal_id

    def generation_state(self, run_id: str) -> GenerationState | None:
        try:
            run = self.store.run(run_id)
            if run is None:
                return None
            state = self.store.current_state(run_id)
            calls = self.store.tool_calls(run_id)
        except MaintenanceStoreError:
            # SelfModelShadowService asks for lifecycle authority before it
            # enters the resolver's exception boundary.  Returning no state
            # makes corrupt lineage a deterministic rejected receipt instead
            # of allowing an integrity exception to escape the runtime.
            return None
        return GenerationState(
            generation_id=run_id,
            status=str(state or "failed"),
            active_canonical=state == "completed",
            canonical_branch_id=run_id,
            canonical_tool_call_event_ids=tuple(item.tool_call_root_id for item in calls),
        )

    def refs_for_generation(self, run_id: str) -> tuple[str, ...]:
        try:
            return tuple(
                item.durable_ref
                for item in self.store.tool_calls(run_id)
                if item.tool_name in SELF_MODEL_MUTATION_TOOLS
            )
        except MaintenanceStoreError as exc:
            raise EvidenceResolutionError(
                "durable_ref_unresolved", "maintenance tool lineage failed integrity validation"
            ) from exc

    def record(self, durable_ref: str) -> AuthoritativeRecord | None:
        try:
            call = self.store.tool_call_by_durable_ref(durable_ref)
            if call is None:
                return None
            run = self.store.run(call.run_id)
            state = self.store.current_state(call.run_id)
            if run is None or state is None or (self.principal_id is not None and run.principal_id != self.principal_id):
                return None
        except MaintenanceStoreError as exc:
            raise EvidenceResolutionError(
                "durable_ref_unresolved", "maintenance authority failed integrity validation"
            ) from exc
        if (
            call.tool_name not in SELF_MODEL_MUTATION_TOOLS
            or call.result_status != "validated"
        ):
            return None
        content = json.dumps(
            call.args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return AuthoritativeRecord(
            durable_ref=call.durable_ref,
            source_type="maintenance_tool_call",
            actor="assistant",
            root_event_id=call.tool_call_root_id,
            generation_id=call.run_id,
            canonical_state="active" if state == "completed" else state,
            outcome="succeeded" if state == "completed" else state,
            committed=state == "completed",
            occurred_at=call.recorded_at,
            surface="self_maintenance",
            relation_hint="supports",
            content=content,
            tool_name=call.tool_name,
        )


class MaintenanceEvidenceResolver:
    """Convert only completed, intact maintenance tool lineage to evidence."""

    def __init__(self, source: MaintenanceAuthoritySource):
        self.source = source

    def resolve(self, context: InvocationContext, intent: SemanticIntent) -> EvidenceBundle:
        try:
            run = self.source.store.run(context.generation_id)
        except MaintenanceStoreError as exc:
            raise EvidenceResolutionError(
                "durable_ref_unresolved", "maintenance run failed integrity validation"
            ) from exc
        if (
            run is None
            or context.surface != "self_maintenance"
            or run.principal_id != context.principal_id
            or run.context_epoch_id != context.context_epoch_id
            or context.canonical_branch_id != run.run_id
        ):
            raise EvidenceResolutionError(
                "durable_ref_unresolved", "maintenance invocation does not match its run envelope"
            )
        generation = self.source.generation_state(run.run_id)
        if (
            generation is None
            or context.canonical_tool_call_event_id
            not in generation.canonical_tool_call_event_ids
        ):
            raise EvidenceResolutionError(
                "durable_ref_unresolved", "maintenance tool call is outside the run lineage"
            )
        try:
            calls = self.source.store.tool_calls(run.run_id)
        except MaintenanceStoreError as exc:
            raise EvidenceResolutionError(
                "durable_ref_unresolved",
                "maintenance tool lineage failed integrity validation",
            ) from exc
        current_call = next(
            (
                call
                for call in calls
                if call.tool_call_root_id == context.canonical_tool_call_event_id
            ),
            None,
        )
        if (
            current_call is None
            or current_call.tool_name != intent.tool_name
            or current_call.args != dict(intent.args)
            or current_call.result_status != "validated"
        ):
            raise EvidenceResolutionError(
                "durable_ref_unresolved",
                "maintenance tool call does not match finalized semantic intent",
            )
        # Only this exact mutation call is evidence for this operation. Other
        # self-model calls in the same run may concern unrelated topics. They
        # retain the same run-level independence key when finalized separately.
        return self.resolve_refs((current_call.durable_ref,))

    def resolve_refs(self, refs: Iterable[str]) -> EvidenceBundle:
        records: dict[tuple[str, str], EvidenceRecord] = {}
        for durable_ref in refs:
            source = self.source.record(str(durable_ref))
            if source is None or source.source_hash != source.authoritative_hash():
                raise EvidenceResolutionError(
                    "durable_ref_unresolved", f"unresolved maintenance ref: {durable_ref}"
                )
            record = self._to_evidence(source)
            records[(record.durable_ref, record.source_hash)] = record
        return EvidenceBundle(
            records=tuple(sorted(records.values(), key=lambda item: item.durable_ref))
        )

    @staticmethod
    def _to_evidence(source: AuthoritativeRecord) -> EvidenceRecord:
        active = (
            source.committed
            and source.canonical_state == "active"
            and source.outcome == "succeeded"
            and source.actor == "assistant"
            and source.tool_name in SELF_MODEL_MUTATION_TOOLS
        )
        digest_seed = (
            f"{source.durable_ref}\x1f{source.source_hash}"
            f"\x1f{MAINTENANCE_RESOLVER_VERSION}"
        )
        evidence_id = "evidence:" + hashlib.sha256(
            digest_seed.encode("utf-8")
        ).hexdigest()[:32]
        return EvidenceRecord(
            evidence_id=evidence_id,
            durable_ref=source.durable_ref,
            source_type="maintenance_tool_call",
            evidence_kind="self_statement",
            actor="assistant",
            relation="supports",
            root_event_id=source.root_event_id,
            generation_id=source.generation_id,
            independence_key=str(source.generation_id),
            canonical_state=source.canonical_state,
            outcome=source.outcome,
            committed=source.committed,
            eligible_for_trait_support=active,
            eligible_for_contradiction=False,
            occurred_at=source.occurred_at,
            source_hash=str(source.source_hash),
            resolver_version=MAINTENANCE_RESOLVER_VERSION,
            surface="self_maintenance",
        )


class MaintenanceReviewedSuggestionResolver:
    """Merge maintenance mutation authority with freshly revalidated evidence."""

    def __init__(
        self,
        maintenance_resolver: MaintenanceEvidenceResolver,
        self_model_store: SQLiteSelfModelStore,
        runtime_resolver: Any,
    ) -> None:
        self.maintenance_resolver = maintenance_resolver
        self.store = self_model_store
        self.runtime_resolver = runtime_resolver

    def resolve(self, context: InvocationContext, intent: SemanticIntent) -> EvidenceBundle:
        authority = self.maintenance_resolver.resolve(context, intent)
        suggestion_id = str(intent.args.get("review_suggestion_id") or "").strip()
        if not suggestion_id:
            # A wording-only clarification may retain *verified* prior support.
            # The Agent authors the clarification; the existing Gate still
            # recomputes maturity from genuinely active evidence roots.
            if (
                intent.tool_name == "self_model_revise"
                and intent.args.get("intent") == "clarify"
            ):
                claim_id = str(intent.args.get("claim_id") or "").strip()
                latest = self.store.p3c_latest(claim_id) if claim_id else None
                if (
                    latest is None
                    or latest.principal_id != context.principal_id
                    or latest.namespace != context.namespace
                    or latest.version != int(intent.args.get("expected_version") or 0)
                ):
                    raise EvidenceResolutionError(
                        "clarification_target_mismatch",
                        "clarification target changed before validation",
                    )
                existing = [
                    self.store.evidence(evidence_id)
                    for evidence_id in latest.evidence_ids
                ]
                if any(record is None for record in existing):
                    raise EvidenceResolutionError(
                        "clarification_evidence_missing",
                        "existing evidence no longer available",
                    )
                resolved = self.resolve_refs(
                    tuple(record.durable_ref for record in existing)
                )
                current_by_ref = {
                    record.durable_ref: record for record in resolved.records
                }
                for stored in existing:
                    current = current_by_ref.get(stored.durable_ref)
                    if (
                        current is None
                        or current.source_hash != stored.source_hash
                        or not current.committed
                        or current.canonical_state != "active"
                    ):
                        raise EvidenceResolutionError(
                            "clarification_evidence_stale",
                            "old evidence is no longer active canonical truth",
                        )
                merged = {record.evidence_id: record for record in authority.records}
                merged.update({record.evidence_id: record for record in resolved.records})
                return EvidenceBundle(
                    records=tuple(sorted(merged.values(), key=lambda item: item.evidence_id))
                )
            return authority
        suggestion = self.store.review_suggestion(suggestion_id)
        if suggestion is None:
            raise EvidenceResolutionError(
                "suggestion_not_found", "review suggestion does not exist"
            )
        claim_id = str(intent.args.get("claim_id") or "").strip()
        latest = self.store.p3c_latest(claim_id) if claim_id else None
        target_version = suggestion.get("target_claim_version")
        if (
            intent.tool_name not in {"self_model_revise", "self_model_retire"}
            or latest is None
            or latest.principal_id != context.principal_id
            or latest.namespace != context.namespace
            or suggestion["namespace"] != context.namespace
            or suggestion["claim_id"] != claim_id
            or target_version is None
            or latest.version != int(target_version)
            or int(intent.args.get("expected_version") or 0) != int(target_version)
        ):
            raise EvidenceResolutionError(
                "suggestion_target_mismatch",
                "review suggestion target or version no longer matches",
            )
        if (
            intent.tool_name == "self_model_revise"
            and intent.args.get("intent") == "add_support"
            and suggestion["suggestion_type"] != "add_support"
        ):
            raise EvidenceResolutionError(
                "suggestion_type_mismatch",
                "add_support requires an add-support suggestion",
            )

        run_prefix = f"maintenance-tool:{context.generation_id}:"
        reviewed = False
        try:
            for event in self.store.review_events(suggestion_id):
                durable_ref = str(event.get("operation_id") or "")
                if event.get("action") != "viewed" or not durable_ref.startswith(run_prefix):
                    continue
                call = self.maintenance_resolver.source.store.tool_call_by_durable_ref(
                    durable_ref
                )
                if (
                    call is not None
                    and call.run_id == context.generation_id
                    and call.tool_name == "self_model_review"
                    and call.result_status == "read_only"
                ):
                    reviewed = True
                    break
        except MaintenanceStoreError as exc:
            raise EvidenceResolutionError(
                "suggestion_review_unverified",
                "maintenance review lineage failed integrity validation",
            ) from exc
        if not reviewed:
            raise EvidenceResolutionError(
                "suggestion_not_reviewed",
                "suggestion was not reviewed in this maintenance run",
            )

        stored_records = []
        for evidence_id in suggestion["evidence_ids"]:
            record = self.store.evidence(evidence_id)
            if record is None:
                raise EvidenceResolutionError(
                    "suggestion_evidence_missing", "suggestion evidence is unavailable"
                )
            stored_records.append(record)
        refs = tuple(record.durable_ref for record in stored_records)
        resolved = self.runtime_resolver.resolve_refs(refs)
        current_by_ref = {record.durable_ref: record for record in resolved.records}
        for stored in stored_records:
            current = current_by_ref.get(stored.durable_ref)
            if (
                current is None
                or current.source_hash != stored.source_hash
                or not current.committed
                or current.canonical_state != "active"
            ):
                raise EvidenceResolutionError(
                    "suggestion_evidence_stale",
                    "suggestion evidence is no longer active canonical truth",
                )
        if suggestion["suggestion_type"] == "add_support":
            if not any(record.is_maturity_support for record in resolved.records):
                raise EvidenceResolutionError(
                    "suggestion_evidence_invalid",
                    "add-support suggestion has no current maturity support",
                )
        elif not any(record.is_active_contradiction for record in resolved.records):
            raise EvidenceResolutionError(
                "suggestion_evidence_invalid",
                "contradiction suggestion is no longer contradicted",
            )
        merged = {record.evidence_id: record for record in authority.records}
        merged.update({record.evidence_id: record for record in resolved.records})
        return EvidenceBundle(
            records=tuple(sorted(merged.values(), key=lambda item: item.evidence_id))
        )

    def resolve_refs(self, refs: Iterable[str]) -> EvidenceBundle:
        return resolve_mixed_refs(refs, self.maintenance_resolver.resolve_refs, self.runtime_resolver.resolve_refs)


def resolve_mixed_refs(refs, maintenance_reader, runtime_reader):
    """每种历史引用只交给它自己的权威；普通聊天对话不打开维护库。"""
    requested = tuple(str(ref or "").strip() for ref in refs)
    maintenance_refs = tuple(
        ref for ref in requested if ref.startswith("maintenance-tool:")
    )
    runtime_refs = tuple(
        ref for ref in requested if not ref.startswith("maintenance-tool:")
    )
    merged: dict[str, EvidenceRecord] = {}
    if maintenance_refs:
        authority = maintenance_reader(maintenance_refs)
        merged.update({record.evidence_id: record for record in authority.records})
    if runtime_refs:
        canonical = runtime_reader(runtime_refs)
        merged.update({record.evidence_id: record for record in canonical.records})
    return EvidenceBundle(
        records=tuple(sorted(merged.values(), key=lambda item: item.evidence_id))
    )


class CanonicalWithMaintenanceRefs:
    """正式对话的操作仍由 Runtime 核对，历史维护引用从原账本按需读取。"""

    def __init__(self, canonical, maintenance_db):
        self.principal_id = canonical.principal_id
        self.generation_state = canonical.generation_state
        self.resolve = canonical.resolve
        self.canonical = canonical
        self.maintenance_db = maintenance_db

    def _maintenance_refs(self, refs):
        from pathlib import Path
        from contextlib import closing
        if not self.maintenance_db or not Path(self.maintenance_db).is_file():
            raise EvidenceResolutionError("durable_ref_unresolved", "maintenance history unavailable")
        with closing(SQLiteSelfModelMaintenanceStore(self.maintenance_db, readonly=True)) as store:
            source = MaintenanceAuthoritySource(store, principal_id=self.principal_id)
            return MaintenanceEvidenceResolver(source).resolve_refs(refs)

    def resolve_refs(self, refs):
        return resolve_mixed_refs(refs, self._maintenance_refs, self.canonical.resolve_refs)
