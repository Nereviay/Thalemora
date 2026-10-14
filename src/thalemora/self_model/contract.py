"""Self Model 公共语义与调用合同（静态来源：self_model_contract.py）。

The schemas in this module are not registered with a provider.  They validate
model-authored semantic intent while keeping runtime-owned identity, evidence,
generation, and version fields in a separate :class:`InvocationContext`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping


CONTRACT_VERSION = "p3c-tool-v1"
SCHEMA_VERSION = "p3c-self-model-v1"
SURFACES = frozenset({"normal", "contact", "find_activity", "self_maintenance"})
DOMAINS = frozenset({
    "relationship",
    "communication",
    "preference",
    "habit",
    "values",
    "self_concept",
})
SCOPES = frozenset({"global", "relationship", "situational"})
MATURITY_REQUESTS = frozenset({"observing", "established"})

# These are rejected explicitly, even when a particular spelling would also be
# caught by additionalProperties=false.  This gives attempted authority
# escalation a stable failure code instead of silently discarding the field.
SYSTEM_OWNED_FIELDS = frozenset({
    "evidence_ref",
    "evidence_refs",
    "evidence_id",
    "evidence_ids",
    "durable_ref",
    "contradiction_ref",
    "contradiction_refs",
    "independence_key",
    "source_type",
    "evidence_kind",
    "actor",
    "relation",
    "root_event_id",
    "canonical_state",
    "outcome",
    "committed",
    "duplicate_suppressed",
    "tool_name",
    "backref",
    "eligible_for_trait_support",
    "eligible_for_contradiction",
    "source_hash",
    "occurred_at",
    "generation_id",
    "message_id",
    "canonical_event_id",
    "canonical_tool_call_event_id",
    "tool_call_event_id",
    "tool_receipt_id",
    "contemplation_id",
    "shared_event_id",
    "surface",
    "principal_id",
    "namespace",
    "claim_kind",
    "version",
    "supersedes",
    "provider_visible",
    "created_at",
    "updated_at",
    "first_seen_at",
    "last_supported_at",
    "gate_version",
    "resolver_version",
    "operation_id",
    "idempotency_key",
    "context_epoch_id",
    "canonical_branch_id",
    "contract_version",
})

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
_TOPIC_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")


class ContractValidationError(ValueError):
    """Structured validation failure suitable for a rejected fake receipt."""

    def __init__(self, code: str, message: str, *, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.field = field


def _object_schema(properties: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
        "required": list(required),
    }


_SHORT_ID = {"type": "string", "minLength": 1, "maxLength": 192}
_STATEMENT = {"type": "string", "minLength": 1, "maxLength": 500}
_NOTE = {"type": "string", "minLength": 1, "maxLength": 300}

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "self_model_observe": _object_schema(
        {
            "statement": _STATEMENT,
            "domain": {"type": "string", "enum": sorted(DOMAINS)},
            "topic_key": {"type": "string", "pattern": _TOPIC_KEY_RE.pattern},
            "scope": {"type": "string", "enum": sorted(SCOPES)},
            "requested_maturity": {
                "type": "string",
                "enum": sorted(MATURITY_REQUESTS),
                "default": "observing",
            },
            "review_suggestion_id": _SHORT_ID,
        },
        ("statement", "domain", "topic_key", "scope"),
    ),
    "self_model_revise": _object_schema(
        {
            "claim_id": _SHORT_ID,
            "expected_version": {"type": "integer", "minimum": 1},
            "intent": {
                "type": "string",
                "enum": ["clarify", "add_support", "mark_uncertain", "restate"],
            },
            "statement": _STATEMENT,
            "requested_maturity": {
                "type": "string",
                "enum": sorted(MATURITY_REQUESTS),
            },
            "reason_note": _NOTE,
            "review_suggestion_id": _SHORT_ID,
        },
        ("claim_id", "expected_version", "intent"),
    ),
    "self_model_retire": _object_schema(
        {
            "claim_id": _SHORT_ID,
            "expected_version": {"type": "integer", "minimum": 1},
            "reason": {
                "type": "string",
                "enum": ["changed", "no_longer_true", "replaced_by_new_understanding"],
            },
            "reason_note": _NOTE,
            "review_suggestion_id": _SHORT_ID,
        },
        ("claim_id", "expected_version", "reason"),
    ),
    "self_model_review": _object_schema(
        {
            "claim_id": _SHORT_ID,
            "review_suggestion_id": _SHORT_ID,
            "view": {
                "type": "string",
                "enum": ["active", "observing", "established", "retired", "all"],
                "default": "active",
            },
            "include_history": {"type": "boolean", "default": False},
            "include_evidence_summary": {"type": "boolean", "default": True},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
        }
    ),
}


def normalize_statement(value: str) -> str:
    """Apply the only semantic normalization allowed by the P3C contract."""
    return " ".join(unicodedata.normalize("NFC", value).strip().split())


def _validate_string(name: str, value: Any, *, minimum: int = 1, maximum: int) -> str:
    if not isinstance(value, str):
        raise ContractValidationError("schema_invalid", f"{name} must be a string", field=name)
    cleaned = normalize_statement(value)
    if not minimum <= len(cleaned) <= maximum:
        raise ContractValidationError(
            "schema_invalid",
            f"{name} must contain {minimum}-{maximum} characters",
            field=name,
        )
    return cleaned


def _validate_id(name: str, value: Any) -> str:
    cleaned = _validate_string(name, value, maximum=192)
    if not _ID_RE.fullmatch(cleaned):
        raise ContractValidationError("schema_invalid", f"{name} is not a stable id", field=name)
    return cleaned


def validate_model_args(tool_name: str, model_args: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize model-authored arguments.

    No runtime-owned value is accepted through this boundary.  The returned
    mapping is safe to retain verbatim as the replayable semantic intent.
    """
    if tool_name not in TOOL_SCHEMAS:
        raise ContractValidationError("schema_invalid", f"unknown tool: {tool_name}")
    if not isinstance(model_args, Mapping):
        raise ContractValidationError("schema_invalid", "tool arguments must be an object")

    supplied = set(model_args)
    forbidden = sorted(supplied & SYSTEM_OWNED_FIELDS)
    if forbidden:
        raise ContractValidationError(
            "forbidden_field",
            f"runtime-owned field is forbidden: {forbidden[0]}",
            field=forbidden[0],
        )
    if tool_name == "self_model_revise":
        identity_changes = sorted(supplied & {"domain", "topic_key", "scope"})
        if identity_changes:
            raise ContractValidationError(
                "immutable_claim_identity",
                f"lineage identity is immutable: {identity_changes[0]}",
                field=identity_changes[0],
            )
    schema = TOOL_SCHEMAS[tool_name]
    allowed = set(schema["properties"])
    unknown = sorted(supplied - allowed)
    if unknown:
        raise ContractValidationError(
            "schema_invalid", f"additional property is not allowed: {unknown[0]}", field=unknown[0]
        )
    missing = [name for name in schema.get("required", ()) if name not in model_args]
    if missing:
        raise ContractValidationError("schema_invalid", f"missing required field: {missing[0]}", field=missing[0])

    result: dict[str, Any] = {}
    for name, value in model_args.items():
        spec = schema["properties"][name]
        expected_type = spec["type"]
        if expected_type == "string":
            if name in {"claim_id", "review_suggestion_id"}:
                cleaned: Any = _validate_id(name, value)
            else:
                cleaned = _validate_string(
                    name,
                    value,
                    minimum=int(spec.get("minLength", 1)),
                    maximum=int(spec.get("maxLength", 500)),
                )
            if "enum" in spec and cleaned not in spec["enum"]:
                raise ContractValidationError("schema_invalid", f"unsupported {name}: {cleaned}", field=name)
            if name == "topic_key" and not _TOPIC_KEY_RE.fullmatch(cleaned):
                raise ContractValidationError("schema_invalid", "topic_key is outside the controlled syntax", field=name)
            result[name] = cleaned
        elif expected_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ContractValidationError("schema_invalid", f"{name} must be an integer", field=name)
            if value < spec.get("minimum", value) or value > spec.get("maximum", value):
                raise ContractValidationError("schema_invalid", f"{name} is outside the allowed range", field=name)
            result[name] = value
        elif expected_type == "boolean":
            if not isinstance(value, bool):
                raise ContractValidationError("schema_invalid", f"{name} must be a boolean", field=name)
            result[name] = value

    for name, spec in schema["properties"].items():
        if name not in result and "default" in spec:
            result[name] = spec["default"]

    if tool_name == "self_model_revise":
        if result["intent"] != "add_support" and not ({"statement", "requested_maturity"} & result.keys()):
            raise ContractValidationError(
                "schema_invalid",
                "revise requires statement or requested_maturity unless intent is add_support",
            )
    return result


