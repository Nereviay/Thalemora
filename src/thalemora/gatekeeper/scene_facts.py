"""用已核对的场景引文约束最终写卡判断。"""

RULES = """
source_quotes 是带说话者的已核对引文。仅引用其中实际支持该结论的原话。
若要写入用户偏好或双方约定，user_rule_quote 须选取一条完整的用户引文，能表明其表达该偏好或接受该约定，
保留当次、条件和期限。助手单方承诺、用户只表达当日感受时不能推断长期通则，无法确认则 uncertain。
同一可写卡须逐项比较。旧卡完整覆盖才是重复（drop，fact_relation=covered，target_card_id 指向旧卡，
old_quote 逐字引用能覆盖全部候选事实的旧卡正文）；同一主题有新增事实用 extend，
fact_relation=adds_detail，new_quote 逐字引用能证明新增内容的来源。明确改口用 correct 或 supersede。
同一事项的决定已改变用 fact_relation=changed_decision、target_card_id、old_quote（旧卡依据）和
new_quote（用户表达新决定的完整引文）。仍未完成的挑选、购买或执行不得写成已完成。
仅因缺少长期价值而 drop 时用 fact_relation=not_durable、long_term_value=false。
人工只读卡遵守原有保护。不要把新信息仅因属于同一主题而丢弃。
"""


def _widens_time_scope(quote, proposed):
    single = ("今天", "这次", "这一次", "这回", "仅限当天")
    ongoing = ("以后", "今后", "每次", "每逢", "每周", "一律", "长期", "始终", "总是")
    return (any(word in quote for word in single)
            and any(word in proposed for word in ongoing)
            and not any(word in quote for word in ongoing))


def _adds_detail(decision, target, quotes):
    detail = decision.get("new_quote")
    return (target and isinstance(detail, str) and detail.strip()
            and any(detail in q["quote"] for q in quotes)
            and detail not in (target.get("content") or ""))


def _covers_candidate(basis, old_text, candidate):
    if not isinstance(basis, str) or not basis.strip() or basis not in old_text:
        return False
    # 若引文恰好只覆盖候选首句，后续独立分句也须能在旧正文找到。
    prefix = basis.rstrip("。.!！")
    if prefix and candidate.startswith(prefix):
        tail = candidate[len(prefix):].strip()
        if tail.startswith(("，", ",", "。", ".", "；", ";")):
            clause = tail.strip("，,。.;； ")
            if clause and clause not in old_text:
                return False
    return True


def ground(decision, core, frozen, target_id, confidence_min):
    """只在最终判断拿得到逐字证据时启用。未证实的结论保留为待定。"""
    quotes = core["source_quotes"]
    target = frozen.get(target_id)
    action = decision["action"]
    relation = decision.get("fact_relation")
    if relation == "changed_decision":
        if target and target.get("read_only"):
            return dict(decision, action="drop", reason="manual_memory_read_only")
        old, new = decision.get("old_quote"), decision.get("new_quote")
        score = decision.get("same_memory_confidence")
        if not (target and decision.get("same_memory") is True and type(score) in (int, float)
                and confidence_min <= score <= 1 and isinstance(old, str) and old.strip()
                and old in target["content"] and any(q["role"] == "user" and new == q["quote"] for q in quotes)):
            return dict(decision, action="uncertain", reason="changed_decision_needs_evidence")
        decision = dict(decision, action="supersede", content=new,
                        importance=core.get("importance") or decision.get("importance"),
                        reason="verified_changed_decision")
        action = "supersede"
    if target and target.get("read_only") and action in {"drop", "extend", "correct", "supersede"}:
        return decision
    if action == "extend" and relation == "adds_detail" and not _adds_detail(decision, target, quotes):
        return dict(decision, action="uncertain", reason="scene_detail_unconfirmed")
    if action == "drop" and frozen:
        if relation == "adds_detail":
            if (not _adds_detail(decision, target, quotes) or decision.get("same_memory") is not True
                    or decision["new_quote"] not in (core.get("content") or "")):
                return dict(decision, action="uncertain", reason="scene_detail_unconfirmed")
            # 置信度不足时禁止把矛盾的 drop 判断转成一次写入。
            try:
                confidence = float(decision.get("same_memory_confidence") or 0)
            except (ValueError, TypeError):
                confidence = 0
            if not confidence_min <= confidence <= 1:
                return dict(decision, action="uncertain", reason="scene_detail_unconfirmed")
            importance = core.get("importance") or decision.get("importance")
            decision = dict(decision, action="extend", importance=importance, reason="scene_verified_detail_extension")
        elif relation == "covered":
            basis = decision.get("old_quote")
            if not (target and decision.get("same_memory") is True
                    and _covers_candidate(basis, target.get("content") or "", core.get("content") or "")):
                return dict(decision, action="uncertain", reason="scene_duplicate_unconfirmed")
        elif not (relation == "not_durable" and decision.get("long_term_value") is False):
            return dict(decision, action="uncertain", reason="scene_drop_unconfirmed")
    if (decision["action"] in {"new", "extend", "correct", "supersede"}
            and core.get("memory_type") in {"preference", "agreement"}
            and core.get("claim_scope") in {"user_fact", "shared"}):
        basis = decision.get("user_rule_quote")
        proposed = "\n".join(str(item or "") for item in
                             (core.get("title"), core.get("content"), decision.get("content")))
        if not (isinstance(basis, str) and basis.strip()
                and any(q["role"] == "user" and basis == q["quote"] for q in quotes)
                and not _widens_time_scope(basis, proposed)):
            return dict(decision, action="uncertain", reason="scene_user_rule_unconfirmed")
    return decision
