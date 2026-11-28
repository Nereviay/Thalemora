from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

OWNER_SCOPES = {"assistant", "user", "shared", "world", "relationship"}
VISIBILITIES = {"primer", "contextual"}
SENSITIVITIES = {"normal", "sensitive"}
STATUSES = {"active", "retired"}
SOURCE_KINDS = {
    "canonical_raw_event",
    "user_managed_prompt",
    "manual_ui",
    "migration_shadow",
}
HEADER = "【我记得的长期经历】"
PROFILE_HEADER = "【连续性资料（事实投影）】"
STORE_VERSION = "continuity-primer-p0-shadow-v1"
# 字段值只在版本表中保存；此处仅定义合同，不保存任何人的资料。
PROFILE_FIELDS = {
    "user": {
        "display_name": "称呼", "gender": "性别", "birth_date": "出生日期",
        "city": "城市", "education": "学历", "work_status": "工作状态",
        "preferences": "爱好偏好", "long_term_goals": "长期目标", "recent_status": "近期状态",
        "aliases": "其他称呼（保留使用范围）", "birth_place": "出生地", "height": "身高",
        "appearance": "外貌", "diet": "饮食偏好与忌口", "living_arrangement": "居住情况",
        "communication_preferences": "交流偏好",
    },
    "assistant": {
        "display_name": "称呼", "gender": "性别", "birth_date": "出生日期",
        "city": "城市", "education": "学历", "work_status": "工作状态",
        "preferences": "爱好偏好", "long_term_goals": "长期目标", "recent_status": "近期状态",
    },
    "relationship": {
        "status": "关系状态", "started_on": "首次开始日期", "reconfirmed_on": "再次确认日期",
        "milestones": "重要经历", "long_term_goals": "长期目标", "recent_status": "近期状态",
    },
    "shared": {"home": "共同的家", "pet": "共同宠物（身份见资料）",
               "long_term_goals": "共同长期目标", "recent_status": "共同近期状态"},
    "world": {"server_home": "服务器位置", "timezone": "时区", "recent_status": "世界近期状态"},
}
SCOPE_LABELS = {"user": "用户", "assistant": "助手", "relationship": "关系", "shared": "共同", "world": "世界"}
PRIMER_TOKEN_BUDGET = 1200
# 当前状态、身份和互动所需资料先于较长的经历；超预算只省略完整事实。
_PROFILE_PRIORITY = {
    "user.work_status": 0, "relationship.status": 0,
    "user.display_name": 1, "user.aliases": 1, "user.gender": 1,
    "user.diet": 1, "user.birth_date": 2,
    "relationship.started_on": 2, "relationship.reconfirmed_on": 2,
    "shared.pet": 2, "shared.home": 3, "world.server_home": 3,
}
_DATE_FIELDS = {"birth_date", "started_on", "reconfirmed_on"}
_ROLE_ASSIGNMENT_PREFIXES = ("你是", "你的名字是", "当前对话对象是", "{{char}} is", "{{char}}是")
_META_ROLEPLAY_TERMS = ("角色扮演", "扮演", "剧情", "人设", "设定", "出戏")
_FACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS continuity_fact_lineages (
  fact_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  owner_scope TEXT NOT NULL CHECK(owner_scope IN (
    'assistant','user','shared','world','relationship'
  )),
  fact_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(principal_id, owner_scope, fact_key)
);
CREATE TABLE IF NOT EXISTS continuity_fact_versions (
  fact_id TEXT NOT NULL REFERENCES continuity_fact_lineages(fact_id),
  version INTEGER NOT NULL CHECK(version >= 1),
  content TEXT NOT NULL,
  visibility TEXT NOT NULL CHECK(visibility IN ('primer','contextual')),
  sensitivity TEXT NOT NULL CHECK(sensitivity IN ('normal','sensitive')),
  status TEXT NOT NULL CHECK(status IN ('active','retired')),
  source_kind TEXT NOT NULL CHECK(source_kind IN (
    'canonical_raw_event','user_managed_prompt','manual_ui','migration_shadow'
  )),
  source_ref TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)),
  evidence_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY(fact_id, version)
);
CREATE INDEX IF NOT EXISTS idx_continuity_fact_versions_current
  ON continuity_fact_versions(fact_id, version DESC);
