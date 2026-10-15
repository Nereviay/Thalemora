"""Scene 精确证据及候选载荷；候选只交既有 ops/Gatekeeper。"""
import hashlib
import json
import os
import sqlite3
from typing import Mapping, Sequence

from thalemora.gatekeeper import ops
from thalemora.gatekeeper.core import VALID_ACTIONS
from .store import SceneContractError, SourceConflict

class SceneWriterMaterializationError(RuntimeError):
    pass


def _stable_json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha(value) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _citable_text(row: Mapping) -> str:
    text = str(row.get("text") or "")
    if str(row.get("role") or "") == "user":
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except Exception:
            metadata = {}
        authored = metadata.get("authored_text") if isinstance(metadata, dict) else None
        if isinstance(authored, str) and authored.strip():
            text = authored
    return text


def _verified_evidence(
    *,
    route: Mapping,
    raw_db_path: str,
    scene_id: str,
    scene_revision_no: int,
) -> list[dict]:
    evidence = route.get("evidence") or []
    if not isinstance(evidence, list) or not evidence:
        raise SceneWriterMaterializationError("scene route has no evidence")

    connection = sqlite3.connect(
        f"file:{os.path.abspath(raw_db_path)}?mode=ro", uri=True, timeout=10
    )
    connection.row_factory = sqlite3.Row
    try:
        verified = []
        source_ref = f"scene:{scene_id}:revision:{int(scene_revision_no)}"
        for index, item in enumerate(evidence):
            if not isinstance(item, Mapping):
                raise SceneWriterMaterializationError(
                    f"evidence {index} is not an object"
                )
            try:
                event_id = int(item.get("event_id"))
            except (TypeError, ValueError) as exc:
                raise SceneWriterMaterializationError(
                    f"evidence {index} has invalid event_id"
                ) from exc
            quote = item.get("quote")
            if not isinstance(quote, str) or not quote:
                raise SceneWriterMaterializationError(
                    f"evidence {index} has empty quote"
                )
            row = connection.execute(
                "SELECT id,event_hash,role,text,metadata_json,deleted "
                "FROM raw_events WHERE id=?",
                (event_id,),
            ).fetchone()
            if row is None or int(row["deleted"] or 0) == 1:
                raise SceneWriterMaterializationError(
                    f"evidence {index} raw event unavailable"
                )
            if row["role"] not in {"user", "assistant"}:
                raise SceneWriterMaterializationError("evidence role is not citable")
            payload = dict(row)
            body = _citable_text(payload)
            char_start = body.find(quote)
            if char_start < 0:
                raise SceneWriterMaterializationError(
                    f"evidence {index} quote is no longer exact"
                )
            verified.append({
                "source_kind": (
                    "user_text_exact"
                    if str(row["role"] or "") == "user"
                    else "assistant_text_exact"
                ),
                "source_ref": source_ref,
                "raw_event_start_id": event_id,
                "raw_event_end_id": event_id,
                "event_id": event_id,
                "event_hash": str(row["event_hash"] or ""),
                "char_start": char_start,
                "char_end": char_start + len(quote),
                "quote_hash": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            })
        return verified
    finally:
        connection.close()


def _candidate_payload(
    route: Mapping,
    *,
    verified_evidence: Sequence[Mapping],
    scene_meta: Mapping,
) -> dict:
    payload = {
        key: route.get(key)
        for key in (
            "title",
            "content",
            "keywords",
            "importance",
            "valence",
            "arousal",
            "happened_at",
            "claim_scope",
        )
    }
    payload.update({
        # Already validated by Scene strict admission. Preserve just enough
        # semantic context for the FINAL Gate's long-term-value decision.
        "memory_type": str(route.get("memory_type") or "")[:32],
        "memory_identity": str(route.get("identity") or "")[:120],
        "admission_reason": str(route.get("reason") or "")[:160],
        "verified_evidence": [dict(item) for item in verified_evidence],
        "evidence_state": "verified",
        "scene_admission": dict(scene_meta),
    })
    return payload


