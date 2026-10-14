"""手动/宿主触发的建议和状态审计；没有正式领域写入能力。"""
from thalemora.inner_world.store import StateDomain


class Reflection(StateDomain):
    kind = "reflection"
    TARGETS = {"memory", "self_model", "persona", "emotion", "diary", "dream", "whisper"}
    TRANSITIONS = {"open": {"reviewed", "dismissed"}, "reviewed": {"dismissed"}}

    def propose(self, *, text, target_kind, target_id, target_version, recorded_at):
        self.session.check()
        if (target_kind not in self.TARGETS or not isinstance(target_id, str) or not target_id
                or type(target_version) is not int or target_version < 1
                or not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000):
            raise ValueError("建议须有正文和明确的领域/目标版本")
        return self.store.append(self.kind, {
            "text": text, "target_kind": target_kind, "target_id": target_id,
            "target_version": target_version, "state": "open", "effect": "suggestion_only",
            "target_verified": False,
        }, self._audit("propose"), recorded_at=recorded_at)

    def _audit(self, action):
        return {"agent_id": self.session.agent_id, "principal_id": self.session.owner.principal_id,
                "app_id": self.session.app_id, "channel": self.session.channel,
                "author_role": self.session.role, "action": action}

    def transition(self, item_id, *, expected_version, state, reason, recorded_at):
        current = self.get(item_id)
        if current["status"] != "active" or state not in self.TRANSITIONS.get(current["payload"]["state"], set()):
            raise ValueError("仅支持 open→reviewed/dismissed、reviewed→dismissed")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("状态变更必须记录理由")
        return self.store.append(self.kind, {**current["payload"], "state": state},
                                 {**self._audit("transition"), "reason": reason},
                                 item_id=item_id, expected_version=expected_version, recorded_at=recorded_at)
