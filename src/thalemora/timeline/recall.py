"""现有聊天档案适配器的记忆选择规则。调用方提供已授权的事件库与现有缓存。"""

from types import SimpleNamespace


def timeline_recall_rules(_owner_runtime, *, familiar_user="用户", actor="助手", timezone_name="UTC"):
    def _render_timeline_period(label, result, query):
        TIMELINE_RECALL_MAX_CHARS = _owner_runtime['TIMELINE_RECALL_MAX_CHARS']
        if not isinstance(result, dict) or not result.get("available"):
            return ""
        lines = [
            f"[时间线回忆 | {label}]",
            f"以下是系统整理的时间线摘要，不是{familiar_user}逐字原话。",
        ]
        if _verbatim_time_query(query):
            lines.append("本次只把时间线当作定位索引；不能用摘要证明对方具体说过什么。")
            return "\n".join(lines)
        rollup = result.get("rollup")
        if isinstance(rollup, dict) and str(rollup.get("content") or "").strip():
            lines.append("- " + str(rollup["content"]).strip())
        else:
            for item in result.get("days") or []:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content") or "").strip()
                if content:
                    lines.append("- " + content)
        rendered = "\n".join(lines) if len(lines) > 2 else ""
        if len(rendered) > TIMELINE_RECALL_MAX_CHARS:
            rendered = (
                rendered[: max(0, TIMELINE_RECALL_MAX_CHARS - 7)].rstrip()
                + "…（已截断）"
            )
        return rendered


    def _time_query_recognized(query):
        re = _owner_runtime['re']
        q = re.sub(r"^\s*\[(?:[01]\d|2[0-3]):[0-5]\d\]\s*", "",
                   str(query or ""), count=1).lower()
        return bool(
            re.search(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", q)
            or re.search(r"(?<!\d)\d{4}-w\d{2}(?!\d)", q)
            or re.search(r"(?<!\d)\d{4}-\d{2}(?!-?\d)", q)
            or any(word in q for word in (
                "今天", "昨天", "前天", "大前天", "昨晚", "昨夜", "今晚", "今早",
                "凌晨", "早晨", "早上", "上午", "中午", "下午", "傍晚", "晚上",
                "夜晚", "夜里", "深夜", "半夜", "午夜", "白天", "全天", "上周", "上个月",
            ))
        )


    def _time_recall_requested(query):
        """Route real history lookups; time words in casual chat aren't lookups."""
        _time_query_recognized = _owner_runtime['_time_query_recognized']
        re = _owner_runtime['re']
        raw = re.sub(
            r"^\s*\[(?:[01]\d|2[0-3]):[0-5]\d\]\s*", "",
            str(query or ""), count=1,
        ).strip()
        if not raw or not _time_query_recognized(raw):
            return False
        compact = re.sub(r"\s+", "", raw)
        if re.search(
            r"(?:发生(?:了)?(?:什么|啥)|说(?:了)?(?:什么|啥)|聊(?:了)?(?:什么|啥)|"
            r"做(?:了)?(?:什么|啥)|干(?:了)?(?:什么|啥)|(?:玩|打)(?:了|过)?(?:什么|啥|哪)|"
            r"怎么了|怎么回事|为什么|还?记得|回忆|原话|说过|提过|聊过|"
            r"有没有|什么时候|啥时候|几点|哪天|哪次|哪一(?:天|次)|"
            r"找(?:一下|到|回)?|翻(?:一下|到|回)?|查(?:一下|到|回)?)",
            compact,
        ):
            return True
        # Bare temporal fragments ("昨晚两点呢？") intentionally request time.
        # A normal question ("今天开心吗？") does not.
        compact = re.sub(r"[!?！？。]+$", "", compact)
        day = r"(?:今天|昨天|前天|大前天|昨晚|昨夜|今晚|今早|上周|上个月|\d{4}-w\d{2}|\d{4}-\d{2}(?:-\d{2})?)"
        period = r"(?:凌晨|早晨|早上|上午|中午|下午|傍晚|晚上|夜晚|夜里|深夜|半夜|午夜|白天|全天)"
        hour = r"(?:[01]?\d|2[0-3])(?::[0-5]\d)|(?:[零〇一二两三四五六七八九十]{1,3}|\d{1,2})点(?:钟)?(?:半|(?:[零〇一二两三四五六七八九十]{1,3}|\d{1,2})分?)?"
        return re.fullmatch(
            rf"(?:(?:{day})(?:{period})?|(?:{period}))(?:{hour})?(?:呢|啊|呀|嘛)?",
            compact.lower(),
        ) is not None


    def _verbatim_time_query(query):
        re = _owner_runtime['re']
        return bool(re.search(
            r"原话|逐字|到底.{0,6}说过|说没说过|有没有说过", query or ""
        ))


    def _raw_time_range_recall(
        period_start_bj, period_end_bj, keyword, include_dates=False
    ):
        RAW_EVENTS_DB = _owner_runtime['RAW_EVENTS_DB']
        sqlite3 = _owner_runtime['sqlite3']
        timezone = _owner_runtime['timezone']
        period_start_utc = period_start_bj.astimezone(timezone.utc)
        period_end_utc = period_end_bj.astimezone(timezone.utc)
        try:
            conn = sqlite3.connect(RAW_EVENTS_DB, timeout=3)
            try:
                rows = conn.execute(
                    "SELECT role, text, created_at FROM raw_events "
                    "WHERE role IN ('user','assistant') AND deleted=0 "
                    "AND datetime(created_at)>=? AND datetime(created_at)<? ORDER BY id",
                    (
                        period_start_utc.strftime("%Y-%m-%d %H:%M:%S"),
                        period_end_utc.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                ).fetchall()
            finally:
                conn.close()
            turns = []
            seen = set()
            for role, raw_text, _created_at in rows:
                raw_text = (raw_text or "").strip()
                if not raw_text:
                    continue
                key = (role, raw_text[:60])
                if key in seen:
                    continue
                seen.add(key)
                turns.append((role, raw_text))
            turns = turns[-50:]
            if not turns:
                return ""
            range_label = (
                f"{period_start_bj.strftime('%m-%d %H:%M')}"
                f"—{period_end_bj.strftime('%m-%d %H:%M')}"
                if include_dates else
                f"{period_start_bj.strftime('%H:%M')}-{period_end_bj.strftime('%H:%M')}"
            )
            lines = [
                f"[时间回忆 | {keyword} | {range_label}]",
                f"以下是你和{familiar_user}的真实对话记录。如果用户问到这些时间段的事，必须依据以下内容回答，禁止编造。",
                "",
            ]
            for role, raw_text in turns:
                label = familiar_user if role == "user" else actor
                lines.append(f"- {label}: {raw_text[:120]}")
            result = "\n".join(lines)
            return result if len(result) <= 4000 else result[:3994] + "…（已截断）"
        except Exception as e:
            print(f"[memory] time_recall failed: {e}", flush=True)
            return ""


    def _time_recall(query, trace_id=None):
        """Route explicit time questions to UTC SQL ranges or canonical timelines."""
        ZoneInfo = _owner_runtime['ZoneInfo']
        _timeline_lookup = _owner_runtime['_timeline_lookup']
        datetime = _owner_runtime['datetime']
        re = _owner_runtime['re']
        timedelta = _owner_runtime['timedelta']
        timezone = _owner_runtime['timezone']
        # Ignore any rendered per-turn system clock header in lookup input.
        q = re.sub(
            r"^\s*\[(?:[01]\d|2[0-3]):[0-5]\d\]\s*", "",
            str(query or "").lower(), count=1,
        )
        bj_tz = ZoneInfo(timezone_name)
        now_bj = datetime.now(timezone.utc).astimezone(bj_tz)

        explicit_day = re.search(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", q)
        if explicit_day:
            try:
                target_day = datetime(
                    int(explicit_day.group(1)),
                    int(explicit_day.group(2)),
                    int(explicit_day.group(3)),
                    tzinfo=bj_tz,
                )
            except ValueError:
                return ""
            matched_date = (explicit_day.group(0), target_day)
        else:
            matched_date = None

        explicit_week = re.search(r"(?<!\d)(\d{4})-w(\d{2})(?!\d)", q)
        if matched_date is None and ("上周" in q or explicit_week):
            if explicit_week:
                try:
                    monday = datetime.combine(
                        datetime.fromisocalendar(
                            int(explicit_week.group(1)), int(explicit_week.group(2)), 1
                        ).date(),
                        datetime.min.time(),
                        tzinfo=bj_tz,
                    )
                except ValueError:
                    return ""
            else:
                this_monday = (now_bj - timedelta(days=now_bj.isoweekday() - 1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                monday = this_monday - timedelta(days=7)
            iso_year, iso_week, _ = monday.isocalendar()
            period_key = f"{iso_year:04d}-W{iso_week:02d}"
            lookup_result = _timeline_lookup({
                "mode": "period", "granularity": "week", "period_key": period_key,
            }, trace_id=trace_id)
            if _verbatim_time_query(query):
                return _raw_time_range_recall(
                    monday, monday + timedelta(days=7), period_key, include_dates=True
                )
            return _render_timeline_period(period_key, lookup_result, query)

        explicit_month = re.search(r"(?<!\d)(\d{4})-(\d{2})(?!-?\d)", q)
        if matched_date is None and ("上个月" in q or explicit_month):
            if explicit_month:
                year, month = int(explicit_month.group(1)), int(explicit_month.group(2))
                if not 1 <= month <= 12:
                    return ""
            else:
                previous = now_bj.replace(day=1) - timedelta(days=1)
                year, month = previous.year, previous.month
            period_key = f"{year:04d}-{month:02d}"
            month_start = datetime(year, month, 1, tzinfo=bj_tz)
            month_end = (
                datetime(year + 1, 1, 1, tzinfo=bj_tz)
                if month == 12 else datetime(year, month + 1, 1, tzinfo=bj_tz)
            )
            lookup_result = _timeline_lookup({
                "mode": "period", "granularity": "month", "period_key": period_key,
            }, trace_id=trace_id)
            if _verbatim_time_query(query):
                return _raw_time_range_recall(
                    month_start, month_end, period_key, include_dates=True
                )
            return _render_timeline_period(period_key, lookup_result, query)

        date_words = [
            ("大前天", -3), ("前天", -2), ("昨天", -1), ("今天", 0),
        ]
        period_words = [
            ("凌晨", 0, 6), ("早晨", 6, 9), ("早上", 6, 9), ("今早", 6, 9),
            ("上午", 9, 12), ("中午", 12, 14), ("下午", 14, 18),
            ("傍晚", 18, 19), ("晚上", 19, 24), ("夜晚", 19, 24),
            ("夜里", 19, 24), ("深夜", 19, 24), ("半夜", 0, 6),
            ("午夜", 0, 2), ("白天", 6, 18),
            ("全天", 0, 24),
        ]
        matched_period = None
        keyword = ""
        for word, day_offset, period_label, h_start, h_end in (
            ("昨晚", -1, "晚上", 19, 24),
            ("昨夜", -1, "夜晚", 19, 24),
            ("今晚", 0, "晚上", 19, 24),
            ("今早", 0, "早晨", 6, 9),
        ):
            if word in q:
                matched_date = (
                    word,
                    (now_bj + timedelta(days=day_offset)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ),
                )
                matched_period = (period_label, h_start, h_end)
                keyword = word
                break
        if matched_period is None:
            if matched_date is None:
                for word, day_offset in date_words:
                    if word in q:
                        matched_date = (
                            word,
                            (now_bj + timedelta(days=day_offset)).replace(
                                hour=0, minute=0, second=0, microsecond=0
                            ),
                        )
                        break
            for word, h_start, h_end in period_words:
                if word in q:
                    matched_period = (word, h_start, h_end)
                    break
        if matched_date is None and matched_period is None:
            return ""
        if matched_date is None:
            matched_date = ("今天", now_bj.replace(hour=0, minute=0, second=0, microsecond=0))
        if matched_period is None:
            matched_period = ("全天", 0, 24)
        if not keyword:
            keyword = (
                matched_date[0]
                if matched_period[0] == "全天"
                else matched_date[0] + matched_period[0]
            )
        _, day_start_bj = matched_date
        _, h_start, h_end = matched_period
        period_start_bj = day_start_bj + timedelta(hours=h_start)
        period_end_bj = day_start_bj + timedelta(hours=h_end)

        # "昨晚" includes the hours after midnight; "昨天晚上" without an
        # hour keeps its narrower 19:00-24:00 calendar-day meaning.
        if keyword in ("昨晚", "昨夜") and not re.search(r"(?:点|[:：])", q):
            period_end_bj += timedelta(hours=6)

        # An explicit clock narrows the time range.  Chinese numerals and
        # 24-hour digital notation are supported without adding any new
        # permanent prompt or touching the underlying conversation.
        clock = re.search(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)", q)
        minute_precision = False
        if clock:
            hour, minute = int(clock.group(1)), int(clock.group(2))
            minute_precision = True
        else:
            clock = re.search(
                r"(?<!\d)([零〇一二两三四五六七八九十]{1,3}|\d{1,2})点(?:钟)?"
                r"(半|(?:[零〇一二两三四五六七八九十]{1,3}|\d{1,2})分?)?", q
            )
            hour = minute = None
            if clock:
                def number(token):
                    token = str(token or "").rstrip("分")
                    if token.isdigit():
                        return int(token)
                    digits = {"零": 0, "〇": 0, "一": 1, "二": 2,
                              "两": 2, "三": 3, "四": 4, "五": 5,
                              "六": 6, "七": 7, "八": 8, "九": 9}
                    if token == "十":
                        return 10
                    if "十" in token:
                        left, right = token.split("十", 1)
                        if left not in ("", *digits) or (right and right not in digits):
                            return None
                        return (digits[left] if left else 1) * 10 + (
                            digits[right] if right else 0
                        )
                    return digits.get(token)
                hour = number(clock.group(1))
                suffix = clock.group(2)
                minute = 30 if suffix == "半" else number(suffix) if suffix else 0
                minute_precision = bool(suffix)
        if hour is not None and minute is not None and 0 <= hour <= 23 and 0 <= minute <= 59:
            period_label = matched_period[0]
            if period_label in ("下午", "傍晚") and 1 <= hour <= 11:
                hour += 12
            elif period_label in ("中午",) and 1 <= hour <= 6:
                hour += 12
            elif period_label in ("晚上", "夜晚", "夜里", "深夜"):
                # A 1–6 a.m. time referred to as "昨晚/昨夜" or "昨天晚上"
                # is after that evening's midnight, not yesterday 02:00.
                if hour == 12:
                    hour = 0
                    day_start_bj += timedelta(days=1)
                elif 0 <= hour <= 6:
                    day_start_bj += timedelta(days=1)
                elif 7 <= hour <= 11:
                    hour += 12
            elif period_label == "凌晨" and hour == 12:
                hour = 0
            center = day_start_bj + timedelta(hours=hour, minutes=minute)
            if minute_precision:
                period_start_bj = center - timedelta(minutes=30)
                period_end_bj = center + timedelta(minutes=30)
            else:
                period_start_bj = center
                period_end_bj = center + timedelta(hours=1)
        return _raw_time_range_recall(period_start_bj, period_end_bj, keyword)

    return SimpleNamespace(
        _render_timeline_period=_render_timeline_period,
        _time_query_recognized=_time_query_recognized,
        _time_recall_requested=_time_recall_requested,
        _verbatim_time_query=_verbatim_time_query,
        _raw_time_range_recall=_raw_time_range_recall,
        _time_recall=_time_recall,
    )


def contemplation_requested(ports, query):
    """沉思室召回降频判断：60 秒内已检索过 → 跳过；query 不含沉思类关键词 → 跳过。
    方案 A（时间降频）为主，方案 B（关键词门控）兜底，两者都通过才触发。"""
    _CONTEMPLATION_LAST_RECALL_TS = ports['_CONTEMPLATION_LAST_RECALL_TS']
    _CONTEMPLATION_RECALL_INTERVAL = ports['_CONTEMPLATION_RECALL_INTERVAL']
    _CONTEMPLATION_TRIGGER_KEYWORDS = ports['_CONTEMPLATION_TRIGGER_KEYWORDS']
    time = ports['time']
    if time.time() - _CONTEMPLATION_LAST_RECALL_TS < _CONTEMPLATION_RECALL_INTERVAL:
        return False
    q = query or ""
    return any(kw in q for kw in _CONTEMPLATION_TRIGGER_KEYWORDS)
