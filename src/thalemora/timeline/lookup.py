"""从现有时间线状态和当前修订读取日、周、月卡。"""
from datetime import timedelta
from thalemora.memory.manual import _positive_int
from .maintenance import period_bounds
from . import state

def bounds(granularity, period_key):
    if granularity not in ("week", "month"):
        raise ValueError("invalid granularity")
    try:
        start, last = period_bounds(granularity, str(period_key or ""))
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid period_key") from exc
    return start, last + timedelta(days=1)

def current_item(card_id, connect):
    card_id = _positive_int(card_id)
    if card_id is None:
        return None
    conn = connect()
    try:
        row = conn.execute(
            "SELECT c.id, c.current_revision_id, c.title, c.happened_at, "
            "c.status, c.memory_kind, r.content AS revision_content "
            "FROM cards c LEFT JOIN card_revisions r "
            "ON r.id=c.current_revision_id AND r.card_id=c.id WHERE c.id=?",
            (card_id,),
        ).fetchone()
    finally:
        conn.close()
    if (
        row is None
        or row["status"] != "fresh"
        or row["memory_kind"] != "timeline"
        or row["current_revision_id"] is None
        or row["revision_content"] is None
    ):
        return None
    return {
        "card_id": int(row["id"]),
        "current_revision_id": int(row["current_revision_id"]),
        "content": row["revision_content"],
        "title": row["title"],
        "happened_at": row["happened_at"],
    }

def ready_days(start, end, connect, ops_db):
    conn = state.connect_timeline_ops(ops_db)
    try:
        rows = conn.execute(
            "SELECT date, card_id FROM timeline_day_state "
            "WHERE status='ready' AND date>=? AND date<? ORDER BY date",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    return [
        item for _date, card_id in rows
        for item in [current_item(card_id, connect)]
        if item is not None
    ]

def recent(granularity, connect, ops_db):
    conn = state.connect_timeline_ops(ops_db)
    try:
        if granularity == "day":
            rows = conn.execute(
                "SELECT card_id FROM timeline_day_state "
                "WHERE status='ready' ORDER BY date DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT card_id FROM timeline_periods "
                "WHERE granularity=? AND status='ready' ORDER BY period_key DESC",
                (granularity,),
            ).fetchall()
    finally:
        conn.close()
    for (card_id,) in rows:
        item = current_item(card_id, connect)
        if item is not None:
            return item
    return None

def lookup(body, *, connect, ops_db):
    if not isinstance(body, dict):
        return {"error": "invalid request"}, 400
    mode = body.get("mode")
    if mode == "recent":
        if set(body) != {"mode"}:
            return {"error": "invalid request"}, 400
        return {
            "mode": "recent",
            "day": recent("day", connect, ops_db),
            "week": recent("week", connect, ops_db),
            "month": recent("month", connect, ops_db),
        }, 200
    if mode != "period":
        return {"error": "invalid mode"}, 400
    if set(body) != {"mode", "granularity", "period_key"}:
        return {"error": "invalid request"}, 400
    granularity = body.get("granularity")
    period_key = body.get("period_key")
    try:
        start, end = bounds(granularity, period_key)
    except ValueError as exc:
        return {"error": str(exc)}, 400
    receipt = state.get_timeline_period(granularity, period_key, ops_db)
    rollup = None
    if receipt is not None and receipt.get("status") == "ready":
        rollup = current_item(receipt.get("card_id"), connect)
    days = ready_days(start, end, connect, ops_db)
    return {
        "mode": "period",
        "granularity": granularity,
        "period_key": period_key,
        "available": rollup is not None or bool(days),
        "rollup": rollup,
        "days": days,
    }, 200
