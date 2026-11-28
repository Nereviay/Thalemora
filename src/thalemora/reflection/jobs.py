from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
from typing import Any, Mapping, Sequence

from thalemora.gatekeeper import keywords as keyword_policy
from thalemora.memory import revision_store
from .maintenance import (
    ReflectiveMaintenanceShadowStore,
    plan_reflective_maintenance,
    validate_reflection,
)
from thalemora.scene.runtime import SceneRuntime


_CORRECTION_RE = re.compile(
    r"(?:不是这个意思|你记错|记错了|搞错了|我说错|我改口|纠正一下|其实不是|前面说错)"
)
_IMPORTANT_RE = re.compile(
    r"(?:记住|别忘|你要记得|这个很重要|这件事很重要|以后别再忘|一定要记)"
)


class ReflectiveJobError(RuntimeError):
    pass


def _ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _vector_cosine(hit: Mapping) -> float | None:
    for reason in hit.get("retrieval_reasons") or ():
        text = str(reason)
        if text.startswith("vector_rank:"):
            try:
                return float(text.rsplit(":", 1)[1])
            except (TypeError, ValueError):
                return None
    return None


def _has_lexical(hit: Mapping) -> bool:
    return any(str(x).startswith("lexical_rank:") for x in hit.get("retrieval_reasons") or ())


def _scene_query(evidence_events: Sequence[Mapping]) -> str:
    parts = []
    for event in evidence_events[-18:]:
        text = re.sub(r"\s+", " ", str(event.get("quotable_text") or "")).strip()
        if text:
            parts.append(text[:260])
    return "\n".join(parts)[-3200:]


