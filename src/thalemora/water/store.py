"""正式 Water 读接口与合成结构创建；不创建第二套卡片存储。"""
from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3
from typing import Any

from .verifier import verify_water_lineage


@contextmanager
def _reader(database):
    """连接需设置 sqlite3.Row；路径入口严格只读并在返回时关闭。"""
    if isinstance(database, sqlite3.Connection):
        yield database
    else:
        path = Path(database).resolve(strict=True)
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            yield connection


def schema_available(database) -> bool:
    with _reader(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    return {"memory_water_nodes", "memory_water_membership"} <= tables


def read_water_map(database) -> dict[str, Any]:
    """读取活动层级、正式成员、lineage 和历史切换回执。"""
    with _reader(database) as connection:
        nodes = [
            dict(row)
            for row in connection.execute(
                "SELECT node_id,node_type,parent_node_id,title,sort_order,origin,"
                "created_at,updated_at FROM memory_water_nodes "
                "WHERE retired_at IS NULL ORDER BY node_type,sort_order,node_id"
            )
        ]
        memberships = [
            dict(row)
            for row in connection.execute(
                "SELECT m.card_id,m.container_node_id,m.based_on_revision_id,m.origin,"
                "m.assignment_reason,m.created_at,m.updated_at "
                "FROM memory_water_membership m JOIN cards c ON c.id=m.card_id "
                "WHERE c.memory_kind='episodic' "
                "AND NOT EXISTS (SELECT 1 FROM memory_card_lineage l "
                "WHERE l.from_card_id=c.id AND l.retired_at IS NULL) "
                "ORDER BY m.card_id"
            )
        ]
        lineage = [
            dict(row)
            for row in connection.execute(
                "SELECT lineage_id,from_card_id,to_card_id,from_revision_id,"
                "to_revision_id,relation_type,origin,reason,created_at "
                "FROM memory_card_lineage WHERE retired_at IS NULL ORDER BY lineage_id"
            )
        ]
        receipt = connection.execute(
            "SELECT cutover_id,refresh_candidate_id,water_candidate_id,applied_at "
            "FROM memory_rebuild_cutovers WHERE status='complete' "
            "ORDER BY applied_at DESC LIMIT 1"
        ).fetchone()
    counts = {
        "lakes": sum(row["node_type"] == "lake" for row in nodes),
        "streams": sum(row["node_type"] == "stream" for row in nodes),
        "branches": sum(row["node_type"] == "branch" for row in nodes),
        "memberships": len(memberships),
    }
    return {
        "available": True,
        "counts": counts,
        "nodes": nodes,
        "memberships": memberships,
        "lineage": lineage,
        "cutover": dict(receipt) if receipt is not None else None,
    }


def create_node(connection, *, node_id, node_type, title, origin, parent_node_id=None, sort_order=0):
    """显式创建 Lake/Stream/Branch；层级与唯一性由正式 DDL 约束。"""
    connection.execute(
        "INSERT INTO memory_water_nodes("
        "node_id,node_type,parent_node_id,title,sort_order,origin,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,datetime('now','localtime'),datetime('now','localtime'))",
        (node_id, node_type, parent_node_id, title, sort_order, origin),
    )
    return node_id


def verify_connection(connection):
    """将同一 SQLite authority 投影为原 verifier 输入；只读且不修复。"""
    nodes = {row["node_id"]: dict(row) for row in connection.execute(
        "SELECT * FROM memory_water_nodes"
    )}
    plan = {
        "sea": {"conceptual_root": True, "persisted_node": False},
        "lakes": [], "streams": [], "branches": [], "memberships": [],
        "relations": [], "unassigned_card_ids": [],
    }
    for node in nodes.values():
        kind = node["node_type"]
        projected = {"key": node["node_id"], "title": node["title"], "retired_at": node["retired_at"]}
        parent_field = {"lake": "parent_key", "stream": "parent_lake_key", "branch": "parent_stream_key"}[kind]
        projected[parent_field] = node["parent_node_id"]
        plan[{"lake": "lakes", "stream": "streams", "branch": "branches"}[kind]].append(projected)
    placed = set()
    for row in connection.execute("SELECT * FROM memory_water_membership"):
        container = nodes[row["container_node_id"]]
        stream_id = container["node_id"] if container["node_type"] == "stream" else container["parent_node_id"]
        stream = nodes.get(stream_id)
        plan["memberships"].append({
            "card_id": row["card_id"],
            "lake_key": stream["parent_node_id"] if stream else None,
            "stream_key": stream_id,
            "branch_key": container["node_id"] if container["node_type"] == "branch" else None,
        })
        placed.add(row["card_id"])
    for row in connection.execute("SELECT * FROM memory_water_relations WHERE retired_at IS NULL"):
        plan["relations"].append({
            "from_node_key": row["from_node_id"], "to_node_key": row["to_node_id"],
            "relation_type": row["relation_type"],
        })
    lineage = [dict(row) for row in connection.execute(
        "SELECT * FROM memory_card_lineage WHERE retired_at IS NULL"
    )]
    canonical = {row[0] for row in connection.execute(
        "SELECT id FROM cards c WHERE memory_kind='episodic' AND status!='trashed' "
        "AND NOT EXISTS(SELECT 1 FROM memory_card_lineage l "
        "WHERE l.from_card_id=c.id AND l.retired_at IS NULL)"
    )}
    plan["unassigned_card_ids"] = sorted(canonical - placed)
    return verify_water_lineage(plan, lineage, canonical_card_ids=canonical)
