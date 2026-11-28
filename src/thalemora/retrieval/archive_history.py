"""现有聊天档案适配器的记忆选择规则。调用方提供已授权的事件库与现有缓存。"""

from types import SimpleNamespace
from datetime import timezone
from zoneinfo import ZoneInfo


def archive_history_rules(_owner_runtime, *, user="用户", actor="助手", familiar_user="用户", timezone_name="UTC"):
    MEMORY_INJECT_MAX_CHARS = _owner_runtime["MEMORY_INJECT_MAX_CHARS"]
    display_zone = ZoneInfo(timezone_name)

    def _local_stamp(ts):
        if ts is None:
            return ""
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(display_zone).strftime("[%m-%d %H:%M] ")

    def _text_hash(text):
        """内容指纹：稳定哈希，整块内容（时间回忆）的注入冷却用"""
        hashlib = _owner_runtime['hashlib']
        return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:16]


    def _recently_injected(kind, key):
        """kind: card 数字id / time 时段块 / hash 整块指纹 → 冷却期内注入过吗"""
        _INJECT_CARD_COOLDOWN = _owner_runtime['_INJECT_CARD_COOLDOWN']
        _INJECT_HISTORY = _owner_runtime['_INJECT_HISTORY']
        time = _owner_runtime['time']
        ts = _INJECT_HISTORY.get((kind, key))
        return ts is not None and time.time() - ts < _INJECT_CARD_COOLDOWN


    def _mark_injected(kind, key):
        _INJECT_HISTORY = _owner_runtime['_INJECT_HISTORY']
        time = _owner_runtime['time']
        _INJECT_HISTORY[(kind, key)] = time.time()


    def _norm_query(q):
        """query 归一化：去空白标点、转小写，取前 40 字符（同话题指纹）"""
        re = _owner_runtime['re']
        return re.sub(r"[\s\W_]+", "", (q or "").lower())[:40]


    def _same_topic(a, b):
        """粗判两句同话题：归一化后互相包含，或公共字符占短句字符集 ≥60%"""
        a, b = _norm_query(a), _norm_query(b)
        if not a or not b:
            return False
        if a in b or b in a:
            return True
        sa, sb = set(a), set(b)
        return len(sa & sb) / min(len(sa), len(sb)) >= 0.6


    def _topic_cooled(now, query):
        """最近注入过相似话题（冷却期内）→ 本轮不再注入"""
        _INJECTED_QUERIES = _owner_runtime['_INJECTED_QUERIES']
        _INJECT_TOPIC_COOLDOWN = _owner_runtime['_INJECT_TOPIC_COOLDOWN']
        return any(
            now - ts < _INJECT_TOPIC_COOLDOWN and _same_topic(q, query)
            for ts, q in _INJECTED_QUERIES
        )


    def _mark_injected_query(query):
        """记录刚注入过的 query，顺手清理过期记录（防列表/字典无限涨）"""
        _INJECTED_QUERIES = _owner_runtime['_INJECTED_QUERIES']
        _INJECT_CARD_COOLDOWN = _owner_runtime['_INJECT_CARD_COOLDOWN']
        _INJECT_HISTORY = _owner_runtime['_INJECT_HISTORY']
        _INJECT_TOPIC_COOLDOWN = _owner_runtime['_INJECT_TOPIC_COOLDOWN']
        time = _owner_runtime['time']
        now = time.time()
        _INJECTED_QUERIES.append((now, _norm_query(query)))
        _INJECTED_QUERIES[:] = [(ts, q) for ts, q in _INJECTED_QUERIES
                                if now - ts < _INJECT_TOPIC_COOLDOWN]
        for k in [k for k, ts in _INJECT_HISTORY.items()
                  if now - ts >= _INJECT_CARD_COOLDOWN]:
            del _INJECT_HISTORY[k]


    def _inject_cooldown_reset():
        """新窗口 = 全新上下文：冷却清零，同话题也能重新注入"""
        _INJECTED_QUERIES = _owner_runtime['_INJECTED_QUERIES']
        _INJECT_HISTORY = _owner_runtime['_INJECT_HISTORY']
        _INJECT_HISTORY.clear()
        _INJECTED_QUERIES.clear()


    def _explicit_personal_fact_query(query):
        re = _owner_runtime['re']
        q = re.sub(r"[\s\W_]+", "", str(query or ""), flags=re.U)
        if not q:
            return False
        personal = any(k in q for k in ("我", user, familiar_user))
        fact = any(k in q for k in (
            "记得", "记不记得", "记住", "忘了", "不吃", "爱吃", "挑食", "忌口", "口味",
            "生日", "名字", "喜欢", "讨厌", "害怕", "说过", "提过", "吃什么",
        ))
        return personal and fact


    def _memory_explicit_fact_candidate(query, item, detail):
        MEMORY_EXPLICIT_FACT_MIN_IMPORTANCE = _owner_runtime['MEMORY_EXPLICIT_FACT_MIN_IMPORTANCE']
        MEMORY_EXPLICIT_FACT_MIN_SCORE = _owner_runtime['MEMORY_EXPLICIT_FACT_MIN_SCORE']
        if not _explicit_personal_fact_query(query):
            return False
        if not (isinstance(item, (list, tuple)) and len(item) > 2):
            return False
        if float(item[2]) < MEMORY_EXPLICIT_FACT_MIN_SCORE:
            return False
        if not isinstance(detail, dict) or str(detail.get("status") or "") != "fresh":
            return False
        tags = {x.strip() for x in str(detail.get("tags") or "").split(",") if x.strip()}
        return "你" in tags and float(detail.get("importance") or 0) >= MEMORY_EXPLICIT_FACT_MIN_IMPORTANCE




    MEMORY_REFERENTIAL_CUES = ("那个", "这个", "那位", "这位")
    MEMORY_REFERENTIAL_RELATIONS = (
        "朋友", "网友", "固排", "同学", "同事", "室友", "老师", "亲戚", "家人", "前任",
    )
    MEMORY_REFERENTIAL_IGNORE_TERMS = frozenset({
        "就是", "那个", "这个", "那位", "这位", "朋友", "网友", "固排", "同学", "同事",
        "室友", "老师", "亲戚", "家人", "前任",
    })

    def _memory_referential_identity_candidate(query, item, detail=None):
        """窄幅放行“那个…朋友/同学”等人物指代的 top-1 边界候选。

        不降低全局 0.8：只接受 >=0.75、fresh、关系词一致，并且查询里至少还有
        一个能在候选正文/标题/关键词中落地的描述词。单说“那个朋友”不会放行。
        """
        MEMORY_EXPLICIT_FACT_MIN_SCORE = _owner_runtime['MEMORY_EXPLICIT_FACT_MIN_SCORE']
        TOPIC_HISTORY_RELATIVE_TIME_RE = _owner_runtime['TOPIC_HISTORY_RELATIVE_TIME_RE']
        _history_recall_terms = _owner_runtime['_history_recall_terms']
        re = _owner_runtime['re']
        if not isinstance(item, dict):
            return False
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            return False
        if score < MEMORY_EXPLICIT_FACT_MIN_SCORE:
            return False
        if str(item.get("status") or (detail or {}).get("status") or "") != "fresh":
            return False
        compact = re.sub(r"[\s\W_]+", "", str(query or ""), flags=re.U)
        if not compact or not any(cue in compact for cue in MEMORY_REFERENTIAL_CUES):
            return False
        blob = " ".join(str(item.get(k) or "") for k in ("title", "content", "keywords"))
        relation = next(
            (rel for rel in MEMORY_REFERENTIAL_RELATIONS if rel in compact and rel in blob),
            None,
        )
        if not relation:
            return False
        descriptors = []
        for term in _history_recall_terms(query):
            if term in MEMORY_REFERENTIAL_IGNORE_TERMS:
                continue
            if TOPIC_HISTORY_RELATIVE_TIME_RE.fullmatch(term):
                continue
            if term not in descriptors:
                descriptors.append(term)
        return any(len(term) >= 2 and term.lower() in blob.lower() for term in descriptors)

    def _memory_query_has_signal(query):
        """低信号短闲聊不查记忆，避免称呼/亲亲类消息每轮带出大块无关卡。"""
        MEMORY_QUERY_TRIGGER_KEYWORDS = _owner_runtime['MEMORY_QUERY_TRIGGER_KEYWORDS']
        re = _owner_runtime['re']
        q = re.sub(r"[\s\W_]+", "", str(query or ""), flags=re.U)
        if not q:
            return False
        if any(k in q for k in MEMORY_QUERY_TRIGGER_KEYWORDS):
            return True
        if len(q) <= 4:
            return False
        unique_chars = set(q)
        if len(q) <= 8 and len(unique_chars) <= 2:
            return False
        return True


    def _explicit_history_reference(query):
        HISTORY_REFERENCE_PHRASES = _owner_runtime['HISTORY_REFERENCE_PHRASES']
        re = _owner_runtime['re']
        q = re.sub(r"\s+", "", str(query or ""))
        return bool(q) and any(phrase in q for phrase in HISTORY_REFERENCE_PHRASES)


    def _history_recall_terms(query):
        """从明确回看句里拿少量内容词；不把“说过/记得”本身当检索词。"""
        HISTORY_RECALL_STOPWORDS = _owner_runtime['HISTORY_RECALL_STOPWORDS']
        HISTORY_REFERENCE_PHRASES = _owner_runtime['HISTORY_REFERENCE_PHRASES']
        MEMORY_QUERY_TRIGGER_KEYWORDS = _owner_runtime['MEMORY_QUERY_TRIGGER_KEYWORDS']
        re = _owner_runtime['re']
        text = re.sub(r"https?://\S+", " ", str(query or ""))
        try:
            import jieba
            raw_tokens = jieba.lcut(text, cut_all=False)
        except Exception:
            raw_tokens = re.findall(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,12}", text)
        out = []
        for token in raw_tokens:
            token = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", str(token or "")).strip()
            if len(token) < 2 or token in HISTORY_RECALL_STOPWORDS:
                continue
            if token in HISTORY_REFERENCE_PHRASES or any(token == x for x in MEMORY_QUERY_TRIGGER_KEYWORDS):
                continue
            if token not in out:
                out.append(token)
            if len(out) >= 6:
                break
        return out


    def _raw_history_temporal_rows(rows, hit_created_at):
        """Keep only rows genuinely near the hit in wall-clock time.

        Some legacy imports preserve row ids but not chronological adjacency, so id
        proximity alone is not reliable evidence that two turns belonged together.
        """
        TOPIC_HISTORY_CONTEXT_MAX_GAP_SECONDS = _owner_runtime['TOPIC_HISTORY_CONTEXT_MAX_GAP_SECONDS']
        _parse_ts = _owner_runtime['_parse_ts']
        hit_ts = _parse_ts(hit_created_at)
        if hit_ts is None:
            return list(rows or [])
        kept = []
        for row in rows or []:
            created_at = row[3] if len(row) > 3 else None
            ts = _parse_ts(created_at)
            if ts is None:
                continue
            if abs((ts - hit_ts).total_seconds()) <= TOPIC_HISTORY_CONTEXT_MAX_GAP_SECONDS:
                kept.append(row)
        kept.sort(key=lambda row: (_parse_ts(row[3]).timestamp(), int(row[0])))
        return kept


    def _explicit_history_recall(query, before_raw_event_id=None, source_raw_event_ids=None):
        """明确找旧话时，从更早 raw_events 取最多两小段原话；失败直接不注入。"""
        HISTORY_RECALL_CONTEXT_RADIUS = _owner_runtime['HISTORY_RECALL_CONTEXT_RADIUS']
        HISTORY_RECALL_MAX_CHARS = _owner_runtime['HISTORY_RECALL_MAX_CHARS']
        HISTORY_RECALL_MAX_WINDOWS = _owner_runtime['HISTORY_RECALL_MAX_WINDOWS']
        HISTORY_RECALL_RECENT_EXCLUDE_IDS = _owner_runtime['HISTORY_RECALL_RECENT_EXCLUDE_IDS']
        RAW_EVENTS_DB = _owner_runtime['RAW_EVENTS_DB']
        _explicit_history_reference = _owner_runtime['_explicit_history_reference']
        _history_recall_terms = _owner_runtime['_history_recall_terms']
        _parse_ts = _owner_runtime['_parse_ts']
        _positive_int = _owner_runtime['_positive_int']
        re = _owner_runtime['re']
        sqlite3 = _owner_runtime['sqlite3']
        timedelta = _owner_runtime['timedelta']
        if not _explicit_history_reference(query):
            return ""
        terms = _history_recall_terms(query)
        if not terms:
            return ""
        try:
            conn = sqlite3.connect(RAW_EVENTS_DB, timeout=2)
            try:
                if _positive_int(before_raw_event_id) is not None:
                    upper_id = max(1, int(before_raw_event_id) - HISTORY_RECALL_RECENT_EXCLUDE_IDS)
                else:
                    row = conn.execute("SELECT COALESCE(MAX(id),0) FROM raw_events").fetchone()
                    upper_id = max(1, int((row or [0])[0] or 0) - HISTORY_RECALL_RECENT_EXCLUDE_IDS)
                likes = []
                params = [upper_id]
                for term in terms:
                    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    likes.append("text LIKE ? ESCAPE '\\'")
                    params.append(f"%{escaped}%")
                sql_tail = (
                    " AND id < ? AND (" + " OR ".join(likes) + ") "
                    "ORDER BY id DESC LIMIT 80"
                )
                rows = conn.execute(
                    "SELECT id, role, text, created_at, source FROM raw_events "
                    "WHERE deleted=0 AND role='user'" + sql_tail,
                    params,
                ).fetchall()
                if not rows:
                    rows = conn.execute(
                        "SELECT id, role, text, created_at, source FROM raw_events "
                        "WHERE deleted=0 AND role='assistant'" + sql_tail,
                        params,
                    ).fetchall()

                ranked = []
                for row in rows:
                    rid = int(row[0])
                    window = conn.execute(
                        "SELECT id, role, text, created_at, source FROM raw_events "
                        "WHERE deleted=0 AND role IN ('user','assistant') AND id BETWEEN ? AND ? "
                        "ORDER BY id",
                        (rid - HISTORY_RECALL_CONTEXT_RADIUS, rid + HISTORY_RECALL_CONTEXT_RADIUS),
                    ).fetchall()
                    window = _raw_history_temporal_rows(window, row[3])
                    user_blob = " ".join(
                        str(item[2] or "") for item in window if item[1] == "user"
                    ).lower()
                    full_blob = " ".join(str(item[2] or "") for item in window).lower()
                    primary_blob = user_blob or full_blob
                    coverage = sum(1 for term in terms if term.lower() in primary_blob)
                    if coverage <= 0:
                        continue
                    ranked.append((coverage, rid, window))
                ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)

                selected_windows = []
                hit_ids = []
                max_windows = (
                    1 if ranked and ranked[0][0] >= len(terms)
                    else HISTORY_RECALL_MAX_WINDOWS
                )
                for _, rid, window in ranked:
                    if any(abs(rid - old) <= HISTORY_RECALL_CONTEXT_RADIUS * 2 for old in hit_ids):
                        continue
                    hit_ids.append(rid)
                    selected_windows.append(window)
                    if len(hit_ids) >= max_windows:
                        break
                if not selected_windows:
                    return ""
                context_rows = []
                seen_ids = set()
                for window in selected_windows:
                    for row in window:
                        if int(row[0]) in seen_ids:
                            continue
                        seen_ids.add(int(row[0]))
                        context_rows.append(row)
            finally:
                conn.close()
        except Exception as exc:
            print(f"[memory] explicit history recall failed: {exc}", flush=True)
            return ""

        lines = [
            f"[历史原话回看 | {user}明确在找以前说过的事]",
            f"以下是更早的真实对话原文。{user}自己的原话优先；{actor}旧回复只用于还原当时语境，不能把旧回复里未经{user}确认的内容当成新事实。",
        ]
        used = len("\n".join(lines))
        seen_text = set()
        for _rid, role, text, created_at, _source in context_rows:
            body = re.sub(r"\s+", " ", str(text or "")).strip()
            if not body or (role, body) in seen_text:
                continue
            seen_text.add((role, body))
            if len(body) > 240:
                body = body[:240].rstrip() + "…"
            ts = _parse_ts(created_at)
            stamp = _local_stamp(ts)
            label = user if role == "user" else actor
            line = f"- {label} {stamp}{body}"
            if used + len(line) + 1 > HISTORY_RECALL_MAX_CHARS:
                break
            lines.append(line)
            if source_raw_event_ids is not None:
                source_raw_event_ids.append(int(_rid))
            used += len(line) + 1
        return "\n".join(lines) if len(lines) > 2 else ""


    def _topic_history_recent_terms(current_terms, before_raw_event_id=None):
        """只从最近一个真正命中当前词的小轮次补上下文，避免把动作/闲聊串进检索词。"""
        RAW_EVENTS_DB = _owner_runtime['RAW_EVENTS_DB']
        TOPIC_HISTORY_GENERIC_TERMS = _owner_runtime['TOPIC_HISTORY_GENERIC_TERMS']
        TOPIC_HISTORY_RECENT_CONTEXT_MESSAGES = _owner_runtime['TOPIC_HISTORY_RECENT_CONTEXT_MESSAGES']
        _history_recall_terms = _owner_runtime['_history_recall_terms']
        _positive_int = _owner_runtime['_positive_int']
        re = _owner_runtime['re']
        sqlite3 = _owner_runtime['sqlite3']
        trigger_id = _positive_int(before_raw_event_id)
        if trigger_id is None or not current_terms:
            return []
        try:
            conn = sqlite3.connect(RAW_EVENTS_DB, timeout=1.5)
            try:
                rows = conn.execute(
                    "SELECT id, role, text FROM raw_events WHERE deleted=0 "
                    "AND role IN ('user','assistant') AND id < ? "
                    "ORDER BY id DESC LIMIT ?",
                    (trigger_id, TOPIC_HISTORY_RECENT_CONTEXT_MESSAGES),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return []
        lowered_terms = [term.lower() for term in current_terms]
        for index, row in enumerate(rows):
            _rid, role, text = row
            body = str(text or "")
            if not any(term in body.lower() for term in lowered_terms):
                continue
            cluster = [row]
            # rows 是倒序：assistant 的前一条 user 在 index+1；user 的后一条 assistant 在 index-1。
            if role == "assistant" and index + 1 < len(rows) and rows[index + 1][1] == "user":
                cluster.append(rows[index + 1])
            elif role == "user" and index > 0 and rows[index - 1][1] == "assistant":
                cluster.append(rows[index - 1])
            candidates = []
            for _cid, _crole, cluster_text in cluster:
                for term in _history_recall_terms(cluster_text):
                    if term in current_terms or term in candidates:
                        continue
                    candidates.append(term)
            # 具体词优先，常见泛词最后；只借这一小轮次，不继续扫更老的匹配。
            strong = [
                term for term in candidates
                if term not in TOPIC_HISTORY_GENERIC_TERMS
                and (len(term) >= 3 or bool(re.search(r"[A-Za-z0-9]", term)))
            ]
            if strong:
                strong.sort(key=lambda term: -len(term))
                return strong[:2]
            weak = [term for term in candidates if term not in TOPIC_HISTORY_GENERIC_TERMS]
            return weak[:1]
        return []


    def _topic_history_query_text(query):
        """普通话题补洞只看 authored speech；括号动作不参与历史检索词。"""
        TOPIC_HISTORY_STAGE_DIRECTION_RE = _owner_runtime['TOPIC_HISTORY_STAGE_DIRECTION_RE']
        text = str(query or "")
        return TOPIC_HISTORY_STAGE_DIRECTION_RE.sub(" ", text)


    def _topic_history_strong_term(term):
        """区分具体话题词与普通场景/时间表达，避免动作闲聊触发 raw 补洞。"""
        TOPIC_HISTORY_FILLER_TERMS = _owner_runtime['TOPIC_HISTORY_FILLER_TERMS']
        TOPIC_HISTORY_GENERIC_TERMS = _owner_runtime['TOPIC_HISTORY_GENERIC_TERMS']
        TOPIC_HISTORY_RELATIVE_TIME_RE = _owner_runtime['TOPIC_HISTORY_RELATIVE_TIME_RE']
        re = _owner_runtime['re']
        value = str(term or "").strip()
        if (
            not value
            or value in TOPIC_HISTORY_GENERIC_TERMS
            or value in TOPIC_HISTORY_FILLER_TERMS
        ):
            return False
        if TOPIC_HISTORY_RELATIVE_TIME_RE.fullmatch(value):
            return False
        # 裸数字（11、45、2026）本身不是话题身份；否则时间/尺寸/价格很容易串台。
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            return False
        # 二字中文在日常动作/场景里过于常见；strong anchor 继续要求三字中文，
        # ASCII 实体可按 token 边界作为 anchor。泛词已在 FILLER 中排除。
        return len(value) >= 3 or bool(re.search(r"[A-Za-z]", value))


    def _topic_history_term_matches_text(term, text):
        """历史窗口匹配：ASCII 必须按 token 边界命中，中文继续允许子串命中。"""
        re = _owner_runtime['re']
        value = str(term or "").strip().lower()
        body = str(text or "").lower()
        if not value or not body:
            return False
        if re.search(r"[A-Za-z0-9]", value):
            return re.search(
                rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])",
                body,
                flags=re.IGNORECASE,
            ) is not None
        return value in body


    def _topic_history_terms(query, before_raw_event_id=None):
        """保守判定普通话题是否值得翻旧原话；返回空即不触发。"""
        TOPIC_HISTORY_CONTEXTUAL_GENERIC_TERMS = _owner_runtime['TOPIC_HISTORY_CONTEXTUAL_GENERIC_TERMS']
        TOPIC_HISTORY_FILLER_TERMS = _owner_runtime['TOPIC_HISTORY_FILLER_TERMS']
        TOPIC_HISTORY_PAST_CUES = _owner_runtime['TOPIC_HISTORY_PAST_CUES']
        TOPIC_HISTORY_RELATIVE_TIME_RE = _owner_runtime['TOPIC_HISTORY_RELATIVE_TIME_RE']
        _explicit_history_reference = _owner_runtime['_explicit_history_reference']
        _history_recall_terms = _owner_runtime['_history_recall_terms']
        re = _owner_runtime['re']
        if _explicit_history_reference(query):
            return []
        topic_text = _topic_history_query_text(query)
        current = [
            term for term in _history_recall_terms(topic_text)
            if term not in TOPIC_HISTORY_FILLER_TERMS
            and not TOPIC_HISTORY_RELATIVE_TIME_RE.fullmatch(term)
        ]
        if not current:
            return []
        compact = re.sub(r"\s+", "", topic_text)
        has_past_cue = any(cue in compact for cue in TOPIC_HISTORY_PAST_CUES)
        specific_current = [term for term in current if _topic_history_strong_term(term)]
        terms = list(current)
        needs_context = (
            not specific_current
            and len(current) == 1
            and current[0] in TOPIC_HISTORY_CONTEXTUAL_GENERIC_TERMS
        )
        if needs_context:
            for term in _topic_history_recent_terms(current, before_raw_event_id):
                if term not in terms:
                    terms.append(term)
        if has_past_cue:
            return terms[:5]
        # 没有明确过去语气时，单个词/短句一律不自动翻 raw：
        # “爽不爽”“客厅”“开灯”这类当前场景表达不能靠长度冒充历史话题。
        # 至少两个检索词，且其中一个是具体实体/话题；“演唱会”可从紧邻上下文借到“单依纯”。
        if len(terms) >= 2 and any(_topic_history_strong_term(term) for term in terms):
            return terms[:5]
        return []


    def _topic_history_window_from_rows(rows, hit_id):
        TOPIC_HISTORY_CONTEXT_TURNS = _owner_runtime['TOPIC_HISTORY_CONTEXT_TURNS']
        TOPIC_HISTORY_MAX_MESSAGES = _owner_runtime['TOPIC_HISTORY_MAX_MESSAGES']
        hit_id = int(hit_id)
        rows = list(rows or [])
        hit_created_at = next(
            (row[3] for row in rows if int(row[0]) == hit_id),
            None,
        )
        rows = _raw_history_temporal_rows(rows, hit_created_at)
        turns, current = [], []
        for row in rows:
            if row[1] == "user":
                if current:
                    turns.append(current)
                current = [row]
            elif current:
                current.append(row)
        if current:
            turns.append(current)
        hit_turn = next(
            (
                i for i, turn in enumerate(turns)
                if any(int(row[0]) == hit_id for row in turn)
            ),
            None,
        )
        if hit_turn is None:
            return []
        lo = max(0, hit_turn - TOPIC_HISTORY_CONTEXT_TURNS)
        hi = min(len(turns), hit_turn + TOPIC_HISTORY_CONTEXT_TURNS + 1)
        flattened = [row for turn in turns[lo:hi] for row in turn]
        return flattened[:TOPIC_HISTORY_MAX_MESSAGES]


    def _topic_history_turn_window(conn, hit_id):
        before = conn.execute(
            "SELECT id, role, text, created_at, source FROM raw_events "
            "WHERE deleted=0 AND role IN ('user','assistant') AND id <= ? "
            "ORDER BY id DESC LIMIT 24",
            (hit_id,),
        ).fetchall()
        after = conn.execute(
            "SELECT id, role, text, created_at, source FROM raw_events "
            "WHERE deleted=0 AND role IN ('user','assistant') AND id > ? "
            "ORDER BY id ASC LIMIT 24",
            (hit_id,),
        ).fetchall()
        return _topic_history_window_from_rows(
            list(reversed(before)) + list(after),
            hit_id,
        )


    def _topic_history_turn_windows(conn, hit_ids):
        ordered_ids = []
        seen = set()
        for value in hit_ids or ():
            try:
                hit_id = int(value)
            except (TypeError, ValueError):
                continue
            if hit_id in seen:
                continue
            seen.add(hit_id)
            ordered_ids.append(hit_id)
        if not ordered_ids:
            return {}

        lo = min(ordered_ids)
        hi = max(ordered_ids)
        # Build one ordered superset instead of ROW_NUMBER-ing the whole dialogue
        # table and joining it once per hit. The first/last 24 rows guarantee the
        # exact same local candidate window for hits at either boundary; every
        # interior dialogue row is included directly.
        rows = conn.execute(
            "SELECT id, role, text, created_at, source FROM ("
            " SELECT id, role, text, created_at, source"
            " FROM raw_events"
            " WHERE deleted=0 AND role IN ('user','assistant') AND id <= ?"
            " ORDER BY id DESC LIMIT 24"
            ")"
            " UNION ALL "
            "SELECT id, role, text, created_at, source"
            " FROM raw_events"
            " WHERE deleted=0 AND role IN ('user','assistant')"
            " AND id > ? AND id <= ?"
            " UNION ALL "
            "SELECT id, role, text, created_at, source FROM ("
            " SELECT id, role, text, created_at, source"
            " FROM raw_events"
            " WHERE deleted=0 AND role IN ('user','assistant') AND id > ?"
            " ORDER BY id ASC LIMIT 24"
            ")",
            (lo, lo, hi, hi),
        ).fetchall()
        rows = sorted(rows, key=lambda row: int(row[0]))
        positions = {int(row[0]): index for index, row in enumerate(rows)}
        windows = {}
        for hit_id in ordered_ids:
            index = positions.get(hit_id)
            if index is None:
                windows[hit_id] = []
                continue
            candidate_rows = rows[
                max(0, index - 23):min(len(rows), index + 25)
            ]
            windows[hit_id] = _topic_history_window_from_rows(
                candidate_rows,
                hit_id,
            )
        return windows


    def _topic_history_recall(query, terms=None, before_raw_event_id=None,
                              source_raw_event_ids=None):
        """普通聊天的保守话题原话兜底：最多一段，搜索命中优先来自用户原话。"""
        HISTORY_RECALL_RECENT_EXCLUDE_IDS = _owner_runtime['HISTORY_RECALL_RECENT_EXCLUDE_IDS']
        RAW_EVENTS_DB = _owner_runtime['RAW_EVENTS_DB']
        TOPIC_HISTORY_MAX_CHARS = _owner_runtime['TOPIC_HISTORY_MAX_CHARS']
        TOPIC_HISTORY_PAST_CUES = _owner_runtime['TOPIC_HISTORY_PAST_CUES']
        _parse_ts = _owner_runtime['_parse_ts']
        _positive_int = _owner_runtime['_positive_int']
        _topic_history_terms = _owner_runtime['_topic_history_terms']
        re = _owner_runtime['re']
        sqlite3 = _owner_runtime['sqlite3']
        timedelta = _owner_runtime['timedelta']
        terms = list(terms or _topic_history_terms(query, before_raw_event_id))
        if not terms:
            return ""
        topic_text = _topic_history_query_text(query)
        compact = re.sub(r"\s+", "", topic_text)
        has_past_cue = any(cue in compact for cue in TOPIC_HISTORY_PAST_CUES)
        # 没有“上次/以前/那次”等明确过去语气时，多词 topic 至少要在同一旧窗口
        # 同时命中两个词；不能再因为 OR 搜索碰到一个“起身”就整段注入。
        required_coverage = 1 if has_past_cue else min(2, len(terms))
        strong_terms = [term for term in terms if _topic_history_strong_term(term)]
        try:
            conn = sqlite3.connect(RAW_EVENTS_DB, timeout=2)
            try:
                trigger_id = _positive_int(before_raw_event_id)
                if trigger_id is not None:
                    upper_id = max(1, trigger_id - HISTORY_RECALL_RECENT_EXCLUDE_IDS)
                else:
                    row = conn.execute("SELECT COALESCE(MAX(id),0) FROM raw_events").fetchone()
                    upper_id = max(1, int((row or [0])[0] or 0) - HISTORY_RECALL_RECENT_EXCLUDE_IDS)
                likes, params = [], [upper_id]
                for term in terms:
                    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    likes.append("text LIKE ? ESCAPE '\\'")
                    params.append(f"%{escaped}%")
                rows = conn.execute(
                    "SELECT id, role, text, created_at, source FROM raw_events "
                    "WHERE deleted=0 AND role='user' AND length(text) <= 600 "
                    "AND id < ? AND (" + " OR ".join(likes) + ") "
                    "ORDER BY id DESC LIMIT 80",
                    params,
                ).fetchall()
                try:
                    windows = _topic_history_turn_windows(
                        conn,
                        [row[0] for row in rows],
                    )
                except Exception as exc:
                    print(
                        f"[memory] topic history batch window failed: {exc}",
                        flush=True,
                    )
                    windows = {}
                ranked = []
                for row in rows:
                    rid = int(row[0])
                    window = windows.get(rid)
                    if window is None:
                        window = _topic_history_turn_window(conn, rid)
                    user_blob = " ".join(str(x[2] or "") for x in window if x[1] == "user")
                    coverage = sum(
                        1 for term in terms
                        if _topic_history_term_matches_text(term, user_blob)
                    )
                    strong_coverage = sum(
                        1 for term in strong_terms
                        if _topic_history_term_matches_text(term, user_blob)
                    )
                    if coverage >= required_coverage and (has_past_cue or strong_coverage >= 1):
                        ranked.append((coverage, rid, window))
                if not ranked:
                    return ""
                ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
                context_rows = ranked[0][2]
            finally:
                conn.close()
        except Exception as exc:
            print(f"[memory] topic history recall failed: {exc}", flush=True)
            return ""

        lines = [
            "[相关旧对话原话 | 当前话题确实关联到过去]",
            f"以下只是一小段真实旧对话。{user}原话优先作为事实；{actor}旧回复仅帮助还原当时语境。",
        ]
        used = len("\n".join(lines))
        seen = set()
        for _rid, role, text, created_at, _source in context_rows:
            body = re.sub(r"\s+", " ", str(text or "")).strip()
            if not body or (role, body) in seen:
                continue
            seen.add((role, body))
            if len(body) > 220:
                body = body[:220].rstrip() + "…"
            ts = _parse_ts(created_at)
            stamp = _local_stamp(ts)
            label = user if role == "user" else actor
            line = f"- {label} {stamp}{body}"
            if used + len(line) + 1 > TOPIC_HISTORY_MAX_CHARS:
                break
            lines.append(line)
            if source_raw_event_ids is not None:
                source_raw_event_ids.append(int(_rid))
            used += len(line) + 1
        return "\n".join(lines) if len(lines) > 2 else ""


    def _memory_items_cover_topic(items, terms):
        """已有 Memory 是否已经覆盖当前具体话题；覆盖够就不再重复塞原话。"""
        if not items:
            return False
        terms = [str(term).lower() for term in terms or [] if str(term).strip()]
        if not terms:
            return True
        needed = 1 if len(terms) == 1 else 2
        for item in items:
            if isinstance(item, dict):
                text = f"{item.get('title') or ''} {item.get('content') or ''}".lower()
            elif isinstance(item, (list, tuple)) and len(item) > 1:
                text = str(item[1] or "").lower()
            else:
                continue
            if sum(1 for term in terms if term in text) >= needed:
                return True
        return False


    def _compact_memory_content(content, max_chars=MEMORY_INJECT_MAX_CHARS):
        """注入上下文只要可用摘要，不把整张长卡塞进每轮 prompt。"""
        re = _owner_runtime['re']
        text = re.sub(r"\s+", " ", str(content or "")).strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "…"


    def _select_stable_user_facts(cards):
        STABLE_USER_FACT_MAX_CARDS = _owner_runtime['STABLE_USER_FACT_MAX_CARDS']
        STABLE_USER_FACT_MAX_CHARS = _owner_runtime['STABLE_USER_FACT_MAX_CHARS']
        STABLE_USER_FACT_MIN_IMPORTANCE = _owner_runtime['STABLE_USER_FACT_MIN_IMPORTANCE']
        selected = []
        for card in cards or []:
            if not isinstance(card, dict) or str(card.get("status") or "") != "fresh":
                continue
            tags = {x.strip() for x in str(card.get("tags") or "").split(",") if x.strip()}
            if "你" not in tags or float(card.get("importance") or 0) < STABLE_USER_FACT_MIN_IMPORTANCE:
                continue
            text = _compact_memory_content(card.get("content") or "", max_chars=220)
            if text:
                selected.append((float(card.get("importance") or 0), int(card.get("id") or 0), text))
        selected.sort(key=lambda x: (-x[0], x[1]))
        out = []
        used = 0
        for _, cid, text in selected[:STABLE_USER_FACT_MAX_CARDS]:
            line = f"[id={cid}] {text}"
            if used + len(line) > STABLE_USER_FACT_MAX_CHARS:
                break
            out.append(line)
            used += len(line)
        return out

    return SimpleNamespace(
        _text_hash=_text_hash,
        _recently_injected=_recently_injected,
        _mark_injected=_mark_injected,
        _norm_query=_norm_query,
        _same_topic=_same_topic,
        _topic_cooled=_topic_cooled,
        _mark_injected_query=_mark_injected_query,
        _inject_cooldown_reset=_inject_cooldown_reset,
        _explicit_personal_fact_query=_explicit_personal_fact_query,
        _memory_explicit_fact_candidate=_memory_explicit_fact_candidate,
        MEMORY_REFERENTIAL_CUES=MEMORY_REFERENTIAL_CUES,
        MEMORY_REFERENTIAL_RELATIONS=MEMORY_REFERENTIAL_RELATIONS,
        MEMORY_REFERENTIAL_IGNORE_TERMS=MEMORY_REFERENTIAL_IGNORE_TERMS,
        _memory_referential_identity_candidate=_memory_referential_identity_candidate,
        _memory_query_has_signal=_memory_query_has_signal,
        _explicit_history_reference=_explicit_history_reference,
        _history_recall_terms=_history_recall_terms,
        _raw_history_temporal_rows=_raw_history_temporal_rows,
        _explicit_history_recall=_explicit_history_recall,
        _topic_history_recent_terms=_topic_history_recent_terms,
        _topic_history_query_text=_topic_history_query_text,
        _topic_history_strong_term=_topic_history_strong_term,
        _topic_history_term_matches_text=_topic_history_term_matches_text,
        _topic_history_terms=_topic_history_terms,
        _topic_history_window_from_rows=_topic_history_window_from_rows,
        _topic_history_turn_window=_topic_history_turn_window,
        _topic_history_turn_windows=_topic_history_turn_windows,
        _topic_history_recall=_topic_history_recall,
        _memory_items_cover_topic=_memory_items_cover_topic,
        _compact_memory_content=_compact_memory_content,
        _select_stable_user_facts=_select_stable_user_facts,
    )


def refresh_cached_revisions(ports, query, cached_items, *, top_n, trace_id,
                             trigger_raw_event_id, caller, include_sunk=False):
    """Use a fresh batch projection; cached body is never a new anchor source."""
    _niannian_recall_items = ports['_niannian_recall_items']
    _positive_int = ports['_positive_int']
    fresh = _niannian_recall_items(
        query,
        top_n,
        trace_id=trace_id,
        trigger_raw_event_id=trigger_raw_event_id,
        caller=caller,
        include_meta=True,
        include_sunk=include_sunk,
    )
    cached_keys = {
        (_positive_int(item.get("id")), _positive_int(item.get("current_revision_id")))
        for item in cached_items or []
        if isinstance(item, dict)
    }
    usable = [
        item for item in fresh
        if (
            _positive_int(item.get("id")),
            _positive_int(item.get("current_revision_id")),
        ) in cached_keys
        and str(item.get("status") or "") in ({"fresh", "sunk"} if include_sunk else {"fresh"})
    ]
    return usable, fresh
