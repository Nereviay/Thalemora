"""念念现役可信证据源到公共 Self Model 合同的适配。

source 必须由宿主用 canonical conversation/generation reader 构造，principal
由宿主配置；不接受客户端自报 actor，不创建另一套 Runtime 或证据事实。
"""
from dataclasses import asdict

from thalemora.self_model.evidence import (
    AuthoritativeRecord, EvidenceResolutionError, GenerationState,
)


class RuntimeEvidenceAdapter:
    def __init__(self, source, *, principal_id):
        self.source = source
        self.principal_id = principal_id

    def _check(self, principal_id):
        if principal_id != self.principal_id:
            raise EvidenceResolutionError("principal_mismatch", "宿主证据不属于该 Agent")

    def generation_state(self, generation_id, principal_id):
        self._check(principal_id)
        state = self.source.generation_state(generation_id)
        return GenerationState(**asdict(state)) if state is not None else None

    def refs_for_generation(self, generation_id, principal_id):
        self._check(principal_id)
        return self.source.refs_for_generation(generation_id)

    def record(self, durable_ref, principal_id):
        self._check(principal_id)
        record = self.source.record_for_ref(durable_ref)
        return AuthoritativeRecord(**asdict(record)) if record is not None else None
