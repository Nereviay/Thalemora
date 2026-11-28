"""存量 RawEvent 批次与证据修复。持久化继续使用既有 ops 与 Gatekeeper。"""
import hashlib
import json
import re
import sqlite3
from thalemora.gatekeeper import keywords as keyword_policy
CHUNK_TARGET_TOKENS = 10000


WRITER_EXTRACT_POLICY_VERSION = "p3c1-extract-v9-profile-proposal"


CLAIM_SCOPES = {"user_fact", "assistant_self", "shared", "derived"}


WRITER_OUTPUT_FIELDS = {
    "title", "content", "keywords", "importance", "valence", "arousal",
    "claim_scope", "evidence",
}


MAX_EVIDENCE_REPAIRS = 3

def _value(row, name, index=None, default=None):
    if isinstance(row, sqlite3.Row):
        try:
            return row[name]
        except (IndexError, KeyError):
            return default
    if isinstance(row, dict):
        return row.get(name, default)
    if index is None:
        return default
    try:
        return row[index]
    except IndexError:
        return default


def _id_of(row):
    return int(_value(row, "id", 0, 0))


def _raw_role_of(row):
    return _value(row, "role", 4 if len(row) > 4 else 1, "")


def _text_of(row):
    value = _value(row, "text", 5 if len(row) > 5 else 2, "")
    return value or ""


