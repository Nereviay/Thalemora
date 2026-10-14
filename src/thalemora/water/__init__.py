"""Water 领域核心；显式 SQLite 配置，无生产默认值。"""
from .growth import (
    create_proposal, discover, move_card, read_state, review_proposal, undo_action,
)
from .store import create_node, read_water_map, schema_available, verify_connection
from .verifier import (
    WaterVerificationError, require_valid_artifact, verify_artifact, verify_water_lineage,
)

__all__ = [
    "create_node", "create_proposal", "discover", "move_card", "read_state",
    "review_proposal", "undo_action", "read_water_map", "schema_available",
    "verify_connection", "WaterVerificationError", "require_valid_artifact",
    "verify_artifact", "verify_water_lineage",
]
