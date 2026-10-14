"""手动构建有限连续性候选；不扫描、不写 Runtime、不生成或猜测 epoch。"""
from thalemora.identity.registry import AccessDenied
from thalemora.inner_world.store import StateDomain


class Continuity(StateDomain):
    kind = "continuity"
    SOURCE_KINDS = {"emotion", "diary", "whisper"}

    def _validate_sources(self, candidate):
        for fact in candidate["payload"]["facts"]:
            current = self.store.get(fact["kind"], fact["item_id"])
            if current["status"] != "active":
                raise AccessDenied("连续性来源已撤销")
        return candidate

    def get(self, item_id, *, version=None):
        return self._validate_sources(super().get(item_id, version=version))

    def list(self, *, after=0, limit=20):
        return [self._validate_sources(item) for item in super().list(after=after, limit=limit)]

    def history(self, item_id, *, after=0, limit=20):
        return [self._validate_sources(item) for item in super().history(item_id, after=after, limit=limit)]

    def build(self, epoch_id, sources, *, recorded_at):
        """sources 是至多 20 个 (kind, item_id, version)，只取明确已保存状态。"""
        self.session.require_epoch(epoch_id)
        if not isinstance(sources, (tuple, list)) or not 1 <= len(sources) <= 20:
            raise ValueError("候选必须显式指定 1..20 条来源")
        facts = []
        for kind, item_id, version in sources:
            if kind not in self.SOURCE_KINDS or (kind == "whisper" and self.session.channel != "private"):
                raise AccessDenied("梦境/反思不是真实连续性事实；私有内容不得进入 main")
            current = self.store.get(kind, item_id)
            if current["version"] != version or current["status"] != "active":
                raise ValueError("来源不是明确的当前有效版本")
            facts.append({"kind": kind, "item_id": item_id, "version": version,
                          "payload": current["payload"], "provenance": current["provenance"]})
        return self.store.append(self.kind, {
            "epoch_id": epoch_id, "facts": facts, "provider_visible": False,
            "adopted": False, "purpose": "continuity_candidate",
        }, {"agent_id": self.session.agent_id, "channel": self.session.channel,
            "app_id": self.session.app_id, "principal_id": self.session.owner.principal_id,
            "action": "manual_candidate", "epoch_confirmed": True}, recorded_at=recorded_at)
