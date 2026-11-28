from __future__ import annotations
import hashlib
import json
import os
import re
from typing import Any, Mapping, Sequence
from thalemora.gatekeeper.core import decide, freeze_candidate_map_from_card_ids
from thalemora.scene import strict, admission
from thalemora.scene.strict_prompt import STRICT_SYSTEM_PROMPT
from thalemora.scene.runtime import SceneRuntime
from thalemora.retrieval.core import RetrievalIndex
from thalemora.gatekeeper.profiles import profile_fingerprint

from thalemora.retrieval.embedding_defaults import SCENE_LOCAL_MODEL as DEFAULT_EMBEDDING_MODEL


def freeze(ids, *, memory_db_path):
    return freeze_candidate_map_from_card_ids(ids, memory_db_path=memory_db_path)

class SceneWriterPreviewError(RuntimeError):
    pass


class SceneWriterPreviewBlocked(SceneWriterPreviewError):
    def __init__(self, requested_disposition: str, reason: str):
        super().__init__(reason)
        self.requested_disposition = str(requested_disposition)
        self.reason = str(reason)


def _stable_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def scene_source_fingerprint(scene: Mapping, events: Sequence[Mapping]) -> str:
    return _sha({
        "scene_id": str(scene.get("scene_id") or ""),
        "scene_revision_no": int(scene.get("current_revision_no") or 0),
        "events": [
            {
                "event_id": int(item.get("event_id") or 0),
                "role": str(item.get("role") or ""),
                "text_sha256": hashlib.sha256(
                    str(item.get("quotable_text") or "").encode("utf-8")
                ).hexdigest(),
            }
            for item in events
        ],
    })


def candidate_core(route: Mapping) -> dict:
    return {
        key: route.get(key)
        for key in (
            "title",
            "content",
            "keywords",
            "importance",
            "valence",
            "arousal",
            "claim_scope",
        )
    }


def _decision_summary(decision: Mapping) -> dict:
    allowed = (
        "action",
        "target_card_id",
        "same_memory",
        "same_memory_confidence",
        "content",
        "extension",
        "category",
        "importance",
        "reason",
    )
    return {key: decision.get(key) for key in allowed if key in decision}

def _adjacent_user_context(
    route: Mapping, events: Sequence[Mapping], *, lookback: int = 2
) -> str | None:
    """Use preceding user message only within the passed validated Scene."""
    cited = {
        int(x["event_id"]) for x in (route.get("evidence") or ())
        if isinstance(x, Mapping) and str(x.get("event_id") or "").isdigit()
    }
    if not cited:
        return None
    by_id = {
        int(e["event_id"]): i
        for i, e in enumerate(events)
        if str(e.get("event_id") or "").isdigit()
    }
    positions = [by_id[i] for i in cited if i in by_id]
    if not positions:
        return None
    earliest = min(positions)
    for i in range(earliest-1, max(-1,earliest-1-lookback), -1):
        event = events[i]
        if str(event.get("role") or "").strip().lower() not in {"user"}:
            continue
        text = str(event.get("quotable_text") or "").strip()
        return text[:220] if len(text)>=12 else None
    return None


