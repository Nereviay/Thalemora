"""从授权的记忆与档案接口选取本轮材料，保持现有缓存及调用时机。"""

import time
from concurrent.futures import ThreadPoolExecutor


def assemble_memory_context(hooks, query="", is_first=False, trace_id=None,
                         trigger_raw_event_id=None, recall_caller="chat_recall",
                         side_effects=True, legacy_semantic_recall=True):
    """拼装工具的记忆注入：新窗口给 handoff，普通消息给时间回忆+关键词向量。
    注入冷却（v0.2.99）：同话题/刚注入过的记忆短时间不重复注入，省 token 不卡。
    时间词查询不参与同话题冷却（不同时段要重新注入），靠内容指纹防重复。"""
    MEMORY_EXPLICIT_FACT_MIN_SCORE = hooks['MEMORY_EXPLICIT_FACT_MIN_SCORE']
    MEMORY_INJECT_TOP_N = hooks['MEMORY_INJECT_TOP_N']
    MEMORY_MIN_SCORE = hooks['MEMORY_MIN_SCORE']
    MEMORY_P4_ENABLED = hooks['MEMORY_P4_ENABLED']
    _MEMORY_DEBUG = hooks['_MEMORY_DEBUG']
    _build_boot_context = hooks['_build_boot_context']
    _card_detail = hooks['_card_detail']
    _contemplation_recall_items = hooks['_contemplation_recall_items']
    _explicit_history_recall = hooks['_explicit_history_recall']
    _explicit_history_reference = hooks['_explicit_history_reference']
    _format_memory_blocks = hooks['_format_memory_blocks']
    _format_self_reflection_blocks = hooks['_format_self_reflection_blocks']
    _format_structured_memory_blocks = hooks['_format_structured_memory_blocks']
    _history_recall_terms = hooks['_history_recall_terms']
    _inject_cooldown_reset = hooks['_inject_cooldown_reset']
    _mark_contemplation_recalled = hooks['_mark_contemplation_recalled']
    _mark_injected = hooks['_mark_injected']
    _mark_injected_query = hooks['_mark_injected_query']
    _memory_cache_get = hooks['_memory_cache_get']
    _memory_cache_set = hooks['_memory_cache_set']
    _memory_explicit_fact_candidate = hooks['_memory_explicit_fact_candidate']
    _memory_items_cover_topic = hooks['_memory_items_cover_topic']
    _memory_p4_cache_get = hooks['_memory_p4_cache_get']
    _memory_p4_cache_set = hooks['_memory_p4_cache_set']
    _memory_query_has_signal = hooks['_memory_query_has_signal']
    _memory_referential_identity_candidate = hooks['_memory_referential_identity_candidate']
    _niannian_recall_items = hooks['_niannian_recall_items']
    _notify_memory_mood = hooks['_notify_memory_mood']
    _parse_id_items = hooks['_parse_id_items']
    _positive_int = hooks['_positive_int']
    _recently_injected = hooks['_recently_injected']
    _refresh_cached_p4_items = hooks['_refresh_cached_p4_items']
    _should_contemplate_now = hooks['_should_contemplate_now']
    _structured_memory_detail = hooks['_structured_memory_detail']
    _text_hash = hooks['_text_hash']
    _time_query_recognized = hooks['_time_query_recognized']
    _time_recall = hooks['_time_recall']
    _time_recall_requested = hooks['_time_recall_requested']
    _topic_cooled = hooks['_topic_cooled']
    _topic_history_recall = hooks['_topic_history_recall']
    _topic_history_terms = hooks['_topic_history_terms']
    memory_context_store = hooks['memory_context_store']
    base_parts = []
    contemplation_parts = []
    mode = "recall"
    handoff_text = ""
    time_text = ""
    history_text = ""
    history_kind = ""
    history_injected = False
    history_selected_raw_event_ids = []
    explicit_history_terms = []
    topic_history_terms = []
    breath_text = ""
    cooldown = None
    memory_gate = "recall_run"
    recall_attempted = False
    cache_hit = False
    raw_recall_count = None
    filtered_recall_count = None
    items = []
    ephemeral_card_refs = []
    boot_context = None
    time_route = False

    if is_first:
        if MEMORY_P4_ENABLED and _positive_int(trigger_raw_event_id) is not None:
            boot_context = (
                _build_boot_context(trigger_raw_event_id, trace_id=trace_id)
                if side_effects
                else memory_context_store.load_boot_context(trigger_raw_event_id)
            )
            if boot_context is not None:
                mode = "boot_context"
        # handoff 已从 Memory 动态尾部拆成独立的窗口级固定前缀块。
        # 新窗口 = 全新上下文，冷却清零（同话题也能重新注入）
        if side_effects:
            _inject_cooldown_reset()

    if query:
        now = time.time()
        explicit_history_ref = _explicit_history_reference(query)
        time_route = (
            _time_query_recognized(query)
            if legacy_semantic_recall
            else _time_recall_requested(query)
        )
        if explicit_history_ref:
            explicit_history_terms = _history_recall_terms(query)
        elif legacy_semantic_recall:
            topic_history_terms = _topic_history_terms(
                query, before_raw_event_id=trigger_raw_event_id
            )
        include_sunk_recall = bool(explicit_history_terms or topic_history_terms)

        # Normal chat has moved to Memory VNext for ordinary semantic recall.
        # Keep this legacy reader only for explicit timeline/history lookups and
        # for non-normal legacy callers.
        if (
            not legacy_semantic_recall
            and not time_route
            and not explicit_history_ref
        ):
            if side_effects:
                _MEMORY_DEBUG.update({
                    "time": False,
                    "breath": False,
                    "handoff": bool(handoff_text),
                    "history": False,
                    "history_kind": "",
                    "cache": False,
                    "cooldown": "vnext_only_normal",
                })
            context = "\n\n".join(base_parts + contemplation_parts)
            return {
                "context": context,
                "memory_context": "\n\n".join(base_parts),
                "contemplation_context": "\n\n".join(contemplation_parts),
                "mode": mode,
                "ephemeral_card_refs": ephemeral_card_refs,
                "boot_context": boot_context,
                "memory_gate": "vnext_only_normal",
                "debug": dict(_MEMORY_DEBUG),
            }

        if not (
            time_route
            or _memory_query_has_signal(query)
            or explicit_history_terms
            or topic_history_terms
        ):
            if side_effects:
                _MEMORY_DEBUG.update({
                    "time": False,
                    "breath": False,
                    "handoff": bool(handoff_text),
                    "history": False,
                    "history_kind": "",
                    "cache": False,
                    "cooldown": "low_signal",
                })
            context = "\n\n".join(base_parts + contemplation_parts)
            return {
                "context": context,
                "memory_context": "\n\n".join(base_parts),
                "contemplation_context": "\n\n".join(contemplation_parts),
                "mode": mode,
                "ephemeral_card_refs": ephemeral_card_refs,
                "boot_context": boot_context,
                "memory_gate": "low_signal",
                "debug": dict(_MEMORY_DEBUG),
            }
        # 同话题复用：同 query 5 分钟内不重复检索（注入稳定 → 缓存命中率上去）
        cache_key = query.strip()[:50]
        cached = None if time_route else (
            _memory_p4_cache_get(cache_key)
            if MEMORY_P4_ENABLED
            else _memory_cache_get(cache_key)
        )
        if side_effects:
            _MEMORY_DEBUG["cache"] = bool(cached)
        contemplation_items = []
        if cached:
            cache_hit = True
            if MEMORY_P4_ENABLED:
                time_text, cached_items = cached
                recall_attempted = True
                items, refreshed_items = _refresh_cached_p4_items(
                    query,
                    cached_items,
                    top_n=MEMORY_INJECT_TOP_N,
                    trace_id=trace_id,
                    trigger_raw_event_id=trigger_raw_event_id,
                    caller=recall_caller,
                    include_sunk=include_sunk_recall,
                )
                raw_recall_count = len(items)
                breath_text = "\n".join(
                    f"[id={item.get('id')}] {item.get('content') or ''}"
                    for item in items
                )
                if side_effects:
                    _memory_p4_cache_set(cache_key, time_text, refreshed_items)
            else:
                time_text, breath_text = cached
        else:
            time_text = _time_recall(query, trace_id=trace_id)
            # 并发召回：主召回（/recall）与沉思室召回（/contemplation/recall）同时发，
            # 沉思室降频：60 秒内已检索过，或 query 不含沉思关键词 → 跳过（主召回保留）。
            do_contemplation = not time_route and _should_contemplate_now(query)
            if time_route:
                items = []
                contemplation_items = []
                raw_recall_count = 0
            else:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fut_items = pool.submit(
                        _niannian_recall_items,
                        query,
                        MEMORY_INJECT_TOP_N,
                        trace_id=trace_id,
                        trigger_raw_event_id=trigger_raw_event_id,
                        caller=recall_caller,
                        include_meta=MEMORY_P4_ENABLED,
                        include_sunk=include_sunk_recall,
                    )
                    fut_cont = (
                        pool.submit(
                            _contemplation_recall_items,
                            query,
                            trace_id=trace_id,
                            caller="chat_contemplation",
                        )
                        if do_contemplation else None
                    )
                    try:
                        recall_attempted = True
                        items = fut_items.result()
                    except Exception:
                        items = []
                    raw_recall_count = len(items)
                    try:
                        contemplation_items = fut_cont.result() if fut_cont else []
                    except Exception:
                        contemplation_items = []
                if do_contemplation:
                    if side_effects:
                        _mark_contemplation_recalled()
            # 默认仍用严格 0.8；只有明确个人事实查询，且 top candidate 是 fresh/高重要度/“你”类卡时，
            # 才允许 0.75~0.8 的窄幅放行，避免 0.798 这类边界误杀而不扩大普通聊天噪声。
            filtered_items = []
            for item_index, item in enumerate(items):
                if MEMORY_P4_ENABLED:
                    if not isinstance(item, dict):
                        continue
                    score = float(item.get("score") or 0.0)
                    legacy_item = (
                        item.get("id"), item.get("content") or "", score
                    )
                    detail = _structured_memory_detail(item)
                else:
                    if not (isinstance(item, (list, tuple)) and len(item) > 2):
                        continue
                    score = float(item[2])
                    legacy_item = item
                    detail = None
                if score >= MEMORY_MIN_SCORE:
                    filtered_items.append(item)
                    continue
                if score >= MEMORY_EXPLICIT_FACT_MIN_SCORE:
                    if detail is None:
                        detail = _card_detail(item[0], trace_id=trace_id) or {}
                    if _memory_explicit_fact_candidate(query, legacy_item, detail):
                        filtered_items.append(item)
                        continue
                    # “就是那个认识很久的朋友”这类指代句常比直接事实问句短，
                    # rerank 可能卡在 0.75~0.8。只给 top-1 且有可落地描述词的关系人物放行。
                    if (
                        item_index == 0
                        and isinstance(item, dict)
                        and _memory_referential_identity_candidate(query, item, detail)
                    ):
                        filtered_items.append(item)
            items = filtered_items
            filtered_recall_count = len(items)
            if items:
                if MEMORY_P4_ENABLED:
                    breath_text = "\n".join(
                        f"[id={item.get('id')}] {item.get('content') or ''}"
                        for item in items
                    )
                else:
                    breath_text = "\n".join(
                        f"[id={cid}] {content}" for cid, content, _ in items
                    )
            else:
                breath_text = ""  # 没有候选就不注入
            contemplation_items = [x for x in contemplation_items
                                   if len(x) > 2 and x[2] >= MEMORY_MIN_SCORE]
            if MEMORY_P4_ENABLED:
                if side_effects:
                    _memory_p4_cache_set(cache_key, time_text, items)
            else:
                if side_effects:
                    _memory_cache_set(cache_key, time_text, breath_text)

        # 原始聊天只做补洞：时间路由优先；已有 Memory 已覆盖具体话题时不重复塞。
        # 同话题 15 分钟内已经注入过任何记忆块，也不再重新翻旧原话。
        coverage_items = items or _parse_id_items(breath_text)
        topic_already_visible = _topic_cooled(now, query)
        if (
            explicit_history_terms
            and not time_route
            and not time_text
            and not topic_already_visible
            and not _memory_items_cover_topic(coverage_items, explicit_history_terms)
        ):
            history_text = _explicit_history_recall(
                query, before_raw_event_id=trigger_raw_event_id,
                source_raw_event_ids=history_selected_raw_event_ids,
            )
            if history_text:
                history_kind = "explicit"
        elif (
            topic_history_terms
            and not time_route
            and not time_text
            and not topic_already_visible
            and not _memory_items_cover_topic(coverage_items, topic_history_terms)
        ):
            history_text = _topic_history_recall(
                query,
                terms=topic_history_terms,
                before_raw_event_id=trigger_raw_event_id,
                source_raw_event_ids=history_selected_raw_event_ids,
            )
            if history_text:
                history_kind = "topic"

        injected = False
        # ① 同话题冷却：非时间词查询，最近注入过相似话题 → 本轮不注入（模型上下文里还有）
        if not time_text and _topic_cooled(now, query):
            cooldown = "topic"
        else:
            # ② 显式时间回忆不做跨轮冷却。time_text 是本轮临时证据，不会进入
            # 正常聊天历史；若上一轮注入后本轮把它吞掉，模型就会真的看不到
            # “昨天下午/昨晚”等目标时段的原始对话。
            # ③ 卡级冷却：刚注入过的记忆卡不重复注入
            if MEMORY_P4_ENABLED:
                structured_items = [
                    item for item in (items or []) if isinstance(item, dict)
                ]
                memory_items = [
                    (_positive_int(item.get("id")), item.get("content") or "")
                    for item in structured_items
                    if _positive_int(item.get("id")) is not None
                ]
                fresh_structured_items = [
                    item for item in structured_items
                    if not _recently_injected("card", item.get("id"))
                ]
                fresh_items = [
                    (item.get("id"), item.get("content") or "")
                    for item in fresh_structured_items
                ]
            else:
                structured_items = []
                fresh_structured_items = []
                memory_items = _parse_id_items(breath_text)
                fresh_items = [it for it in memory_items
                               if not _recently_injected("card", it[0])]
            if memory_items and not fresh_items:
                cooldown = cooldown or "card"
            # ④ 兜底（无 id 可标号）：整块指纹冷却
            if not memory_items and breath_text and _recently_injected("hash", _text_hash(breath_text)):
                breath_text = ""
                cooldown = cooldown or "fallback"

            if time_text:
                base_parts.append(time_text)
                if side_effects:
                    _mark_injected("time", _text_hash(time_text))
                injected = True
            if history_text:
                history_hash = _text_hash(history_text)
                if not _recently_injected("history", history_hash):
                    base_parts.append(history_text)
                    if side_effects:
                        _mark_injected("history", history_hash)
                    history_injected = True
                    injected = True
            if fresh_items and MEMORY_P4_ENABLED:
                signal_cards = []
                if fresh_structured_items:
                    base_parts.append(
                        _format_structured_memory_blocks(fresh_structured_items)
                    )
                    for item in fresh_structured_items:
                        card_id = _positive_int(item.get("id"))
                        if card_id is not None:
                            ephemeral_card_refs.append({
                                "card_id": card_id,
                                "revision_id": _positive_int(
                                    item.get("current_revision_id")
                                ) or 0,
                            })
                        if side_effects:
                            _mark_injected("card", item.get("id"))
                        signal_cards.append({
                            "id": item.get("id"),
                            "title": item.get("title") or "",
                            "valence": item.get("valence"),
                            "arousal": item.get("arousal"),
                        })
                if side_effects:
                    _notify_memory_mood(signal_cards)
                injected = injected or bool(fresh_structured_items)
            elif fresh_items:
                signal_cards = []
                base_parts.append(_format_memory_blocks(
                    fresh_items,
                    signal_cards=signal_cards,
                    trace_id=trace_id,
                ))
                for cid, _content in fresh_items:
                    card_id = _positive_int(cid)
                    if card_id is not None:
                        ephemeral_card_refs.append({
                            "card_id": card_id,
                            "revision_id": 0,
                        })
                    if side_effects:
                        _mark_injected("card", cid)
                if side_effects:
                    _notify_memory_mood(signal_cards)
                injected = True
            fresh_contemplations = [it for it in contemplation_items
                                    if not _recently_injected("contemplation", it[0])]
            if fresh_contemplations:
                contemplation_parts.append(_format_self_reflection_blocks(fresh_contemplations))
                for cid, _ in fresh_contemplations:
                    if side_effects:
                        _mark_injected("contemplation", cid)
                injected = True
            if injected and side_effects:
                _mark_injected_query(query)

    cooldown_gate = {
        "topic": "topic_cooldown_after_recall",
        "card": "card_cooldown_after_recall",
        "time": "time_cooldown_after_recall",
        "fallback": "fallback_cooldown",
    }.get(cooldown)
    if cooldown_gate:
        memory_gate = cooldown_gate
    elif history_injected:
        memory_gate = (
            "topic_history_route" if history_kind == "topic" else "history_route"
        )
    elif cache_hit:
        memory_gate = "exact_cache_hit"
    elif time_route:
        memory_gate = "time_route"
    elif recall_attempted:
        if raw_recall_count == 0:
            memory_gate = "no_candidate"
        elif filtered_recall_count == 0:
            memory_gate = "score_below_floor"
        else:
            memory_gate = "recall_run"

    if side_effects:
        _MEMORY_DEBUG.update({
            "time": bool(time_text),
            "breath": bool(breath_text),
            "handoff": bool(handoff_text),
            "history": bool(history_injected),
            "history_kind": history_kind if history_injected else "",
            "cooldown": cooldown,
        })
    context = "\n\n".join(base_parts + contemplation_parts)
    return {
        "context": context,
        "memory_context": "\n\n".join(base_parts),
        "contemplation_context": "\n\n".join(contemplation_parts),
        "mode": "both" if (is_first and query and (time_text or breath_text)) else mode,
        "ephemeral_card_refs": ephemeral_card_refs,
        "history_raw_event_ids": (
            history_selected_raw_event_ids if history_injected else []
        ),
        "boot_context": boot_context,
        "memory_gate": memory_gate,
        "debug": dict(_MEMORY_DEBUG),
    }
