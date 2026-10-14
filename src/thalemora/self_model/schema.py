"""静态来源 DDL 与 append-only 表清单；仅初始化调用方隔离连接。"""

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS self_model_namespaces (
    namespace TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_requests (
    request_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    tool_name TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    tool_call_event_id TEXT NOT NULL,
    canonical_branch_id TEXT NOT NULL,
    context_epoch_id TEXT NOT NULL,
    surface TEXT NOT NULL CHECK (surface IN ('normal', 'contact', 'find_activity', 'self_maintenance')),
    invoked_at TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    FOREIGN KEY (namespace) REFERENCES self_model_namespaces(namespace)
);

CREATE TABLE IF NOT EXISTS operation_receipt_events (
    store_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_event_id TEXT NOT NULL UNIQUE,
    receipt_fingerprint TEXT NOT NULL UNIQUE,
    operation_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    code TEXT NOT NULL,
    claim_id TEXT,
    claim_version INTEGER,
    maturity TEXT,
    provider_visible INTEGER NOT NULL DEFAULT 0 CHECK (provider_visible = 0),
    eligible_for_epoch TEXT,
    detail TEXT,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_lineages (
    claim_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    claim_kind TEXT NOT NULL CHECK (claim_kind = 'self_trait'),
    domain TEXT NOT NULL,
    topic_key TEXT NOT NULL,
    scope TEXT NOT NULL,
    created_operation_id TEXT NOT NULL,
    related_retired_claim_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (namespace) REFERENCES self_model_namespaces(namespace),
    FOREIGN KEY (created_operation_id) REFERENCES operation_requests(operation_id)
);

CREATE TABLE IF NOT EXISTS claim_versions (
    claim_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    supersedes_version INTEGER,
    statement TEXT NOT NULL,
    statement_hash TEXT NOT NULL,
    maturity TEXT NOT NULL CHECK (maturity IN ('observing', 'established', 'retired')),
    gate_version TEXT NOT NULL,
    decision_fingerprint TEXT NOT NULL UNIQUE,
    created_operation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    surface TEXT NOT NULL CHECK (surface IN ('normal', 'contact', 'find_activity', 'self_maintenance')),
    mechanical INTEGER NOT NULL DEFAULT 0 CHECK (mechanical IN (0, 1)),
    created_during_epoch_id TEXT NOT NULL,
    eligibility_rule TEXT NOT NULL CHECK (eligibility_rule = 'next_boundary_only'),
    PRIMARY KEY (claim_id, version),
    FOREIGN KEY (claim_id) REFERENCES claim_lineages(claim_id),
    FOREIGN KEY (created_operation_id) REFERENCES operation_requests(operation_id)
);

CREATE TABLE IF NOT EXISTS evidence_records (
    evidence_id TEXT PRIMARY KEY,
    durable_ref TEXT NOT NULL,
    source_type TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('supports', 'contradicts', 'context')),
    root_event_id TEXT NOT NULL,
    generation_id TEXT,
    independence_key TEXT NOT NULL,
    canonical_state TEXT NOT NULL,
    outcome TEXT NOT NULL,
    committed INTEGER NOT NULL CHECK (committed IN (0, 1)),
    eligible_for_trait_support INTEGER NOT NULL CHECK (eligible_for_trait_support IN (0, 1)),
    eligible_for_contradiction INTEGER NOT NULL CHECK (eligible_for_contradiction IN (0, 1)),
    occurred_at TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    resolver_version TEXT NOT NULL,
    surface TEXT NOT NULL,
    UNIQUE (durable_ref, source_hash, resolver_version)
);

CREATE TABLE IF NOT EXISTS evidence_validity_events (
    event_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('resolved_active', 'invalidated', 'superseded')),
    reason TEXT NOT NULL,
    source_watermark TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (evidence_id) REFERENCES evidence_records(evidence_id)
);

CREATE TABLE IF NOT EXISTS claim_evidence_links (
    claim_id TEXT NOT NULL,
    claim_version INTEGER NOT NULL,
    evidence_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('supports', 'contradicts', 'context')),
    carried_from_version INTEGER,
    PRIMARY KEY (claim_id, claim_version, evidence_id, relation),
    FOREIGN KEY (claim_id, claim_version) REFERENCES claim_versions(claim_id, version),
    FOREIGN KEY (evidence_id) REFERENCES evidence_records(evidence_id)
);

CREATE TABLE IF NOT EXISTS review_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    suggestion_type TEXT NOT NULL,
    claim_id TEXT,
    summary TEXT NOT NULL,
    curator_run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (namespace) REFERENCES self_model_namespaces(namespace),
    FOREIGN KEY (claim_id) REFERENCES claim_lineages(claim_id)
);

CREATE TABLE IF NOT EXISTS review_suggestion_evidence (
    suggestion_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    PRIMARY KEY (suggestion_id, evidence_id),
    FOREIGN KEY (suggestion_id) REFERENCES review_suggestions(suggestion_id),
    FOREIGN KEY (evidence_id) REFERENCES evidence_records(evidence_id)
);

CREATE TABLE IF NOT EXISTS review_events (
    review_event_id TEXT PRIMARY KEY,
    suggestion_id TEXT NOT NULL,
    operation_id TEXT,
    action TEXT NOT NULL CHECK (action IN ('viewed', 'dismissed', 'acted')),
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (suggestion_id) REFERENCES review_suggestions(suggestion_id)
);

