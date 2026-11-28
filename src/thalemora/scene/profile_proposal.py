"""从已准入候选核对明确自述，正式人物档案由调用方投影。"""
PROFILE_STATEMENT_PREFIXES = {
    "work_status": ("我目前的工作状态是", "我的工作状态是", "我已经入职", "我已入职", "我目前待业", "我目前在职"),
    "recent_status": ("我目前的状态是", "我最近的状态是"),
    "city": ("我目前居住在", "我现在住在", "我目前住在"),
    "preferences": ("我的偏好是", "我喜欢"),
    "education": ("我的学历是", "我已经毕业于"),
    "long_term_goals": ("我的长期目标是",),
}


def profile_proposal_evidence(payload, event_reader):
    proposal = payload["profile_update"]
    required = {"owner_scope", "field", "value", "event_id", "assertion", "temporal_status", "confirmation"}
    if not isinstance(proposal, dict) or set(proposal) != required:
        raise ValueError("invalid profile_update contract")
    if (proposal["owner_scope"] != "user" or proposal["field"] not in PROFILE_STATEMENT_PREFIXES
            or payload.get("claim_scope") not in {"user_fact", "shared"}
            or proposal["temporal_status"] != "current"
            or proposal["assertion"] not in {"statement", "correction"}
            or proposal["confirmation"] not in {"first", "reconfirmed"}
            or type(proposal["event_id"]) is not int):
        raise ValueError("unsupported profile scope/subject/temporal assertion")
    value = proposal["value"]
    if not isinstance(value, str) or not value.startswith(PROFILE_STATEMENT_PREFIXES[proposal["field"]]):
        raise ValueError("profile value is not a supported explicit self statement")
    quotes = payload.get("evidence") or []
    verified = payload.get("verified_evidence") or []
    selected = [(quote, check) for quote, check in zip(quotes, verified)
                if quote["event_id"] == proposal["event_id"] and quote["quote"] == value]
    if payload.get("evidence_state") != "verified" or len(quotes) != len(verified) or len(selected) != 1:
        raise ValueError("profile proposal has no unique verified author quote")
    event = event_reader(proposal["event_id"])
    if value != event["metadata_json"]["authored_text"]:
        raise ValueError("profile value must be the complete author statement")
    quote, check = selected[0]
    return proposal, {**payload, "evidence": [quote], "verified_evidence": [check]}