CREATE TRIGGER IF NOT EXISTS continuity_fact_lineages_no_update
BEFORE UPDATE ON continuity_fact_lineages
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS continuity_fact_lineages_no_delete
BEFORE DELETE ON continuity_fact_lineages
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS continuity_fact_versions_no_update
BEFORE UPDATE ON continuity_fact_versions
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS continuity_fact_versions_no_delete
BEFORE DELETE ON continuity_fact_versions
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value: object) -> str:
    return str(value or "").strip()


def field_contract_keys() -> set[str]:
    return {f"{scope}.{field}" for scope, fields in PROFILE_FIELDS.items() for field in fields}


def profile_field_schema() -> dict:
    return {
        "version": 1,
        "scopes": {
            scope: {field: {
                "label": label, "type": "date" if field in _DATE_FIELDS else "text",
                "automatic_proposal_supported": scope == "user" and field in {
                    "work_status", "recent_status", "city", "preferences", "education", "long_term_goals",
                },
                "manual_value_edit": scope != "assistant",
                "author": "assistant" if scope == "assistant" else "user",
            } for field, label in fields.items()}
            for scope, fields in PROFILE_FIELDS.items()
        },
        "derived": {"user.age": "user.birth_date + snapshot date", "assistant.age": "assistant.birth_date + snapshot date"},
        "unknown": "omit", "primer_max_tokens": PRIMER_TOKEN_BUDGET,
        "writer_submission": "accepted_writer_candidate_with_canonical_evidence",
    }


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("evidence timestamp requires timezone")
    return parsed


def _evidence_time(item: dict) -> datetime:
    return _parse_time(item["created_at"])


def estimate_tokens(text: str) -> int:
    """固定近似预算：非 ASCII 每字计 2，连续 ASCII 每 4 字符计 1。"""
    return sum(math.ceil(len(part) / 4) if part.isascii() else 2
               for part in re.findall(r"[\x00-\x7f]+|[^\x00-\x7f]", text))


def _validate_field(scope: str, field: str, value: str) -> str:
    if field not in PROFILE_FIELDS.get(scope, {}):
        raise ValueError("unknown continuity field")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("unknown/empty field must not be filled")
    value = value.strip()
    if field in _DATE_FIELDS:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("date field requires YYYY-MM-DD")
        if field == "birth_date" and parsed > datetime.now(timezone.utc).date():
            raise ValueError("birth date cannot be in the future")
    return value


def _require_path(path: str) -> str:
    raw = _clean(path)
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        raise ValueError("explicit filesystem path required")
    p = Path(raw)
    if not p.is_absolute():
        raise ValueError("absolute path required")
    if p.exists() and p.is_symlink():
        raise ValueError("symlink path rejected")
    return str(p)


@dataclass(frozen=True)
class ContinuityFact:
    fact_id: str
    principal_id: str
    owner_scope: str
    fact_key: str
    version: int
    content: str
    visibility: str
    sensitivity: str
    status: str
    source_kind: str
    source_ref: str
    source_hash: str
    created_at: str
    locked: bool = False
    evidence_json: str = "[]"

    def validate(self) -> "ContinuityFact":
        if not _FACT_ID_RE.fullmatch(_clean(self.fact_id)):
            raise ValueError("invalid fact_id")
        if not _clean(self.principal_id):
            raise ValueError("principal_id required")
        if self.owner_scope not in OWNER_SCOPES:
            raise ValueError("invalid owner_scope")
        if self.fact_key in field_contract_keys():
            scope, field = self.fact_key.split(".", 1)
            if scope != self.owner_scope:
                raise ValueError("field owner scope mismatch")
            _validate_field(scope, field, self.content)
        if not _clean(self.fact_key):
            raise ValueError("fact_key required")
        if int(self.version) < 1:
            raise ValueError("version must be >=1")
        if not _clean(self.content):
            raise ValueError("content required")
        if self.visibility not in VISIBILITIES:
            raise ValueError("invalid visibility")
        if self.sensitivity not in SENSITIVITIES:
            raise ValueError("invalid sensitivity")
        if self.status not in STATUSES:
            raise ValueError("invalid status")
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError("invalid source_kind")
        if not _clean(self.source_ref):
            raise ValueError("source provenance required")
        if not _SHA256_RE.fullmatch(_clean(self.source_hash).lower()):
            raise ValueError("source_hash must be sha256")
        if self.locked not in (False, True):
            raise ValueError("locked must be boolean")
        if not isinstance(json.loads(self.evidence_json), list):
            raise ValueError("evidence must be a list")
        return self


