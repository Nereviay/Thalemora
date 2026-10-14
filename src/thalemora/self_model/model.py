"""不可变版本与最小存储合同；不含内存模拟器。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, ContextManager, Protocol
from .contract import OperationRequest, OperationReceipt
from .evidence import EvidenceRecord
MATURITIES = frozenset({"observing", "established", "retired"})

@dataclass(frozen=True)
class P3CClaimVersion:
    """隔离存储中的不可变 claim 版本。"""

    principal_id: str
    namespace: str
    claim_id: str
    claim_kind: str
    domain: str
    topic_key: str
    scope: str
    statement: str
    maturity: str
    version: int
    supersedes: int | None
    evidence_ids: tuple[str, ...]
    support_evidence_ids: tuple[str, ...]
    support_independence_keys: tuple[str, ...]
    contradiction_evidence_ids: tuple[str, ...]
    decision_fingerprint: str
    created_operation_id: str
    created_at: str
    surface: str
    mechanical: bool = False
    status: str = "shadow-only"
    provider_visible: bool = False

    def __post_init__(self) -> None:
        if self.claim_kind != "self_trait":
            raise ValueError("P3C claims must be self_trait")
        if self.maturity not in MATURITIES:
            raise ValueError("unsupported P3C maturity")
        if self.status != "shadow-only" or self.provider_visible:
            raise ValueError("P3C-1 claims must remain shadow-only")
        if self.version == 1 and self.supersedes is not None:
            raise ValueError("version 1 cannot supersede another version")
        if self.version > 1 and self.supersedes != self.version - 1:
            raise ValueError("P3C versions must form a continuous lineage")


class SelfModelStore(Protocol):
    """领域 Gate 使用的最小存储合同。"""

    def stage_transaction(self) -> ContextManager[None]: ...

    def finalize_transaction(self) -> ContextManager[None]: ...

    def stage_operation(self, request: OperationRequest) -> tuple[str, OperationRequest]: ...

    def operation(self, operation_id: str) -> OperationRequest | None: ...

    def operations(self) -> tuple[OperationRequest, ...]: ...

    def append_receipt(self, receipt: OperationReceipt) -> OperationReceipt: ...

    def latest_receipt(self, operation_id: str) -> OperationReceipt | None: ...

    def p3c_latest(self, claim_id: str) -> P3CClaimVersion | None: ...

    def p3c_history(self, claim_id: str) -> tuple[P3CClaimVersion, ...]: ...

    def p3c_latest_claims(self) -> tuple[P3CClaimVersion, ...]: ...

    def find_active_semantic_duplicate(
        self,
        *,
        namespace: str,
        domain: str,
        topic_key: str,
        scope: str,
        statement: str,
    ) -> P3CClaimVersion | None: ...

    def evidence(self, evidence_id: str) -> EvidenceRecord | None: ...

    def append_p3c_version(self, **kwargs: Any) -> P3CClaimVersion: ...
