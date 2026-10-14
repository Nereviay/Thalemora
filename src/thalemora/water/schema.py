"""新隔离库的正式 Water/lineage DDL；不执行迁移或切换。

静态来源：r5_production_cutover.py 的 SCHEMA_STATEMENTS。
Memory 基表由调用者用既有 thalemora.memory.schema 初始化。
"""
from .growth import ensure

SCHEMA_STATEMENTS = ('CREATE TABLE memory_water_nodes (\n'
 '        node_id TEXT PRIMARY KEY,\n'
 "        node_type TEXT NOT NULL CHECK(node_type IN ('lake','stream','branch')),\n"
 '        parent_node_id TEXT REFERENCES memory_water_nodes(node_id) ON DELETE RESTRICT,\n'
 '        title TEXT NOT NULL,\n'
 '        sort_order INTEGER NOT NULL DEFAULT 0,\n'
 '        origin TEXT NOT NULL,\n'
 '        created_at TEXT NOT NULL,\n'
 '        updated_at TEXT NOT NULL,\n'
 '        retired_at TEXT,\n'
 '        CHECK(parent_node_id IS NULL OR parent_node_id <> node_id)\n'
 '    )',
 'CREATE INDEX idx_memory_water_nodes_parent ON memory_water_nodes(parent_node_id)',
 'CREATE UNIQUE INDEX uq_memory_water_active_sibling_title\n'
 "       ON memory_water_nodes(node_type,COALESCE(parent_node_id,''),lower(title))\n"
 '       WHERE retired_at IS NULL',
 'CREATE TABLE memory_water_membership (\n'
 '        card_id INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,\n'
 '        container_node_id TEXT NOT NULL REFERENCES memory_water_nodes(node_id) ON DELETE '
 'RESTRICT,\n'
 '        based_on_revision_id INTEGER NOT NULL REFERENCES card_revisions(id) ON DELETE RESTRICT,\n'
 '        origin TEXT NOT NULL,\n'
 "        assignment_reason TEXT NOT NULL DEFAULT '',\n"
 '        created_at TEXT NOT NULL,\n'
 '        updated_at TEXT NOT NULL\n'
 '    )',
 'CREATE INDEX idx_memory_water_membership_container ON memory_water_membership(container_node_id)',
 'CREATE TABLE memory_water_relations (\n'
 '        relation_id TEXT PRIMARY KEY,\n'
 '        from_node_id TEXT NOT NULL REFERENCES memory_water_nodes(node_id) ON DELETE RESTRICT,\n'
 '        to_node_id TEXT NOT NULL REFERENCES memory_water_nodes(node_id) ON DELETE RESTRICT,\n'
 "        relation_type TEXT NOT NULL CHECK(relation_type IN ('related','converges_into')),\n"
 '        origin TEXT NOT NULL,\n'
 "        reason TEXT NOT NULL DEFAULT '',\n"
 '        created_at TEXT NOT NULL,\n'
 '        retired_at TEXT,\n'
 '        CHECK(from_node_id <> to_node_id)\n'
 '    )',
 'CREATE UNIQUE INDEX uq_memory_water_active_relation\n'
 '       ON memory_water_relations(from_node_id,to_node_id,relation_type)\n'
 '       WHERE retired_at IS NULL',
 'CREATE TABLE memory_card_lineage (\n'
 '        lineage_id INTEGER PRIMARY KEY AUTOINCREMENT,\n'
 '        from_card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE RESTRICT,\n'
 '        to_card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE RESTRICT,\n'
 '        from_revision_id INTEGER NOT NULL REFERENCES card_revisions(id) ON DELETE RESTRICT,\n'
 '        to_revision_id INTEGER NOT NULL REFERENCES card_revisions(id) ON DELETE RESTRICT,\n'
 '        relation_type TEXT NOT NULL CHECK(relation_type IN '
 "('split_into','merged_into','superseded_by')),\n"
 '        origin TEXT NOT NULL,\n'
 "        reason TEXT NOT NULL DEFAULT '',\n"
 '        created_at TEXT NOT NULL,\n'
 '        retired_at TEXT,\n'
 '        CHECK(from_card_id <> to_card_id)\n'
 '    )',
 'CREATE UNIQUE INDEX uq_memory_card_lineage_active_edge\n'
 '       ON memory_card_lineage(from_card_id,to_card_id,relation_type)\n'
 '       WHERE retired_at IS NULL',
 'CREATE INDEX idx_memory_card_lineage_from_active\n'
 '       ON memory_card_lineage(from_card_id) WHERE retired_at IS NULL',
 'CREATE INDEX idx_memory_card_lineage_to_active\n'
 '       ON memory_card_lineage(to_card_id) WHERE retired_at IS NULL',
 'CREATE TABLE memory_rebuild_cutovers (\n'
 '        cutover_id TEXT PRIMARY KEY,\n'
 '        refresh_candidate_id TEXT NOT NULL,\n'
 '        water_candidate_id TEXT NOT NULL,\n'
 '        manifest_sha256 TEXT NOT NULL,\n'
 '        backup_dir TEXT NOT NULL,\n'
 "        status TEXT NOT NULL CHECK(status IN ('complete','rolled_back')),\n"
 '        applied_at TEXT NOT NULL,\n'
 '        receipt_json TEXT NOT NULL\n'
 '    )',
 'CREATE TRIGGER trg_water_node_parent_insert BEFORE INSERT ON memory_water_nodes BEGIN\n'
 '       SELECT CASE\n'
 "         WHEN NEW.node_type='lake' AND NEW.parent_node_id IS NOT NULL THEN RAISE(ABORT,'lake "
 "parent')\n"
 "         WHEN NEW.node_type='stream' AND NOT EXISTS(\n"
 '           SELECT 1 FROM memory_water_nodes p WHERE p.node_id=NEW.parent_node_id\n'
 "           AND p.node_type='lake' AND p.retired_at IS NULL) THEN RAISE(ABORT,'stream parent')\n"
 "         WHEN NEW.node_type='branch' AND NOT EXISTS(\n"
 '           SELECT 1 FROM memory_water_nodes p WHERE p.node_id=NEW.parent_node_id\n'
 "           AND p.node_type='stream' AND p.retired_at IS NULL) THEN RAISE(ABORT,'branch parent')\n"
 '       END;\n'
 '    END',
 'CREATE TRIGGER trg_water_membership_insert BEFORE INSERT ON memory_water_membership BEGIN\n'
 '       SELECT CASE WHEN NOT EXISTS(\n'
 '         SELECT 1 FROM cards c JOIN card_revisions r ON r.id=NEW.based_on_revision_id\n'
 '         JOIN memory_water_nodes n ON n.node_id=NEW.container_node_id\n'
 "         WHERE c.id=NEW.card_id AND c.memory_kind='episodic'\n"
 "         AND r.card_id=c.id AND n.node_type IN ('stream','branch') AND n.retired_at IS NULL\n"
 "       ) THEN RAISE(ABORT,'invalid Water membership') END;\n"
 '    END',
 'CREATE TRIGGER trg_memory_lineage_insert BEFORE INSERT ON memory_card_lineage BEGIN\n'
 '       SELECT CASE WHEN NOT EXISTS(\n'
 '         SELECT 1 FROM card_revisions rf,card_revisions rt\n'
 '         WHERE rf.id=NEW.from_revision_id AND rf.card_id=NEW.from_card_id\n'
 '         AND rt.id=NEW.to_revision_id AND rt.card_id=NEW.to_card_id\n'
 "       ) THEN RAISE(ABORT,'invalid lineage revision ownership') END;\n"
 '    END')


def initialize(connection):
    """在调用者事务内建立新 Water 表；现有表冲突原样报错。"""
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    ensure(connection)
