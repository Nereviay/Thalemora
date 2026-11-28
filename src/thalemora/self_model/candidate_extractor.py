"""Provider-neutral Candidate Extractor shadow orchestration.

This module can only turn a bounded extractor response into an append-only
review suggestion.  It has no provider adapter, scheduler, chat wiring, or
claim mutation authority.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Mapping, Protocol, Sequence

from .evidence import EvidenceRecord
from .model import P3CClaimVersion
from .store import SQLiteSelfModelStore
from .suggestions import (
    CandidateSuggestionPayload,
    SelfModelSuggestionIntake,
    SuggestionValidationError,
)


EXTRACTOR_CONTRACT_VERSION = "self-model-candidate-extractor-v1"
_PROMPT_TEMPLATE = (
    "Review only the supplied evidence metadata against the bound observing "
    "self-trait claim. Return one structured review decision; never decide "
    "maturity, visibility, adoption, or create durable references."
)
PROMPT_TEMPLATE_FINGERPRINT = "sha256:" + hashlib.sha256(
    _PROMPT_TEMPLATE.encode("utf-8")
).hexdigest()
FIXTURE_PROFILE_FINGERPRINT = "fixture:self-model-candidate-extractor-v1"
_DECISIONS = frozenset({"relevant_support", "relevant_contradiction", "no_match"})
_REQUIRED_OUTPUT_FIELDS = frozenset({
    "decision",
    "target_claim_id",
    "target_claim_version",
    "target_topic_key",
    "target_scope",
    "target_principal_id",
    "target_namespace",
    "evidence_refs",
    "summary",
})
_OPTIONAL_OUTPUT_FIELDS = frozenset({"uncertainties"})
_FORBIDDEN_OUTPUT_FIELDS = frozenset({
    "maturity",
    "provider_visible",
    "adoption",
    "candidate_id",
    "candidate_kind",
    "claim_id",
    "topic_key",
    "scope",
    "new_claim_id",
    "new_topic_key",
    "new_scope",
    "durable_ref",
})


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(value: Any, field_name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise CandidateExtractionError(
            "malformed_schema", f"{field_name} must be a string"
        )
    clean = value.strip()
    if not clean or len(clean) > maximum:
        raise CandidateExtractionError(
            "malformed_schema", f"{field_name} must contain 1-{maximum} characters"
        )
    return clean


class CandidateExtractionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CandidateExtractorProtocol(Protocol):
    """Injected structured extractor boundary; implementations own no writes."""

    profile_fingerprint: str
    prompt_template_fingerprint: str

    def extract_candidate(self, request: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True)
class CandidateExtractionRequest:
    target_claim: Mapping[str, Any]
    evidence: tuple[Mapping[str, Any], ...]
    extractor_contract_version: str
    model_profile_fingerprint: str
    prompt_template_fingerprint: str
    job_fingerprint: str

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "target_claim": dict(self.target_claim),
            "evidence": [dict(item) for item in self.evidence],
            "extractor_contract_version": self.extractor_contract_version,
            "model_profile_fingerprint": self.model_profile_fingerprint,
            "prompt_template_fingerprint": self.prompt_template_fingerprint,
            "job_fingerprint": self.job_fingerprint,
        }


@dataclass(frozen=True)
class CandidateExtractionResult:
    extraction_run_id: str
    job_fingerprint: str
    decision: str
    suggestion_id: str | None
    validation_status: str
    validation_code: str
    idempotent: bool
    provider_visible: bool = False


@dataclass(frozen=True)
class _ValidatedOutput:
    decision: str
    evidence_refs: tuple[str, ...]
    summary: str
    uncertainties: tuple[str, ...]


class FixtureCandidateExtractor:
    """Deterministic, offline extractor with caller-supplied canned outputs."""

    def __init__(
        self,
        outputs: Any | Sequence[Any],
        *,
        profile_fingerprint: str = FIXTURE_PROFILE_FINGERPRINT,
    ) -> None:
        if isinstance(outputs, (list, tuple)):
            if not outputs:
                raise ValueError("at least one fixture output is required")
            self._outputs = list(outputs)
        else:
            self._outputs = [outputs]
        self.profile_fingerprint = profile_fingerprint
        self.requests: list[Mapping[str, Any]] = []

    def extract_candidate(self, request: Mapping[str, Any]) -> Any:
        self.requests.append(deepcopy(dict(request)))
        index = min(len(self.requests) - 1, len(self._outputs) - 1)
        return deepcopy(self._outputs[index])


class SelfModelCandidateExtractor:
    """Build, validate, audit, and intake one bounded extraction job."""

    def __init__(
        self,
        *,
        store: SQLiteSelfModelStore,
        intake: SelfModelSuggestionIntake,
        extractor: CandidateExtractorProtocol,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.intake = intake
        self.extractor = extractor
        self.clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )

    def _validate_target(self, target: P3CClaimVersion) -> None:
        current = self.store.p3c_latest(target.claim_id)
        if current is None:
            raise CandidateExtractionError("claim_not_found", "target claim does not exist")
        binding = (
            "version",
            "topic_key",
            "scope",
            "principal_id",
            "namespace",
            "statement",
            "claim_kind",
            "maturity",
        )
        if any(getattr(current, field) != getattr(target, field) for field in binding):
            raise CandidateExtractionError(
                "stale_target", "target is not the current stored claim version"
            )
        if target.claim_kind != "self_trait" or target.maturity != "observing":
            raise CandidateExtractionError(
                "target_not_observing", "extractor only reviews observing self traits"
            )

    def _build_request(
        self, target: P3CClaimVersion, evidence: Sequence[EvidenceRecord]
    ) -> CandidateExtractionRequest:
        if not 1 <= len(evidence) <= 64:
            raise CandidateExtractionError(
                "invalid_input", "extractor requires 1-64 evidence records"
            )
        profile = _required_text(
            self.extractor.profile_fingerprint, "model_profile_fingerprint"
        )
        prompt_fingerprint = _required_text(
            getattr(
                self.extractor,
                "prompt_template_fingerprint",
                PROMPT_TEMPLATE_FINGERPRINT,
            ),
            "prompt_template_fingerprint",
        )
        evidence_payload: list[Mapping[str, Any]] = []
        identities: list[Mapping[str, Any]] = []
        seen_ids: set[str] = set()
        seen_refs: set[str] = set()
        for record in evidence:
            if (
                not record.committed
                or record.canonical_state != "active"
                or record.evidence_id in seen_ids
                or record.durable_ref in seen_refs
            ):
                raise CandidateExtractionError(
                    "invalid_input", "evidence must be active and uniquely ordered"
                )
            seen_ids.add(record.evidence_id)
            seen_refs.add(record.durable_ref)
            evidence_payload.append({
                "evidence_id": record.evidence_id,
                "evidence_ref": record.durable_ref,
                "source_type": record.source_type,
                "evidence_kind": record.evidence_kind,
                "actor": record.actor,
                "relation": record.relation,
                "independence_key": record.independence_key,
                "occurred_at": record.occurred_at,
                "source_hash": record.source_hash,
                "resolver_version": record.resolver_version,
            })
            identities.append({
                "evidence_id": record.evidence_id,
                "evidence_ref": record.durable_ref,
                "source_hash": record.source_hash,
                "resolver_version": record.resolver_version,
            })
        target_payload = {
            "claim_id": target.claim_id,
            "version": target.version,
            "topic_key": target.topic_key,
            "scope": target.scope,
            "principal_id": target.principal_id,
            "namespace": target.namespace,
            "statement": target.statement,
        }
        job_fingerprint = _fingerprint({
            "extractor_contract_version": EXTRACTOR_CONTRACT_VERSION,
            "model_profile_fingerprint": profile,
            "ordered_evidence_identities": identities,
            "prompt_template_fingerprint": prompt_fingerprint,
            "target_claim_id": target.claim_id,
            "target_claim_version": target.version,
        })
        return CandidateExtractionRequest(
            target_claim=target_payload,
            evidence=tuple(evidence_payload),
            extractor_contract_version=EXTRACTOR_CONTRACT_VERSION,
            model_profile_fingerprint=profile,
            prompt_template_fingerprint=prompt_fingerprint,
            job_fingerprint=job_fingerprint,
        )

    def prepare_request(
        self, *, target_claim: P3CClaimVersion, evidence: Sequence[EvidenceRecord]
    ) -> CandidateExtractionRequest:
        """Build the stable job identity without making a provider call."""
        self._validate_target(target_claim)
        return self._build_request(target_claim, evidence)

    @staticmethod
    def _serialize_raw(raw: Any) -> tuple[str, str, CandidateExtractionError | None]:
        try:
            raw_json = _canonical_json(raw)
        except (TypeError, ValueError) as exc:
            raw_json = _canonical_json({
                "non_json_output_type": type(raw).__name__,
                "serialization_error": type(exc).__name__,
            })
            error = CandidateExtractionError(
                "malformed_schema", "extractor output must be JSON serializable"
            )
        else:
            error = None
        return raw_json, _fingerprint(json.loads(raw_json)), error

    @staticmethod
    def _validate_output(
        raw: Any,
        *,
        target: P3CClaimVersion,
        allowed_refs: frozenset[str],
    ) -> _ValidatedOutput:
        if not isinstance(raw, Mapping):
            raise CandidateExtractionError(
                "malformed_schema", "extractor output must be an object"
            )
        fields = frozenset(raw)
        forbidden = fields & _FORBIDDEN_OUTPUT_FIELDS
        if forbidden:
            raise CandidateExtractionError(
                "forbidden_field",
                "extractor output contains forbidden authority fields: "
                + ", ".join(sorted(forbidden)),
            )
        if not _REQUIRED_OUTPUT_FIELDS.issubset(fields):
            raise CandidateExtractionError(
                "malformed_schema", "extractor output is missing required fields"
            )
        unexpected = fields - _REQUIRED_OUTPUT_FIELDS - _OPTIONAL_OUTPUT_FIELDS
        if unexpected:
            raise CandidateExtractionError(
                "malformed_schema",
                "extractor output contains unexpected fields: "
                + ", ".join(sorted(unexpected)),
            )
        decision = _required_text(raw["decision"], "decision")
        if decision not in _DECISIONS:
            raise CandidateExtractionError("malformed_schema", "unsupported decision")
        if isinstance(raw["target_claim_version"], bool) or not isinstance(
            raw["target_claim_version"], int
        ):
            raise CandidateExtractionError(
                "malformed_schema", "target_claim_version must be an integer"
            )
        expected_target = {
            "target_claim_id": target.claim_id,
            "target_claim_version": target.version,
            "target_topic_key": target.topic_key,
            "target_scope": target.scope,
            "target_principal_id": target.principal_id,
            "target_namespace": target.namespace,
        }
        if any(raw[field] != value for field, value in expected_target.items()):
            raise CandidateExtractionError(
                "target_binding_mismatch", "extractor output drifted from its target"
            )
        refs_value = raw["evidence_refs"]
        if not isinstance(refs_value, list) or len(refs_value) > 64:
            raise CandidateExtractionError(
                "malformed_schema", "evidence_refs must be a bounded JSON array"
            )
        refs = tuple(_required_text(ref, "evidence_ref") for ref in refs_value)
        if len(set(refs)) != len(refs):
            raise CandidateExtractionError(
                "malformed_schema", "evidence_refs must not contain duplicates"
            )
        if not set(refs).issubset(allowed_refs):
            raise CandidateExtractionError(
                "evidence_ref_out_of_scope", "extractor invented an evidence ref"
            )
        if decision != "no_match" and not refs:
            raise CandidateExtractionError(
                "malformed_schema", "relevant decisions require evidence refs"
            )
        summary = _required_text(raw["summary"], "summary", maximum=500)
        uncertainties_value = raw.get("uncertainties", [])
        if not isinstance(uncertainties_value, list) or len(uncertainties_value) > 8:
            raise CandidateExtractionError(
                "malformed_schema", "uncertainties must be a bounded JSON array"
            )
        uncertainties = tuple(
            _required_text(value, "uncertainty", maximum=256)
            for value in uncertainties_value
        )
        return _ValidatedOutput(decision, refs, summary, uncertainties)

    @staticmethod
    def _result_from_row(row: Mapping[str, Any], *, idempotent: bool) -> CandidateExtractionResult:
        return CandidateExtractionResult(
            extraction_run_id=str(row["extraction_run_id"]),
            job_fingerprint=str(row["job_fingerprint"]),
            decision=str(row["decision"]),
            suggestion_id=row["resulting_suggestion_id"],
            validation_status=str(row["validation_status"]),
            validation_code=str(row["validation_code"]),
            idempotent=idempotent,
        )

    def _append_audit(
        self,
        *,
        request: CandidateExtractionRequest,
        target: P3CClaimVersion,
        raw_json: str,
        raw_hash: str,
        decision: str | None,
        status: str,
        code: str,
        suggestion_id: str | None,
        created_at: str,
    ) -> str:
        run_id = "extraction-run:" + _fingerprint({
            "job_fingerprint": request.job_fingerprint,
            "raw_output_hash": raw_hash,
            "validation_code": code,
            "validation_status": status,
        }).removeprefix("sha256:")[:32]
        self.store.append_extraction_run(
            extraction_run_id=run_id,
            job_fingerprint=request.job_fingerprint,
            target_claim_id=target.claim_id,
            target_claim_version=target.version,
            extractor_contract_version=request.extractor_contract_version,
            profile_fingerprint=request.model_profile_fingerprint,
            prompt_template_fingerprint=request.prompt_template_fingerprint,
            ordered_input_refs_hash=_fingerprint(
                [item["evidence_ref"] for item in request.evidence]
            ),
            raw_output_json=raw_json,
            raw_output_hash=raw_hash,
            decision=decision,
            validation_status=status,
            validation_code=code,
            resulting_suggestion_id=suggestion_id,
            created_at=created_at,
        )
        return run_id

    def run(
        self,
        *,
        target_claim: P3CClaimVersion,
        evidence: Sequence[EvidenceRecord],
    ) -> CandidateExtractionResult:
        request = self.prepare_request(
            target_claim=target_claim,
            evidence=evidence,
        )
        previous = self.store.successful_extraction_run(request.job_fingerprint)
        if previous is not None:
            return self._result_from_row(previous, idempotent=True)
        raw = self.extractor.extract_candidate(request.as_mapping())
        raw_json, raw_hash, serialization_error = self._serialize_raw(raw)
        created_at = self.clock()
        if serialization_error is not None:
            self._append_audit(
                request=request,
                target=target_claim,
                raw_json=raw_json,
                raw_hash=raw_hash,
                decision=None,
                status="rejected",
                code=serialization_error.code,
                suggestion_id=None,
                created_at=created_at,
            )
            raise serialization_error
        try:
            validated = self._validate_output(
                raw,
                target=target_claim,
                allowed_refs=frozenset(item["evidence_ref"] for item in request.evidence),
            )
        except CandidateExtractionError as exc:
            decision = raw.get("decision") if isinstance(raw, Mapping) else None
            self._append_audit(
                request=request,
                target=target_claim,
                raw_json=raw_json,
                raw_hash=raw_hash,
                decision=decision if isinstance(decision, str) else None,
                status="rejected",
                code=exc.code,
                suggestion_id=None,
                created_at=created_at,
            )
            raise

        previous = self.store.successful_extraction_run(request.job_fingerprint)
        if previous is not None:
            if previous["raw_output_hash"] == raw_hash:
                return self._result_from_row(previous, idempotent=True)
            conflict = CandidateExtractionError(
                "successful_job_output_conflict",
                "a successful job fingerprint cannot be reused with different output",
            )
            self._append_audit(
                request=request,
                target=target_claim,
                raw_json=raw_json,
                raw_hash=raw_hash,
                decision=validated.decision,
                status="rejected",
                code=conflict.code,
                suggestion_id=None,
                created_at=created_at,
            )
            raise conflict

        if validated.decision == "no_match":
            replay: CandidateExtractionResult | None = None
            conflict: CandidateExtractionError | None = None
            with self.store.finalize_transaction():
                previous = self.store.successful_extraction_run(
                    request.job_fingerprint
                )
                if previous is None:
                    run_id = self._append_audit(
                        request=request,
                        target=target_claim,
                        raw_json=raw_json,
                        raw_hash=raw_hash,
                        decision=validated.decision,
                        status="accepted",
                        code="no_match",
                        suggestion_id=None,
                        created_at=created_at,
                    )
                elif previous["raw_output_hash"] == raw_hash:
                    replay = self._result_from_row(previous, idempotent=True)
                else:
                    conflict = CandidateExtractionError(
                        "successful_job_output_conflict",
                        "a successful job fingerprint cannot be reused with different output",
                    )
                    self._append_audit(
                        request=request,
                        target=target_claim,
                        raw_json=raw_json,
                        raw_hash=raw_hash,
                        decision=validated.decision,
                        status="rejected",
                        code=conflict.code,
                        suggestion_id=None,
                        created_at=created_at,
                    )
            if conflict is not None:
                raise conflict
            if replay is not None:
                return replay
            return CandidateExtractionResult(
                extraction_run_id=run_id,
                job_fingerprint=request.job_fingerprint,
                decision=validated.decision,
                suggestion_id=None,
                validation_status="accepted",
                validation_code="no_match",
                idempotent=False,
            )

        suggestion_type = (
            "add_support"
            if validated.decision == "relevant_support"
            else "contradiction"
        )
        payload = CandidateSuggestionPayload(
            suggestion_type=suggestion_type,
            target_claim_id=target_claim.claim_id,
            target_current_version=target_claim.version,
            target_topic_key=target_claim.topic_key,
            target_scope=target_claim.scope,
            target_principal_id=target_claim.principal_id,
            target_namespace=target_claim.namespace,
            evidence_refs=validated.evidence_refs,
            summary=validated.summary,
            extractor_fingerprint=request.model_profile_fingerprint,
            job_fingerprint=request.job_fingerprint,
            contract_fingerprint=_fingerprint({
                "extractor_contract_version": request.extractor_contract_version,
                "prompt_template_fingerprint": request.prompt_template_fingerprint,
            }),
            created_at=created_at,
        )
        replay = None
        conflict = None
        try:
            with self.store.finalize_transaction():
                previous = self.store.successful_extraction_run(
                    request.job_fingerprint
                )
                if previous is None:
                    accepted = self.intake.accept(payload)
                    run_id = self._append_audit(
                        request=request,
                        target=target_claim,
                        raw_json=raw_json,
                        raw_hash=raw_hash,
                        decision=validated.decision,
                        status="accepted",
                        code="suggestion_created",
                        suggestion_id=accepted.suggestion_id,
                        created_at=created_at,
                    )
                elif previous["raw_output_hash"] == raw_hash:
                    replay = self._result_from_row(previous, idempotent=True)
                else:
                    conflict = CandidateExtractionError(
                        "successful_job_output_conflict",
                        "a successful job fingerprint cannot be reused with different output",
                    )
                    self._append_audit(
                        request=request,
                        target=target_claim,
                        raw_json=raw_json,
                        raw_hash=raw_hash,
                        decision=validated.decision,
                        status="rejected",
                        code=conflict.code,
                        suggestion_id=None,
                        created_at=created_at,
                    )
        except SuggestionValidationError as exc:
            self._append_audit(
                request=request,
                target=target_claim,
                raw_json=raw_json,
                raw_hash=raw_hash,
                decision=validated.decision,
                status="rejected",
                code=exc.code,
                suggestion_id=None,
                created_at=created_at,
            )
            raise CandidateExtractionError(exc.code, str(exc)) from exc
        if conflict is not None:
            raise conflict
        if replay is not None:
            return replay
        return CandidateExtractionResult(
            extraction_run_id=run_id,
            job_fingerprint=request.job_fingerprint,
            decision=validated.decision,
            suggestion_id=accepted.suggestion_id,
            validation_status="accepted",
            validation_code="suggestion_created",
            idempotent=False,
        )