def _metadata(row):
    try:
        value = json.loads(_value(row, "metadata_json", 7, "{}") or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _citable_text(row):
    if _raw_role_of(row) == "user":
        authored = _metadata(row).get("authored_text")
        return authored if isinstance(authored, str) else ""
    return _text_of(row)

def _stable_hash(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

RAW_WRITER_PROMPT = """从已授权聊天提炼独立的长期事实、明确偏好与约定。保留否定、改口、期限、说话者和未完成状态。
返回 JSON 数组，每项含 title、content、keywords、importance、valence、arousal、claim_scope、evidence。
evidence 用 event_id 与连续原文 quote，用户事实必须有用户证据。无需保留的信息返回空数组。
可选 profile_update 仅用于用户对本人现状的完整直接自述。owner_scope=user，field 仅 work_status/recent_status/city/preferences/education/long_term_goals，
给出完整原文 value、event_id、assertion(statement/correction)、temporal_status(current) 和 confirmation(first/reconfirmed)。"""

class RawEventWriter:
    def __init__(self, *, source, ops, profiles, apply_candidate, user_name, perspective):
        self.source = source
        self.ops = ops
        self.profiles = profiles
        self.apply_candidate = apply_candidate
        self.user_name = user_name
        self.prompt = RAW_WRITER_PROMPT + perspective

    def _perspective_matches_scope(self, core):
        text = f"{core.get('title') or ''}\n{core.get('content') or ''}"
        scope = core.get("claim_scope")
        if scope == "user_fact":
            return self.user_name in text
        if scope == "assistant_self":
            return "我" in text
        if scope == "shared":
            return "我们" in text or (self.user_name in text and "我" in text)
        if scope == "derived":
            return any(marker in text for marker in (self.user_name, "我", "我们"))
        return False


    def writer_input_hash(self, scanned_rows, branch_decisions=None):
        decision_by_id = {
            int(item["raw_event_id"]): item for item in (branch_decisions or [])
        }
        return _stable_hash([
            {
                "id": _id_of(row),
                "source": _value(row, "source", 1, ""),
                "source_event_id": _value(row, "source_event_id", 2, ""),
                "event_hash": _value(row, "event_hash", 3, ""),
                "role": _raw_role_of(row),
                "beijing_time": self.source._beijing_time_of(row),
                "citable_text": _citable_text(row),
                "deleted": int(_value(row, "deleted", 8, 0) or 0),
                "branch": decision_by_id.get(_id_of(row)),
            }
            for row in scanned_rows
        ])


    def writer_batch_key(self, raw_start_id, raw_end_id, input_hash, profile=None):
        profile = profile or self.profiles.get_profile("writer")
        return _stable_hash({
            "raw_start_id": raw_start_id,
            "raw_end_id": raw_end_id,
            "input_hash": input_hash,
            "model_profile": self.profiles.profile_fingerprint(profile),
            "policy_version": WRITER_EXTRACT_POLICY_VERSION,
        })


    def _core(self, point):
        keywords = point.get("keywords") or []
        if isinstance(keywords, str):
            keywords = keywords.split(",")
        title = str(point.get("title") or "").strip()[:40]
        return {
            "title": title,
            "content": str(point.get("content") or "").strip(),
            "keywords": keyword_policy.normalize_keywords(keywords, title=title, limit=5),
            "importance": self._clamp_float(point.get("importance"), 5.0, 1.0, 10.0),
            "valence": self._clamp_float(point.get("valence"), 0.5),
            "arousal": self._clamp_float(point.get("arousal"), 0.3),
            "claim_scope": str(point.get("claim_scope") or "").strip(),
        }


    def candidate_id(self, batch_key, item_index, point):
        return _stable_hash({
            "batch_key": batch_key,
            "item_index": item_index,
            "core": self._core(point),
        })


    def _estimate_tokens(self, text):
        total = 0
        for part in re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\s]", text or ""):
            if len(part) == 1 and "\u4e00" <= part <= "\u9fff":
                total += 1
            elif re.match(r"^[A-Za-z0-9_]+$", part):
                total += max(1, (len(part) + 3) // 4)
            else:
                total += 1
        return total


    def _chunk_messages(self, messages, target_tokens=CHUNK_TARGET_TOKENS):
        chunks, current, current_tokens = [], [], 0
        for message in messages:
            line = (
                f"{_id_of(message)} {self.source._beijing_time_of(message)} "
                f"{self.source._role_of(message)}: {_citable_text(message)}"
            )
            tokens = self._estimate_tokens(line)
            if current and current_tokens + tokens > target_tokens:
                chunks.append(current)
                current, current_tokens = [], 0
            current.append(message)
            current_tokens += tokens
        if current:
            chunks.append(current)
        return chunks


    def _strip_json_noise(self, raw):
        value = (raw or "").strip()
        value = re.sub(r"(?is)<think>.*?</think>", "", value).strip()
        match = re.search(r"```(?:json)?\s*(.*?)```", value, re.S | re.I)
        return match.group(1).strip() if match else value


    def _parse_json_array(self, raw):
        if isinstance(raw, list):
            return raw
        cleaned = self._strip_json_noise(raw)
        try:
            value = json.loads(cleaned)
        except Exception:
            match = re.search(r"\[.*\]", cleaned, re.S)
            value = json.loads(match.group(0)) if match else []
        return value if isinstance(value, list) else []


    def _clamp_float(self, value, default, low=0.0, high=1.0):
        try:
            return max(low, min(high, float(value)))
        except Exception:
            return default


    def _normalize_item(self, item):
        if not isinstance(item, dict):
            return None
        if not WRITER_OUTPUT_FIELDS.issubset(item):
            return None
        core = self._core(item)
        if (
            not core["title"] or not core["content"]
            or core["claim_scope"] not in CLAIM_SCOPES
        ):
            return None
        if not self._perspective_matches_scope(core):
            return None
        core["evidence"] = item.get("evidence") or []
        if "profile_update" in item:
            core["profile_update"] = item["profile_update"]
        return core


    def summarize_chunk(self, messages, idx=1, total=1, profile=None):
        citable = [self.source._citable_event(row) for row in messages]
        raw = self.source._extract_call(
            self.prompt, json.dumps(citable, ensure_ascii=False), profile
        )
        return [item for item in (self._normalize_item(value) for value in self._parse_json_array(raw)) if item]


    def summarize(self, messages, profile=None):
        profile = profile or self.profiles.get_profile("writer")
        points = []
        chunks = self._chunk_messages(messages)
        for index, chunk in enumerate(chunks, 1):
            print(f"[writer] 提炼分块 {index}/{len(chunks)}（{len(chunk)} 条）")
            points.extend(self.summarize_chunk(chunk, index, len(chunks), profile))
        return points


    def repair_candidate_evidence(self, core, messages, errors):
        citable = [self.source._citable_event(row) for row in messages]
        system = (
            "只修复候选的 evidence，不修改候选 core。输出 JSON 对象："
            "{\"evidence\":[{\"event_id\":整数,\"quote\":\"逐字短引文\"}]}。"
            "只能引用给出的 quotable_text；1到3条。"
        )
        raw = self.source._extract_call(system, json.dumps(
            {"core": core, "errors": errors, "events": citable}, ensure_ascii=False
        ))
        try:
            value = json.loads(self._strip_json_noise(raw))
        except Exception:
            value = {}
        return value.get("evidence") if isinstance(value, dict) else []


    def verify_evidence(self, point, eligible_rows, raw_start_id, raw_end_id):
        row_by_id = {_id_of(row): row for row in eligible_rows}
        verified, errors = [], []
        evidence = point.get("evidence") or []
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 3:
            return [], ["evidence_count"]
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"evidence_{index}_not_object")
                continue
            try:
                event_id = int(item.get("event_id"))
            except (TypeError, ValueError):
                errors.append(f"evidence_{index}_bad_event")
                continue
            row = row_by_id.get(event_id)
            if not raw_start_id <= event_id <= raw_end_id:
                errors.append(f"evidence_{index}_event_outside_batch")
                continue
            if row is None or int(_value(row, "deleted", 8, 0) or 0) == 1:
                errors.append(f"evidence_{index}_event_ineligible")
                continue
            quote = item.get("quote")
            if not isinstance(quote, str) or not quote:
                errors.append(f"evidence_{index}_empty_quote")
                continue
            body = _citable_text(row)
            char_start = body.find(quote)
            if char_start < 0:
                errors.append(f"evidence_{index}_quote_not_exact")
                continue
            verified.append({
                "source_kind": (
                    "user_text_exact" if _raw_role_of(row) == "user"
                    else "assistant_text_exact"
                ),
                "raw_event_start_id": event_id,
                "raw_event_end_id": event_id,
                "event_id": event_id,
                "event_hash": _value(row, "event_hash", 3, ""),
                "char_start": char_start,
                "char_end": char_start + len(quote),
                "quote_hash": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            })
        if point.get("claim_scope") == "user_fact" and not any(
            item["source_kind"] == "user_text_exact" for item in verified
        ):
            errors.append("user_fact_requires_user_text_exact")
        return (verified, []) if not errors else ([], errors)


    def _candidate_payload(self, point, eligible_rows, raw_start_id, raw_end_id):
        core = self._core(point)
        # Gatekeeper's card core stays unchanged; retain the author's quotes and
        # optional projection separately, including malformed proposals for receipts.
        core["evidence"] = point.get("evidence") or []
        if "profile_update" in point:
            core["profile_update"] = point["profile_update"]
        verified, errors = self.verify_evidence(point, eligible_rows, raw_start_id, raw_end_id)
        if verified:
            return {**core, "verified_evidence": verified, "evidence_state": "verified"}
        return {**core, "verified_evidence": [], "evidence_state": "evidence_invalid",
                "evidence_errors": errors}


    def _repair_pending_evidence(self, candidate, payload):
        if int(candidate.get("evidence_attempts") or 0) >= MAX_EVIDENCE_REPAIRS:
            return False
        batch = self.ops.get_writer_batch(candidate["batch_key"])
        if batch is None:
            return False
        rows = self.source._fetch_batch_rows(batch["raw_start_id"], batch["raw_end_id"])
        eligible, _ = self.source.filter_canonical_branch(rows)
        core = self._core(payload)
        evidence = self.repair_candidate_evidence(
            core, eligible, payload.get("evidence_errors") or []
        )
        attempt = {**payload, "evidence": evidence}
        updated = self._candidate_payload(attempt, eligible, batch["raw_start_id"], batch["raw_end_id"])
        if updated["evidence_state"] == "verified":
            error = None
        else:
            next_attempt = int(candidate.get("evidence_attempts") or 0) + 1
            error = "evidence_exhausted" if next_attempt >= MAX_EVIDENCE_REPAIRS else "evidence_invalid"
        self.ops.record_evidence_attempt(
            candidate["candidate_id"], updated, error
        )
        return updated["evidence_state"] == "verified"


    def _p3_batch_from_raw_range(self, batch):
        raw_start_id = int(batch["raw_start_id"])
        raw_end_id = int(batch["raw_end_id"])
        rows = self.source._fetch_batch_rows(raw_start_id, raw_end_id)
        eligible, branch_decisions = self.source.filter_canonical_branch(rows)
        input_hash = self.writer_input_hash(rows, branch_decisions)
        profile = self.profiles.get_profile("writer")
        batch_key = self.writer_batch_key(raw_start_id, raw_end_id, input_hash, profile)
        if self.ops.get_writer_batch(batch_key) is not None:
            return batch_key

        points = self.summarize(eligible, profile=profile) if eligible else []
        candidates = [
            {
                "candidate_id": self.candidate_id(batch_key, index, point),
                "payload": self._candidate_payload(
                    point, eligible, raw_start_id, raw_end_id
                ),
            }
            for index, point in enumerate(points)
        ]
        self.ops.put_writer_batch(
            {
                "batch_key": batch_key,
                "raw_start_id": raw_start_id,
                "raw_end_id": raw_end_id,
                "input_hash": input_hash,
                "extractor_model": profile["model"],
                "policy_version": WRITER_EXTRACT_POLICY_VERSION,
            },
            candidates,
        )
        return batch_key


    def _reextract_legacy_candidate(self, candidate):
        if candidate.get("status") != "pending":
            return True
        batch = self.ops.get_writer_batch(candidate["batch_key"])
        if batch is None:
            return False
        p3_batch_key = self._p3_batch_from_raw_range(batch)
        if self.ops.get_writer_batch(p3_batch_key) is None:
            return False
        self.ops.supersede_legacy_candidate(candidate["candidate_id"])
        self.process_pending_candidates(p3_batch_key)
        return True


    def _process_candidate(self, candidate):
        try:
            payload = json.loads(candidate["payload_json"])
        except Exception:
            self.ops.mark_candidate_failed(candidate["candidate_id"], "invalid_candidate_payload")
            return
        evidence_state = payload.get("evidence_state")
        if evidence_state is None:
            self._reextract_legacy_candidate(candidate)
            return
        if evidence_state == "evidence_invalid":
            try:
                repaired = self._repair_pending_evidence(candidate, payload)
            except self.source.RuntimeBranchUnavailable:
                raise
            except Exception as exc:
                print(f"[writer] evidence repair deferred: {exc}")
                return
            if not repaired:
                return
        result, status = self.apply_candidate(candidate["candidate_id"])
        print(f"[writer] candidate={candidate['candidate_id'][:12]} http={status} {result}")
        self.source.project_candidate_profile(candidate["candidate_id"])


    def process_pending_candidates(self, batch_key=None, exclude_ids=None):
        attempted = set()
        excluded = set(exclude_ids or ())
        for candidate in self.ops.get_pending_candidates(batch_key):
            candidate_key = candidate["candidate_id"]
            if candidate_key in excluded:
                continue
            attempted.add(candidate_key)
            self._process_candidate(candidate)
        return attempted


    def run_writer_once(self):
        watermark = self.source.get_watermark()
        messages = self.source.fetch_new(watermark)
        try:
            eligible, branch_decisions = self.source.filter_canonical_branch(messages)
            self.source.retry_profile_projections()
            attempted = self.process_pending_candidates()
        except self.source.RuntimeBranchUnavailable as exc:
            print(f"[writer] Runtime canonical branch unavailable; fail-closed: {exc}")
            return
        if not messages:
            print(f"[writer] 无新对话 (watermark={watermark})")
            return

        raw_start_id = min(_id_of(row) for row in messages)
        raw_end_id = max(_id_of(row) for row in messages)
        input_hash = self.writer_input_hash(messages, branch_decisions)
        profile = self.profiles.get_profile("writer")
        batch_key = self.writer_batch_key(raw_start_id, raw_end_id, input_hash, profile)
        batch = self.ops.get_writer_batch(batch_key)
        is_new_batch = batch is None
        if is_new_batch:
            points = self.summarize(eligible, profile=profile) if eligible else []
            candidates = [
                {
                    "candidate_id": self.candidate_id(batch_key, index, point),
                    "payload": self._candidate_payload(
                        point, eligible, raw_start_id, raw_end_id
                    ),
                }
                for index, point in enumerate(points)
            ]
            is_new_batch = self.ops.put_writer_batch(
                {
                    "batch_key": batch_key,
                    "raw_start_id": raw_start_id,
                    "raw_end_id": raw_end_id,
                    "input_hash": input_hash,
                    "extractor_model": profile["model"],
                    "policy_version": WRITER_EXTRACT_POLICY_VERSION,
                },
                candidates,
            )
        else:
            raw_end_id = batch["raw_end_id"]
            print(f"[writer] 命中批次 receipt，跳过提炼 ({batch_key[:12]})")

        try:
            if is_new_batch:
                self.process_pending_candidates(batch_key)
            else:
                self.process_pending_candidates(batch_key, exclude_ids=attempted)
        except self.source.RuntimeBranchUnavailable as exc:
            print(f"[writer] Runtime canonical branch unavailable; fail-closed: {exc}")
            return

        try:
            self.source.set_watermark(raw_end_id)
        except Exception as exc:
            print(f"[writer] watermark 写入失败，下轮复用批次 receipt: {exc}")
        print("[writer] 完成")
