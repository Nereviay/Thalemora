"""时间线来源指纹、人工保护、修订和周期补偿。"""
import datetime
import hashlib
import json
import re
import sqlite3
from thalemora.memory import revision_store, operations
from . import state as ops_store
from .content import validate_timeline_content

DAILY_IMPORTANCE = 5.5


MAX_MSG_CHARS = 160


MAX_TOTAL_CHARS = 24000


PERIOD_RETRY_LIMIT_PER_RUN = 4


UNAVAILABLE_CARD_STATUSES = {"archived", "trashed", "purged"}

SUMMARY_REQUEST_RE = re.compile(
    r"(?:请你|麻烦你|帮我).{0,30}(?:梳理|整理).{0,20}(?:整个|完整|全(?:部|量)?).{0,10}窗口",
    re.S,
)


CONTAINER_MARKER_RE = re.compile(
    r"(?:上一窗口整理|窗口(?:一)?(?:完整)?整理|继承说明|本窗口事件全览|新窗口交接|交接清单)"
)


STRUCTURE_RE = re.compile(
    r"(?:#{1,4}\s*[^\n]+|阶段[一二三四五六七八九十0-9]+|核心约定|事件全览|当前状态|新增/变更)"
)

def historical_context_reason(text, role):
    stripped = (text or "").lstrip()
    head = stripped[:420]
    if role == "user" and len(stripped) >= 120 and SUMMARY_REQUEST_RE.search(head):
        return "summary_request"
    if len(stripped) >= 500 and CONTAINER_MARKER_RE.search(head) and STRUCTURE_RE.search(stripped[:1000]):
        return "historical_container"
    if (
        len(stripped) >= 400
        and re.search(r"(?:^|\n)\s*(?:#{1,4}\s*)?(?:交接清单|新窗口交接)", head)
        and re.search(r"(?:当前状态|下一步|按序|清单)", stripped[:1000])
    ):
        return "handoff_checklist"
    return None