def enqueue_candidates(
    routes: Sequence[Mapping], *, scene_store, scene_id: str,
    expected_revision_no: int, raw_db_path: str, ops_db_path: str,
    batch_key: str, extractor_model: str,
    gate_previews: Sequence[Mapping],
) -> dict:
    """接收调用者已提取的 memory 路由；核验来源后原子入既有候选队列。

    调用者显式提供 Preview 回执；此入口不执行模型 admission/preview。
    最终价值、更新关系、canonical 写入仍由已有 Gatekeeper 处理。
    """
    with scene_store.transaction():
        scene = scene_store.get_scene(scene_id)
        if scene["current_revision_no"] != expected_revision_no:
            raise SourceConflict("scene revision conflict")
        if scene["state"] != "finalized" or scene["projection_status"] != "current":
            raise SceneContractError("candidate source must be current finalized scene")
        if not routes:
            raise SceneContractError("candidate routes must not be empty")
        refs = {item["raw_event_id"]: item for item in scene["events"]}
        revision = scene_store.revisions(scene_id)[-1]
        candidates = []
        raw_ids = []
        previews = {int(item["candidate_index"]): item for item in gate_previews}
        input_hash = _sha({"scene_id": scene_id, "revision": expected_revision_no,
                           "source_fingerprint": revision["source_fingerprint"],
                           "routes": routes, "previews": gate_previews,
                           "extractor_model": extractor_model})
        previous = ops.get_writer_batch(batch_key, ops_db_path)
        if previous is not None and previous["input_hash"] != input_hash:
            raise SourceConflict("candidate batch conflict")
        for index, route in enumerate(routes):
            if route.get("route") != "memory":
                raise SceneContractError("only memory routes can enter Gatekeeper")
            preview = previews.get(index)
            if preview is not None and preview.get("action") == "deferred_budget":
                continue
            if preview is None or preview.get("action") not in VALID_ACTIONS:
                raise SceneContractError("candidate requires replayable gate preview")
            decision = preview.get("decision_summary")
            frozen_refs = preview.get("frozen_memory_refs")
            if not isinstance(decision, Mapping) or not decision or not isinstance(frozen_refs, list):
                raise SceneContractError("candidate requires preview decision and frozen refs")
            verified = _verified_evidence(
                route=route, raw_db_path=raw_db_path, scene_id=scene_id,
                scene_revision_no=expected_revision_no,
            )
            for evidence in verified:
                ref = refs.get(evidence["event_id"])
                if not evidence["event_hash"]:
                    raise SceneWriterMaterializationError("exact evidence requires source event hash")
                if ref is None or ref["event_hash"] != evidence["event_hash"]:
                    raise SceneWriterMaterializationError("evidence not in current scene source")
                expected_kind = ("user_text_exact" if ref["role"] == "user"
                                 else "assistant_text_exact")
                if ref["role"] not in {"user", "assistant"} or evidence["source_kind"] != expected_kind:
                    raise SceneWriterMaterializationError("evidence source role changed")
            if not route.get("memory_type") or not route.get("identity"):
                raise SceneContractError("candidate must preserve normalized memory type and identity")
            raw_ids.extend(item["event_id"] for item in verified)
            candidate_hash = _sha({"receipt_id": batch_key, "candidate_index": index,
                                   "route": route, "preview_action": preview["action"]})
            candidate_id = hashlib.sha256(
                f"scene-writer:{batch_key}:{index}:{candidate_hash}".encode("utf-8")
            ).hexdigest()
            candidates.append({
                "candidate_id": candidate_id,
                "payload": _candidate_payload(route, verified_evidence=verified, scene_meta={
                    "receipt_id": batch_key,
                    "scene_id": scene_id, "scene_revision_no": expected_revision_no,
                    "source_fingerprint": revision["source_fingerprint"],
                    "candidate_index": index,
                    "gate_action": preview["action"],
                    "gate_decision": dict(decision),
                    "frozen_memory_refs": frozen_refs,
                }),
            })
        if not candidates:
            return {"inserted": False, "candidate_count": 0, "candidate_ids": [],
                    "status": "deferred_budget"}
        inserted = ops.put_writer_batch({
            "batch_key": batch_key, "raw_start_id": min(raw_ids),
            "raw_end_id": max(raw_ids), "input_hash": input_hash,
            "extractor_model": extractor_model,
            "policy_version": "scene-source-candidates-v1",
        }, candidates, ops_db_path)
        return {"inserted": inserted, "candidate_count": len(candidates),
                "candidate_ids": [item["candidate_id"] for item in candidates]}



# 独立与念念 Writer 共用已验的事实边界；不增加额外分类调用。
WRITER_FACT_RULES = '当次“记住、更新、纠正上面那条、不要重复建立”等记忆管理指令不是事实或长期偏好，不单独提成候选。\n事实与指令混在一句时，只提事实内容，逐字证据只引支撑事实的片段；纯管理指令返回空 candidates。\n例如“通知改为电话，请更新原来那条”只提“通知改为电话”，不能再提“用户偏好更新而非新建”。\n明确要求未来长期采用某种记忆管理方式（如“以后所有记忆标题始终用中文”）仍是可提炼的长期偏好，\n不能仅因句子含记忆相关词就过滤。一个事实只提一项，不把同一事实的操作要求拆成第二项。\n'