class SQLiteContinuityFactsShadowStore:
    def __init__(self, db_path: str):
        self.db_path = _require_path(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(continuity_fact_versions)")}
            if "locked" not in columns:
                conn.execute("ALTER TABLE continuity_fact_versions ADD COLUMN locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1))")
            if "evidence_json" not in columns:
                conn.execute("ALTER TABLE continuity_fact_versions ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '[]'")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def append_fact(
        self,
        *,
        fact_id: str,
        principal_id: str,
        owner_scope: str,
        fact_key: str,
        content: str,
        visibility: str,
        sensitivity: str,
        status: str,
        source_kind: str,
        source_ref: str,
        source_hash: str,
        created_at: str | None = None,
        locked: bool = False,
        evidence_json: str = "[]",
        expected_version: int | None = None,
    ) -> ContinuityFact:
        if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
            raise ValueError("expected_version must be a nonnegative integer")
        created = _clean(created_at) or _utc_now()
        base = ContinuityFact(
            fact_id=_clean(fact_id),
            principal_id=_clean(principal_id),
            owner_scope=_clean(owner_scope),
            fact_key=_clean(fact_key),
            version=1,
            content=_clean(content),
            visibility=_clean(visibility),
            sensitivity=_clean(sensitivity),
            status=_clean(status),
            source_kind=_clean(source_kind),
            source_ref=_clean(source_ref),
            source_hash=_clean(source_hash),
            created_at=created,
            locked=locked,
            evidence_json=evidence_json,
        ).validate()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT * FROM continuity_fact_versions WHERE fact_id=? ORDER BY version DESC LIMIT 1",
                (base.fact_id,),
            ).fetchone()
            if previous is not None:
                if previous["locked"] and base.source_kind != "manual_ui":
                    raise ValueError("field is locked")
                compared = ("content", "visibility", "sensitivity", "status", "source_kind", "source_ref", "source_hash", "locked", "evidence_json")
                # 重试不产生新版本；身份仍由下方 lineage 检查约束。
                same = expected_version is not None and all(previous[name] == getattr(base, name) for name in compared)
                if base.source_kind != "manual_ui" and base.fact_key in field_contract_keys():
                    old_evidence = json.loads(previous["evidence_json"])
                    old_time = max((_evidence_time(item) for item in old_evidence), default=_parse_time(previous["created_at"]))
                    new_evidence = json.loads(base.evidence_json)
                    older = not new_evidence or min(_evidence_time(item) for item in new_evidence) <= old_time
                    if older and old_evidence and new_evidence and previous["source_kind"] == "canonical_raw_event":
                        # Raw timestamps may have second precision. Within this
                        # one raw event log its durable IDs disambiguate order;
                        # manual corrections still require a strictly later time.
                        order = lambda item: (_evidence_time(item), int(item["event_id"]))
                        older = min(map(order, new_evidence)) <= max(map(order, old_evidence))
                    if not new_evidence or (not same and older):
                        raise ValueError("automatic evidence is older than current field")
            else:
                same = False
            actual_version = int(previous["version"]) if previous else 0
            if expected_version is not None and expected_version != actual_version and not same:
                raise ValueError("field version conflict")
            row = conn.execute(
                "SELECT principal_id,owner_scope,fact_key FROM continuity_fact_lineages "
                "WHERE fact_id=?",
                (base.fact_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO continuity_fact_lineages "
                    "(fact_id,principal_id,owner_scope,fact_key,created_at) VALUES (?,?,?,?,?)",
                    (
                        base.fact_id,
                        base.principal_id,
                        base.owner_scope,
                        base.fact_key,
                        base.created_at,
                    ),
                )
                version = 1
            else:
                if (
                    row["principal_id"] != base.principal_id
                    or row["owner_scope"] != base.owner_scope
                    or row["fact_key"] != base.fact_key
                ):
                    raise ValueError("lineage identity mismatch")
                version = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(version),0)+1 FROM continuity_fact_versions "
                        "WHERE fact_id=?",
                        (base.fact_id,),
                    ).fetchone()[0]
                )
            if same:
                return ContinuityFact(**{**base.__dict__, "version": actual_version, "created_at": previous["created_at"]})
            fact = ContinuityFact(**{**base.__dict__, "version": version}).validate()
            conn.execute(
                "INSERT INTO continuity_fact_versions "
                "(fact_id,version,content,visibility,sensitivity,status,source_kind,"
                "source_ref,source_hash,created_at,locked,evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fact.fact_id,
                    fact.version,
                    fact.content,
                    fact.visibility,
                    fact.sensitivity,
                    fact.status,
                    fact.source_kind,
                    fact.source_ref,
                    fact.source_hash,
                    fact.created_at,
                    int(fact.locked),
                    fact.evidence_json,
                ),
            )
            conn.commit()
            return fact

    def append_fact_idempotent(self, **kwargs) -> tuple[ContinuityFact, bool]:
        """Append once for an approved deterministic migration.

        An exact replay returns the existing latest version.  A replay with a
        different payload is rejected instead of silently creating a revision.
        """
        fact_id = _clean(kwargs.get("fact_id"))
        if not fact_id:
            raise ValueError("fact_id required")
        current = {fact.fact_id: fact for fact in self.current(
            _clean(kwargs.get("principal_id"))
        )}.get(fact_id)
        if current is not None:
            compared = (
                "principal_id", "owner_scope", "fact_key", "content",
                "visibility", "sensitivity", "status", "source_kind",
                "source_ref", "source_hash",
            )
            if all(
                _clean(getattr(current, name)) == _clean(kwargs.get(name))
                for name in compared
            ):
                return current, False
            raise ValueError("approved migration replay payload mismatch")
        return self.append_fact(**kwargs), True

    def revise_field(
        self, *, principal_id: str, owner_scope: str, field: str, value: str,
        expected_version: int, locked: bool | None = None,
        visibility: str | None = None, sensitivity: str | None = None, status: str = "active",
    ) -> ContinuityFact:
        """人工纠正是新版本，不修改 Prompt、证据原文或现役 snapshot。"""
        value = _validate_field(owner_scope, field, value)
        if locked is not None and type(locked) is not bool:
            raise ValueError("locked must be boolean")
        if owner_scope == "assistant":
            raise ValueError("assistant profile requires its Self Model author evidence")
        previous = self._field(principal_id, owner_scope, field)
        visibility = visibility if visibility is not None else (previous.visibility if previous else "primer")
        sensitivity = sensitivity if sensitivity is not None else (previous.sensitivity if previous else "normal")
        payload = {"owner_scope": owner_scope, "field": field, "value": value,
                   "expected_version": expected_version, "locked": locked,
                   "visibility": visibility, "sensitivity": sensitivity, "status": status}
        return self.append_fact(
            fact_id=previous.fact_id if previous else f"continuity:{owner_scope}.{field}:{uuid.uuid4().hex}",
            principal_id=principal_id, owner_scope=owner_scope, fact_key=f"{owner_scope}.{field}",
            content=value, visibility=visibility, sensitivity=sensitivity, status=status,
            locked=previous.locked if locked is None and previous else bool(locked),
            source_kind="manual_ui", source_ref=f"manual-ui:{uuid.uuid4().hex}",
            source_hash=hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            expected_version=expected_version,
        )

    def _field(self, principal_id: str, scope: str, field: str) -> ContinuityFact | None:
        return next((fact for fact in self.current(principal_id)
                     if fact.owner_scope == scope and fact.fact_key == f"{scope}.{field}"), None)

    def set_field_lock(self, *, principal_id: str, owner_scope: str, field: str,
                       locked: bool, expected_version: int) -> ContinuityFact:
        current = self._field(principal_id, owner_scope, field)
        if current is None:
            raise ValueError("field has no version to lock")
        return self.append_fact(
            **{key: value for key, value in current.__dict__.items()
               if key not in {"version", "created_at", "locked", "source_kind", "source_ref", "source_hash"}},
            locked=locked, source_kind="manual_ui", source_ref=f"manual-ui:{uuid.uuid4().hex}",
            source_hash=hashlib.sha256(f"{current.fact_id}:{expected_version}:locked={locked}".encode()).hexdigest(),
            expected_version=expected_version,
        )

    def apply_writer_proposal(self, proposal: dict, *, principal_id: str,
                              candidate: dict, event_reader) -> ContinuityFact:
        """接收现有 Writer 已有候选；reader 必须由 canonical owner 提供，不接模型自报证据。

        Writer 复用既有提炼输出字段提案，此函数不会自行提取或调用模型。日期的
        语义归属不能仅凭原文日期证明，因此日期仅走人工纠正入口。
        reader 须提供 main/principal/canonical/status；助手证据还须核实 delivered
        与 author_principal_id。不能把草稿、失败发送或工具探测映射成此证据。
        """
        scope, field = proposal["owner_scope"], proposal["field"]
        value = _validate_field(scope, field, proposal["value"])
        if field in _DATE_FIELDS:
            raise ValueError("date semantics require explicit manual correction")
        if candidate.get("evidence_state") != "verified":
            raise ValueError("writer evidence is not verified")
        expected_scope = {"user": "user_fact", "assistant": "assistant_self", "shared": "shared", "relationship": "shared", "world": "derived"}[scope]
        allowed_scopes = {"user_fact", "shared"} if scope == "user" else {expected_scope}
        if candidate.get("claim_scope") not in allowed_scopes:
            raise ValueError("writer owner scope mismatch")
        quotes = candidate.get("evidence") or []
        verified = candidate.get("verified_evidence") or []
        if not 1 <= len(quotes) <= 3 or len(quotes) != len(verified):
            raise ValueError("writer evidence is incomplete")
        evidence = []
        supported = False
        for item, check in zip(quotes, verified):
            event_id = item["event_id"]
            event = event_reader(event_id)
            if (not event or event["id"] != event_id or event.get("deleted")
                    or not event.get("canonical") or event.get("channel") != "main"
                    or event.get("principal_id") != principal_id or event.get("status") != "active"
                    or event.get("role") not in {"user", "assistant"}
                    or (event["role"] == "assistant" and event.get("delivered") is not True)):
                raise ValueError("canonical evidence identity/visibility mismatch")
            metadata = event.get("metadata_json") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            text = metadata.get("authored_text", "") if event["role"] == "user" else event["text"]
            quote = item["quote"]
            start = check.get("char_start", -1)
            end = check.get("char_end", -1)
            if (not quote or start < 0 or end != start + len(quote)
                    or text[start:end] != quote or check.get("event_id") != event_id
                    or not event.get("event_hash") or check.get("event_hash") != event["event_hash"]
                    or check.get("quote_hash") != hashlib.sha256(quote.encode()).hexdigest()):
                raise ValueError("writer evidence no longer matches canonical text")
            author_ok = event["role"] == ("assistant" if scope == "assistant" else "user")
            if scope == "assistant":
                author_ok = author_ok and event.get("author_principal_id") == principal_id
            supported = supported or (author_ok and value == quote)
            _parse_time(event["created_at"])
            evidence.append({"event_id": event_id, "event_hash": event["event_hash"],
                             "role": event["role"], "quote": quote, "created_at": event["created_at"]})
        if not supported:
            raise ValueError("field value must be an exact author statement, without inference")
        previous = self._field(principal_id, scope, field)
        evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        return self.append_fact(
            fact_id=previous.fact_id if previous else f"continuity:{scope}.{field}:{uuid.uuid4().hex}",
            principal_id=principal_id, owner_scope=scope, fact_key=f"{scope}.{field}", content=value,
            visibility=previous.visibility if previous else "primer",
            sensitivity=previous.sensitivity if previous else "normal", status="active",
            source_kind="canonical_raw_event", source_ref="raw-events:" + ",".join(str(item["event_id"]) for item in evidence),
            source_hash=hashlib.sha256(evidence_json.encode()).hexdigest(), evidence_json=evidence_json,
            expected_version=proposal["expected_version"],
        )

    def current(self, principal_id: str) -> list[ContinuityFact]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT l.fact_id,l.principal_id,l.owner_scope,l.fact_key,
                       v.version,v.content,v.visibility,v.sensitivity,v.status,
                       v.source_kind,v.source_ref,v.source_hash,v.created_at,v.locked,v.evidence_json
                FROM continuity_fact_lineages l
                JOIN continuity_fact_versions v
                  ON v.fact_id=l.fact_id
                 AND v.version=(
                   SELECT MAX(v2.version)
                   FROM continuity_fact_versions v2
                   WHERE v2.fact_id=l.fact_id
                 )
                WHERE l.principal_id=?
                ORDER BY l.owner_scope,l.fact_key,l.fact_id
                """,
                (_clean(principal_id),),
            ).fetchall()
        return [ContinuityFact(**dict(row)).validate() for row in rows]


class SQLiteContinuityFactsReadOnlyStore:
    """Read an already-bootstrapped continuity store without creating/mutating it."""

    def __init__(self, db_path: str, *, check_integrity: bool = True):
        self.db_path = _require_path(db_path)
        if not Path(self.db_path).is_file():
            raise FileNotFoundError(self.db_path)
        self._validate_schema(check_integrity)

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.db_path).as_posix().replace("?", "%3f").replace("#", "%23")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _validate_schema(self, check_integrity: bool) -> None:
        required = {
            "continuity_fact_lineages",
            "continuity_fact_versions",
        }
        with self._connect() as conn:
            if check_integrity and conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("continuity store quick_check failed")
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not required.issubset(tables):
                raise sqlite3.DatabaseError("continuity schema unavailable")
            version_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(continuity_fact_versions)"
                ).fetchall()
            }
            expected = {
                "fact_id", "version", "content", "visibility", "sensitivity",
                "status", "source_kind", "source_ref", "source_hash", "created_at",
            }
            if not expected.issubset(version_columns):
                raise sqlite3.DatabaseError("continuity schema incompatible")
            self._version_extras = ",".join(
                f"v.{name}" if name in version_columns else f"{default} AS {name}"
                for name, default in (("locked", "0"), ("evidence_json", "'[]'"))
            )

    def current(self, principal_id: str) -> list[ContinuityFact]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT l.fact_id,l.principal_id,l.owner_scope,l.fact_key,
                       v.version,v.content,v.visibility,v.sensitivity,v.status,
                       v.source_kind,v.source_ref,v.source_hash,v.created_at,{self._version_extras}
                FROM continuity_fact_lineages l
                JOIN continuity_fact_versions v
                  ON v.fact_id=l.fact_id
                 AND v.version=(
                   SELECT MAX(v2.version)
                   FROM continuity_fact_versions v2
                   WHERE v2.fact_id=l.fact_id
                 )
                WHERE l.principal_id=?
                ORDER BY l.owner_scope,l.fact_key,l.fact_id
                """,
                (_clean(principal_id),),
            ).fetchall()
        return [ContinuityFact(**dict(row)).validate() for row in rows]


