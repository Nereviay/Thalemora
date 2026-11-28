from __future__ import annotations
import datetime
import hashlib
import json
import os
import sqlite3
import uuid
from typing import Mapping, Sequence
from . import strict
from .strict_prompt import STRICT_SYSTEM_PROMPT

EVENT_STATE_RULE = """只有用户直接陈述同一现实事项的当前状态时才附加 event_state=ongoing/update/closed；
证据必须是一条完整用户原话。不得把计划、转述、疑问或否定标成已发生。"""
SCENE_ADMISSION_SYSTEM_PROMPT = """从定稿场景提炼值得在三十天后保留的事实、偏好、边界、约定、共同项目或重要经历。
返回 JSON 对象 disposition、reason、memory_candidates。没有长期事实则用 scene_only，纯寒暄可 drop_scene；两者候选为空。
未完成事项用 pending 和 route=plan。明确改变的决定属于当前事实，使用 route=memory，保留旧决定、当前选择和未完成的边界。
两类并存用 promote_memory，分别保留 memory 和 plan，只让 memory 进入长期库。纯猜测不能升级。
每项含 route、identity、memory_type、title、content、keywords、importance、valence、arousal、claim_scope、reason、evidence。
memory_type 可为 stable_fact、preference、agreement、significant_episode、shared_project，计划用 plan。
claim_scope 可为 user_fact、assistant_self、shared、derived，以说话者的逐字引文支持，不代替用户确认。
evidence 每项为 event_id 和连续原文 quote。importance 1–10，valence、arousal 0–1，关键词一至四个，正文简短。""" + EVENT_STATE_RULE

SCHEMA_VERSION = 1


