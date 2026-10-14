"""P3 授权适配：可信宿主调用原 SelfModelCore/Gate/epoch，不接受外部 actor。

维护器可提交既有 review suggestion；Agent 的正式工具名/参数/context 均取自
独立已完成事件。每 Agent/channel 的库与 P2 private 授权相接。
"""
import sqlite3

from thalemora.inner_world.host import ScopedEvidenceSource, page_size, timestamp
from .contract import namespace_for_principal
from .core import SelfModelCore
from .epoch import EpochFreezer
from .evidence import EvidenceResolver
from .store import SQLiteSelfModelStore


class HostedSelfModel:
    def __init__(self, session):
        self.session = session
        session.check()
        path = session.registry.archive_db_path(session.owner, session.agent_id).with_name(
            f"self_model_{session.channel}.sqlite")
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.store = SQLiteSelfModelStore(self.connection)
        self.store.initialize()
        self.resolver = EvidenceResolver(ScopedEvidenceSource(session), principal_id=session.agent_id)
        self.core = SelfModelCore(store=self.store, resolver=self.resolver)

    def close(self):
        self.connection.close()

    def apply(self, event_ref):
        event, record, payload = self.session.verified_event(event_ref, kind="self_model")
        self.session.require_epoch(event.context.context_epoch_id)
        if (record.source_type != "canonical_tool_call"
                or record.root_event_id != event.context.canonical_tool_call_event_id
                or set(payload) != {"tool_name", "args"}
                or payload["tool_name"] != record.tool_name):
            raise ValueError("Self Model 必须来自真实对应工具调用")
        receipt = self.core.stage(payload["tool_name"], payload["args"], event.context)
        return self.core.finalize(receipt.operation_id)

    def claim(self, claim_id):
        self.session.check()
        return self.store.p3c_latest(claim_id)

    def history(self, claim_id, *, after=0, limit=20):
        self.session.check()
        page_size(limit)
        rows = self.connection.execute(
            "SELECT v.*, l.namespace,l.principal_id,l.claim_kind,l.domain,l.topic_key,l.scope "
            "FROM claim_versions v JOIN claim_lineages l USING(claim_id) "
            "WHERE v.claim_id=? AND v.version>? ORDER BY v.version LIMIT ?",
            (claim_id, after, limit),
        ).fetchall()
        return tuple(self.store._claim_from_row(row) for row in rows)

    def suggest(self, *, suggestion_id, claim_id, summary, recorded_at):
        self.session.check()
        timestamp(recorded_at)
        if self.store.p3c_latest(claim_id) is None:
            raise KeyError("建议目标不存在")
        if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 8000:
            raise ValueError("建议正文须为 1..8000 字符")
        return self.store.append_review_suggestion(
            suggestion_id=suggestion_id, namespace=namespace_for_principal(self.session.agent_id),
            suggestion_type="review", claim_id=claim_id, summary=summary,
            curator_run_id=f"host:{self.session.owner.principal_id}:{self.session.app_id}:{self.session.role}",
            created_at=recorded_at,
        )

    def freeze_epoch(self, epoch_id, *, now):
        """显式宿主慢路径，沿用 P1 established/独立证据与冻结规则。"""
        self.session.require_epoch(epoch_id)
        return EpochFreezer(store=self.store, resolver=self.resolver).freeze(epoch_id, now=now)
