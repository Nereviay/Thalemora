"""卡片历史归属；不读取当前 Archive 正文，不把来源失效误当作归属丢失。"""
import json


SOURCE = "source-scoped"
OWNER = "owner-global"


def validate_scope(scope):
    if scope not in (SOURCE, OWNER):
        raise ValueError("scope 须为 source-scoped 或 owner-global")


def reference_scope(evidence, agent_id, *, creation=False):
    """仅支持已知 envelope；旧版缺 Agent 采用可信 per-Agent 文件上下文。"""
    if evidence["source_kind"] not in {"manual", "derived"}:
        return None
    try:
        ref = json.loads(evidence["source_ref"])
    except (ValueError, TypeError):
        return None
    if not isinstance(ref, dict) or ref.get("agent_id", agent_id) != agent_id:
        return None
    kind = ref.get("kind")
    if kind not in (None, "archive_quote", "mcp_explicit_manual_revision"):
        return None
    if not isinstance(ref.get("app"), str) or not ref["app"].strip() or ref.get("channel") not in ("main", "private"):
        return None
    if kind == "mcp_explicit_manual_revision":
        if creation or evidence["source_kind"] != "manual":
            return None
    elif (not isinstance(ref.get("source_event_id"), str) or not ref["source_event_id"].strip()
          or type(ref.get("version")) is not int or not 0 < ref["version"] <= 2**63 - 1
          or (evidence["source_kind"] == "derived" and kind != "archive_quote")):
        return None
    return ref["app"], ref["channel"]


def ownership(db, agent_id, channel):
    """同一连接/快照内读 scope 与全部首修订证据；无回填、无首次访问认领。"""
    recorded = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='archive_card_scopes'").fetchone():
        recorded = {r[0]: (r[1], r[2]) for r in db.execute("SELECT card_id,app,channel FROM archive_card_scopes")}
    first, markers = {}, {}
    for row in db.execute("SELECT r.card_id,r.revision_no,e.source_kind,e.source_ref FROM card_revisions r "
                          "JOIN card_evidence e ON e.revision_id=r.id ORDER BY e.id"):
        if row["revision_no"] == 1:
            first.setdefault(row["card_id"], []).append(reference_scope(row, agent_id, creation=True))
        else:
            # 旧手改 marker 不得把已知归属转投其他应用。
            try:
                ref = json.loads(row["source_ref"])
            except (ValueError, TypeError):
                continue
            if isinstance(ref, dict) and ref.get("kind") == "mcp_explicit_manual_revision":
                markers.setdefault(row["card_id"], []).append(reference_scope(row, agent_id))
    out = {}
    for (card_id,) in db.execute("SELECT id FROM cards"):
        refs = first.get(card_id, [])
        known = {ref for ref in refs if ref is not None}
        record = recorded.get(card_id)
        candidates = known | ({record} if record is not None else set())
        state = "unknown"
        app = None
        if len(candidates) > 1 or any(ref[1] != channel for ref in candidates):
            state = "conflict"
        elif refs and None not in refs and len(known) == 1:
            app, _ = next(iter(known))
            state = "known"
            if any(marker != (app, channel) for marker in markers.get(card_id, [])):
                state, app = "conflict", None
        out[card_id] = dict(state=state, app=app, channel=channel)
    return out


def belongs(item, app_id, channel):
    return bool(item and item["state"] == "known" and (item["app"], item["channel"]) == (app_id, channel))


def require_card(db, card_id, agent_id, app_id, channel, scope):
    item = ownership(db, agent_id, channel).get(card_id)
    if scope == SOURCE and not belongs(item, app_id, channel):
        raise PermissionError("card_outside_scope")
    if item is None:
        raise KeyError("找不到记忆")
    return item
