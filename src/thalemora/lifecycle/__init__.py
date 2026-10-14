"""独立 Memory 生命周期与可证的显式时间关系。"""
from .activation import ActivationLedger, MemoryActivationError
from .core import (
    apply_dormancy,
    apply_reactivation,
    dormancy_candidates,
    reactivation_candidates,
    record_lifecycle_sunk_epochs,
)
from .scoring import dormancy_score, summarize
from .temporal import read_card_temporal_relations, temporal_context_hint

__all__ = [
    'ActivationLedger', 'MemoryActivationError', 'apply_dormancy',
    'apply_reactivation', 'dormancy_candidates', 'reactivation_candidates',
    'record_lifecycle_sunk_epochs', 'dormancy_score', 'summarize',
    'read_card_temporal_relations', 'temporal_context_hint',
]
