"""隔离 Self Model 账本；来源 self_model_sqlite_store.py。"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sqlite3
import threading
from typing import Any, ContextManager, Iterable, Iterator

from .contract import (
    OperationReceipt,
    OperationRequest,
    SCHEMA_VERSION,
)
from .evidence import EvidenceRecord
from .model import P3CClaimVersion
from .schema import _SCHEMA, _APPEND_ONLY_TABLES


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: bytes | str) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class SQLiteSelfModelStore:
    """显式隔离连接上的 append-only 领域存储。"""

    provider_visible = False

    def __init__(self, connection: sqlite3.Connection):
        # 调用方持有连接所有权；必须是明确提供的独立、自动提交连接。
        if connection.in_transaction or connection.isolation_level is not None:
            raise ValueError("an explicit autocommit isolated connection is required")
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._savepoint_counter = 0

    def initialize(self) -> None:
        self._initialize_schema()

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the test-only connection for schema/trigger assertions."""
        return self._connection

    def _initialize_schema(self) -> None:
        with self._lock:
            self._connection.executescript(_SCHEMA)
            for table in _APPEND_ONLY_TABLES:
                self._connection.executescript(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {table}_reject_update
                    BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'append_only:{table}'); END;
                    CREATE TRIGGER IF NOT EXISTS {table}_reject_delete
                    BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'append_only:{table}'); END;
                    """
                )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            outer = self._transaction_depth == 0
            savepoint = ""
            if outer:
                self._connection.execute("BEGIN IMMEDIATE")
            else:
                self._savepoint_counter += 1
                savepoint = f"p3c_{self._savepoint_counter}"
                self._connection.execute(f"SAVEPOINT {savepoint}")
            self._transaction_depth += 1
            try:
                yield
            except BaseException:
                self._transaction_depth -= 1
                if outer:
                    self._connection.execute("ROLLBACK")
                else:
                    self._connection.execute(f"ROLLBACK TO {savepoint}")
                    self._connection.execute(f"RELEASE {savepoint}")
                raise
            else:
                self._transaction_depth -= 1
                if outer:
                    self._connection.execute("COMMIT")
                else:
                    self._connection.execute(f"RELEASE {savepoint}")

    def stage_transaction(self) -> ContextManager[None]:
        return self._transaction()

    def finalize_transaction(self) -> ContextManager[None]:
        return self._transaction()

    def stage_operation(self, request: OperationRequest) -> tuple[str, OperationRequest]:
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM operation_requests WHERE idempotency_key = ?",
                (request.idempotency_key,),
            ).fetchone()
            if row is not None:
                existing = self._request_from_row(row)
                if (
                    existing.args_hash != request.args_hash
                    or existing.intent_json != request.intent_json
                ):
                    return "conflict", existing
                return "replay", existing
            by_id = self._connection.execute(
                "SELECT * FROM operation_requests WHERE operation_id = ?",
                (request.operation_id,),
            ).fetchone()
            if by_id is not None:
                return "conflict", self._request_from_row(by_id)
            self._ensure_namespace(request)
            self._connection.execute(
                """
                INSERT INTO operation_requests (
                    operation_id, idempotency_key, tool_name, principal_id,
                    namespace, generation_id, tool_call_event_id,
                    canonical_branch_id, context_epoch_id, surface, invoked_at,
                    intent_json, args_hash, contract_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.operation_id,
                    request.idempotency_key,
                    request.tool_name,
                    request.principal_id,
                    request.namespace,
                    request.generation_id,
                    request.tool_call_event_id,
                    request.canonical_branch_id,
                    request.context_epoch_id,
                    request.surface,
                    request.invoked_at,
                    request.intent_json,
                    request.args_hash,
                    request.contract_version,
                ),
            )
            return "created", request

    def _ensure_namespace(self, request: OperationRequest) -> None:
        row = self._connection.execute(
            "SELECT * FROM self_model_namespaces WHERE namespace = ?",
            (request.namespace,),
        ).fetchone()
        if row is not None:
            if row["principal_id"] != request.principal_id or row["schema_version"] != SCHEMA_VERSION:
                raise ValueError("namespace_identity_conflict")
            return
        self._connection.execute(
            """
            INSERT INTO self_model_namespaces
                (namespace, principal_id, schema_version, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (request.namespace, request.principal_id, SCHEMA_VERSION, request.invoked_at),
        )

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> OperationRequest:
        return OperationRequest(
            operation_id=row["operation_id"],
            idempotency_key=row["idempotency_key"],
            tool_name=row["tool_name"],
            principal_id=row["principal_id"],
            namespace=row["namespace"],
            generation_id=row["generation_id"],
            tool_call_event_id=row["tool_call_event_id"],
            canonical_branch_id=row["canonical_branch_id"],
            context_epoch_id=row["context_epoch_id"],
            surface=row["surface"],
            invoked_at=row["invoked_at"],
            intent_json=row["intent_json"],
            args_hash=row["args_hash"],
            contract_version=row["contract_version"],
        )

    def operation(self, operation_id: str) -> OperationRequest | None:
        row = self._connection.execute(
            "SELECT * FROM operation_requests WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return self._request_from_row(row) if row is not None else None

    def operations(self) -> tuple[OperationRequest, ...]:
        rows = self._connection.execute(
            "SELECT * FROM operation_requests ORDER BY request_seq"
        ).fetchall()
        return tuple(self._request_from_row(row) for row in rows)

    def append_receipt(self, receipt: OperationReceipt) -> OperationReceipt:
        if receipt.provider_visible:
            raise ValueError("shadow receipts cannot be provider-visible")
        payload = {
            "operation_id": receipt.operation_id,
            "phase": receipt.phase,
            "status": receipt.status,
            "code": receipt.code,
            "claim_id": receipt.claim_id,
            "claim_version": receipt.claim_version,
            "maturity": receipt.maturity,
            "provider_visible": receipt.provider_visible,
            "eligible_for_epoch": receipt.eligible_for_epoch,
            "detail": receipt.detail,
        }
        fingerprint = _sha256(_canonical_json(payload))
        event_id = "receipt:" + fingerprint.removeprefix("sha256:")[:32]
        request = self.operation(receipt.operation_id)
        recorded_at = request.invoked_at if request else "1970-01-01T00:00:00+00:00"
        with self._transaction():
            existing = self._connection.execute(
                "SELECT 1 FROM operation_receipt_events WHERE receipt_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    """
                    INSERT INTO operation_receipt_events (
                        receipt_event_id, receipt_fingerprint, operation_id,
                        phase, status, code, claim_id, claim_version, maturity,
                        provider_visible, eligible_for_epoch, detail, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        event_id,
                        fingerprint,
                        receipt.operation_id,
                        receipt.phase,
                        receipt.status,
                        receipt.code,
                        receipt.claim_id,
                        receipt.claim_version,
                        receipt.maturity,
                        receipt.eligible_for_epoch,
                        receipt.detail,
                        recorded_at,
                    ),
                )
        return receipt

    @staticmethod
    def _receipt_from_row(row: sqlite3.Row) -> OperationReceipt:
        return OperationReceipt(
            operation_id=row["operation_id"],
            phase=row["phase"],
            status=row["status"],
            code=row["code"],
            claim_id=row["claim_id"],
            claim_version=row["claim_version"],
            maturity=row["maturity"],
            provider_visible=False,
            eligible_for_epoch=row["eligible_for_epoch"],
            detail=row["detail"],
        )

    def receipts(self, operation_id: str | None = None) -> tuple[OperationReceipt, ...]:
        if operation_id is None:
            rows = self._connection.execute(
                "SELECT * FROM operation_receipt_events ORDER BY store_seq"
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT * FROM operation_receipt_events
                WHERE operation_id = ? ORDER BY store_seq
                """,
                (operation_id,),
            ).fetchall()
        return tuple(self._receipt_from_row(row) for row in rows)

    def latest_receipt(self, operation_id: str) -> OperationReceipt | None:
        row = self._connection.execute(
            """
            SELECT * FROM operation_receipt_events
            WHERE operation_id = ? ORDER BY store_seq DESC LIMIT 1
            """,
            (operation_id,),
        ).fetchone()
        return self._receipt_from_row(row) if row is not None else None

    def pending_operations(self) -> tuple[OperationRequest, ...]:
        rows = self._connection.execute(
            """
            SELECT request.* FROM operation_requests AS request
            WHERE NOT EXISTS (
                SELECT 1 FROM operation_receipt_events AS receipt
                WHERE receipt.operation_id = request.operation_id
                  AND receipt.phase IN ('finalized', 'rejected', 'aborted')
            )
            ORDER BY request.request_seq
            """
        ).fetchall()
        return tuple(self._request_from_row(row) for row in rows)

    def append_p3c_version(
        self,
        *,
        request: OperationRequest,
        claim_id: str,
        expected_version: int | None,
        statement: str,
        domain: str,
        topic_key: str,
        scope: str,
        maturity: str,
        evidence: Iterable[EvidenceRecord],
        support_evidence_ids: Iterable[str],
        support_independence_keys: Iterable[str],
        contradiction_evidence_ids: Iterable[str],
        decision_fingerprint: str,
        mechanical: bool = False,
    ) -> P3CClaimVersion:
        evidence_tuple = tuple(evidence)
        support_ids = tuple(sorted(set(support_evidence_ids)))
        support_keys = tuple(sorted(set(support_independence_keys)))
        contradiction_ids = tuple(sorted(set(contradiction_evidence_ids)))
        try:
            with self._transaction():
                latest = self.p3c_latest(claim_id)
                if latest is None:
                    if expected_version not in {None, 0}:
                        raise ValueError("version_conflict")
                    version = 1
                    supersedes = None
                    self._connection.execute(
                        """
                        INSERT INTO claim_lineages (
                            claim_id, namespace, principal_id, claim_kind, domain,
                            topic_key, scope, created_operation_id,
                            related_retired_claim_id, created_at
                        ) VALUES (?, ?, ?, 'self_trait', ?, ?, ?, ?, NULL, ?)
                        """,
                        (
                            claim_id,
                            request.namespace,
                            request.principal_id,
                            domain,
                            topic_key,
                            scope,
                            request.operation_id,
                            request.invoked_at,
                        ),
                    )
                else:
                    if expected_version != latest.version:
                        raise ValueError("version_conflict")
                    if latest.maturity == "retired":
                        raise ValueError("lineage_retired")
                    if (
                        latest.principal_id != request.principal_id
                        or latest.namespace != request.namespace
                        or latest.claim_kind != "self_trait"
                        or latest.domain != domain
                        or latest.topic_key != topic_key
                        or latest.scope != scope
                    ):
                        raise ValueError("immutable_claim_identity")
                    version = latest.version + 1
                    supersedes = latest.version

                evidence_by_id = {item.evidence_id: item for item in evidence_tuple}
                if not set(support_ids).issubset(evidence_by_id):
                    raise ValueError("evidence_identity_conflict")
                if not set(contradiction_ids).issubset(evidence_by_id):
                    raise ValueError("evidence_identity_conflict")
                for item in evidence_tuple:
                    self._append_evidence(item)

                self._connection.execute(
                    """
                    INSERT INTO claim_versions (
                        claim_id, version, supersedes_version, statement,
                        statement_hash, maturity, gate_version,
                        decision_fingerprint, created_operation_id, created_at,
                        surface, mechanical, created_during_epoch_id,
                        eligibility_rule
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'next_boundary_only')
                    """,
                    (
                        claim_id,
                        version,
                        supersedes,
                        statement,
                        _sha256(statement),
                        maturity,
                        "p3c-gate-v2",
                        decision_fingerprint,
                        request.operation_id,
                        request.invoked_at,
                        request.surface,
                        int(mechanical),
                        request.context_epoch_id,
                    ),
                )
                previous_ids: set[str] = set()
                if latest is not None:
                    previous_ids = set(latest.evidence_ids)
                for item in sorted(evidence_tuple, key=lambda record: record.evidence_id):
                    if item.evidence_id in support_ids:
                        relation = "supports"
                    elif item.evidence_id in contradiction_ids:
                        relation = "contradicts"
                    else:
                        relation = "context"
                    self._connection.execute(
                        """
                        INSERT INTO claim_evidence_links (
                            claim_id, claim_version, evidence_id, relation,
                            carried_from_version
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            claim_id,
                            version,
                            item.evidence_id,
                            relation,
                            latest.version if latest and item.evidence_id in previous_ids else None,
                        ),
                    )
                claim = self.p3c_latest(claim_id)
                if claim is None:
                    raise ValueError("storage_conflict")
                if set(claim.support_independence_keys) != set(support_keys):
                    raise ValueError("evidence_identity_conflict")
                return claim
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "lineage_retired" in message:
                raise ValueError("lineage_retired") from exc
            if "claim_version" in message or "supersedes" in message:
                raise ValueError("version_conflict") from exc
            if "decision_fingerprint" in message:
                raise ValueError("decision_fingerprint_conflict") from exc
            raise ValueError("storage_conflict") from exc

    def _append_evidence(self, item: EvidenceRecord) -> None:
        existing = self._connection.execute(
            "SELECT * FROM evidence_records WHERE evidence_id = ?",
            (item.evidence_id,),
        ).fetchone()
        is_new_revision = existing is None
        if existing is not None:
            if self._evidence_from_row(existing) != item:
                raise ValueError("evidence_identity_conflict")
        else:
            tuple_row = self._connection.execute(
                """
                SELECT evidence_id FROM evidence_records
                WHERE durable_ref = ? AND source_hash = ? AND resolver_version = ?
                """,
                (item.durable_ref, item.source_hash, item.resolver_version),
            ).fetchone()
            if tuple_row is not None and tuple_row["evidence_id"] != item.evidence_id:
                raise ValueError("evidence_identity_conflict")
            self._connection.execute(
                """
                INSERT INTO evidence_records (
                    evidence_id, durable_ref, source_type, evidence_kind, actor, relation,
                    root_event_id, generation_id, independence_key,
                    canonical_state, outcome, committed,
                    eligible_for_trait_support, eligible_for_contradiction,
                    occurred_at, source_hash, resolver_version, surface
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.evidence_id,
                    item.durable_ref,
                    item.source_type,
                    item.evidence_kind,
                    item.actor,
                    item.relation,
                    item.root_event_id,
                    item.generation_id,
                    item.independence_key,
                    item.canonical_state,
                    item.outcome,
                    int(item.committed),
                    int(item.eligible_for_trait_support),
                    int(item.eligible_for_contradiction),
                    item.occurred_at,
                    item.source_hash,
                    item.resolver_version,
                    item.surface,
                ),
            )
        if is_new_revision:
            prior_revisions = self._connection.execute(
                """
                SELECT evidence_id FROM evidence_records
                WHERE durable_ref = ? AND evidence_id != ?
                ORDER BY rowid
                """,
                (item.durable_ref, item.evidence_id),
            ).fetchall()
            for prior in prior_revisions:
                self._append_validity_event(
                    evidence_id=prior["evidence_id"],
                    state="superseded",
                    reason="authoritative_revision",
                    source_watermark=item.source_hash,
                    recorded_at=item.occurred_at,
                )
        if item.canonical_state == "superseded":
            state = "superseded"
        elif item.committed and item.canonical_state == "active":
            state = "resolved_active"
        else:
            state = "invalidated"
        self._append_validity_event(
            evidence_id=item.evidence_id,
            state=state,
            reason="verified_authoritative_source",
            source_watermark=item.source_hash,
            recorded_at=item.occurred_at,
        )

    def _append_validity_event(
        self,
        *,
        evidence_id: str,
        state: str,
        reason: str,
        source_watermark: str,
        recorded_at: str,
    ) -> None:
        event_seed = _canonical_json(
            {
                "evidence_id": evidence_id,
                "reason": reason,
                "source_watermark": source_watermark,
                "state": state,
            }
        )
        event_id = "evidence-validity:" + _sha256(event_seed).removeprefix("sha256:")[:32]
        self._connection.execute(
            """
            INSERT OR IGNORE INTO evidence_validity_events (
                event_id, evidence_id, state, reason, source_watermark, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                evidence_id,
                state,
                reason,
                source_watermark,
                recorded_at,
            ),
        )

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=row["evidence_id"],
            durable_ref=row["durable_ref"],
            source_type=row["source_type"],
            evidence_kind=row["evidence_kind"],
            actor=row["actor"],
            relation=row["relation"],
            root_event_id=row["root_event_id"],
            generation_id=row["generation_id"],
            independence_key=row["independence_key"],
            canonical_state=row["canonical_state"],
            outcome=row["outcome"],
            committed=bool(row["committed"]),
            eligible_for_trait_support=bool(row["eligible_for_trait_support"]),
            eligible_for_contradiction=bool(row["eligible_for_contradiction"]),
            occurred_at=row["occurred_at"],
            source_hash=row["source_hash"],
            resolver_version=row["resolver_version"],
            surface=row["surface"],
        )

    def evidence(self, evidence_id: str) -> EvidenceRecord | None:
        row = self._connection.execute(
            "SELECT * FROM evidence_records WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        return self._evidence_from_row(row) if row is not None else None

    def append_collected_evidence(
        self, evidence: Iterable[EvidenceRecord]
    ) -> tuple[EvidenceRecord, ...]:
        """Append independently collected evidence transactionally and idempotently."""
        records = tuple(evidence)
        with self._transaction():
            for record in records:
                self._append_evidence(record)
        return records

    def _claim_from_row(self, row: sqlite3.Row) -> P3CClaimVersion:
        links = self._connection.execute(
            """
            SELECT link.evidence_id, link.relation, evidence.independence_key
            FROM claim_evidence_links AS link
            JOIN evidence_records AS evidence USING (evidence_id)
            WHERE link.claim_id = ? AND link.claim_version = ?
            ORDER BY link.evidence_id
            """,
            (row["claim_id"], row["version"]),
        ).fetchall()
        evidence_ids = tuple(sorted({link["evidence_id"] for link in links}))
        linked_support_ids = {
            link["evidence_id"] for link in links if link["relation"] == "supports"
        }
        support_records = tuple(
            record
            for evidence_id in linked_support_ids
            if (record := self.evidence(evidence_id)) is not None
            and record.is_maturity_support
        )
        support_ids = tuple(sorted(record.evidence_id for record in support_records))
        support_keys = tuple(
            sorted({record.independence_key for record in support_records})
        )
        contradiction_ids = tuple(
            sorted({link["evidence_id"] for link in links if link["relation"] == "contradicts"})
        )
        return P3CClaimVersion(
            principal_id=row["principal_id"],
            namespace=row["namespace"],
            claim_id=row["claim_id"],
            claim_kind=row["claim_kind"],
            domain=row["domain"],
            topic_key=row["topic_key"],
            scope=row["scope"],
            statement=row["statement"],
            maturity=row["maturity"],
            version=row["version"],
            supersedes=row["supersedes_version"],
            evidence_ids=evidence_ids,
            support_evidence_ids=support_ids,
            support_independence_keys=support_keys,
            contradiction_evidence_ids=contradiction_ids,
            decision_fingerprint=row["decision_fingerprint"],
            created_operation_id=row["created_operation_id"],
            created_at=row["created_at"],
            surface=row["surface"],
            mechanical=bool(row["mechanical"]),
        )

    def p3c_latest(self, claim_id: str) -> P3CClaimVersion | None:
        row = self._connection.execute(
            """
            SELECT version.*, lineage.namespace, lineage.principal_id,
                   lineage.claim_kind, lineage.domain, lineage.topic_key, lineage.scope
            FROM claim_versions AS version
            JOIN claim_lineages AS lineage USING (claim_id)
            WHERE version.claim_id = ?
            ORDER BY version.version DESC LIMIT 1
            """,
            (claim_id,),
        ).fetchone()
        return self._claim_from_row(row) if row is not None else None

    def p3c_history(self, claim_id: str) -> tuple[P3CClaimVersion, ...]:
        rows = self._connection.execute(
            """
            SELECT version.*, lineage.namespace, lineage.principal_id,
                   lineage.claim_kind, lineage.domain, lineage.topic_key, lineage.scope
            FROM claim_versions AS version
            JOIN claim_lineages AS lineage USING (claim_id)
            WHERE version.claim_id = ? ORDER BY version.version
            """,
            (claim_id,),
        ).fetchall()
        return tuple(self._claim_from_row(row) for row in rows)

    def p3c_latest_claims(self) -> tuple[P3CClaimVersion, ...]:
        rows = self._connection.execute(
            """
            SELECT version.*, lineage.namespace, lineage.principal_id,
                   lineage.claim_kind, lineage.domain, lineage.topic_key, lineage.scope
            FROM claim_versions AS version
            JOIN claim_lineages AS lineage USING (claim_id)
            WHERE version.version = (
                SELECT MAX(candidate.version) FROM claim_versions AS candidate
                WHERE candidate.claim_id = version.claim_id
            )
            ORDER BY version.claim_id
            """
        ).fetchall()
        return tuple(self._claim_from_row(row) for row in rows)

    def find_active_semantic_duplicate(
        self,
        *,
        namespace: str,
        domain: str,
        topic_key: str,
        scope: str,
        statement: str,
    ) -> P3CClaimVersion | None:
        for claim in self.p3c_latest_claims():
            if (
                claim.namespace == namespace
                and claim.maturity != "retired"
                and claim.domain == domain
                and claim.topic_key == topic_key
                and claim.scope == scope
                and claim.statement == statement
            ):
                return claim
        return None

    def append_review_suggestion(
        self,
        *,
        suggestion_id: str,
        namespace: str,
        suggestion_type: str,
        claim_id: str | None,
        summary: str,
        curator_run_id: str,
        evidence_ids: Iterable[str] = (),
        created_at: str,
    ) -> bool:
        with self._transaction():
            evidence_set = tuple(sorted(set(evidence_ids)))
            existing = self._connection.execute(
                "SELECT * FROM review_suggestions WHERE suggestion_id = ?",
                (suggestion_id,),
            ).fetchone()
            if existing is not None:
                existing_evidence = tuple(
                    row["evidence_id"]
                    for row in self._connection.execute(
                        "SELECT evidence_id FROM review_suggestion_evidence "
                        "WHERE suggestion_id = ? ORDER BY evidence_id",
                        (suggestion_id,),
                    ).fetchall()
                )
                identity = (
                    existing["namespace"],
                    existing["suggestion_type"],
                    existing["claim_id"],
                    existing["curator_run_id"],
                    existing_evidence,
                )
                proposed = (
                    namespace,
                    suggestion_type,
                    claim_id,
                    curator_run_id,
                    evidence_set,
                )
                if identity != proposed:
                    raise ValueError("review_suggestion_identity_conflict")
                return False
            self._connection.execute(
                """
                INSERT INTO review_suggestions (
                    suggestion_id, namespace, suggestion_type, claim_id,
                    summary, curator_run_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    suggestion_id,
                    namespace,
                    suggestion_type,
                    claim_id,
                    summary,
                    curator_run_id,
                    created_at,
                ),
            )
            for evidence_id in evidence_set:
                self._connection.execute(
                    """
                    INSERT INTO review_suggestion_evidence (suggestion_id, evidence_id)
                    VALUES (?, ?)
                    """,
                    (suggestion_id, evidence_id),
                )
            return True

    def list_review_suggestions(
        self,
        *,
        namespace: str | None = None,
        claim_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Return stored suggestions and their latest append-only review state."""
        clauses: list[str] = []
        parameters: list[str] = []
        if namespace is not None:
            clauses.append("suggestion.namespace = ?")
            parameters.append(namespace)
        if claim_id is not None:
            clauses.append("suggestion.claim_id = ?")
            parameters.append(claim_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._connection.execute(
            """
            SELECT suggestion.*
            FROM review_suggestions AS suggestion
            """
            + where
            + " ORDER BY suggestion.created_at, suggestion.suggestion_id",
            parameters,
        ).fetchall()
        results = []
        for row in rows:
            evidence_rows = self._connection.execute(
                """
                SELECT evidence_id FROM review_suggestion_evidence
                WHERE suggestion_id = ? ORDER BY evidence_id
                """,
                (row["suggestion_id"],),
            ).fetchall()
            latest_event = self.latest_review_event(row["suggestion_id"])
            extraction = self._connection.execute(
                """
                SELECT target_claim_version FROM extraction_runs
                WHERE resulting_suggestion_id = ?
                  AND validation_status = 'accepted'
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (row["suggestion_id"],),
            ).fetchone()
            results.append({
                "suggestion_id": row["suggestion_id"],
                "namespace": row["namespace"],
                "suggestion_type": row["suggestion_type"],
                "claim_id": row["claim_id"],
                "summary": row["summary"],
                "curator_run_id": row["curator_run_id"],
                "created_at": row["created_at"],
                "target_claim_version": (
                    int(extraction["target_claim_version"])
                    if extraction is not None
                    else None
                ),
                "evidence_ids": tuple(item["evidence_id"] for item in evidence_rows),
                "latest_event": latest_event,
                "provider_visible": False,
            })
        return tuple(results)

    def review_suggestion(self, suggestion_id: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self.list_review_suggestions()
                if item["suggestion_id"] == suggestion_id
            ),
            None,
        )

    def pending_review_suggestions(
        self, *, namespace: str, limit: int = 10
    ) -> tuple[dict[str, Any], ...]:
        if limit < 1:
            return ()
        pending: list[dict[str, Any]] = []
        for item in self.list_review_suggestions(namespace=namespace):
            latest = self.p3c_latest(str(item.get("claim_id") or ""))
            target_version = item.get("target_claim_version")
            if (
                latest is None
                or latest.maturity == "retired"
                or target_version is None
                or latest.version != int(target_version)
            ):
                continue
            event = item.get("latest_event")
            action = str(event.get("action") or "") if isinstance(event, dict) else ""
            if action in {"acted", "dismissed"}:
                continue
            pending.append(item)
            if len(pending) >= limit:
                break
        return tuple(pending)

    def review_events(self, suggestion_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._connection.execute(
            """
            SELECT review_event_id, suggestion_id, operation_id, action, recorded_at
            FROM review_events WHERE suggestion_id = ?
            ORDER BY recorded_at, rowid
            """,
            (suggestion_id,),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def latest_review_event(self, suggestion_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT review_event_id, suggestion_id, operation_id, action, recorded_at
            FROM review_events WHERE suggestion_id = ?
            ORDER BY recorded_at DESC, rowid DESC LIMIT 1
            """,
            (suggestion_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def append_review_event(
        self,
        *,
        suggestion_id: str,
        operation_id: str | None,
        action: str,
        recorded_at: str,
    ) -> str:
        canonical = _canonical_json(
            {
                "suggestion_id": suggestion_id,
                "operation_id": operation_id,
                "action": action,
                "recorded_at": recorded_at,
            }
        )
        event_id = "review-event:" + _sha256(canonical).removeprefix("sha256:")[:32]
        with self._transaction():
            self._connection.execute(
                """
                INSERT OR IGNORE INTO review_events (
                    review_event_id, suggestion_id, operation_id, action, recorded_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, suggestion_id, operation_id, action, recorded_at),
            )
        return event_id

    def successful_extraction_run(
        self, job_fingerprint: str
    ) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT * FROM extraction_runs
            WHERE job_fingerprint = ? AND validation_status = 'accepted'
            LIMIT 1
            """,
            (job_fingerprint,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["provider_visible"] = bool(result["provider_visible"])
        return result

    def append_extraction_run(
        self,
        *,
        extraction_run_id: str,
        job_fingerprint: str,
        target_claim_id: str,
        target_claim_version: int,
        extractor_contract_version: str,
        profile_fingerprint: str,
        prompt_template_fingerprint: str,
        ordered_input_refs_hash: str,
        raw_output_json: str,
        raw_output_hash: str,
        decision: str | None,
        validation_status: str,
        validation_code: str,
        resulting_suggestion_id: str | None,
        created_at: str,
    ) -> bool:
        values = (
            extraction_run_id,
            job_fingerprint,
            target_claim_id,
            target_claim_version,
            extractor_contract_version,
            profile_fingerprint,
            prompt_template_fingerprint,
            ordered_input_refs_hash,
            raw_output_json,
            raw_output_hash,
            decision,
            validation_status,
            validation_code,
            resulting_suggestion_id,
        )
        with self._transaction():
            existing = self._connection.execute(
                "SELECT * FROM extraction_runs WHERE extraction_run_id = ?",
                (extraction_run_id,),
            ).fetchone()
            if existing is not None:
                stored = tuple(
                    existing[key]
                    for key in (
                        "extraction_run_id",
                        "job_fingerprint",
                        "target_claim_id",
                        "target_claim_version",
                        "extractor_contract_version",
                        "profile_fingerprint",
                        "prompt_template_fingerprint",
                        "ordered_input_refs_hash",
                        "raw_output_json",
                        "raw_output_hash",
                        "decision",
                        "validation_status",
                        "validation_code",
                        "resulting_suggestion_id",
                    )
                )
                if stored != values:
                    raise ValueError("extraction_run_identity_conflict")
                return False
            self._connection.execute(
                """
                INSERT INTO extraction_runs (
                    extraction_run_id, job_fingerprint, target_claim_id,
                    target_claim_version, extractor_contract_version,
                    profile_fingerprint, prompt_template_fingerprint,
                    ordered_input_refs_hash, raw_output_json, raw_output_hash,
                    decision, validation_status, validation_code,
                    resulting_suggestion_id, created_at, provider_visible
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (*values, created_at),
            )
            return True

    def list_extraction_runs(
        self, *, job_fingerprint: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        if job_fingerprint is None:
            rows = self._connection.execute(
                "SELECT * FROM extraction_runs ORDER BY rowid"
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT * FROM extraction_runs WHERE job_fingerprint = ?
                ORDER BY rowid
                """,
                (job_fingerprint,),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["provider_visible"] = bool(item["provider_visible"])
            results.append(item)
        return tuple(results)

