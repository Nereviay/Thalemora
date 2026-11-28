"""Read-only candidate renderer for future Self Model adoption.

P3D-4A deliberately stops before Provider delivery.  It renders the exact
owner-scoped text that *could* be adopted later, plus management metadata for
inspection.  It never mutates a claim, store, adoption snapshot, or context.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from typing import Any, Callable, Iterable, Mapping

from .contract import namespace_for_principal
from .evidence import EvidenceRecord


RENDERER_VERSION = "p3d4a-self-model-preview-v2-bounded"
LEGACY_RENDERER_VERSION = "p3d4a-self-model-preview-v1"
TOKEN_MEASURE = "utf8_bytes_upper_bound_v1"
HEADER = "【你的长期自我记忆】"


@dataclass(frozen=True)
class CoreSelfBudget:
    """Hard limits for one newly-created Core Self projection.

    UTF-8 bytes are used as a provider-neutral conservative token upper bound:
    no provider tokenizer call or mutable provider state is needed while an
    epoch is being frozen.
    """

    max_claims: int = 10
    max_chars: int = 800
    max_tokens: int = 512
    token_measure: str = TOKEN_MEASURE

    def __post_init__(self) -> None:
        for name in ("max_claims", "max_chars", "max_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.token_measure != TOKEN_MEASURE:
            raise ValueError("unsupported Core Self token measure")

    def payload(self) -> dict[str, Any]:
        return {
            "max_claims": self.max_claims,
            "max_chars": self.max_chars,
            "max_tokens": self.max_tokens,
            "token_measure": self.token_measure,
        }


DEFAULT_CORE_SELF_BUDGET = CoreSelfBudget()


def measured_provider_tokens(text: str) -> int:
    """Return the deterministic conservative provider-token upper bound."""
    return len(str(text).encode("utf-8"))


@dataclass(frozen=True)
class AdoptionExclusion:
    claim_id: str
    version: int
    reason: str

    def payload(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "version": self.version,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AdoptionPreviewItem:
    claim_id: str
    version: int
    statement: str
    domain: str
    topic_key: str
    scope: str

    def management_payload(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "version": self.version,
            "statement": self.statement,
            "domain": self.domain,
            "topic_key": self.topic_key,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class AdoptionPreview:
    principal_id: str
    namespace: str
    provider_text: str
    sha256: str
    items: tuple[AdoptionPreviewItem, ...]
    excluded_counts: tuple[tuple[str, int], ...]
    exclusions: tuple[AdoptionExclusion, ...]
    budget: CoreSelfBudget
    rendered_chars: int
    rendered_tokens: int

    @property
    def has_candidate(self) -> bool:
        return bool(self.items)

    def payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "module": "self_model",
            "preview_only": True,
            "renderer_version": RENDERER_VERSION,
            "owner": {
                "principal_id": self.principal_id,
                "namespace": self.namespace,
                "actor": "assistant",
            },
            "provider_delivery": {
                "given": False,
                "label": "仅预览，尚未给模型",
            },
            "candidate": {
                "has_candidate": self.has_candidate,
                "provider_text": self.provider_text,
                "sha256": self.sha256,
                "items": [item.management_payload() for item in self.items],
                "budget": self.budget.payload(),
                "rendered_chars": self.rendered_chars,
                "rendered_tokens": self.rendered_tokens,
            },
            "excluded_counts": dict(self.excluded_counts),
            "exclusions": [item.payload() for item in self.exclusions],
        }


def adoption_eligibility_reason(
    claim: Any,
    principal_id: str,
    namespace: str,
    *,
    evidence_reader: Callable[[str], EvidenceRecord | None] | None,
) -> str | None:
    if str(getattr(claim, "principal_id", "") or "") != principal_id:
        return "other_principal"
    if str(getattr(claim, "namespace", "") or "") != namespace:
        return "other_namespace"
    if str(getattr(claim, "claim_kind", "") or "") != "self_trait":
        return "not_self_trait"
    maturity = str(getattr(claim, "maturity", "") or "")
    if maturity != "established":
        return maturity if maturity in {"observing", "retired"} else "not_established"
    if str(getattr(claim, "status", "") or "") != "shadow-only":
        return "invalid_status"
    if bool(getattr(claim, "provider_visible", False)):
        return "already_provider_visible"
    if evidence_reader is None:
        return "maturity_support_unverifiable"
    support_records = []
    for evidence_id in tuple(getattr(claim, "support_evidence_ids", ()) or ()):
        record = evidence_reader(str(evidence_id))
        if record is not None and record.is_maturity_support:
            support_records.append(record)
    support_keys = {record.independence_key for record in support_records}
    if len(support_keys) < 2:
        return "insufficient_independent_support"
    contradictions = tuple(getattr(claim, "contradiction_evidence_ids", ()) or ())
    if contradictions:
        return "has_contradiction"
    statement = str(getattr(claim, "statement", "") or "").strip()
    if not statement:
        return "empty_statement"
    return None


def _sort_key(claim: Any) -> tuple[str, str, str, str]:
    return (
        str(getattr(claim, "domain", "") or ""),
        str(getattr(claim, "topic_key", "") or ""),
        str(getattr(claim, "scope", "") or ""),
        str(getattr(claim, "claim_id", "") or ""),
    )


def _render_items(items: Iterable[AdoptionPreviewItem]) -> str:
    rows = tuple(items)
    if not rows:
        return ""
    return HEADER + "\n" + "\n".join(f"- {item.statement}" for item in rows)


def _exclusion(claim: Any, reason: str) -> AdoptionExclusion:
    try:
        version = int(getattr(claim, "version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    return AdoptionExclusion(
        claim_id=str(getattr(claim, "claim_id", "") or ""),
        version=version,
        reason=str(reason),
    )


def build_adoption_preview(
    claims: Iterable[Any],
    *,
    principal_id: str,
    evidence_reader: Callable[[str], EvidenceRecord | None] | None = None,
    budget: CoreSelfBudget = DEFAULT_CORE_SELF_BUDGET,
) -> AdoptionPreview:
    """Build a deterministic, read-only future-adoption candidate."""
    if not isinstance(budget, CoreSelfBudget):
        raise TypeError("budget must be CoreSelfBudget")
    namespace = namespace_for_principal(principal_id)
    accepted = []
    excluded: dict[str, int] = {}
    exclusions: list[AdoptionExclusion] = []

    for claim in tuple(claims):
        reason = adoption_eligibility_reason(
            claim,
            principal_id,
            namespace,
            evidence_reader=evidence_reader,
        )
        if reason is not None:
            excluded[reason] = excluded.get(reason, 0) + 1
            exclusions.append(_exclusion(claim, reason))
            continue
        accepted.append(claim)

    accepted.sort(key=_sort_key)
    selected: list[AdoptionPreviewItem] = []
    for claim in accepted:
        item = AdoptionPreviewItem(
            claim_id=str(claim.claim_id),
            version=int(claim.version),
            statement=str(claim.statement),
            domain=str(claim.domain),
            topic_key=str(claim.topic_key),
            scope=str(claim.scope),
        )
        if len(selected) >= budget.max_claims:
            reason = "budget_max_claims"
        else:
            candidate_text = _render_items((*selected, item))
            if len(candidate_text) > budget.max_chars:
                reason = "budget_max_chars"
            elif measured_provider_tokens(candidate_text) > budget.max_tokens:
                reason = "budget_max_tokens"
            else:
                selected.append(item)
                continue
        excluded[reason] = excluded.get(reason, 0) + 1
        exclusions.append(_exclusion(claim, reason))

    items = tuple(selected)
    provider_text = _render_items(items)

    digest = hashlib.sha256(provider_text.encode("utf-8")).hexdigest()
    return AdoptionPreview(
        principal_id=principal_id,
        namespace=namespace,
        provider_text=provider_text,
        sha256=digest,
        items=items,
        excluded_counts=tuple(sorted(excluded.items())),
        exclusions=tuple(
            sorted(
                exclusions,
                key=lambda item: (item.claim_id, item.version, item.reason),
            )
        ),
        budget=budget,
        rendered_chars=len(provider_text),
        rendered_tokens=measured_provider_tokens(provider_text),
    )


def build_store_adoption_preview(
    store: Any,
    *,
    principal_id: str,
    budget: CoreSelfBudget = DEFAULT_CORE_SELF_BUDGET,
) -> AdoptionPreview:
    """Read latest claims only; never writes adoption state."""
    return build_adoption_preview(
        store.p3c_latest_claims() if store is not None else (),
        principal_id=principal_id,
        evidence_reader=store.evidence if store is not None else None,
        budget=budget,
    )
