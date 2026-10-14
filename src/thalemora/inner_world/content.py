"""本人内容只能取自可信已完成事件；历史导入也不接受请求自报正文/作者。"""
from .store import StateDomain, event_provenance


class AgentContent(StateDomain):
    nature = "personal_record"

    def _save(self, ref, *, autonomous):
        event, record, data = self.session.verified_event(ref, kind=self.kind, autonomous=autonomous)
        allowed = {"text", "title", "entry_id", "segment", "target_id", "expected_version"}
        if set(data) - allowed:
            raise ValueError("内容事件含未知字段")
        text = data.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise ValueError("每段正文须为 1..8000 字符")
        if "title" in data and (not isinstance(data["title"], str) or len(data["title"]) > 200):
            raise ValueError("标题最多 200 字符")
        if ("segment" in data) != ("entry_id" in data):
            raise ValueError("分段须同时指定 entry_id 与 segment")
        if "segment" in data and (type(data["segment"]) is not int or data["segment"] < 0
                                  or not isinstance(data["entry_id"], str) or not data["entry_id"]):
            raise ValueError("分段身份无效")
        target = data.get("target_id")
        expected = data.get("expected_version", 0)
        if target is not None and (not isinstance(target, str) or not target
                                   or type(expected) is not int or expected < 1):
            raise ValueError("修订须指明本人事件中的目标及版本")
        payload = {k: v for k, v in data.items() if k not in {"target_id", "expected_version"}}
        payload["nature"] = self.nature
        return self.store.append(self.kind, payload, event_provenance(self.session, event, record),
                                 recorded_at=record.occurred_at, source_ref=ref,
                                 item_id=target, expected_version=expected)

    def publish(self, ref):
        return self._save(ref, autonomous=True)

    def import_event(self, ref):
        """维护器可导入独立核证的本人历史，不能伪装成当前自主创作。"""
        return self._save(ref, autonomous=False)
