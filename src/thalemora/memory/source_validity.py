"""Archive 证据的共享状态判定；current 仅指版本一致，不等于 verified。"""
import json


def resolve_archive_evidence(db, evidence, agent_id, app_id, channel):
    result = dict(resolution="unsupported", current_source_text=None, verified=False, source_key=None)
    kind = evidence["source_kind"]
    if kind not in {"manual", "derived"}:
        return result
    try:
        ref = json.loads(evidence["source_ref"])
    except (ValueError, TypeError):
        return dict(result, resolution="invalid_reference")
    if not isinstance(ref, dict):
        return dict(result, resolution="invalid_reference")
    if ref.get("kind") not in (None, "archive_quote", "mcp_explicit_manual_revision"):
        return result
    if (ref.get("app") != app_id or ref.get("channel") != channel
            or ref.get("agent_id", agent_id) != agent_id):
        return dict(result, resolution="outside_scope")
    if kind == "manual" and ref.get("kind") == "mcp_explicit_manual_revision":
        return dict(result, resolution="manual_revision")
    if kind == "derived" and ref.get("kind") != "archive_quote":
        return result
    if (not isinstance(ref.get("source_event_id"), str) or not ref["source_event_id"].strip()
            or type(ref.get("version")) is not int or not 0 < ref["version"] <= 2**63 - 1):
        return dict(result, resolution="invalid_reference")
    key = (agent_id, app_id, channel, ref["source_event_id"], ref["version"])
    row = db.execute("SELECT version,deleted,text FROM history_events WHERE app=? AND channel=? "
                     "AND source_event_id=?", (app_id, channel, ref["source_event_id"])).fetchone()
    status = ("missing" if row is None else "deleted" if row[1] else
              "current" if row[0] == ref["version"] else "source_changed")
    return dict(result, resolution=status, source_key=key,
                current_source_text=row[2] if row and not row[1] else None)