DISPOSITIONS = {"scene_only", "promote_memory", "pending", "drop_scene"}

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS scene_admission_schema (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    schema_version INTEGER NOT NULL,
    authority_note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scene_admission_receipts (
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    scene_revision_no INTEGER NOT NULL,
    source_fingerprint TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK(disposition IN (
        'scene_only','promote_memory','pending','drop_scene'
    )),
    reason TEXT NOT NULL,
    normalized_candidates_json TEXT NOT NULL,
    route_validity_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    judge_output_json TEXT NOT NULL,
    contract_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, scene_id, scene_revision_no, source_fingerprint)
);
CREATE TABLE IF NOT EXISTS scene_admission_blocked (
    blocked_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    scene_revision_no INTEGER NOT NULL,
    source_fingerprint TEXT NOT NULL,
    requested_disposition TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, scene_id, scene_revision_no, source_fingerprint)
);
CREATE TABLE IF NOT EXISTS scene_admission_gate_previews (
    receipt_id TEXT NOT NULL,
    candidate_index INTEGER NOT NULL,
    candidate_hash TEXT NOT NULL,
    route TEXT NOT NULL,
    action TEXT NOT NULL,
    target_card_id INTEGER,
    frozen_memory_refs_json TEXT NOT NULL,
    decision_summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(receipt_id, candidate_index),
    FOREIGN KEY(receipt_id) REFERENCES scene_admission_receipts(receipt_id)
        ON DELETE CASCADE
);
"""


class SceneAdmissionError(RuntimeError):
    pass


def _stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _parse_object(raw) -> dict:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str):
        raise SceneAdmissionError("judge output must be mapping or JSON string")
    text = raw.strip()
    if text.startswith(chr(96) * 3):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SceneAdmissionError("judge output is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise SceneAdmissionError("judge output must be a JSON object")
    return parsed


def normalize_scene_events(events: Sequence[Mapping]) -> list[dict]:
    normalized = []
    seen = set()
    for event in events:
        try:
            event_id = int(event.get("event_id"))
        except (TypeError, ValueError) as exc:
            raise SceneAdmissionError("event_id must be an integer") from exc
        if event_id <= 0 or event_id in seen:
            raise SceneAdmissionError("event ids must be unique positive integers")
        seen.add(event_id)
        role = str(event.get("role") or "").strip()
        if role not in {"user", "assistant"}:
            raise SceneAdmissionError(f"unsupported event role: {role}")
        normalized.append({
            "event_id": event_id,
            "role": role,
            "beijing_time": str(event.get("beijing_time") or ""),
            "quotable_text": str(event.get("quotable_text") or ""),
        })
    if not normalized:
        raise SceneAdmissionError("Scene admission requires source events")
    return normalized


def parse_judgement_header(raw_judgement) -> dict:
    parsed = _parse_object(raw_judgement)
    disposition = str(parsed.get("disposition") or "").strip().lower()
    reason = str(parsed.get("reason") or "").strip()
    if disposition not in DISPOSITIONS:
        raise SceneAdmissionError(f"invalid disposition: {disposition}")
    if not reason:
        raise SceneAdmissionError("admission reason is required")
    return {
        "disposition": disposition,
        "reason": reason,
        "judge_output": parsed,
    }


def validate_judgement(events: Sequence[Mapping], raw_judgement) -> dict:
    scene_events = normalize_scene_events(events)
    header = parse_judgement_header(raw_judgement)
    parsed = header["judge_output"]
    disposition = header["disposition"]
    reason = header["reason"]

    raw_candidates = parsed.get("memory_candidates")
    if raw_candidates is None:
        raw_candidates = []
    if not isinstance(raw_candidates, list):
        raise SceneAdmissionError("memory_candidates must be a list")

    candidates = strict.normalize_routes(raw_candidates)
    route_validity, metrics = strict.evaluate_routes(candidates, scene_events)
    invalid = [item for item in route_validity if not item.get("valid")]

    if disposition in {"scene_only", "drop_scene"} and candidates:
        raise SceneAdmissionError(f"{disposition} must not emit candidates")
    if disposition in {"promote_memory", "pending"}:
        lanes = {item["route"] for item in candidates}
        if not lanes or not lanes <= {"memory", "plan"}:
            raise SceneAdmissionError("memory or plan candidate required")
        if lanes == {"plan"} and disposition == "promote_memory":
            raise SceneAdmissionError("promote_memory requires a memory candidate")
        if "memory" in lanes:
            disposition = "promote_memory"
    if invalid:
        raise SceneAdmissionError(
            "strict-admission validation failed: " + _stable_json(invalid)
        )

    return {
        "disposition": disposition,
        "reason": reason,
        "candidates": candidates,
        "route_validity": route_validity,
        "metrics": metrics,
        "judge_output": parsed,
        "contract_hash": _hash({
            "prompt": SCENE_ADMISSION_SYSTEM_PROMPT,
            "strict_prompt": STRICT_SYSTEM_PROMPT,
            "schema_version": SCHEMA_VERSION,
        }),
    }


class SceneAdmissionPreviewStore:
    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(os.path.expanduser(db_path))
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            row = connection.execute(
                "SELECT schema_version FROM scene_admission_schema WHERE singleton=1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO scene_admission_schema(singleton,schema_version,authority_note) VALUES(1,?,?)",
                    (SCHEMA_VERSION, "shadow receipts only; no production Memory/Pending writes"),
                )
            elif int(row["schema_version"]) != SCHEMA_VERSION:
                raise SceneAdmissionError("scene admission schema mismatch")

    def get_receipt(self, receipt_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM scene_admission_receipts WHERE receipt_id=?",
                (str(receipt_id),),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in (
            "normalized_candidates_json",
            "route_validity_json",
            "metrics_json",
            "judge_output_json",
        ):
            try:
                result[key[:-5]] = json.loads(result.get(key) or "null")
            except Exception:
                result[key[:-5]] = None
        return result

    def find_receipt(
        self,
        *,
        run_id: str,
        scene_id: str,
        scene_revision_no: int,
        source_fingerprint: str,
    ) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM scene_admission_receipts "
                "WHERE run_id=? AND scene_id=? AND scene_revision_no=? "
                "AND source_fingerprint=?",
                (
                    str(run_id),
                    str(scene_id),
                    int(scene_revision_no),
                    str(source_fingerprint),
                ),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in (
            "normalized_candidates_json",
            "route_validity_json",
            "metrics_json",
            "judge_output_json",
        ):
            try:
                result[key[:-5] if key.endswith("_json") else key] = json.loads(
                    result.get(key) or "null"
                )
            except Exception:
                result[key[:-5] if key.endswith("_json") else key] = None
        return result

    def find_blocked(
        self,
        *,
        run_id: str,
        scene_id: str,
        scene_revision_no: int,
        source_fingerprint: str,
    ) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM scene_admission_blocked "
                "WHERE run_id=? AND scene_id=? AND scene_revision_no=? "
                "AND source_fingerprint=?",
                (
                    str(run_id),
                    str(scene_id),
                    int(scene_revision_no),
                    str(source_fingerprint),
                ),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["detail"] = json.loads(item.get("detail_json") or "{}")
        except Exception:
            item["detail"] = {}
        return item

    def record_blocked(
        self,
        *,
        run_id: str,
        scene_id: str,
        scene_revision_no: int,
        source_fingerprint: str,
        requested_disposition: str,
        reason: str,
        detail: Mapping | None = None,
    ) -> dict:
        blocked_id = f"scene-admission-blocked-{uuid.uuid4().hex}"
        created_at = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(timespec="seconds")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT blocked_id FROM scene_admission_blocked "
                "WHERE run_id=? AND scene_id=? AND scene_revision_no=? "
                "AND source_fingerprint=?",
                (
                    str(run_id),
                    str(scene_id),
                    int(scene_revision_no),
                    str(source_fingerprint),
                ),
            ).fetchone()
            if existing is not None:
                blocked_id = str(existing["blocked_id"])
            connection.execute(
                "INSERT OR REPLACE INTO scene_admission_blocked("
                "blocked_id,run_id,scene_id,scene_revision_no,source_fingerprint,"
                "requested_disposition,reason,detail_json,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    blocked_id,
                    str(run_id),
                    str(scene_id),
                    int(scene_revision_no),
                    str(source_fingerprint),
                    str(requested_disposition),
                    str(reason),
                    _stable_json(dict(detail or {})),
                    created_at,
                ),
            )
        return {
            "blocked_id": blocked_id,
            "requested_disposition": str(requested_disposition),
            "reason": str(reason),
        }

    def record_gate_preview(
        self,
        *,
        receipt_id: str,
        candidate_index: int,
        candidate_hash: str,
        route: str,
        action: str,
        target_card_id=None,
        frozen_memory_refs: Sequence[Mapping] = (),
        decision_summary: Mapping | None = None,
    ) -> dict:
        created_at = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(timespec="seconds")
        refs = [
            dict(item) for item in frozen_memory_refs
            if isinstance(item, Mapping)
        ]
        summary = dict(decision_summary or {})
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO scene_admission_gate_previews("
                "receipt_id,candidate_index,candidate_hash,route,action,"
                "target_card_id,frozen_memory_refs_json,decision_summary_json,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    str(receipt_id),
                    int(candidate_index),
                    str(candidate_hash),
                    str(route),
                    str(action),
                    int(target_card_id) if target_card_id is not None else None,
                    _stable_json(refs),
                    _stable_json(summary),
                    created_at,
                ),
            )
        return {
            "receipt_id": str(receipt_id),
            "candidate_index": int(candidate_index),
            "action": str(action),
            "target_card_id": (
                int(target_card_id) if target_card_id is not None else None
            ),
        }

    def list_gate_previews(self, receipt_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM scene_admission_gate_previews "
                "WHERE receipt_id=? ORDER BY candidate_index",
                (str(receipt_id),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("frozen_memory_refs_json", "decision_summary_json"):
                try:
                    item[key[:-5]] = json.loads(item.get(key) or "null")
                except Exception:
                    item[key[:-5]] = None
            result.append(item)
        return result

    def record(
        self,
        *,
        run_id: str,
        scene_id: str,
        scene_revision_no: int,
        source_fingerprint: str,
        events: Sequence[Mapping],
        raw_judgement,
    ) -> dict:
        result = validate_judgement(events, raw_judgement)
        receipt_id = f"scene-admission-{uuid.uuid4().hex}"
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT receipt_id,disposition,normalized_candidates_json,contract_hash FROM scene_admission_receipts "
                "WHERE run_id=? AND scene_id=? AND scene_revision_no=? AND source_fingerprint=?",
                (run_id, scene_id, int(scene_revision_no), source_fingerprint),
            ).fetchone()
            if existing:
                if (existing["disposition"] != result["disposition"] or
                        existing["normalized_candidates_json"] != _stable_json(result["candidates"])):
                    raise SceneAdmissionError("admission receipt already records a different decision")
                return dict(receipt_id=existing["receipt_id"], disposition=existing["disposition"],
                            candidate_count=len(result["candidates"]), contract_hash=existing["contract_hash"])
            connection.execute(
                "INSERT INTO scene_admission_receipts("
                "receipt_id,run_id,scene_id,scene_revision_no,source_fingerprint,"
                "disposition,reason,normalized_candidates_json,route_validity_json,"
                "metrics_json,judge_output_json,contract_hash,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id, str(run_id), str(scene_id), int(scene_revision_no),
                    str(source_fingerprint), result["disposition"], result["reason"],
                    _stable_json(result["candidates"]), _stable_json(result["route_validity"]),
                    _stable_json(result["metrics"]), _stable_json(result["judge_output"]),
                    result["contract_hash"], created_at,
                ),
            )
        return {
            "receipt_id": receipt_id,
            "disposition": result["disposition"],
            "candidate_count": len(result["candidates"]),
            "contract_hash": result["contract_hash"],
        }