CREATE TABLE IF NOT EXISTS extraction_runs (
    extraction_run_id TEXT PRIMARY KEY,
    job_fingerprint TEXT NOT NULL,
    target_claim_id TEXT NOT NULL,
    target_claim_version INTEGER NOT NULL CHECK (target_claim_version >= 1),
    extractor_contract_version TEXT NOT NULL,
    profile_fingerprint TEXT NOT NULL,
    prompt_template_fingerprint TEXT NOT NULL,
    ordered_input_refs_hash TEXT NOT NULL,
    raw_output_json TEXT NOT NULL,
    raw_output_hash TEXT NOT NULL,
    decision TEXT,
    validation_status TEXT NOT NULL CHECK (validation_status IN ('accepted', 'rejected')),
    validation_code TEXT NOT NULL,
    resulting_suggestion_id TEXT,
    created_at TEXT NOT NULL,
    provider_visible INTEGER NOT NULL DEFAULT 0 CHECK (provider_visible = 0),
    FOREIGN KEY (target_claim_id, target_claim_version)
        REFERENCES claim_versions(claim_id, version),
    FOREIGN KEY (resulting_suggestion_id) REFERENCES review_suggestions(suggestion_id)
);

CREATE TABLE IF NOT EXISTS epoch_adoption_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    snapshot_contract_version TEXT NOT NULL,
    owner_version TEXT NOT NULL,
    renderer_version TEXT NOT NULL,
    created_from_store_seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    shadow_only INTEGER NOT NULL DEFAULT 1 CHECK (shadow_only = 1),
    FOREIGN KEY (namespace) REFERENCES self_model_namespaces(namespace)
);

CREATE TABLE IF NOT EXISTS epoch_adoption_items (
    snapshot_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    claim_id TEXT NOT NULL,
    claim_version INTEGER NOT NULL,
    maturity TEXT NOT NULL,
    statement_hash TEXT NOT NULL,
    rendered_bytes_hash TEXT NOT NULL,
    rendered_bytes BLOB NOT NULL,
    PRIMARY KEY (snapshot_id, ordinal),
    UNIQUE (snapshot_id, claim_id),
    FOREIGN KEY (snapshot_id) REFERENCES epoch_adoption_snapshots(snapshot_id),
    FOREIGN KEY (claim_id, claim_version) REFERENCES claim_versions(claim_id, version)
);

CREATE TABLE IF NOT EXISTS epoch_adoption_events (
    event_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    target_epoch_id TEXT,
    action TEXT NOT NULL CHECK (action IN ('proposed', 'approved', 'adopted', 'rolled_back')),
    approval_ref TEXT,
    recorded_at TEXT NOT NULL,
    shadow_only INTEGER NOT NULL DEFAULT 1 CHECK (shadow_only = 1),
    FOREIGN KEY (snapshot_id) REFERENCES epoch_adoption_snapshots(snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_claim_versions_latest
    ON claim_versions(claim_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_claim_versions_maturity
    ON claim_versions(maturity, created_at);
CREATE INDEX IF NOT EXISTS idx_evidence_independence
    ON evidence_records(independence_key, evidence_kind);
CREATE INDEX IF NOT EXISTS idx_evidence_generation
    ON evidence_records(generation_id);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_lookup
    ON claim_evidence_links(claim_id, evidence_id, relation);
CREATE INDEX IF NOT EXISTS idx_review_suggestion_claim
    ON review_suggestions(namespace, claim_id, suggestion_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_extraction_run_successful_job
    ON extraction_runs(job_fingerprint)
    WHERE validation_status = 'accepted';
CREATE INDEX IF NOT EXISTS idx_extraction_run_claim
    ON extraction_runs(target_claim_id, target_claim_version, created_at);
CREATE INDEX IF NOT EXISTS idx_adoption_event_target
    ON epoch_adoption_events(target_epoch_id, action);

CREATE TRIGGER IF NOT EXISTS claim_versions_continuity
BEFORE INSERT ON claim_versions
BEGIN
    SELECT CASE WHEN NEW.version != COALESCE(
        (SELECT MAX(version) FROM claim_versions WHERE claim_id = NEW.claim_id), 0
    ) + 1 THEN RAISE(ABORT, 'claim_version_continuity') END;
    SELECT CASE WHEN NEW.version = 1 AND NEW.supersedes_version IS NOT NULL
        THEN RAISE(ABORT, 'claim_supersedes_invalid') END;
    SELECT CASE WHEN NEW.version > 1 AND NEW.supersedes_version != NEW.version - 1
        THEN RAISE(ABORT, 'claim_supersedes_invalid') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM claim_versions
        WHERE claim_id = NEW.claim_id
        ORDER BY version DESC LIMIT 1
    ) AND (
        SELECT maturity FROM claim_versions
        WHERE claim_id = NEW.claim_id
        ORDER BY version DESC LIMIT 1
    ) = 'retired' THEN RAISE(ABORT, 'lineage_retired') END;
END;
"""


_APPEND_ONLY_TABLES = (
    "self_model_namespaces",
    "operation_requests",
    "operation_receipt_events",
    "claim_lineages",
    "claim_versions",
    "evidence_records",
    "evidence_validity_events",
    "claim_evidence_links",
    "review_suggestions",
    "review_suggestion_evidence",
    "review_events",
    "extraction_runs",
    "epoch_adoption_snapshots",
    "epoch_adoption_items",
    "epoch_adoption_events",
)


