"""One-shot DeepSeek adapter for bounded Self Model candidate extraction.

The adapter owns no persistence and does not discover evidence.  Callers pass
one already-frozen authoritative snapshot plus the records collected from that
same snapshot.  Request metadata is matched back to the frozen authoritative
body before any provider transport is attempted.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
import urllib.error
import urllib.request

from .candidate_extractor import CandidateExtractionError
from .evidence import AuthoritativeRecord, EvidenceRecord


PROVIDER_PROMPT_VERSION = "self-model-historical-review-deepseek-v1"
_SYSTEM_PROMPT = """你是 Self Model 历史证据复核器。你只能判断给定历史条目是否支持、反驳、或不匹配已经存在的观察项；不得发明、改写或扩展人格陈述，不得决定成熟度、可见性或采纳。

只输出一个 JSON 对象，且字段必须严格为：
- decision: relevant_support | relevant_contradiction | no_match
- target_claim_id, target_claim_version, target_topic_key, target_scope, target_principal_id, target_namespace：原样复制目标绑定
- evidence_refs: 只能从输入 evidence_ref 中选择；相关决定至少一个，可多选；no_match 应为空数组
- summary: 简短中文说明
- uncertainties: 可选的简短中文字符串数组，最多 8 项

判断单条历史事实本身，不根据元数据臆测隐藏内容。多个相互独立且明确的条目可一起选择。"""
PROVIDER_PROMPT_FINGERPRINT = "sha256:" + hashlib.sha256(
    (PROVIDER_PROMPT_VERSION + "\n" + _SYSTEM_PROMPT).encode("utf-8")
).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True)
class MaterializedEvidence:
    record: EvidenceRecord
    content: str
    tool_name: str | None
    truncated: bool
    original_chars: int


def materialize_frozen_evidence(
    records: Sequence[EvidenceRecord],
    authoritative_records: Mapping[str, AuthoritativeRecord],
    *,
    content_char_limit: int | None = None,
) -> Mapping[str, MaterializedEvidence]:
    """Bind collected identities to content from the exact frozen snapshot."""
    if content_char_limit is not None and content_char_limit < 1:
        raise ValueError("content_char_limit must be positive")
    materialized: dict[str, MaterializedEvidence] = {}
    for record in records:
        if record.durable_ref in materialized:
            raise CandidateExtractionError(
                "duplicate_evidence_ref", "collected evidence refs must be unique"
            )
        authoritative = authoritative_records.get(record.durable_ref)
        if authoritative is None:
            raise CandidateExtractionError(
                "evidence_ref_mismatch", "collected evidence ref is absent from snapshot"
            )
        if authoritative.durable_ref != record.durable_ref:
            raise CandidateExtractionError(
                "evidence_ref_mismatch", "evidence alias does not identify its canonical root"
            )
        if authoritative.source_hash != record.source_hash:
            raise CandidateExtractionError(
                "evidence_hash_mismatch", "collected source hash differs from snapshot"
            )
        if authoritative.source_hash != authoritative.authoritative_hash():
            raise CandidateExtractionError(
                "evidence_hash_mismatch", "authoritative snapshot content failed its hash"
            )
        content = authoritative.content
        original_chars = len(content)
        truncated = content_char_limit is not None and original_chars > content_char_limit
        if truncated:
            content = content[:content_char_limit]
        materialized[record.durable_ref] = MaterializedEvidence(
            record=record,
            content=content,
            tool_name=authoritative.tool_name,
            truncated=truncated,
            original_chars=original_chars,
        )
    return materialized


class DeepSeekCandidateExtractor:
    """Provider implementation with a hard process-local request-attempt cap."""

    prompt_template_fingerprint = PROVIDER_PROMPT_FINGERPRINT

    def __init__(
        self,
        materialized: Mapping[str, MaterializedEvidence],
        *,
        endpoint: str,
        max_calls: int = 2,
        timeout: int = 120,
        transport: Callable[..., Mapping[str, Any]],
        key_loader: Callable[[], str],
        profile_loader: Callable[[], tuple[Mapping[str, Any], Mapping[str, Any], str]],
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self.materialized = dict(materialized)
        self.max_calls = int(max_calls)
        self.timeout = int(timeout)
        self.endpoint = endpoint
        self.transport = transport
        self.key_loader = key_loader
        self.profile, self.request_profile, self.profile_fingerprint = profile_loader()
        self.calls_attempted = 0
        self.calls_succeeded = 0
        self.last_payload: Mapping[str, Any] | None = None

    def _materialize_request(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_evidence = request.get("evidence")
        if not isinstance(raw_evidence, list) or not raw_evidence:
            raise CandidateExtractionError("invalid_input", "request evidence is required")
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        checked_fields = (
            "evidence_id",
            "source_type",
            "evidence_kind",
            "actor",
            "relation",
            "independence_key",
            "occurred_at",
            "source_hash",
            "resolver_version",
        )
        for raw in raw_evidence:
            if not isinstance(raw, Mapping):
                raise CandidateExtractionError("invalid_input", "evidence item must be an object")
            durable_ref = str(raw.get("evidence_ref") or "")
            if not durable_ref or durable_ref in seen:
                raise CandidateExtractionError("evidence_ref_mismatch", "evidence refs must be unique")
            seen.add(durable_ref)
            frozen = self.materialized.get(durable_ref)
            if frozen is None:
                raise CandidateExtractionError("evidence_ref_mismatch", "request ref is outside frozen evidence")
            expected = frozen.record
            for field in checked_fields:
                expected_value = getattr(expected, "durable_ref" if field == "evidence_ref" else field)
                if raw.get(field) != expected_value:
                    code = "evidence_hash_mismatch" if field == "source_hash" else "evidence_metadata_mismatch"
                    raise CandidateExtractionError(code, f"request {field} differs from frozen evidence")
            item = dict(raw)
            item["surface"] = expected.surface
            item["content"] = frozen.content
            if frozen.tool_name:
                item["tool_name"] = frozen.tool_name
            if frozen.truncated:
                item["content_truncation"] = {
                    "truncated": True,
                    "original_chars": frozen.original_chars,
                    "sent_chars": len(frozen.content),
                }
            output.append(item)
        return output

    def build_provider_payload(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        target = request.get("target_claim")
        if not isinstance(target, Mapping):
            raise CandidateExtractionError("invalid_input", "target claim is required")
        materialized = self._materialize_request(request)
        body = {
            "provider_prompt_version": PROVIDER_PROMPT_VERSION,
            "target_claim": dict(target),
            "evidence": materialized,
        }
        payload: dict[str, Any] = {
            **self.request_profile,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _canonical_json(body)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "stream": False,
        }
        return payload

    @staticmethod
    def _parse_response(response: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(response, Mapping):
            raise CandidateExtractionError("transport_error", "provider response must be an object")
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
            raise CandidateExtractionError("transport_error", "provider response must contain one choice")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str):
            raise CandidateExtractionError("non_json_output", "provider returned no JSON text")
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise CandidateExtractionError("non_json_output", "provider output was not JSON") from exc
        if not isinstance(value, Mapping):
            raise CandidateExtractionError("malformed_schema", "provider JSON must be an object")
        return dict(value)

    def extract_candidate(self, request: Mapping[str, Any]) -> Any:
        if self.calls_attempted >= self.max_calls:
            raise CandidateExtractionError("provider_call_cap", "provider call cap reached")
        payload = self.build_provider_payload(request)
        key = self.key_loader()
        if not key:
            raise CandidateExtractionError("credential_missing", "DeepSeek credential is not configured")
        self.calls_attempted += 1
        self.last_payload = payload
        try:
            response = self.transport(
                url=self.endpoint,
                api_key=key,
                payload=payload,
                timeout=self.timeout,
            )
        except CandidateExtractionError:
            raise
        except Exception as exc:
            raise CandidateExtractionError("transport_error", str(exc)) from exc
        value = self._parse_response(response)
        self.calls_succeeded += 1
        return value
