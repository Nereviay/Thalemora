"""宿主边界。Session/reader 由认证后的宿主构造，绝不可从请求体反序列化。

reader 按稳定 Agent ID 返回独立、持久、已核验事件；payload 是事件原文 JSON，
不是本次请求提供的正文。无需 Runtime 运行器，也不发现或读取生产数据。
"""
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Protocol

from thalemora.identity.registry import AccessDenied, IdentityRegistry, OwnerPrincipal
from thalemora.self_model.contract import InvocationContext
from thalemora.self_model.evidence import VerifiedEvidenceSource, EvidenceResolver


class AgentRequired(RuntimeError):
    """需连接可信 Agent 宿主。"""


def timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须含时区")
    return value


def page_size(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("分页大小必须为 1..100")
    return limit


@dataclass(frozen=True)
class HostEvent:
    kind: str
    app_id: str
    channel: str
    context: InvocationContext


class HostReader(VerifiedEvidenceSource, Protocol):
    def event(self, durable_ref: str, principal_id: str) -> HostEvent | None: ...
    def confirmed_epoch(self, epoch_id: str, principal_id: str, channel: str) -> bool: ...


@dataclass(frozen=True)
class HostSession:
    registry: IdentityRegistry
    owner: OwnerPrincipal
    agent_id: str
    app_id: str
    channel: str = "main"
    role: str = "maintenance"
    reader: HostReader | None = None

    def __post_init__(self):
        if self.role not in {"agent", "maintenance", "curator"}:
            raise ValueError("未知宿主角色")
        self.check()

    def check(self):
        self.registry.assert_source(self.owner, self.agent_id, self.app_id, self.channel)

    def require_agent(self):
        self.check()
        if self.role != "agent":
            raise AccessDenied("只有 Agent 本人可提交自主语义")
        if self.reader is None:
            raise AgentRequired("需连接 Agent")

    def require_epoch(self, epoch_id):
        self.check()
        if self.reader is None:
            raise AgentRequired("需连接 Agent 宿主确认 ContextEpoch")
        if not self.reader.confirmed_epoch(epoch_id, self.agent_id, self.channel):
            raise AccessDenied("ContextEpoch 未由宿主确认或不属于当前域")

    def verified_event(self, ref, *, kind, autonomous=True):
        if autonomous:
            self.require_agent()
        else:
            self.check()
        if self.reader is None:
            raise AgentRequired("需连接 Agent 事件来源；可查询或管理已有状态")
        source = ScopedEvidenceSource(self)
        event = source.event(ref)
        record = source.record(ref, self.agent_id)
        if event.kind != kind:
            raise ValueError("事件领域不符")
        generation = source.generation_state(record.generation_id, self.agent_id)
        if (record.actor != "assistant" or record.source_type not in
                {"canonical_reply", "canonical_tool_call", "contemplation"}
                or not record.committed or record.canonical_state != "active"
                or record.outcome != "succeeded" or record.duplicate_suppressed
                or generation is None or not generation.finalizable
                or generation.canonical_branch_id != event.context.canonical_branch_id
                or event.context.canonical_tool_call_event_id not in
                generation.canonical_tool_call_event_ids):
            raise AccessDenied("事件不是已完成的 canonical Agent 行动")
        # 复用已有独立证据解析器，包含来源哈希及 generation 核验。
        EvidenceResolver(source, principal_id=self.agent_id).resolve_refs((ref,))
        timestamp(record.occurred_at)
        payload = json.loads(record.content)
        if not isinstance(payload, dict):
            raise ValueError("事件正文必须是 JSON 对象")
        return event, record, payload


class ScopedEvidenceSource:
    """在原 VerifiedEvidenceSource 前接 P2 授权，不另建证据/Gate。"""

    def __init__(self, session: HostSession):
        self.session = session

    def _check(self, principal_id):
        self.session.check()
        if principal_id != self.session.agent_id:
            raise AccessDenied("证据属于另一 Agent")
        if self.session.reader is None:
            raise AgentRequired("需连接 Agent")

    def event(self, ref):
        s = self.session
        self._check(s.agent_id)
        event = s.reader.event(ref, s.agent_id)
        if event is None or event.context.principal_id != s.agent_id or event.channel != s.channel:
            raise AccessDenied("缺少当前 Agent/通道的可信事件")
        s.registry.assert_source(s.owner, s.agent_id, event.app_id, event.channel)
        return event

    def generation_state(self, generation_id, principal_id):
        self._check(principal_id)
        return self.session.reader.generation_state(generation_id, principal_id)

    def refs_for_generation(self, generation_id, principal_id):
        self._check(principal_id)
        refs = self.session.reader.refs_for_generation(generation_id, principal_id)
        for ref in refs:
            self.event(ref)
        return refs

    def record(self, durable_ref, principal_id):
        self._check(principal_id)
        event = self.event(durable_ref)
        record = self.session.reader.record(durable_ref, principal_id)
        if record is None or record.generation_id != event.context.generation_id:
            raise AccessDenied("证据与可信 generation 不匹配")
        return record