def namespace_for_principal(principal_id: str) -> str:
    principal = _validate_id("principal_id", principal_id)
    return f"self-model:{SCHEMA_VERSION}:{principal}"


def _parse_timestamp(value: str, name: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ContractValidationError("invocation_context_missing", f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ContractValidationError("invocation_context_missing", f"{name} must include a timezone")
    return value


@dataclass(frozen=True)
class InvocationContext:
    principal_id: str
    generation_id: str
    canonical_tool_call_event_id: str
    canonical_branch_id: str
    context_epoch_id: str
    surface: str
    invoked_at: str

    def __post_init__(self) -> None:
        for name in (
            "principal_id",
            "generation_id",
            "canonical_tool_call_event_id",
            "canonical_branch_id",
            "context_epoch_id",
        ):
            object.__setattr__(self, name, _validate_id(name, getattr(self, name)))
        if self.surface not in SURFACES:
            raise ContractValidationError("invocation_context_missing", "unsupported invocation surface", field="surface")
        object.__setattr__(self, "invoked_at", _parse_timestamp(self.invoked_at, "invoked_at"))

    @property
    def namespace(self) -> str:
        return namespace_for_principal(self.principal_id)


@dataclass(frozen=True)
class SemanticIntent:
    tool_name: str
    args: Mapping[str, Any]

    @property
    def is_mutation(self) -> bool:
        return self.tool_name != "self_model_review"

    @property
    def canonical_json(self) -> str:
        return json.dumps(dict(self.args), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def args_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


def build_semantic_intent(tool_name: str, model_args: Mapping[str, Any]) -> SemanticIntent:
    return SemanticIntent(tool_name=tool_name, args=validate_model_args(tool_name, model_args))


def idempotency_key(context: InvocationContext, tool_name: str) -> str:
    raw = "\x1f".join(
        (
            CONTRACT_VERSION,
            context.principal_id,
            context.generation_id,
            context.canonical_tool_call_event_id,
            tool_name,
        )
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def operation_id_for(key: str) -> str:
    return "self-model-operation:" + key.removeprefix("sha256:")[:32]


@dataclass(frozen=True)
class OperationRequest:
    operation_id: str
    idempotency_key: str
    tool_name: str
    principal_id: str
    namespace: str
    generation_id: str
    tool_call_event_id: str
    canonical_branch_id: str
    context_epoch_id: str
    surface: str
    invoked_at: str
    intent_json: str
    args_hash: str
    contract_version: str = CONTRACT_VERSION

    @property
    def replay_intent(self) -> dict[str, Any]:
        """Return the validated semantic intent, not merely its hash."""
        return json.loads(self.intent_json)


@dataclass(frozen=True)
class OperationReceipt:
    operation_id: str
    phase: str
    status: str
    code: str
    claim_id: str | None = None
    claim_version: int | None = None
    maturity: str | None = None
    provider_visible: bool = False
    eligible_for_epoch: str | None = None
    detail: str | None = None


def make_operation_request(intent: SemanticIntent, context: InvocationContext) -> OperationRequest:
    key = idempotency_key(context, intent.tool_name)
    return OperationRequest(
        operation_id=operation_id_for(key),
        idempotency_key=key,
        tool_name=intent.tool_name,
        principal_id=context.principal_id,
        namespace=context.namespace,
        generation_id=context.generation_id,
        tool_call_event_id=context.canonical_tool_call_event_id,
        canonical_branch_id=context.canonical_branch_id,
        context_epoch_id=context.context_epoch_id,
        surface=context.surface,
        invoked_at=context.invoked_at,
        intent_json=intent.canonical_json,
        args_hash=intent.args_hash,
    )