def messages_from_rows(rows):
    msgs = [
        {
            "event_id": int(r["id"]),
            "role": r["role"],
            "created_at": r["created_at"],
            "quotable_text": (r["text"] or "").strip()[:MAX_MSG_CHARS],
            "truncated": len((r["text"] or "").strip()) > MAX_MSG_CHARS,
        }
        for r in rows
    ]
    total = sum(len(m["quotable_text"]) for m in msgs)
    if total > MAX_TOTAL_CHARS:
        keep = max(1, MAX_TOTAL_CHARS * len(msgs) // total)
        idxs = sorted({round(i * (len(msgs) - 1) / (keep - 1)) for i in range(keep)}) if keep > 1 else [0]
        msgs = [msgs[i] for i in idxs]
        total = sum(len(m["quotable_text"]) for m in msgs)
        print(f"[daily_summary] 对话超长，均匀抽样保留 {len(msgs)} 条（{total} 字）")
    return msgs

def daily_source_identity(utc_range, rows):
    start_utc, end_utc = utc_range
    stable = []
    for row in rows:
        fallback_hash = hashlib.sha256(
            f"{row['role']}\0{row['created_at']}\0{row['text']}".encode("utf-8")
        ).hexdigest()
        stable.append([int(row["id"]), (row.get("event_hash") or fallback_hash)])
    payload = json.dumps(stable, ensure_ascii=False, separators=(",", ":"))
    return {
        "source_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "source_count": len(rows),
        "source_max_raw_event_id": max((int(r["id"]) for r in rows), default=None),
        "raw_event_start_id": min((int(r["id"]) for r in rows), default=None),
        "raw_event_end_id": max((int(r["id"]) for r in rows), default=None),
        "start_utc": start_utc,
        "end_utc": end_utc,
    }

def period_for_day(day_str, granularity):
    day = datetime.date.fromisoformat(day_str)
    if granularity == "week":
        iso = day.isocalendar()
        start = datetime.date.fromisocalendar(iso.year, iso.week, 1)
        return f"{iso.year}-W{iso.week:02d}", start, start + datetime.timedelta(days=6)
    if granularity == "month":
        start = day.replace(day=1)
        next_month = (
            datetime.date(day.year + 1, 1, 1)
            if day.month == 12 else datetime.date(day.year, day.month + 1, 1)
        )
        return f"{day.year:04d}-{day.month:02d}", start, next_month - datetime.timedelta(days=1)
    raise ValueError(f"invalid timeline granularity: {granularity}")


def period_bounds(granularity, period_key):
    if granularity == "week":
        match = re.fullmatch(r"(\d{4})-W(\d{2})", period_key)
        if not match:
            raise ValueError(f"invalid week key: {period_key}")
        start = datetime.date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
        return start, start + datetime.timedelta(days=6)
    if granularity == "month":
        match = re.fullmatch(r"(\d{4})-(\d{2})", period_key)
        if not match:
            raise ValueError(f"invalid month key: {period_key}")
        start = datetime.date(int(match.group(1)), int(match.group(2)), 1)
        next_month = (
            datetime.date(start.year + 1, 1, 1)
            if start.month == 12 else datetime.date(start.year, start.month + 1, 1)
        )
        return start, next_month - datetime.timedelta(days=1)
    raise ValueError(f"invalid timeline granularity: {granularity}")


def period_is_closed(granularity, period_key, today):
    _start, end = period_bounds(granularity, period_key)
    return end < today


def _period_dates(start, end):
    return [
        (start + datetime.timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]


def _period_state_payload(
    granularity, period_key, *, source_set_hash=None, status,
    card_id=None, revision_id=None, last_error=None,
):
    return {
        "granularity": granularity,
        "period_key": period_key,
        "source_set_hash": source_set_hash,
        "status": status,
        "card_id": card_id,
        "revision_id": revision_id,
        "last_error": last_error,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }


def _period_sources(granularity, period_key, memory_db_path, ops_db_path):
    start, end = period_bounds(granularity, period_key)
    ready_days = []
    empty_days = []
    blockers = []
    day_sources = []
    conn = sqlite3.connect(memory_db_path)
    conn.row_factory = sqlite3.Row
    try:
        for day_str in _period_dates(start, end):
            state = ops_store.get_timeline_day_state(day_str, db_path=ops_db_path)
            if state is None:
                blockers.append(f"{day_str}:missing")
                continue
            if state["status"] == "empty":
                empty_days.append(day_str)
                continue
            if state["status"] != "ready":
                blockers.append(f"{day_str}:{state['status']}")
                continue
            if state.get("card_id") is None:
                blockers.append(f"{day_str}:ready_card_missing")
                continue
            row = conn.execute(
                "SELECT c.id, c.status, c.memory_kind, c.current_revision_id, "
                "r.card_id AS revision_card_id, r.content, r.content_hash "
                "FROM cards c LEFT JOIN card_revisions r ON r.id=c.current_revision_id "
                "WHERE c.id=?",
                (state["card_id"],),
            ).fetchone()
            if (
                row is None
                or row["status"] in UNAVAILABLE_CARD_STATUSES
                or row["memory_kind"] != "timeline"
                or row["current_revision_id"] is None
                or row["revision_card_id"] != row["id"]
            ):
                blockers.append(f"{day_str}:ready_card_unavailable")
                continue
            ready_days.append(day_str)
            day_sources.append({
                "date": day_str,
                "revision_id": int(row["current_revision_id"]),
                "content_hash": row["content_hash"],
                "content": row["content"],
            })
    finally:
        conn.close()
    if blockers:
        return {
            "start": start, "end": end, "blockers": blockers,
            "ready_days": ready_days, "empty_days": empty_days,
            "day_sources": day_sources, "source_set_hash": None,
        }
    stable = [
        [source["date"], source["revision_id"], source["content_hash"]]
        for source in day_sources
    ]
    payload = json.dumps(stable, ensure_ascii=False, separators=(",", ":"))
    return {
        "start": start, "end": end, "blockers": [],
        "ready_days": ready_days, "empty_days": empty_days,
        "day_sources": day_sources,
        "source_set_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


def _period_card_by_id(memory_db_path, card_id):
    if card_id is None:
        return None, None
    conn = sqlite3.connect(memory_db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None, "purged"
    card = dict(row)
    if card.get("memory_kind") != "timeline":
        return card, "invalid_memory_kind"
    if card.get("status") in UNAVAILABLE_CARD_STATUSES:
        return card, card["status"]
    if card.get("current_revision_id") is None:
        return card, "current_revision_missing"
    return card, None


def _canonical_period_result(granularity, period_key, memory_db_path):
    """Recover period identity from canonical derived evidence, never from title."""
    start, end = period_bounds(granularity, period_key)
    conn = sqlite3.connect(memory_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT e.source_ref, r.id AS derived_revision_id, r.revision_no, "
            "r.card_id "
            "FROM card_evidence e "
            "JOIN card_revisions r ON r.id=e.revision_id "
            "WHERE e.source_kind='derived' AND r.origin='daily_summary' "
            "ORDER BY r.revision_no, r.id"
        ).fetchall()
    finally:
        conn.close()
    matches = []
    for row in rows:
        try:
            source_ref = json.loads(row["source_ref"] or "")
        except (TypeError, ValueError):
            continue
        if (
            source_ref.get("granularity") == granularity
            and source_ref.get("period_start") == start.isoformat()
            and source_ref.get("period_end") == end.isoformat()
        ):
            matches.append((row, source_ref))
    card_ids = {int(row["card_id"]) for row, _source_ref in matches}
    if len(card_ids) > 1:
        raise RuntimeError(
            f"multiple canonical cards found for {granularity} {period_key}"
        )
    if not matches:
        return None
    row, source_ref = matches[-1]
    return {
        "card_id": int(row["card_id"]),
        "revision_id": int(row["derived_revision_id"]),
        "source_set_hash": source_ref.get("source_set_hash"),
    }


def _period_evidence(granularity, sources):
    ref = json.dumps({
        "granularity": granularity,
        "period_start": sources["start"].isoformat(),
        "period_end": sources["end"].isoformat(),
        "source_set_hash": sources["source_set_hash"],
        "day_revision_ids": [source["revision_id"] for source in sources["day_sources"]],
        "ready_days": sources["ready_days"],
        "empty_days": sources["empty_days"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return [{"source_kind": "derived", "source_ref": ref}]


def process_period(
    granularity, period_key, *, memory_db_path, ops_db_path, summarize_fn, force=False,
):
    previous = ops_store.get_timeline_period(
        granularity, period_key, db_path=ops_db_path
    )
    sources = _period_sources(granularity, period_key, memory_db_path, ops_db_path)
    try:
        recovered = _canonical_period_result(
            granularity, period_key, memory_db_path
        )
    except RuntimeError as exc:
        blocked = _period_state_payload(
            granularity, period_key,
            source_set_hash=sources.get("source_set_hash")
            or (previous or {}).get("source_set_hash"),
            status="blocked", card_id=(previous or {}).get("card_id"),
            revision_id=(previous or {}).get("revision_id"),
            last_error=f"canonical_identity_conflict:{str(exc)[:150]}",
        )
        ops_store.upsert_timeline_period(blocked, db_path=ops_db_path)
        print(f"[daily_summary] {granularity} {period_key} canonical identity 冲突，0 provider")
        return {"status": "blocked", "state": blocked, "provider_called": False}

    card_id = (previous or {}).get("card_id")
    derived_revision_id = (previous or {}).get("revision_id")
    if recovered:
        if card_id is not None and card_id != recovered["card_id"]:
            blocked = _period_state_payload(
                granularity, period_key,
                source_set_hash=sources.get("source_set_hash")
                or (previous or {}).get("source_set_hash"),
                status="blocked", card_id=card_id,
                revision_id=derived_revision_id,
                last_error="canonical_identity_conflict:state_card_mismatch",
            )
            ops_store.upsert_timeline_period(blocked, db_path=ops_db_path)
            print(f"[daily_summary] {granularity} {period_key} state/evidence identity 冲突，0 provider")
            return {"status": "blocked", "state": blocked, "provider_called": False}
        card_id = recovered["card_id"]
        derived_revision_id = recovered["revision_id"]

    card, lifecycle_error = _period_card_by_id(memory_db_path, card_id)
    if lifecycle_error:
        blocked = _period_state_payload(
            granularity, period_key,
            source_set_hash=sources.get("source_set_hash")
            or (previous or {}).get("source_set_hash"),
            status="blocked", card_id=card_id,
            revision_id=derived_revision_id,
            last_error=f"period_card_unavailable:{lifecycle_error}",
        )
        ops_store.upsert_timeline_period(blocked, db_path=ops_db_path)
        print(f"[daily_summary] {granularity} {period_key} period card 不可用，0 provider")
        return {"status": "unavailable", "state": blocked, "provider_called": False}

    if sources["blockers"]:
        blocked = _period_state_payload(
            granularity, period_key,
            source_set_hash=(previous or {}).get("source_set_hash"),
            status="blocked", card_id=card_id,
            revision_id=derived_revision_id,
            last_error="day_sources_blocked:" + ",".join(sources["blockers"]),
        )
        ops_store.upsert_timeline_period(blocked, db_path=ops_db_path)
        print(f"[daily_summary] {granularity} {period_key} day source 未就绪，0 provider")
        return {"status": "blocked", "state": blocked, "provider_called": False}

    source_set_hash = sources["source_set_hash"]
    if (
        recovered and recovered["source_set_hash"] == source_set_hash
        and not force
        and (
            not previous
            or previous["status"] != "ready"
            or previous.get("card_id") != recovered["card_id"]
            or previous.get("revision_id") != recovered["revision_id"]
            or previous.get("source_set_hash") != source_set_hash
        )
    ):
        ready = _period_state_payload(
            granularity, period_key, source_set_hash=source_set_hash,
            status="ready", card_id=card_id,
            revision_id=derived_revision_id, last_error=None,
        )
        ops_store.upsert_timeline_period(ready, db_path=ops_db_path)
        print(f"[daily_summary] {granularity} {period_key} canonical result 已恢复，0 provider")
        return {"status": "recovered", "state": ready, "provider_called": False}

    if (
        previous and previous["status"] == "ready"
        and previous["source_set_hash"] == source_set_hash and not force
    ):
        print(f"[daily_summary] {granularity} {period_key} source 未变化，0 provider")
        return {"status": "skipped", "state": previous, "provider_called": False}

    if len(sources["day_sources"]) < 2:
        sparse = _period_state_payload(
            granularity, period_key, source_set_hash=source_set_hash,
            status="sparse", card_id=card_id,
            revision_id=derived_revision_id,
            last_error=f"insufficient_ready_days:{len(sources['day_sources'])}",
        )
        ops_store.upsert_timeline_period(sparse, db_path=ops_db_path)
        print(f"[daily_summary] {granularity} {period_key} ready day <2，不生成卡，0 provider")
        return {"status": "sparse", "state": sparse, "provider_called": False}

    if previous and previous["status"] == "ready" and card is None:
        blocked = _period_state_payload(
            granularity, period_key, source_set_hash=source_set_hash,
            status="blocked", last_error="canonical_identity_missing",
        )
        ops_store.upsert_timeline_period(blocked, db_path=ops_db_path)
        print(f"[daily_summary] {granularity} {period_key} ready identity 缺失，0 provider")
        return {"status": "blocked", "state": blocked, "provider_called": False}

    if card and derived_revision_id is None:
        blocked = _period_state_payload(
            granularity, period_key, source_set_hash=source_set_hash,
            status="blocked", card_id=card_id,
            last_error="canonical_revision_missing",
        )
        ops_store.upsert_timeline_period(blocked, db_path=ops_db_path)
        print(f"[daily_summary] {granularity} {period_key} derived revision 缺失，0 provider")
        return {"status": "blocked", "state": blocked, "provider_called": False}

    if card and derived_revision_id:
        reachable, manual_content_changed = operations.revision_manual_content_changed_since(
            memory_db_path,
            card_id=card["id"],
            ancestor_revision_id=derived_revision_id,
            current_revision_id=card["current_revision_id"],
        )
        if not reachable or manual_content_changed:
            blocked = _period_state_payload(
                granularity, period_key, source_set_hash=source_set_hash,
                status="blocked", card_id=card["id"],
                revision_id=derived_revision_id,
                last_error="manual_override_source_changed",
            )
            ops_store.upsert_timeline_period(blocked, db_path=ops_db_path)
            print(f"[daily_summary] {granularity} {period_key} 有人工正文改写，0 provider")
            return {"status": "manual_override", "state": blocked, "provider_called": False}

    pending = _period_state_payload(
        granularity, period_key, source_set_hash=source_set_hash,
        status="pending", card_id=(card or {}).get("id"),
        revision_id=derived_revision_id,
    )
    ops_store.upsert_timeline_period(pending, db_path=ops_db_path)
    try:
        content = validate_timeline_content(
            summarize_fn(granularity, period_key, sources["day_sources"])
        )
        evidence_rows = _period_evidence(granularity, sources)
        if card is None:
            result = revision_store.create_card(
                memory_db_path,
                content=content, happened_at=sources["end"].isoformat(),
                importance=DAILY_IMPORTANCE, tags="时间印象", title=period_key,
                candidate_id=f"timeline:{granularity}:{period_key}",
                embedding=None, source="daily_summary", memory_kind="timeline",
                origin="daily_summary", evidence_rows=evidence_rows,
            )
        else:
            result = revision_store.update_card(
                memory_db_path,
                card_id=card["id"], expected_current_revision_id=card["current_revision_id"],
                content=content, happened_at=sources["end"].isoformat(),
                importance=DAILY_IMPORTANCE, embedding=None,
                change_type="derived_refresh", origin="daily_summary",
                evidence_rows=evidence_rows, force_revision=True,
            )
    except Exception as exc:
        failed = _period_state_payload(
            granularity, period_key, source_set_hash=source_set_hash,
            status="failed", card_id=(card or {}).get("id"),
            revision_id=derived_revision_id,
            last_error=f"{type(exc).__name__}:{str(exc)[:180]}",
        )
        ops_store.upsert_timeline_period(failed, db_path=ops_db_path)
        raise

    ready = _period_state_payload(
        granularity, period_key, source_set_hash=source_set_hash,
        status="ready", card_id=result["card_id"],
        revision_id=result["current_revision_id"], last_error=None,
    )
    ops_store.upsert_timeline_period(ready, db_path=ops_db_path)
    print(
        f"[daily_summary] {granularity} {period_key} ready "
        f"card={result['card_id']} rev={result['current_revision_id']}"
    )
    return {"status": "ready", "state": ready, "provider_called": True, "result": result}


def run_period_rollups(
    day_str, *, memory_db_path, ops_db_path, summarize_fn, as_of_date, explicit_date=False, force=False,
):
    day = datetime.date.fromisoformat(day_str)
    today = as_of_date
    candidates = []
    if explicit_date or day.weekday() == 6:
        candidates.append(("week", period_for_day(day_str, "week")[0]))
    if explicit_date or day == period_for_day(day_str, "month")[2]:
        candidates.append(("month", period_for_day(day_str, "month")[0]))
    immediate = [
        item for item in candidates if period_is_closed(item[0], item[1], today)
    ]

    results = []
    for granularity, period_key in immediate:
        if explicit_date:
            ops_store.mark_timeline_period_pending(granularity, period_key, db_path=ops_db_path)
        try:
            results.append(process_period(
                granularity, period_key, force=force,
                memory_db_path=memory_db_path, ops_db_path=ops_db_path,
                summarize_fn=summarize_fn,
            ))
        except Exception as exc:
            print(f"[daily_summary] {granularity} {period_key} 失败：{type(exc).__name__}:{exc}")

    retry_candidates = ops_store.list_retry_timeline_periods(
        None, exclude=immediate, db_path=ops_db_path,
    )
    retries = [
        state for state in retry_candidates
        if period_is_closed(state["granularity"], state["period_key"], today)
    ][:PERIOD_RETRY_LIMIT_PER_RUN]
    for state in retries:
        try:
            results.append(process_period(
                state["granularity"], state["period_key"], force=False,
                memory_db_path=memory_db_path, ops_db_path=ops_db_path,
                summarize_fn=summarize_fn,
            ))
        except Exception as exc:
            print(
                f"[daily_summary] retry {state['granularity']} {state['period_key']} "
                f"失败：{type(exc).__name__}:{exc}"
            )
    return results


def _day_card(day_str, memory_db_path, state=None):
    conn = sqlite3.connect(memory_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if state and state.get("card_id") is not None:
            row = conn.execute("SELECT * FROM cards WHERE id=?", (state["card_id"],)).fetchone()
            if row is None:
                raise RuntimeError(f"day_state {day_str} points to missing card {state['card_id']}")
            rows = [row]
        else:
            rows = conn.execute(
                "SELECT * FROM cards "
                "WHERE memory_kind='timeline' AND tags='日子' "
                "AND substr(happened_at,1,10)=? ORDER BY id",
                (day_str,),
            ).fetchall()
    finally:
        conn.close()
    if len(rows) > 1:
        raise RuntimeError(f"multiple cards use day title {day_str}")
    if not rows:
        return None
    card = dict(rows[0])
    if card.get("memory_kind") != "timeline":
        raise RuntimeError(f"day {day_str} resolves to non-timeline card {card['id']}")
    return card


def _evidence(day_str, identity):
    ref = json.dumps({
        "date": day_str,
        "start_utc": identity["start_utc"],
        "end_utc": identity["end_utc"],
        "source_hash": identity["source_hash"],
        "source_count": identity["source_count"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return [{
        "source_kind": "raw_range",
        "source_ref": ref,
        "raw_event_start_id": identity["raw_event_start_id"],
        "raw_event_end_id": identity["raw_event_end_id"],
        "event_hash": identity["source_hash"],
    }]


def _state_payload(day_str, identity, *, status, card_id=None, revision_id=None, last_error=None):
    return {
        "date": day_str,
        "source_hash": identity["source_hash"],
        "source_count": identity["source_count"],
        "source_max_raw_event_id": identity["source_max_raw_event_id"],
        "status": status,
        "card_id": card_id,
        "revision_id": revision_id,
        "last_error": last_error,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }


def process_day(day_str, *, rows, utc_range, memory_db_path, ops_db_path, summarize_fn, force=False):
    rows = [r for r in rows if not historical_context_reason(r["text"], r["role"])]
    identity = daily_source_identity(utc_range, rows)
    state = ops_store.get_timeline_day_state(day_str, db_path=ops_db_path)
    card = _day_card(day_str, memory_db_path, state=state)

    if card and card.get("source") == "daily_summary" and card.get("current_revision_id"):
        # 先前可能已经提交卡片，但尚未来得及写入派生回执。
        with sqlite3.connect(memory_db_path) as conn:
            refs = conn.execute(
                "SELECT e.source_ref FROM card_evidence e JOIN card_revisions r ON r.id=e.revision_id "
                "WHERE r.id=? AND r.origin='daily_summary' AND e.source_kind='raw_range'",
                (card["current_revision_id"],)).fetchall()
        if any(json.loads(ref[0]).get("source_hash") == identity["source_hash"] for ref in refs) and (
                not state or state["status"] != "ready" or state.get("revision_id") != card["current_revision_id"]):
            ready = _state_payload(day_str, identity, status="ready", card_id=card["id"], revision_id=card["current_revision_id"])
            ops_store.upsert_timeline_day_state(ready, db_path=ops_db_path)
            return {"status": "recovered", "state": ready, "provider_called": False}
    if card and (card.get("source") != "daily_summary" or card.get("status") in UNAVAILABLE_CARD_STATUSES
                 or (card.get("current_revision_id") and not (state or {}).get("revision_id"))):
        return {"status": "protected_existing_card", "state": state, "provider_called": False}
    if card and card.get("current_revision_id") is None:
        boot = operations.bootstrap_legacy_snapshot(memory_db_path, card_id=card["id"])
        card = _day_card(day_str, memory_db_path, state=state)
        print(f"[daily_summary] 过渡日卡 #{card['id']} 已保留 legacy_snapshot rev={boot['current_revision_id']}")

    if state and state["source_hash"] == identity["source_hash"] and state["status"] in ("ready", "empty") and not force:
        print(f"[daily_summary] {day_str} source 未变化，状态={state['status']}，0 provider")
        return {"status": "skipped", "state": state, "provider_called": False}

    if identity["source_count"] == 0:
        empty_state = _state_payload(day_str, identity, status="empty")
        ops_store.upsert_timeline_day_state(empty_state, db_path=ops_db_path)
        if card:
            print(f"[daily_summary] 异常：{day_str} 有旧日卡 #{card['id']} 但当前有效 raw=0；保留卡片，day_state=empty")
        else:
            print(f"[daily_summary] {day_str} 无有效对话，day_state=empty，0 provider")
        return {"status": "empty", "state": empty_state, "provider_called": False}

    if card and state and state.get("revision_id"):
        reachable, content_changed = operations.revision_content_changed_since(
            memory_db_path,
            card_id=card["id"],
            ancestor_revision_id=state["revision_id"],
            current_revision_id=card["current_revision_id"],
        )
        if not reachable or content_changed:
            blocked = _state_payload(
                day_str, identity, status="ready", card_id=card["id"],
                revision_id=state["revision_id"], last_error="manual_override_source_changed",
            )
            ops_store.upsert_timeline_day_state(blocked, db_path=ops_db_path)
            print(f"[daily_summary] {day_str} 当前日卡正文有人工改写，保留人工正文，0 provider")
            return {"status": "manual_override", "state": blocked, "provider_called": False}

    msgs = messages_from_rows(rows)
    try:
        content = validate_timeline_content(summarize_fn(day_str, msgs))
        evidence_rows = _evidence(day_str, identity)
        if card is None:
            result = revision_store.create_card(
                memory_db_path,
                content=content, happened_at=day_str, importance=DAILY_IMPORTANCE,
                tags="日子", title=day_str, embedding=None, source="daily_summary",
                candidate_id=f"timeline:day:{day_str}",
                memory_kind="timeline", origin="daily_summary",
                evidence_rows=evidence_rows,
            )
        else:
            result = revision_store.update_card(
                memory_db_path,
                card_id=card["id"], expected_current_revision_id=card["current_revision_id"],
                content=content, happened_at=day_str, importance=DAILY_IMPORTANCE,
                embedding=None, change_type="derived_refresh", origin="daily_summary",
                evidence_rows=evidence_rows,
            )
    except Exception as exc:
        failed = _state_payload(
            day_str, identity, status="failed",
            card_id=(state or {}).get("card_id") or (card or {}).get("id"),
            revision_id=(state or {}).get("revision_id"),
            last_error=f"{type(exc).__name__}:{str(exc)[:180]}",
        )
        ops_store.upsert_timeline_day_state(failed, db_path=ops_db_path)
        raise

    ready = _state_payload(
        day_str, identity, status="ready",
        card_id=result["card_id"], revision_id=result["current_revision_id"], last_error=None,
    )
    ops_store.upsert_timeline_day_state(ready, db_path=ops_db_path)
    print(f"[daily_summary] 日卡 ready card={result['card_id']} rev={result['current_revision_id']} source_count={identity['source_count']}")
    return {"status": "ready", "state": ready, "provider_called": True, "result": result}

def retry_failed_days(*, before, ops_db_path, run_day, since=None, limit=1):
    """一次后台触发最多补给定天数；保留各日期的错误和人工保护。"""
    if limit < 0:
        raise ValueError("invalid_retry_limit")
    conn = ops_store.connect_timeline_ops(ops_db_path)
    try:
        dates = [row[0] for row in conn.execute(
            "SELECT date FROM timeline_day_state WHERE status='failed' AND date < ? "
            "AND (? IS NULL OR date >= ?) AND coalesce(last_error,'') NOT LIKE 'auto-retry:%' "
            "ORDER BY updated_at, date LIMIT ?", (before, since, since, limit))]
    finally:
        conn.close()
    results = []
    for day in dates:
        try:
            result = run_day(day)
        except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
            result = {"status": "failed", "error": str(exc)}
        if result["status"] in {"failed", "protected_existing_card"}:
            conn = ops_store.connect_timeline_ops(ops_db_path)
            try:
                with conn:
                    conn.execute("UPDATE timeline_day_state SET last_error='auto-retry:' || coalesce(last_error,'') "
                                 "WHERE date=? AND status='failed' AND coalesce(last_error,'') NOT LIKE 'auto-retry:%'",
                                 (day,))
            finally:
                conn.close()
        results.append({"date": day, **result})
    return results
