"""Bounded future-evidence review for observing Self Model claims.

The curator reads one already-frozen authoritative snapshot.  It may append
extractor audits and review suggestions, but it never appends claim versions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .candidate_extractor import (
    CandidateExtractionError,
    CandidateExtractorProtocol,
    SelfModelCandidateExtractor,
)
from .candidate_provider import (
    MaterializedEvidence,
    materialize_frozen_evidence,
)
from .evidence import (
    EvidenceRecord,
    EvidenceResolutionError,
)
from .model import P3CClaimVersion
from .store import SQLiteSelfModelStore
from .suggestions import SelfModelSuggestionIntake


_PROSPECTIVE_SOURCE_TYPES = frozenset({
    "canonical_reply",
    "canonical_tool_call",
    "tool_receipt",
    "contemplation",
    "shared_event",
    "user_correction",
    "user_preference",
})


@dataclass(frozen=True)
class ProspectiveCuratorResult:
    scanned_claims: int
    provider_calls_attempted: int
    no_match: int
    suggestions_created: int
    idempotent_replays: int
    failures: int
    provider_visible: bool = False


@dataclass(frozen=True)
class _ClaimBatch:
    claim: P3CClaimVersion
    evidence: tuple[EvidenceRecord, ...]


ExtractorFactory = Callable[
    [Mapping[str, MaterializedEvidence]], CandidateExtractorProtocol
]


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


class ProspectiveSelfModelCurator:
    """Review post-creation canonical evidence with a hard provider-call cap."""

    def __init__(
        self,
        *,
        store: SQLiteSelfModelStore,
        frozen_snapshot: Any,
        namespace: str,
        extractor: CandidateExtractorProtocol | None = None,
        extractor_factory: ExtractorFactory | None = None,
        evidence_resolver,
        provider_call_cap: int = 4,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if provider_call_cap < 0:
            raise ValueError("provider_call_cap must be non-negative")
        if (extractor is None) == (extractor_factory is None):
            raise ValueError("provide exactly one extractor or extractor_factory")
        self.store = store
        self.snapshot = frozen_snapshot
        self.resolver = evidence_resolver
        self.namespace = str(namespace)
        self.extractor = extractor
        self.extractor_factory = extractor_factory
        self.provider_call_cap = int(provider_call_cap)
        self.clock = clock

    def _resolved_records(self, frozen_snapshot: Any) -> tuple[EvidenceRecord, ...]:
        resolver = self.resolver
        records: dict[tuple[str, str], EvidenceRecord] = {}
        for durable_ref in sorted(frozen_snapshot.records):
            try:
                bundle = resolver.resolve_refs((durable_ref,))
            except EvidenceResolutionError:
                continue
            for record in bundle.records:
                if (
                    record.source_type in _PROSPECTIVE_SOURCE_TYPES
                    and record.committed
                    and record.canonical_state == "active"
                ):
                    records[(record.durable_ref, record.source_hash)] = record
        return tuple(
            sorted(records.values(), key=lambda item: (item.occurred_at, item.durable_ref))
        )

    def _batches(self) -> tuple[_ClaimBatch, ...]:
        records = self._resolved_records(self.snapshot)
        batches: list[_ClaimBatch] = []
        claims = sorted(
            (
                claim
                for claim in self.store.p3c_latest_claims()
                if claim.namespace == self.namespace
                and claim.claim_kind == "self_trait"
                and claim.maturity == "observing"
            ),
            key=lambda item: (item.created_at, item.claim_id),
        )
        for claim in claims:
            history = self.store.p3c_history(claim.claim_id)
            created_at = _timestamp(
                history[0].created_at if history else claim.created_at
            )
            bound_roots = {
                record.durable_ref
                for evidence_id in claim.evidence_ids
                if (record := self.store.evidence(evidence_id)) is not None
            }
            eligible = [
                record
                for record in records
                if _timestamp(record.occurred_at) > created_at
                and record.durable_ref not in bound_roots
            ]
            # Keep the newest bounded window, then restore chronological order.
            selected = tuple(eligible[-64:])
            batches.append(_ClaimBatch(claim=claim, evidence=selected))
        return tuple(batches)

    def run(self) -> ProspectiveCuratorResult:
        batches = self._batches()
        non_empty = tuple(batch for batch in batches if batch.evidence)
        if not non_empty:
            return ProspectiveCuratorResult(
                scanned_claims=len(batches),
                provider_calls_attempted=0,
                no_match=0,
                suggestions_created=0,
                idempotent_replays=0,
                failures=0,
            )

        extractor = self.extractor
        if extractor is None:
            selected = {
                record.durable_ref: record
                for batch in non_empty
                for record in batch.evidence
            }
            materialized = materialize_frozen_evidence(
                tuple(selected.values()),
                self.snapshot.records,
            )
            assert self.extractor_factory is not None
            extractor = self.extractor_factory(materialized)

        resolver = self.resolver
        intake = SelfModelSuggestionIntake(self.store, resolver)
        orchestrator = SelfModelCandidateExtractor(
            store=self.store,
            intake=intake,
            extractor=extractor,
            clock=self.clock,
        )
        attempted = no_match = created = replays = failures = 0
        for batch in non_empty:
            try:
                request = orchestrator.prepare_request(
                    target_claim=batch.claim,
                    evidence=batch.evidence,
                )
                previous = self.store.successful_extraction_run(
                    request.job_fingerprint
                )
                if previous is None:
                    if attempted >= self.provider_call_cap:
                        break
                    attempted += 1
                result = orchestrator.run(
                    target_claim=batch.claim,
                    evidence=batch.evidence,
                )
            except (CandidateExtractionError, EvidenceResolutionError, ValueError):
                failures += 1
                continue
            if result.idempotent:
                replays += 1
            elif result.decision == "no_match":
                no_match += 1
            elif result.suggestion_id:
                created += 1
        return ProspectiveCuratorResult(
            scanned_claims=len(batches),
            provider_calls_attempted=attempted,
            no_match=no_match,
            suggestions_created=created,
            idempotent_replays=replays,
            failures=failures,
        )
