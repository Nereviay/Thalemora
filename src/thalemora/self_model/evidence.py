"""独立证据合同与纯解析规则。

VerifiedEvidenceSource 由调用方提供，并已经核验指定 principal 的 Agent 作者、
canonical generation、工具事件与来源内容。这里不连接、模拟或发现真实 Runtime。
哈希保留来源身份合同；哈希相符本身不能证明作者或运行态真实性。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Protocol

from .contract import InvocationContext, SemanticIntent


RESOLVER_VERSION = "p3c-evidence-v1"
AGENT_EVIDENCE_KINDS = frozenset({"self_statement", "behavior", "contemplation"})
MATURITY_SUPPORT_SOURCE_TYPES = frozenset({
    "canonical_reply",
    "canonical_tool_call",
    "tool_receipt",
    "contemplation",
})
SELF_MODEL_MUTATION_TOOLS = frozenset({
    "self_model_observe",
    "self_model_revise",
    "self_model_retire",
})
CONTRADICTION_SOURCE_TYPES = frozenset({
    "canonical_reply",
    "canonical_tool_call",
    "tool_receipt",
    "contemplation",
    "shared_event",
    "user_correction",
})
SOURCE_TYPES = frozenset({
    "canonical_reply",
    "canonical_tool_call",
    "tool_receipt",
    "self_model_operation_receipt",
    "contemplation",
    "shared_event",
    "user_correction",
    "user_preference",
    "memory_index",
    "maintenance_tool_call",
})
ABORTED_GENERATION_STATES = frozenset({
    "cancelled", "failed", "superseded", "abandoned", "draft"
})


class EvidenceResolutionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GenerationState:
    generation_id: str
    status: str
    active_canonical: bool
    canonical_branch_id: str
    canonical_tool_call_event_ids: tuple[str, ...] = ()

    @property
    def finalizable(self) -> bool:
        return self.status in {"completed", "succeeded", "terminal"} and self.active_canonical

    @property
    def pending(self) -> bool:
        return self.status in {"running", "pending"}


@dataclass(frozen=True)
class AuthoritativeRecord:
    """调用方已经核验、按 principal 隔离的不可变权威记录。"""

    durable_ref: str
    source_type: str
    actor: str
    root_event_id: str
    generation_id: str | None
    canonical_state: str
    outcome: str
    committed: bool
    occurred_at: str
    surface: str
    relation_hint: str = "supports"
    content: str = ""
    duplicate_suppressed: bool = False
    tool_name: str | None = None
    backref: str | None = None
    source_hash: str | None = None

    def __post_init__(self) -> None:
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"unsupported source_type: {self.source_type}")
        if not self.durable_ref or ":" not in self.durable_ref:
            raise ValueError("durable_ref must be a compact namespaced reference")
        if self.relation_hint not in {"supports", "contradicts", "context"}:
            raise ValueError("unsupported relation_hint")
        expected_hash = self.authoritative_hash()
        if self.source_hash is None:
            object.__setattr__(self, "source_hash", expected_hash)
        elif not self.source_hash.startswith("sha256:"):
            raise ValueError("source_hash must be a SHA-256 identifier")

    def authoritative_hash(self) -> str:
        canonical = json.dumps(
            {
                "actor": self.actor,
                "backref": self.backref,
                "canonical_state": self.canonical_state,
                "committed": self.committed,
                "content": self.content,
                "durable_ref": self.durable_ref,
                "duplicate_suppressed": self.duplicate_suppressed,
                "generation_id": self.generation_id,
                "occurred_at": self.occurred_at,
                "outcome": self.outcome,
                "relation_hint": self.relation_hint,
                "root_event_id": self.root_event_id,
                "source_type": self.source_type,
                "surface": self.surface,
                "tool_name": self.tool_name,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    durable_ref: str
    source_type: str
    evidence_kind: str
    actor: str
    relation: str
    root_event_id: str
    generation_id: str | None
    independence_key: str
    canonical_state: str
    outcome: str
    committed: bool
    eligible_for_trait_support: bool
    eligible_for_contradiction: bool
    occurred_at: str
    source_hash: str
    resolver_version: str
    surface: str

    @property
    def is_agent_support(self) -> bool:
        """Compatibility name for evidence that can authorize a mutation."""
        return (
            self.relation == "supports"
            and self.eligible_for_trait_support
            and self.evidence_kind in AGENT_EVIDENCE_KINDS
        )

    @property
    def is_mutation_authority(self) -> bool:
        return self.is_agent_support

    @property
    def is_maturity_support(self) -> bool:
        """Whether this real agent-origin event may mature a self trait."""
        return (
            self.is_mutation_authority
            and self.source_type in MATURITY_SUPPORT_SOURCE_TYPES
        )

    @property
    def is_active_contradiction(self) -> bool:
        return self.eligible_for_contradiction


@dataclass(frozen=True)
class EvidenceBundle:
    records: tuple[EvidenceRecord, ...]

    @property
    def agent_support(self) -> tuple[EvidenceRecord, ...]:
        """Compatibility projection for mutation-authority evidence."""
        return self.mutation_authority

    @property
    def mutation_authority(self) -> tuple[EvidenceRecord, ...]:
        return tuple(record for record in self.records if record.is_agent_support)

    @property
    def maturity_support(self) -> tuple[EvidenceRecord, ...]:
        return tuple(record for record in self.records if record.is_maturity_support)

    @property
    def support_independence_keys(self) -> tuple[str, ...]:
        return tuple(
            sorted({record.independence_key for record in self.maturity_support})
        )

    @property
    def contradictions(self) -> tuple[EvidenceRecord, ...]:
        return tuple(record for record in self.records if record.is_active_contradiction)


class VerifiedEvidenceSource(Protocol):
    """所有读取均已限定 principal；不能只凭 actor 字符串宣称 Agent 作者。"""

    def generation_state(self, generation_id: str, principal_id: str) -> GenerationState | None: ...
    def refs_for_generation(self, generation_id: str, principal_id: str) -> tuple[str, ...]: ...
    def record(self, durable_ref: str, principal_id: str) -> AuthoritativeRecord | None: ...


class EvidenceResolver:
    """解析已核验来源；失效或不独立来源不能推动成熟度。"""

    def __init__(self, source: VerifiedEvidenceSource, *, principal_id: str):
        self.source = source
        self.principal_id = principal_id

    def generation_state(self, generation_id: str) -> GenerationState | None:
        return self.source.generation_state(generation_id, self.principal_id)

    def resolve(self, context: InvocationContext, intent: SemanticIntent) -> EvidenceBundle:
        if context.principal_id != self.principal_id:
            raise EvidenceResolutionError("principal_mismatch", "resolver belongs to another principal")
        generation = self.generation_state(context.generation_id)
        if (generation is None or not generation.finalizable
                or generation.canonical_branch_id != context.canonical_branch_id
                or context.canonical_tool_call_event_id not in generation.canonical_tool_call_event_ids):
            raise EvidenceResolutionError("durable_ref_unresolved", "invocation is not canonical Agent authority")
        return self.resolve_refs(self.source.refs_for_generation(context.generation_id, self.principal_id))

    def resolve_refs(self, refs: Iterable[str]) -> EvidenceBundle:
        resolved_by_root: dict[tuple[str, str], EvidenceRecord] = {}
        for durable_ref in refs:
            root = self._resolve_root(str(durable_ref), seen=frozenset())
            record = self._to_evidence(root)
            # A Memory/index projection resolves back to the root record and is
            # never a second evidence item.
            resolved_by_root[(record.durable_ref, record.source_hash)] = record
        records = tuple(sorted(resolved_by_root.values(), key=lambda item: item.durable_ref))
        return EvidenceBundle(records=records)

    def _resolve_root(self, durable_ref: str, *, seen: frozenset[str]) -> AuthoritativeRecord:
        if durable_ref in seen:
            raise EvidenceResolutionError("durable_ref_unresolved", "derived evidence contains a cycle")
        record = self.source.record(durable_ref, self.principal_id)
        if record is None:
            raise EvidenceResolutionError("durable_ref_unresolved", f"unresolved durable ref: {durable_ref}")
        if record.source_hash != record.authoritative_hash():
            raise EvidenceResolutionError(
                "durable_ref_unresolved",
                "source hash does not match authoritative record",
            )
        if record.source_type != "memory_index":
            return record
        if not record.backref:
            raise EvidenceResolutionError("durable_ref_unresolved", "Memory-derived record lacks a durable backref")
        return self._resolve_root(record.backref, seen=seen | {durable_ref})

    def _to_evidence(self, source: AuthoritativeRecord) -> EvidenceRecord:
        if source.source_hash != source.authoritative_hash():
            raise EvidenceResolutionError(
                "durable_ref_unresolved",
                "source hash does not match authoritative record",
            )
        if source.generation_id:
            independence_key = source.generation_id
        else:
            independence_key = f"event-root:{source.root_event_id}"

        generation = (
            self.generation_state(source.generation_id)
            if source.generation_id
            else None
        )
        if source.generation_id and (generation is None or not generation.finalizable):
            raise EvidenceResolutionError("durable_ref_unresolved", "evidence generation is not canonical and completed")
        canonical_self_model_tool_call = (
            source.source_type == "canonical_tool_call"
            and source.tool_name in SELF_MODEL_MUTATION_TOOLS
            and generation is not None
            and source.root_event_id in generation.canonical_tool_call_event_ids
        )
        kind_by_source = {
            "canonical_reply": "self_statement",
            "canonical_tool_call": (
                "self_statement" if canonical_self_model_tool_call else "tool_call"
            ),
            "tool_receipt": "behavior",
            "self_model_operation_receipt": "behavior",
            "contemplation": "contemplation",
            "shared_event": "shared_event",
            "user_correction": "user_correction",
            "user_preference": "user_preference",
            "maintenance_tool_call": "self_statement",
        }
        evidence_kind = kind_by_source[source.source_type]
        relation = source.relation_hint
        if source.source_type == "user_preference":
            relation = "context"
        elif source.source_type == "user_correction":
            relation = "contradicts" if relation == "contradicts" else "context"

        content_marker = source.content.strip() == "NO_RESPONSE"
        active = (source.committed and source.canonical_state == "active"
                  and source.outcome not in {"failed", "cancelled", "aborted"})
        successful_behavior = source.source_type != "tool_receipt" or source.outcome == "succeeded"
        is_self_model_receipt = (
            source.source_type == "self_model_operation_receipt"
            or (
                source.source_type == "tool_receipt"
                and bool(source.tool_name)
                and source.tool_name.startswith("self_model_")
            )
        )
        eligible = (
            active
            and relation == "supports"
            and evidence_kind in AGENT_EVIDENCE_KINDS
            and source.actor in {"assistant", "tool"}
            and successful_behavior
            and not content_marker
            and not source.duplicate_suppressed
            and not is_self_model_receipt
        )
        if source.source_type == "contemplation" and source.actor != "assistant":
            eligible = False
        eligible_for_contradiction = (
            active
            and relation == "contradicts"
            and source.source_type in CONTRADICTION_SOURCE_TYPES
        )

        digest_seed = f"{source.durable_ref}\x1f{source.source_hash}\x1f{RESOLVER_VERSION}"
        evidence_id = "evidence:" + hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()[:32]
        return EvidenceRecord(
            evidence_id=evidence_id,
            durable_ref=source.durable_ref,
            source_type=source.source_type,
            evidence_kind=evidence_kind,
            actor=source.actor,
            relation=relation,
            root_event_id=source.root_event_id,
            generation_id=source.generation_id,
            independence_key=independence_key,
            canonical_state=source.canonical_state,
            outcome=source.outcome,
            committed=source.committed,
            eligible_for_trait_support=eligible,
            eligible_for_contradiction=eligible_for_contradiction,
            occurred_at=source.occurred_at,
            source_hash=str(source.source_hash),
            resolver_version=RESOLVER_VERSION,
            surface=source.surface,
        )