def _maintenance_evidence_window(
    evidence_events: Sequence[Mapping],
    *,
    max_events: int = 24,
) -> list[dict]:
    """Bound one reflective run to the locally relevant tail of a broad Scene.

    Scene is allowed to be broader than one maintenance decision. Reflection
    must not receive hours of unrelated material just because real-time Scene
    segmentation intentionally under-cuts.
    """
    events = [dict(item) for item in evidence_events if isinstance(item, Mapping)]
    limit = max(6, int(max_events))
    if len(events) <= limit:
        return events

    signal_indices = []
    for index, event in enumerate(events):
        if str(event.get("role") or "") != "user":
            continue
        text = str(event.get("quotable_text") or "")
        if _CORRECTION_RE.search(text) or _IMPORTANT_RE.search(text):
            signal_indices.append(index)

    if signal_indices:
        center = signal_indices[-1]
        before = max(4, limit // 2)
        start = max(0, center - before)
        end = min(len(events), start + limit)
        start = max(0, end - limit)
        return events[start:end]

    return events[-limit:]


def _raw_event_for_quote(raw_db: str, event_id: int) -> dict:
    with _ro(raw_db) as conn:
        row = conn.execute(
            "SELECT id,event_hash,role,text,metadata_json,created_at,deleted "
            "FROM raw_events WHERE id=?",
            (int(event_id),),
        ).fetchone()
    if row is None or int(row["deleted"] or 0) == 1:
        raise ReflectiveJobError(f"raw event unavailable: {event_id}")
    text = str(row["text"] or "")
    if str(row["role"] or "") == "user":
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except Exception:
            meta = {}
        authored = meta.get("authored_text") if isinstance(meta, dict) else None
        if isinstance(authored, str) and authored.strip():
            text = authored
    return {
        "id": int(row["id"]),
        "event_hash": str(row["event_hash"] or ""),
        "role": str(row["role"] or ""),
        "text": text,
        "created_at": str(row["created_at"] or ""),
    }


def _evidence_rows(raw_db: str, scene_id: str, candidate: Mapping) -> list[dict]:
    rows = []
    for evidence in candidate.get("evidence") or ():
        event_id = int(evidence["event_id"])
        quote = str(evidence.get("quote") or "")
        raw = _raw_event_for_quote(raw_db, event_id)
        start = raw["text"].find(quote)
        if start < 0:
            raise ReflectiveJobError(
                f"validated quote no longer matches raw event {event_id}"
            )
        rows.append({
            "source_kind": (
                "user_text_exact"
                if raw["role"] == "user"
                else "assistant_text_exact"
            ),
            "source_ref": f"reflective_scene:{scene_id}:raw:{event_id}",
            "raw_event_start_id": event_id,
            "raw_event_end_id": event_id,
            "event_hash": raw["event_hash"],
            "char_start": start,
            "char_end": start + len(quote),
            "quote_hash": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        })
    if not rows:
        raise ReflectiveJobError("revision candidate has no exact evidence")
    return rows


class ReflectiveJobRuntime:
    def __init__(
        self,
        *,
        scene_runtime: SceneRuntime,
        retrieval_index,
        embedder,
        reflective_store: ReflectiveMaintenanceShadowStore,
        memory_db: str,
        raw_db: str,
        search_memory=None,
        refresh_memory=None,
        revision_origin="agent_mcp",
        normalize_events=list,
    ):
        self.scene_runtime = scene_runtime
        self.search_memory = search_memory or retrieval_index.search
        self.refresh_memory = refresh_memory or retrieval_index.refresh_memory_card
        self.revision_origin = revision_origin
        self.normalize_events = normalize_events
        self.embedder = embedder
        self.store = reflective_store
        self.memory_db = os.path.abspath(memory_db)
        self.raw_db = os.path.abspath(raw_db)

    def _regular_runs_today(self) -> int:
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM reflective_memory_receipts "
                "WHERE substr(created_at,1,10)=?",
                (today,),
            ).fetchone()
        return int(row["n"] or 0)

    def _memory_rows(self, ids: Sequence[int]) -> list[dict]:
        ids = [int(x) for x in ids if int(x) > 0]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with _ro(self.memory_db) as conn:
            rows = conn.execute(
                "SELECT c.id,c.title,c.content,c.status,c.importance,c.keywords,"
                "c.source,c.memory_kind,c.current_revision_id,cr.origin,cr.change_type "
                "FROM cards c LEFT JOIN card_revisions cr "
                "ON cr.id=c.current_revision_id "
                f"WHERE c.id IN ({placeholders}) AND c.status!='trashed'",
                ids,
            ).fetchall()
        by_id = {int(row["id"]): dict(row) for row in rows}
        return [by_id[x] for x in ids if x in by_id]

    def _overlap_hits(self, query: str) -> list[dict]:
        hits = self.search_memory(query, top_k=8, pool=40)
        selected = []
        for hit in hits:
            cosine = _vector_cosine(hit)
            # Reflective maintenance is low-frequency. Ordinary semantic
            # resemblance is insufficient evidence for a structural change.
            # Require a literal overlap or a clearly strong local-vector match.
            if _has_lexical(hit) or (cosine is not None and cosine >= 0.66):
                selected.append(hit)
            if len(selected) >= 5:
                break
        return selected

    def next_job(self) -> dict:
        self.scene_runtime.sync()
        regular_runs_today = self._regular_runs_today()
        for scene in self.scene_runtime.finalized_unconsidered(limit=16):
            full_evidence = self.normalize_events(self.scene_runtime.scene_evidence(scene))
            evidence = _maintenance_evidence_window(full_evidence, max_events=24)
            if len(evidence) < 2:
                self.scene_runtime.mark_considered(
                    scene["scene_id"],
                    int(scene["current_revision_no"]),
                    "skip",
                    "too_few_events",
                )
                continue
            query = _scene_query(evidence)
            if not query:
                self.scene_runtime.mark_considered(
                    scene["scene_id"],
                    int(scene["current_revision_no"]),
                    "skip",
                    "empty_scene_text",
                )
                continue
            overlaps = self._overlap_hits(query)
            overlap_ids = [
                int((item.get("provenance") or {}).get("card_id") or item["source_id"])
                for item in overlaps
            ]
            user_text = "\n".join(
                str(item.get("quotable_text") or "")
                for item in evidence
                if item.get("role") == "user"
            )
            correction = bool(_CORRECTION_RE.search(user_text))
            important = bool(_IMPORTANT_RE.search(user_text))
            admission_disposition = (
                "promote_memory" if important or correction else "scene_only"
            )
            plan = plan_reflective_maintenance(
                scene_finalized=True,
                admission_disposition=admission_disposition,
                overlap_memory_ids=overlap_ids,
                contradiction=False,
                correction=correction,
                repeated_reactivation_count=0,
                user_marked_important=important,
                regular_runs_today=regular_runs_today,
                max_regular_runs_per_day=2,
            )
            if not plan.get("should_run"):
                self.scene_runtime.mark_considered(
                    scene["scene_id"],
                    int(scene["current_revision_no"]),
                    "skip",
                    ",".join(plan.get("reasons") or ("not_eligible",)),
                )
                continue
            existing = self._memory_rows(overlap_ids)
            job_id = (
                f"reflective:{scene['scene_id']}:"
                f"{int(scene['current_revision_no'])}"
            )
            evidence_ids = [
                int(item["event_id"])
                for item in evidence
                if item.get("event_id") is not None
            ]
            boundary_trace = self.scene_runtime.boundary_trace(scene)
            relations = [
                dict(item)
                for item in (boundary_trace.get("relations") or ())
                if isinstance(item, Mapping)
            ]
            memory_refs = [
                {
                    "card_id": int(item["id"]),
                    "revision_id": int(item.get("current_revision_id") or 0),
                }
                for item in existing
            ]
            return {
                "ok": True,
                "has_job": True,
                "job_id": job_id,
                "scene": {
                    "scene_id": scene["scene_id"],
                    "revision_no": int(scene["current_revision_no"]),
                    "state": scene["state"],
                    "title_hint": scene.get("title_hint") or "",
                    "topic_hint": scene.get("topic_hint") or "",
                },
                "evidence_events": evidence,
                "existing_memories": existing,
                "overlap_memory_ids": overlap_ids,
                "planner": plan,
                "query": query,
                "trace": {
                    "job_id": job_id,
                    "scene_id": str(scene["scene_id"]),
                    "scene_revision_no": int(scene["current_revision_no"]),
                    "scene_event_count": len(full_evidence),
                    "evidence_event_ids": evidence_ids,
                    "evidence_first_id": evidence_ids[0] if evidence_ids else None,
                    "evidence_last_id": evidence_ids[-1] if evidence_ids else None,
                    "memory_refs": memory_refs,
                    "scene_state": str(scene.get("state") or ""),
                    "boundary_review": boundary_trace.get("boundary_review"),
                    "accepted_boundaries": list(
                        boundary_trace.get("accepted_boundaries") or ()
                    ),
                    "relations": relations,
                    "planner_reasons": list(plan.get("reasons") or ()),
                    "planner_urgent": bool(plan.get("urgent")),
                    "admission_disposition": admission_disposition,
                },
            }
        return {"ok": True, "has_job": False}

    def retire_failed_job(self, job: Mapping[str, Any], reason: str) -> dict:
        """Retire an exhausted Scene revision, never a canonical Memory card."""
        scene = job.get("scene") if isinstance(job.get("scene"), Mapping) else {}
        scene_id = str(scene.get("scene_id") or "").strip()
        revision_no = int(scene.get("revision_no") or 0)
        if not scene_id or revision_no <= 0 or job.get("job_id") != f"reflective:{scene_id}:{revision_no}":
            return {"ok": False, "reason": "invalid_job"}
        current = self.scene_runtime.store.get_scene(scene_id)
        if not current or int(current.get("current_revision_no") or 0) != revision_no or current.get("state") != "finalized":
            return {"ok": False, "reason": "stale_scene"}
        self.scene_runtime.mark_considered(scene_id, revision_no, "failed", "retry_exhausted:" + str(reason or "unknown")[:80])
        return {"ok": True, "scene_id": scene_id, "revision_no": revision_no, "decision": "failed"}

    def record_result(
        self,
        job: Mapping[str, Any],
        *,
        normal_provider_messages: Sequence[Mapping[str, Any]],
        raw_output: Any,
    ) -> dict:
        scene = job.get("scene") if isinstance(job.get("scene"), Mapping) else {}
        scene_id = str(scene.get("scene_id") or "").strip()
        revision_no = int(scene.get("revision_no") or 0)
        if not scene_id or revision_no <= 0:
            raise ReflectiveJobError("invalid reflective job")
        evidence = job.get("evidence_events")
        if not isinstance(evidence, list):
            raise ReflectiveJobError("job evidence missing")
        evidence = self.normalize_events(evidence)
        available_ids = {
            int(item.get("id"))
            for item in (job.get("existing_memories") or ())
            if isinstance(item, Mapping) and item.get("id") is not None
        }
        validated = validate_reflection(
            evidence,
            raw_output,
            available_memory_ids=available_ids,
        )
        existing_by_id = {
            int(item.get("id")): item
            for item in (job.get("existing_memories") or ())
            if isinstance(item, Mapping) and item.get("id") is not None
        }
        target_revision_refs = []
        for target_id in validated.get("target_memory_ids") or ():
            target = existing_by_id.get(int(target_id))
            revision_id = int((target or {}).get("current_revision_id") or 0)
            if revision_id > 0:
                target_revision_refs.append({
                    "card_id": int(target_id),
                    "revision_id": revision_id,
                })
        receipt = self.store.record(
            run_id=str(job.get("job_id") or f"reflective:{scene_id}:{revision_no}"),
            scene_id=scene_id,
            scene_revision_no=revision_no,
            normal_provider_messages=normal_provider_messages,
            evidence_events=evidence,
            raw_output=raw_output,
            available_memory_ids=available_ids,
            target_revision_refs=target_revision_refs,
        )
        frozen_revision_by_id = {
            int(item["card_id"]): int(item["revision_id"])
            for item in receipt.get("target_revision_refs") or ()
        }

        # Serialize retries and keep the first terminal outcome alongside the
        # immutable receipt. Canonical candidate IDs recover a commit if the
        # process stopped between the two databases' commits.
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            outcome = conn.execute(
                "SELECT outcome_json FROM reflective_memory_apply_outcomes WHERE receipt_id=?",
                (receipt["receipt_id"],),
            ).fetchone()
            if outcome:
                applied = json.loads(outcome["outcome_json"])
                if (
                    validated["action"] == "revise_memory"
                    and applied.get("status") == "applied_index_refresh_failed"
                ):
                    applied = self._repair_index_refresh(applied)
                    conn.execute(
                        "UPDATE reflective_memory_apply_outcomes SET outcome_json=? WHERE receipt_id=?",
                        (json.dumps(applied, ensure_ascii=False, sort_keys=True), receipt["receipt_id"]),
                    )
            else:
                applied = {
                    "status": "proposal_only",
                    "reason": "structural_action_requires_system_reconciliation",
                }
                if validated["action"] == "revise_memory":
                    target_id = int(validated["target_memory_ids"][0])
                    expected_revision_id = int(frozen_revision_by_id.get(target_id) or 0)
                    applied = self._apply_revision(
                        scene_id,
                        revision_no,
                        validated,
                        expected_current_revision_id=expected_revision_id,
                    )
                conn.execute(
                    "INSERT INTO reflective_memory_apply_outcomes(receipt_id,outcome_json) VALUES(?,?)",
                    (receipt["receipt_id"], json.dumps(applied, ensure_ascii=False, sort_keys=True)),
                )

        self.scene_runtime.mark_considered(
            scene_id,
            revision_no,
            "recorded:" + validated["action"],
            applied.get("status") or "recorded",
        )
        return {
            "ok": True,
            "receipt": receipt,
            "action": validated["action"],
            "target_memory_ids": list(validated.get("target_memory_ids") or ()),
            "apply": applied,
        }

    def _repair_index_refresh(self, applied: Mapping) -> dict:
        card_id = int(applied.get("card_id") or 0)
        revision_id = int(applied.get("revision_id") or 0)
        if card_id <= 0 or revision_id <= 0:
            return dict(applied)
        try:
            self.refresh_memory(card_id, embedder=self.embedder)
        except Exception as exc:
            return {
                "status": "applied_index_refresh_failed",
                "card_id": card_id,
                "revision_id": revision_id,
                "reason": type(exc).__name__,
            }
        return {
            "status": "applied",
            "card_id": card_id,
            "revision_id": revision_id,
        }

    def _apply_revision(
        self,
        scene_id: str,
        scene_revision_no: int,
        validated: Mapping,
        *,
        expected_current_revision_id: int,
    ) -> dict:
        target_id = int(validated["target_memory_ids"][0])
        candidate = dict(validated["candidates"][0])
        candidate_id = (
            f"reflective:{scene_id}:{scene_revision_no}:"
            f"memory:{target_id}"
        )
        committed = revision_store.find_candidate_revision(self.memory_db, candidate_id)
        if committed:
            return self._repair_index_refresh({"status": "applied", **committed})
        if expected_current_revision_id <= 0:
            return {"status": "blocked", "reason": "target_revision_missing"}
        with _ro(self.memory_db) as conn:
            current = conn.execute(
                "SELECT c.*,cr.origin AS revision_origin,cr.change_type AS revision_change_type "
                "FROM cards c JOIN card_revisions cr ON cr.id=c.current_revision_id "
                "WHERE c.id=?",
                (target_id,),
            ).fetchone()
        if current is None:
            return {"status": "blocked", "reason": "target_missing"}
        if int(current["current_revision_id"]) != expected_current_revision_id:
            return {
                "status": "conflict",
                "card_id": target_id,
                "expected_revision_id": expected_current_revision_id,
                "current_revision_id": int(current["current_revision_id"]),
            }
        if str(current["memory_kind"] or "") != "episodic":
            return {"status": "blocked", "reason": "non_episodic_target"}
        if str(current["source"] or "") == "manual":
            return {"status": "blocked", "reason": "manual_source"}
        if str(current["revision_origin"] or "") == "manual_ui":
            return {"status": "blocked", "reason": "manual_ui_current_revision"}

        new_keywords = keyword_policy.merge_keywords(
            current["keywords"],
            ",".join(candidate.get("keywords") or ()),
            title=candidate.get("title") or "",
            tags=current["tags"] or "",
        )
        evidence_rows = _evidence_rows(self.raw_db, scene_id, candidate)
        try:
            result = revision_store.update_card(
                self.memory_db,
                card_id=target_id,
                expected_current_revision_id=expected_current_revision_id,
                content=candidate.get("content") or current["content"],
                title=candidate.get("title") or current["title"],
                keywords=new_keywords,
                # Personality does not directly control importance/emotion/status/time.
                importance=revision_store.UNCHANGED,
                valence=revision_store.UNCHANGED,
                arousal=revision_store.UNCHANGED,
                happened_at=revision_store.UNCHANGED,
                embedding=revision_store.UNCHANGED,
                change_type="correct" if "correction" in (validated.get("reason") or "").lower() else "consolidate",
                origin=self.revision_origin,
                evidence_rows=evidence_rows,
                candidate_id=candidate_id,
                forbidden_memory_kind="timeline",
            )
        except revision_store.RevisionConflict as exc:
            return {
                "status": "conflict",
                "card_id": target_id,
                "expected_revision_id": exc.expected_revision_id,
                "current_revision_id": exc.current_revision_id,
            }
        if result.get("changed"):
            try:
                self.refresh_memory(target_id, embedder=self.embedder)
            except Exception as exc:
                return {
                    "status": "applied_index_refresh_failed",
                    "card_id": target_id,
                    "revision_id": result.get("current_revision_id"),
                    "reason": type(exc).__name__,
                }
        return {
            "status": "applied" if result.get("changed") else "no_op",
            "card_id": target_id,
            "revision_id": result.get("current_revision_id"),
        }
