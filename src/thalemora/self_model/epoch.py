"""隔离 ContextEpoch 冻结规则；不向 Provider 投递或接入 Context assembler。

采用由调用方明确启动的新 epoch；首次冻结后，重入只读取原字节。
当前 epoch 内创建的版本不得进入该 epoch，旧版本失效不热改已冻结 identity。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from .contract import SCHEMA_VERSION, namespace_for_principal
from .evidence import EvidenceResolver
from .store import SQLiteSelfModelStore, _sha256, _canonical_json


@dataclass(frozen=True)
class FrozenItem:
    claim_id: str
    claim_version: int
    statement: str


@dataclass(frozen=True)
class FrozenEpoch:
    epoch_id: str
    snapshot_id: str
    items: tuple[FrozenItem, ...]
    text: str
    provider_visible: bool = False


class EpochFreezer:
    def __init__(self, *, store: SQLiteSelfModelStore, resolver: EvidenceResolver):
        self.store = store
        self.resolver = resolver

    def _read(self, epoch_id: str, snapshot_id: str) -> FrozenEpoch:
        rows = self.store.connection.execute(
            "SELECT * FROM epoch_adoption_items WHERE snapshot_id = ? ORDER BY ordinal",
            (snapshot_id,),
        ).fetchall()
        items = tuple(FrozenItem(row["claim_id"], row["claim_version"], bytes(row["rendered_bytes"]).decode("utf-8")) for row in rows)
        text = "\n".join(f"- {item.statement}" for item in items)
        return FrozenEpoch(epoch_id, snapshot_id, items, text)

    def freeze(self, epoch_id: str, *, now: str) -> FrozenEpoch:
        namespace = namespace_for_principal(self.resolver.principal_id)
        with self.store.finalize_transaction():
            # 首个 epoch 可以先于任何 claim；空 identity 也必须有独立 namespace。
            self.store.connection.execute(
                "INSERT OR IGNORE INTO self_model_namespaces "
                "(namespace, principal_id, schema_version, created_at) VALUES (?, ?, ?, ?)",
                (namespace, self.resolver.principal_id, SCHEMA_VERSION, now),
            )
            existing = self.store.connection.execute(
                "SELECT event.snapshot_id FROM epoch_adoption_events event "
                "JOIN epoch_adoption_snapshots snapshot USING (snapshot_id) "
                "WHERE event.target_epoch_id = ? AND snapshot.namespace = ? AND event.action = 'adopted'",
                (epoch_id, namespace),
            ).fetchone()
            if existing is not None:
                return self._read(epoch_id, existing["snapshot_id"])
            selected = []
            for claim in self.store.p3c_latest_claims():
                if claim.namespace != namespace or claim.maturity != "established":
                    continue
                created_epoch = self.store.connection.execute(
                    "SELECT created_during_epoch_id FROM claim_versions WHERE claim_id = ? AND version = ?",
                    (claim.claim_id, claim.version),
                ).fetchone()[0]
                if created_epoch == epoch_id:
                    continue
                refs = [self.store.evidence(key).durable_ref for key in claim.evidence_ids]
                # 不可解析或 generation 失败时抛出真实错误，整次冻结回滚。
                # 可解析但已失效的根则按确定性支持数/矛盾规则禁入。
                resolved = self.resolver.resolve_refs(refs)
                if len(resolved.support_independence_keys) >= 2 and not resolved.contradictions:
                    selected.append(claim.claim_id)
            snapshot = self._create_snapshot(namespace=namespace, created_at=now, claim_ids=selected)
            self._append_event(snapshot_id=snapshot["snapshot_id"], action="adopted", recorded_at=now, target_epoch_id=epoch_id)
            return self._read(epoch_id, snapshot["snapshot_id"])

    def _create_snapshot(
        self,
        *,
        namespace: str,
        created_at: str,
        snapshot_contract_version: str = "p3c-snapshot-v1",
        owner_version: str = "p3c-self-model-v1",
        renderer_version: str = "thalemora-isolated-freeze-v1",
        claim_ids: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        selected = [
            claim
            for claim in self.store.p3c_latest_claims()
            if claim.namespace == namespace
            and claim.maturity == "established"
            and (claim_ids is None or claim.claim_id in claim_ids)
        ]
        selected.sort(key=lambda claim: claim.claim_id)
        items: list[dict[str, Any]] = []
        rendered: list[bytes] = []
        for ordinal, claim in enumerate(selected):
            payload = claim.statement.encode("utf-8")
            rendered.append(payload)
            items.append(
                {
                    "ordinal": ordinal,
                    "claim_id": claim.claim_id,
                    "claim_version": claim.version,
                    "maturity": claim.maturity,
                    "statement_hash": _sha256(claim.statement),
                    "rendered_bytes_hash": _sha256(payload),
                }
            )
        content = {
            "snapshot_contract_version": snapshot_contract_version,
            "namespace": namespace,
            "owner_version": owner_version,
            "renderer_version": renderer_version,
            "items": items,
        }
        content_hash = _sha256(_canonical_json(content))
        snapshot_id = "self-model-snapshot:" + content_hash.removeprefix("sha256:")[:32]
        watermark_row = self.store.connection.execute(
            "SELECT COALESCE(MAX(store_seq), 0) AS watermark FROM operation_receipt_events"
        ).fetchone()
        watermark = int(watermark_row["watermark"])
        with self.store.finalize_transaction():
            existing = self.store.connection.execute(
                "SELECT snapshot_id FROM epoch_adoption_snapshots WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
            if existing is None:
                self.store.connection.execute(
                    """
                    INSERT INTO epoch_adoption_snapshots (
                        snapshot_id, namespace, content_hash,
                        snapshot_contract_version, owner_version, renderer_version,
                        created_from_store_seq, created_at, shadow_only
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        snapshot_id,
                        namespace,
                        content_hash,
                        snapshot_contract_version,
                        owner_version,
                        renderer_version,
                        watermark,
                        created_at,
                    ),
                )
                for item, payload in zip(items, rendered):
                    self.store.connection.execute(
                        """
                        INSERT INTO epoch_adoption_items (
                            snapshot_id, ordinal, claim_id, claim_version,
                            maturity, statement_hash, rendered_bytes_hash,
                            rendered_bytes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_id,
                            item["ordinal"],
                            item["claim_id"],
                            item["claim_version"],
                            item["maturity"],
                            item["statement_hash"],
                            item["rendered_bytes_hash"],
                            payload,
                        ),
                    )
            else:
                snapshot_id = existing["snapshot_id"]
        return {
            "snapshot_id": snapshot_id,
            "content_hash": content_hash,
            "content": content,
            "provider_visible": False,
            "adopted": False,
        }

    def _append_event(
        self,
        *,
        snapshot_id: str,
        action: str,
        recorded_at: str,
        target_epoch_id: str | None = None,
        approval_ref: str | None = None,
    ) -> str:
        canonical = _canonical_json(
            {
                "snapshot_id": snapshot_id,
                "target_epoch_id": target_epoch_id,
                "action": action,
                "approval_ref": approval_ref,
                "recorded_at": recorded_at,
                "shadow_only": True,
            }
        )
        event_id = "adoption-event:" + _sha256(canonical).removeprefix("sha256:")[:32]
        with self.store.finalize_transaction():
            self.store.connection.execute(
                """
                INSERT OR IGNORE INTO epoch_adoption_events (
                    event_id, snapshot_id, target_epoch_id, action,
                    approval_ref, recorded_at, shadow_only
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    event_id,
                    snapshot_id,
                    target_epoch_id,
                    action,
                    approval_ref,
                    recorded_at,
                ),
            )
        return event_id