def bootstrap_continuity_tables(db_path: str) -> dict:
    """One-time explicit schema bootstrap inside an existing SQLite authority."""
    resolved = _require_path(db_path)
    path = Path(resolved)
    if not path.is_file():
        raise FileNotFoundError(resolved)
    before = path.stat().st_size
    with sqlite3.connect(resolved, isolation_level=None) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("database quick_check failed")
        fk_before = [tuple(row) for row in conn.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()]
        conn.executescript(_SCHEMA)
        conn.commit()
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("database quick_check failed after bootstrap")
        fk_after = [tuple(row) for row in conn.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()]
        if fk_after != fk_before:
            raise sqlite3.IntegrityError(
                "continuity bootstrap changed foreign key violations"
            )
        counts = {
            "lineages": conn.execute(
                "SELECT COUNT(*) FROM continuity_fact_lineages"
            ).fetchone()[0],
            "versions": conn.execute(
                "SELECT COUNT(*) FROM continuity_fact_versions"
            ).fetchone()[0],
        }
    SQLiteContinuityFactsReadOnlyStore(resolved)
    return {
        "ok": True,
        "store_version": STORE_VERSION,
        "db_path": resolved,
        "size_before": before,
        "size_after": path.stat().st_size,
        "counts": counts,
        "preexisting_foreign_key_violations": len(fk_before),
    }


