"""念念旧 Gatekeeper 调用契约到公共核心的接线；不配置模型或生产路径。"""
import json

from thalemora.gatekeeper import core
from thalemora.memory import revision_store

GatekeeperUnavailable = core.GatekeeperUnavailable
InvalidGateDecision = core.InvalidGateDecision


class NiannianGatekeeper:
    def __init__(self, *, memory_db, ops_db, raw_db, profile, provider,
                 frozen_candidates, embedding_provider=None, embedding_model=None):
        self.memory_db = memory_db
        self.ops_db = ops_db
        self.raw_db = raw_db
        self.profile = profile
        self.provider = provider
        self.frozen_candidates = frozen_candidates
        self.embedding_provider = embedding_provider
        self.embedding_model = embedding_model

    def apply_candidate(self, candidate_id, *, ops_db_path=None, memory_db_path=None,
                        frozen_map=None, provider=None, embedding_provider=None):
        return core.apply_candidate(
            candidate_id, ops_db_path=ops_db_path or self.ops_db,
            memory_db_path=memory_db_path or self.memory_db, raw_db_path=self.raw_db,
            profile=self.profile(), frozen_map=frozen_map, include_read_only=True,
            provider=provider or self.provider,
            embedding_provider=embedding_provider or self.embedding_provider,
            embedding_model=self.embedding_model,
        )

    def apply(self, new_info, title=None, keywords="", source="chenlin", *,
              frozen_map=None, provider=None, embedding_provider=None, memory_db_path=None):
        # 保留已上线旧 DB 的作者/证据枚举；不批量改写历史 CHECK 或来源。
        if source != "chenlin":
            raise ValueError("use_candidate_apply")
        payload = {
            "title": str(title or "").strip(), "content": str(new_info or "").strip(),
            "keywords": [x.strip() for x in str(keywords or "").split(",") if x.strip()],
            "importance": None, "valence": None, "arousal": None,
            "happened_at": None, "claim_scope": "assistant_self",
        }
        frozen = (self.frozen_candidates(payload["content"]) if frozen_map is None
                  else list(frozen_map))
        decision, frozen = core.decide(payload, frozen, provider or self.provider)
        if decision["action"] in {"drop", "uncertain"}:
            return {"ok": True, "status": decision["action"],
                    "action": decision["action"], "reason": decision["reason"]}
        evidence_ref = json.dumps(
            {"content_hash": core._stable_hash(payload["content"]), "source": source},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        committed = core._commit(
            decision, payload, frozen, memory_db_path=memory_db_path or self.memory_db,
            candidate_id=None, source=source, origin="chenlin_mcp",
            evidence_source_kind="chenlin_mcp", evidence_source_ref=evidence_ref,
            embedding_provider=embedding_provider or self.embedding_provider,
            embedding_model=self.embedding_model,
            agent_source="chenlin", agent_field_source="chenlin_explicit",
        )
        result = {"ok": True, "status": "applied", "action": decision["action"],
                  "card_id": committed["card_id"],
                  "current_revision_id": committed["current_revision_id"]}
        if decision["action"] == "extend":
            result["extension_id"] = committed["extension_id"]
        return result
