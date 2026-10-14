"""复用 mood_engine 的 11 维及事件字段；只保存完整快照，不生成或轮询。"""
from thalemora.inner_world.store import StateDomain, event_provenance

DIM_KEYS = ("valence", "arousal", "longing", "security", "attachment",
            "protectiveness", "tenderness", "jealousy", "desire", "confidence", "guilt")
DIM_RANGES = {"valence": (-10, 10), **{k: (0, 10) for k in DIM_KEYS if k != "valence"}}
SOURCE_TYPES = {"conversation", "memory", "activity", "drift"}


class Emotion(StateDomain):
    kind = "emotion"

    def import_event(self, ref, *, autonomous=False):
        event, record, data = self.session.verified_event(ref, kind=self.kind, autonomous=autonomous)
        if set(data) != {"dims", "label", "emoji", "note", "source", "source_type"}:
            raise ValueError("Emotion 需要完整 11 维 mood 事件")
        dims = data["dims"]
        if not isinstance(dims, dict) or set(dims) != set(DIM_KEYS):
            raise ValueError("dims 必须含完整 11 维")
        for key, value in dims.items():
            lo, hi = DIM_RANGES[key]
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError("情绪维度超出 mood_engine 定义范围")
        if data["source_type"] not in SOURCE_TYPES:
            raise ValueError("未知情绪来源类型")
        if any(not isinstance(data[k], str) for k in ("label", "emoji", "note", "source")):
            raise ValueError("情绪文字字段必须为字符串")
        return self.store.append(self.kind, data, event_provenance(self.session, event, record),
                                 recorded_at=record.occurred_at, source_ref=ref)

    def publish(self, ref):
        return self.import_event(ref, autonomous=True)

    def latest(self):
        # 头索引访问，不扫描历史/补全稀疏生产旧行。
        with self.store.connect() as db:
            row = db.execute(
                "SELECT v.* FROM inner_heads h JOIN inner_versions v ON v.seq=h.seq "
                "WHERE h.channel=? AND h.kind=? AND v.status='active' ORDER BY h.seq DESC LIMIT 1",
                (self.session.channel, self.kind),
            ).fetchone()
        return self.store.decode(row) if row else None
