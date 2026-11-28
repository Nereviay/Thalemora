"""Validated shadow suggestion intake for historical Self Model evidence.

The extractor boundary is intentionally only a protocol.  This module has no
provider implementation, scheduler, or chat wiring; accepted candidates append
review suggestions and evidence links only, never claim versions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Mapping, Protocol, Sequence

from .evidence import (
    EvidenceBundle,
    EvidenceRecord,
    EvidenceResolutionError,
)
from .store import SQLiteSelfModelStore


SUGGESTION_CONTRACT_VERSION = "self-model-review-suggestion-v1"
SUGGESTION_TYPES = frozenset({"add_support", "contradiction"})


class SuggestionValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CandidateExtractorProtocol(Protocol):
    """Future provider-neutral extractor boundary; no implementation here."""

    def extract_candidate(
        self,
        *,
        topic_key: str,
        evidence: Sequence[EvidenceRecord],
        current_claim: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class HistoricalEvidenceResolverProtocol(Protocol):
    def resolve_refs(self, refs: Sequence[str]) -> EvidenceBundle: ...


def _clean_required(value: Any, field_name: str, *, maximum: int = 256) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > maximum:
        raise SuggestionValidationError(
            "invalid_payload", f"{field_name} must contain 1-{maximum} characters"
        )
    return clean


@dataclass(frozen=True)
class CandidateSuggestionPayload:
    suggestion_type: str
    target_claim_id: str
    target_current_version: int
    target_topic_key: str
    target_scope: str
    target_principal_id: str
    target_namespace: str
    evidence_refs: tuple[str, ...]
    summary: str
    extractor_fingerprint: str
    job_fingerprint: str
    contract_fingerprint: str
    created_at: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CandidateSuggestionPayload":
        if not isinstance(value, Mapping):
            raise SuggestionValidationError("invalid_payload", "candidate must be an object")
        suggestion_type = _clean_required(value.get("suggestion_type"), "suggestion_type")
        if suggestion_type not in SUGGESTION_TYPES:
            raise SuggestionValidationError(
                "invalid_suggestion_type", "unsupported suggestion type"
            )
        try:
            target_version = int(value.get("target_current_version"))
        except (TypeError, ValueError):
            target_version = 0
        if target_version < 1:
            raise SuggestionValidationError(
                "invalid_payload", "target_current_version must be positive"
            )
        raw_refs = value.get("evidence_refs")
        if not isinstance(raw_refs, (list, tuple)) or not raw_refs:
            raise SuggestionValidationError(
                "no_evidence", "at least one evidence ref or id is required"
            )
        refs = tuple(_clean_required(item, "evidence_ref") for item in raw_refs)
        if len(refs) > 64:
            raise SuggestionValidationError(
                "invalid_payload", "evidence_refs exceeds the bounded intake size"
            )
        created_at = _clean_required(value.get("created_at"), "created_at")
        try:
            parsed_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SuggestionValidationError(
                "invalid_payload", "created_at must be ISO-8601"
            ) from exc
        if parsed_at.tzinfo is None:
            raise SuggestionValidationError(
                "invalid_payload", "created_at must include a timezone"
            )
        return cls(
            suggestion_type=suggestion_type,
            target_claim_id=_clean_required(value.get("target_claim_id"), "target_claim_id"),
            target_current_version=target_version,
            target_topic_key=_clean_required(value.get("target_topic_key"), "target_topic_key"),
            target_scope=_clean_required(value.get("target_scope"), "target_scope"),
            target_principal_id=_clean_required(
                value.get("target_principal_id"), "target_principal_id"
            ),
            target_namespace=_clean_required(
                value.get("target_namespace"), "target_namespace"
            ),
            evidence_refs=refs,
            summary=_clean_required(value.get("summary"), "summary", maximum=500),
            extractor_fingerprint=_clean_required(
                value.get("extractor_fingerprint"), "extractor_fingerprint"
            ),
            job_fingerprint=_clean_required(
                value.get("job_fingerprint"), "job_fingerprint"
            ),
            contract_fingerprint=_clean_required(
                value.get("contract_fingerprint"), "contract_fingerprint"
            ),
            created_at=created_at,
        )


@dataclass(frozen=True)
class AcceptedReviewSuggestion:
    suggestion_id: str
    claim_id: str
    claim_version: int
    suggestion_type: str
    evidence_ids: tuple[str, ...]
    support_independence_keys: tuple[str, ...]
    idempotent: bool
    provider_visible: bool = False


class SelfModelSuggestionIntake:
    """Fail-closed validator and append-only suggestion writer."""

    def __init__(
        self,
        store: SQLiteSelfModelStore,
        evidence_resolver: HistoricalEvidenceResolverProtocol,
    ) -> None:
        self.store = store
        self.evidence_resolver = evidence_resolver

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _resolve_one(self, ref_or_id: str) -> EvidenceRecord:
        stored = self.store.evidence(ref_or_id)
        durable_ref = stored.durable_ref if stored is not None else ref_or_id
        try:
            bundle = self.evidence_resolver.resolve_refs((durable_ref,))
        except EvidenceResolutionError as exc:
            raise SuggestionValidationError(exc.code, str(exc)) from exc
        if len(bundle.records) != 1:
            raise SuggestionValidationError(
                "evidence_unresolved", "evidence ref did not resolve to one canonical root"
            )
        resolved = bundle.records[0]
        if stored is not None and stored != resolved:
            raise SuggestionValidationError(
                "evidence_stale", "stored evidence no longer matches historical authority"
            )
        if not resolved.committed or resolved.canonical_state != "active":
            raise SuggestionValidationError(
                "evidence_invalid", "evidence is not committed canonical history"
            )
        return resolved

    def accept(
        self, candidate: CandidateSuggestionPayload | Mapping[str, Any]
    ) -> AcceptedReviewSuggestion:
        payload = (
            candidate
            if isinstance(candidate, CandidateSuggestionPayload)
            else CandidateSuggestionPayload.from_mapping(candidate)
        )
        with self.store.finalize_transaction():
            claim = self.store.p3c_latest(payload.target_claim_id)
            if claim is None:
                raise SuggestionValidationError("claim_not_found", "target claim does not exist")
            if (
                claim.principal_id != payload.target_principal_id
                or claim.namespace != payload.target_namespace
            ):
                raise SuggestionValidationError(
                    "wrong_owner", "target claim owner or namespace does not match"
                )
            if claim.maturity == "retired":
                raise SuggestionValidationError("claim_retired", "target claim is retired")
            if claim.version != payload.target_current_version:
                raise SuggestionValidationError("stale_claim", "target claim version changed")
            if (
                claim.topic_key != payload.target_topic_key
                or claim.scope != payload.target_scope
            ):
                raise SuggestionValidationError(
                    "target_binding_mismatch", "target topic or scope does not match"
                )

            ordered_records: list[EvidenceRecord] = []
            seen_evidence_ids: set[str] = set()
            for ref_or_id in payload.evidence_refs:
                record = self._resolve_one(ref_or_id)
                if record.evidence_id not in seen_evidence_ids:
                    seen_evidence_ids.add(record.evidence_id)
                    ordered_records.append(record)

            maturity_support = tuple(
                record for record in ordered_records if record.is_maturity_support
            )
            if payload.suggestion_type == "add_support":
                if any(
                    record.source_type in {
                        "maintenance_tool_call",
                        "self_model_operation_receipt",
                    }
                    for record in ordered_records
                ):
                    raise SuggestionValidationError(
                        "maintenance_not_support",
                        "maintenance evidence cannot enter an add-support suggestion",
                    )
                if not maturity_support:
                    raise SuggestionValidationError(
                        "no_maturity_support",
                        "add-support requires real agent-origin maturity evidence",
                    )
            elif not any(record.is_active_contradiction for record in ordered_records):
                raise SuggestionValidationError(
                    "no_active_contradiction",
                    "contradiction suggestion requires active contradiction evidence",
                )

            evidence_ids = tuple(record.evidence_id for record in ordered_records)
            support_keys = tuple(
                sorted({record.independence_key for record in maturity_support})
            )
            identity = {
                "contract_fingerprint": payload.contract_fingerprint,
                "extractor_fingerprint": payload.extractor_fingerprint,
                "job_fingerprint": payload.job_fingerprint,
                "ordered_evidence_ids": evidence_ids,
                "suggestion_contract_version": SUGGESTION_CONTRACT_VERSION,
                "suggestion_type": payload.suggestion_type,
                "target_claim_id": claim.claim_id,
                "target_claim_version": claim.version,
            }
            digest = hashlib.sha256(
                self._canonical_json(identity).encode("utf-8")
            ).hexdigest()
            suggestion_id = f"review-suggestion:{digest[:32]}"
            curator_run_id = "suggestion-job:" + hashlib.sha256(
                self._canonical_json(
                    {
                        "contract": payload.contract_fingerprint,
                        "extractor": payload.extractor_fingerprint,
                        "job": payload.job_fingerprint,
                    }
                ).encode("utf-8")
            ).hexdigest()[:32]

            self.store.append_collected_evidence(ordered_records)
            created = self.store.append_review_suggestion(
                suggestion_id=suggestion_id,
                namespace=claim.namespace,
                suggestion_type=payload.suggestion_type,
                claim_id=claim.claim_id,
                summary=payload.summary,
                curator_run_id=curator_run_id,
                evidence_ids=evidence_ids,
                created_at=payload.created_at,
            )
            return AcceptedReviewSuggestion(
                suggestion_id=suggestion_id,
                claim_id=claim.claim_id,
                claim_version=claim.version,
                suggestion_type=payload.suggestion_type,
                evidence_ids=evidence_ids,
                support_independence_keys=support_keys,
                idempotent=not created,
            )
