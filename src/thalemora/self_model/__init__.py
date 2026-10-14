"""Self Model 独立领域核心；导入不打开数据库或启动 Runtime。"""
from .contract import (
    ContractValidationError, InvocationContext, OperationReceipt, OperationRequest,
    SemanticIntent, TOOL_SCHEMAS, build_semantic_intent, namespace_for_principal,
)
from .core import SelfModelCore
from .epoch import EpochFreezer, FrozenEpoch, FrozenItem
from .evidence import (
    AuthoritativeRecord, EvidenceBundle, EvidenceRecord, EvidenceResolutionError,
    EvidenceResolver, GenerationState, VerifiedEvidenceSource,
)
from .gate import DeterministicSelfModelGate
from .model import P3CClaimVersion
from .store import SQLiteSelfModelStore

__all__ = [
    "AuthoritativeRecord", "ContractValidationError", "DeterministicSelfModelGate",
    "EpochFreezer", "EvidenceBundle", "EvidenceRecord", "EvidenceResolutionError",
    "EvidenceResolver", "FrozenEpoch", "FrozenItem", "GenerationState", "InvocationContext",
    "OperationReceipt", "OperationRequest", "P3CClaimVersion", "SQLiteSelfModelStore",
    "SelfModelCore", "SemanticIntent", "TOOL_SCHEMAS", "VerifiedEvidenceSource",
    "build_semantic_intent", "namespace_for_principal",
]