class SceneWriterAdmissionPreviewRuntime:
    def __init__(
        self,
        *,
        scene_runtime: SceneRuntime,
        retrieval_index: RetrievalIndex,
        embedder,
        admission_store: admission.SceneAdmissionPreviewStore,
        memory_db: str,
        run_id: str,
        admission_provider,
        strict_provider,
        gate_provider,
        writer_profile: Mapping, gate_profile, perspective="", fingerprint_roles=None, role_aliases=None, search_memory=None, decide_fn=decide,
        max_gate_previews: int = 3,
    ):
        self.scene_runtime = scene_runtime
        self.index = retrieval_index
        self.search_memory = search_memory or retrieval_index.search
        self.embedder = embedder
        self.store = admission_store
        self.memory_db = os.path.abspath(memory_db)
        self.run_id = str(run_id)
        self.admission_provider = admission_provider
        self.strict_provider = strict_provider
        self.decide = decide_fn
        self.gate_provider = gate_provider
        self.max_gate_previews = max(0, min(int(max_gate_previews), 8))
        self.writer_profile = dict(writer_profile)
        self.gate_profile = gate_profile
        self.perspective = perspective
        self.fingerprint_roles = fingerprint_roles or {}
        self.role_aliases = role_aliases or {}

    def _frozen_map(
        self, route: Mapping, events: Sequence[Mapping] = ()
    ) -> list[dict]:
        """Freeze 8 current cards with bounded adjacent-user context recall.

        Candidate-text search remains primary. Only when a directly preceding
        SAME-Scene user message is topically close do we spend two slots on
        extra old-card candidates. This affects recall only: the existing
        Gatekeeper still decides whether to new/extend/drop.
        """
        query = "\n".join(
            part
            for part in (
                str(route.get("title") or "").strip(),
                str(route.get("content") or "").strip(),
                " ".join(str(x) for x in (route.get("keywords") or ())),
            )
            if part
        )
        # FTS5 MATCH parses punctuation as operators; query text is data.
        query = re.sub(r"[^\w\s]", " ", query).strip()
        main_hits = self.search_memory(query, top_k=8, pool=40)
        def _ids(hits):
            ids = []
            for item in hits:
                provenance = (
                    item.get("provenance")
                    if isinstance(item.get("provenance"), Mapping)
                    else {}
                )
                try:
                    card_id = int(provenance.get("card_id", item.get("source_id")))
                except (ValueError, TypeError):
                    continue
                if card_id not in ids:
                    ids.append(card_id)
            return ids

        main_ids = _ids(main_hits)
        context = _adjacent_user_context(route, events)
        if context is not None:
            context = re.sub(r"[^\w\s]", " ", context).strip()
        if not context:
            return freeze(
                main_ids, memory_db_path=self.memory_db
            )

        # Local embedding only; no second Gate/Provider request.
        import numpy as np
        a = np.asarray(self.embedder.encode_one(query), dtype=np.float32)
        b = np.asarray(self.embedder.encode_one(context), dtype=np.float32)
        if (
            a.shape != b.shape or not a.size
            or not np.isfinite(a).all() or not np.isfinite(b).all()
        ):
            return freeze(
                main_ids, memory_db_path=self.memory_db
            )
        norm = float(np.linalg.norm(a) * np.linalg.norm(b))
        similarity = float(np.dot(a, b) / norm) if norm > 0 else 0.0
        if not np.isfinite(similarity) or similarity < 0.40:
            return freeze(
                main_ids, memory_db_path=self.memory_db
            )
        context_ids = _ids(self.search_memory(context, top_k=8, pool=40))
        # Existing freeze rejects manual/manual_ui/timeline update targets.
        context_frozen = freeze(
            [x for x in context_ids if x not in main_ids],
            memory_db_path=self.memory_db
        )
        main = freeze(
            main_ids, memory_db_path=self.memory_db
        )
        # Prefer important contextual facts, but never give up more than
        # one already-found candidate when the primary map is full.
        slots = 1 if len(main) >= 7 else min(2, 8 - len(main))
        context_rank = {cid: rank for rank, cid in enumerate(context_ids)}
        context_frozen.sort(key=lambda item: (
            -float(item.get("importance") or 0),
            context_rank.get(int(item["card_id"]),999),
        ))
        extra = context_frozen[:slots]
        if not extra:
            return main[:8]
        return main[:8-len(extra)] + extra

    def _strict_fallback_judgement(
        self,
        *,
        events: Sequence[Mapping],
        header: Mapping,
    ) -> dict:
        disposition = str(header.get("disposition") or "")
        if disposition not in {"promote_memory", "pending"}:
            raise SceneWriterPreviewError(
                "strict candidate fallback is only valid for durable admission lanes"
            )
        raw = self.strict_provider(
            STRICT_SYSTEM_PROMPT + self.perspective,
            json.dumps(
                {"disposition": disposition, "events": list(events)},
                ensure_ascii=False,
            ),
            self.writer_profile,
        )
        routes = strict.normalize_routes(raw)
        for route in routes:
            strict.finalize_derived_metadata(route)
        validity, _metrics = strict.evaluate_routes(routes, events)
        checks = {
            int(item.get("route_index")): item
            for item in validity
            if item.get("route_index") is not None
        }
        selected = []
        invalid_expected = []
        for index, route in enumerate(routes):
            if route["route"] in {"memory", "plan"}:
                check = checks.get(index, {})
                if check.get("valid"):
                    selected.append(route)
                else:
                    invalid_expected.append(check or {"route_index": index})
        if invalid_expected:
            raise SceneWriterPreviewBlocked(
                disposition,
                "strict fallback candidates failed validation: "
                + _stable_json(invalid_expected),
            )
        if not selected:
            if disposition == "pending":
                raise SceneWriterPreviewBlocked(
                    "pending",
                    "strict admission produced no valid plan candidate",
                )
            judgement = {
                "disposition": "scene_only",
                "reason": (
                    "strict admission vetoed durable promotion: "
                    "no valid memory candidate survived strict evidence validation"
                ),
                "memory_candidates": [],
                "candidate_source": "strict_admission_veto",
                "requested_disposition": disposition,
                "outer_reason": str(header.get("reason") or ""),
            }
            return admission.validate_judgement(events, judgement)

        judgement = {
            "disposition": disposition,
            "reason": str(header.get("reason") or ""),
            "memory_candidates": selected,
            "candidate_source": "strict_admission_fallback",
        }
        return admission.validate_judgement(events, judgement)

    def _record_candidate_previews(
        self,
        *,
        receipt_id: str,
        routes: Sequence[Mapping],
        events: Sequence[Mapping] = (),
    ) -> list[dict]:
        existing = {
            int(item["candidate_index"]): item
            for item in self.store.list_gate_previews(receipt_id)
        }
        previews = []
        gate_calls = 0
        for index, route in enumerate(routes):
            if index in existing:
                item = existing[index]
                previews.append({
                    "candidate_index": index,
                    "route": item.get("route"),
                    "action": item.get("action"),
                    "target_card_id": item.get("target_card_id"),
                    "idempotent": True,
                })
                continue

            route_name = str(route.get("route") or "")
            core = candidate_core(route)
            candidate_hash = _sha({
                "route": route_name,
                "identity": route.get("identity"),
                "memory_type": route.get("memory_type"),
                "core": core,
                "evidence": route.get("evidence") or [],
            })

            if route_name == "plan":
                recorded = self.store.record_gate_preview(
                    receipt_id=receipt_id,
                    candidate_index=index,
                    candidate_hash=candidate_hash,
                    route=route_name,
                    action="pending_route",
                    target_card_id=None,
                    frozen_memory_refs=(),
                    decision_summary={
                        "action": "pending_route",
                        "reason": "scene_admission_pending_lane",
                    },
                )
                previews.append({**recorded, "route": route_name, "idempotent": False})
                continue

            if route_name != "memory":
                raise SceneWriterPreviewError(
                    f"unsupported admitted route: {route_name}"
                )

            if gate_calls >= self.max_gate_previews:
                recorded = self.store.record_gate_preview(
                    receipt_id=receipt_id,
                    candidate_index=index,
                    candidate_hash=candidate_hash,
                    route=route_name,
                    action="deferred_budget",
                    target_card_id=None,
                    frozen_memory_refs=(),
                    decision_summary={
                        "action": "deferred_budget",
                        "reason": "per_scene_gate_preview_budget",
                    },
                )
                previews.append({
                    **recorded,
                    "route": route_name,
                    "idempotent": False,
                })
                continue

            frozen = self._frozen_map(route, events)
            decision, frozen = self.decide(
                core,
                frozen_map=frozen,
                provider=self.gate_provider,
            )
            gate_calls += 1
            refs = [
                {
                    "card_id": int(item["card_id"]),
                    "revision_id": int(item["current_revision_id"]),
                }
                for item in frozen
            ]
            summary = _decision_summary(decision)
            summary["gate_profile_fingerprint"] = profile_fingerprint(
                self.gate_profile()
            )
            recorded = self.store.record_gate_preview(
                receipt_id=receipt_id,
                candidate_index=index,
                candidate_hash=candidate_hash,
                route=route_name,
                action=str(decision.get("action") or ""),
                target_card_id=decision.get("target_card_id"),
                frozen_memory_refs=refs,
                decision_summary=summary,
            )
            previews.append({
                **recorded,
                "route": route_name,
                "idempotent": False,
                "frozen_memory_refs": refs,
                "decision": summary,
            })
        return previews

    def preview_scene(self, scene: Mapping) -> dict:
        scene_id = str(scene.get("scene_id") or "").strip()
        revision_no = int(scene.get("current_revision_no") or 0)
        if not scene_id or revision_no <= 0:
            raise SceneWriterPreviewError("invalid Scene identity")
        if str(scene.get("state") or "") != "finalized":
            raise SceneWriterPreviewError("Scene must be finalized")

        events = self.scene_runtime.scene_evidence(scene)
        if not events:
            return {
                "ok": True,
                "scene_id": scene_id,
                "scene_revision_no": revision_no,
                "state": "skipped",
                "reason": "no_citable_events",
            }
        fingerprint = scene_source_fingerprint(scene, [dict(e, role=self.fingerprint_roles.get(e["role"], e["role"])) for e in events])
        if self.role_aliases:
            events = [dict(e, role=self.role_aliases.get(e["role"], e["role"])) for e in events]
        blocked = self.store.find_blocked(
            run_id=self.run_id,
            scene_id=scene_id,
            scene_revision_no=revision_no,
            source_fingerprint=fingerprint,
        )
        if blocked is not None:
            return {
                "ok": True,
                "scene_id": scene_id,
                "scene_revision_no": revision_no,
                "source_fingerprint": fingerprint,
                "state": "blocked",
                "requested_disposition": blocked["requested_disposition"],
                "reason": blocked["reason"],
                "blocked_id": blocked["blocked_id"],
                "idempotent": True,
                "did_work": False,
                "canonical_memory_mutated": False,
                "production_writer_queue_mutated": False,
            }

        existing = self.store.find_receipt(
            run_id=self.run_id,
            scene_id=scene_id,
            scene_revision_no=revision_no,
            source_fingerprint=fingerprint,
        )
        if existing is None:
            payload = {
                "scene": {
                    "scene_id": scene_id,
                    "revision_no": revision_no,
                    "state": "finalized",
                    "event_count": len(events),
                },
                "events": events,
            }
            raw = self.admission_provider(
                admission.SCENE_ADMISSION_SYSTEM_PROMPT + self.perspective,
                json.dumps(payload, ensure_ascii=False),
                self.writer_profile,
            )
            try:
                validated = admission.validate_judgement(events, raw)
            except admission.SceneAdmissionError:
                header = admission.parse_judgement_header(raw)
                try:
                    validated = self._strict_fallback_judgement(
                        events=events,
                        header=header,
                    )
                except SceneWriterPreviewBlocked as exc:
                    blocked = self.store.record_blocked(
                        run_id=self.run_id,
                        scene_id=scene_id,
                        scene_revision_no=revision_no,
                        source_fingerprint=fingerprint,
                        requested_disposition=exc.requested_disposition,
                        reason=exc.reason,
                        detail={
                            "outer_reason": str(header.get("reason") or ""),
                            "candidate_source": "strict_admission_blocked",
                        },
                    )
                    return {
                        "ok": True,
                        "scene_id": scene_id,
                        "scene_revision_no": revision_no,
                        "source_fingerprint": fingerprint,
                        "state": "blocked",
                        "requested_disposition": exc.requested_disposition,
                        "reason": exc.reason,
                        "blocked_id": blocked["blocked_id"],
                        "idempotent": False,
                        "did_work": True,
                        "canonical_memory_mutated": False,
                        "production_writer_queue_mutated": False,
                    }
            receipt = self.store.record(
                run_id=self.run_id,
                scene_id=scene_id,
                scene_revision_no=revision_no,
                source_fingerprint=fingerprint,
                events=events,
                raw_judgement=validated["judge_output"],
            )
            routes = validated["candidates"]
            idempotent = False
        else:
            receipt = {
                "receipt_id": existing["receipt_id"],
                "disposition": existing["disposition"],
                "candidate_count": len(existing.get("normalized_candidates") or ()),
                "contract_hash": existing["contract_hash"],
            }
            routes = list(existing.get("normalized_candidates") or ())
            idempotent = True

        previews = self._record_candidate_previews(
            receipt_id=receipt["receipt_id"],
            routes=routes,
            events=events,
        )
        did_work = (not idempotent) or any(
            not bool(item.get("idempotent")) for item in previews
        )
        return {
            "ok": True,
            "scene_id": scene_id,
            "scene_revision_no": revision_no,
            "source_fingerprint": fingerprint,
            "receipt_id": receipt["receipt_id"],
            "disposition": receipt["disposition"],
            "candidate_count": len(routes),
            "gate_previews": previews,
            "idempotent": idempotent,
            "did_work": did_work,
            "canonical_memory_mutated": False,
            "production_writer_queue_mutated": False,
        }

    def preview_finalized(self, *, limit: int = 2) -> list[dict]:
        remaining = max(0, min(int(limit), 8))
        if remaining == 0:
            return []
        results = []
        for row in self.scene_runtime.store.list_scenes(states=["finalized"]):
            scene = self.scene_runtime.store.get_scene(row["scene_id"])
            result = self.preview_scene(scene)
            if not result.get("did_work", True):
                continue
            results.append(result)
            if len(results) >= remaining:
                break
        return results