def migrate_canonical_raw_event_source_kind(db_path: str) -> dict:
    """Expand the source-kind CHECK without changing any fact rows.

    SQLite cannot alter a CHECK constraint in place, so this performs one
    transactional table rebuild, verifies row equality, and restores the
    append-only triggers/index.  Existing foreign-key violations elsewhere in
    the shared memory authority are tolerated only when unchanged.
    """
    resolved = _require_path(db_path)
    path = Path(resolved)
    if not path.is_file():
        raise FileNotFoundError(resolved)
    with sqlite3.connect(resolved) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("database quick_check failed")
        fk_before = [tuple(row) for row in conn.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()]
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' "
            "AND name='continuity_fact_versions'"
        ).fetchone()
        if sql_row is None:
            raise sqlite3.DatabaseError("continuity schema unavailable")
        if "canonical_raw_event" in str(sql_row[0] or ""):
            return {
                "ok": True,
                "changed": False,
                "foreign_key_violations": len(fk_before),
            }
        before_rows = conn.execute(
            "SELECT fact_id,version,content,visibility,sensitivity,status,"
            "source_kind,source_ref,source_hash,created_at "
            "FROM continuity_fact_versions ORDER BY fact_id,version"
        ).fetchall()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "ALTER TABLE continuity_fact_versions "
                "RENAME TO continuity_fact_versions_before_canonical_source"
            )
            conn.execute(
                """CREATE TABLE continuity_fact_versions (
                  fact_id TEXT NOT NULL REFERENCES continuity_fact_lineages(fact_id),
                  version INTEGER NOT NULL CHECK(version >= 1),
                  content TEXT NOT NULL,
                  visibility TEXT NOT NULL CHECK(visibility IN ('primer','contextual')),
                  sensitivity TEXT NOT NULL CHECK(sensitivity IN ('normal','sensitive')),
                  status TEXT NOT NULL CHECK(status IN ('active','retired')),
                  source_kind TEXT NOT NULL CHECK(source_kind IN (
                    'canonical_raw_event','user_managed_prompt','manual_ui','migration_shadow'
                  )),
                  source_ref TEXT NOT NULL,
                  source_hash TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(fact_id, version)
                )"""
            )
            conn.execute(
                "INSERT INTO continuity_fact_versions SELECT * FROM "
                "continuity_fact_versions_before_canonical_source"
            )
            conn.execute("DROP TABLE continuity_fact_versions_before_canonical_source")
            conn.execute(
                "CREATE INDEX idx_continuity_fact_versions_current "
                "ON continuity_fact_versions(fact_id, version DESC)"
            )
            conn.execute(
                """CREATE TRIGGER continuity_fact_versions_no_update
                BEFORE UPDATE ON continuity_fact_versions
                BEGIN SELECT RAISE(ABORT, 'append-only'); END"""
            )
            conn.execute(
                """CREATE TRIGGER continuity_fact_versions_no_delete
                BEFORE DELETE ON continuity_fact_versions
                BEGIN SELECT RAISE(ABORT, 'append-only'); END"""
            )
            after_rows = conn.execute(
                "SELECT fact_id,version,content,visibility,sensitivity,status,"
                "source_kind,source_ref,source_hash,created_at "
                "FROM continuity_fact_versions ORDER BY fact_id,version"
            ).fetchall()
            if after_rows != before_rows:
                raise sqlite3.IntegrityError("continuity rows changed during migration")
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("database quick_check failed after migration")
            fk_after = [tuple(row) for row in conn.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()]
            if fk_after != fk_before:
                raise sqlite3.IntegrityError(
                    "continuity migration changed foreign key violations"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "ok": True,
        "changed": True,
        "rows_preserved": len(before_rows),
        "foreign_key_violations": len(fk_before),
    }


def _primer_memory_style_ok(content: str) -> bool:
    text = _clean(content)
    if not text:
        return False
    lowered = text.lower()
    if any(lowered.startswith(prefix.lower()) for prefix in _ROLE_ASSIGNMENT_PREFIXES):
        return False
    if any(term.lower() in lowered for term in _META_ROLEPLAY_TERMS):
        return False
    return re.search(r"(?<![a-z])rp(?![a-z])", lowered) is None


def _primer_sort_key(fact: ContinuityFact) -> tuple:
    """Sort dated continuity facts chronologically, then keep a stable fallback.

    Approved continuity keys use compact YYYYMMDD dates.  When a future fact
    has no date in its key/id/content, keep it deterministically after all
    dated facts instead of guessing a chronology.
    """
    haystacks = (fact.fact_key, fact.fact_id, fact.content)
    date_value = None
    for value in haystacks:
        match = re.search(r"(?<!\d)(20\d{6})(?!\d)", str(value or ""))
        if match:
            date_value = match.group(1)
            break
    fallback = (fact.owner_scope, fact.fact_key, fact.fact_id, fact.version)
    return (0, date_value, *fallback) if date_value else (1, "", *fallback)


def render_primer(
    facts: Iterable[ContinuityFact],
    *,
    principal_id: str,
    max_tokens: int = PRIMER_TOKEN_BUDGET,
    as_of: date | None = None,
) -> dict:
    if not 0 <= max_tokens <= PRIMER_TOKEN_BUDGET:
        raise ValueError("primer budget outside contract")
    latest = {}
    for fact in facts or ():
        fact.validate()
        identity = (fact.principal_id, fact.owner_scope, fact.fact_key)
        if identity not in latest or latest[identity].version < fact.version:
            latest[identity] = fact
    eligible = []
    for fact in latest.values():
        if fact.principal_id != principal_id:
            continue
        if fact.status != "active":
            continue
        if fact.visibility != "primer":
            continue
        if fact.sensitivity != "normal":
            continue
        # 类型化资料是带标签的事实，RP 别名/虚拟宠物的限定词必须保留。
        if fact.fact_key not in field_contract_keys() and not _primer_memory_style_ok(fact.content):
            continue
        eligible.append(fact)
    eligible.sort(key=lambda fact: (fact.fact_key not in field_contract_keys(),
                                   _PROFILE_PRIORITY.get(fact.fact_key, 4), _primer_sort_key(fact)))
    selected = []
    content = ""
    last_header = None
    today = as_of or datetime.now(timezone.utc).date()
    for fact in eligible:
        field = fact.fact_key.removeprefix(f"{fact.owner_scope}.")
        label = PROFILE_FIELDS.get(fact.owner_scope, {}).get(field)
        text = fact.content
        if label:
            if field == "birth_date":
                born = date.fromisoformat(text)
                age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
                text += f"（截至{today.isoformat()}为{age}岁）"
            text = f"{SCOPE_LABELS[fact.owner_scope]}·{label}：{text}"
        header = PROFILE_HEADER if label else HEADER
        prefix = content if last_header == header else "\n\n".join(part for part in (content, header) if part)
        candidate = prefix + f"\n- {text}"
        if estimate_tokens(candidate) <= max_tokens:
            content = candidate
            last_header = header
            selected.append(fact)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    refs = [
        {
            "fact_id": fact.fact_id,
            "version": fact.version,
            "owner_scope": fact.owner_scope,
            "fact_key": fact.fact_key,
            "source_kind": fact.source_kind,
            "source_ref": fact.source_ref,
            "source_hash": fact.source_hash,
        }
        for fact in selected
    ]
    return {
        "store_version": STORE_VERSION,
        "principal_id": principal_id,
        "content": content,
        "sha256": digest,
        "fact_refs": refs,
        "count": len(selected),
        "budget": {"max_tokens": max_tokens, "estimated_tokens": estimate_tokens(content),
                   "omitted_count": len(eligible) - len(selected),
                   "omitted_fact_keys": [fact.fact_key for fact in eligible if fact not in selected]},
        "provider_visible": False,
        "shadow_only": True,
    }
