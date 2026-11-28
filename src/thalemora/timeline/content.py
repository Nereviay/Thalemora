"""时间线输出校验与呈现。"""
import re
import json

RELATIVE_DATE_RE = re.compile(
    r"今天|昨日|昨天|前天|昨晚|今晚|明天|后天|本周|这周|上周|下周|本月|这个月|上月|下月"
)


OUTPUT_STRUCTURE_RE = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s*|[-*]\s+|\d+[.)、]\s*|(?:ROLE|OBJECTIVE|"
    r"INPUT CONTRACT|EVIDENCE POLICY|CONTENT SELECTION|TEMPORAL POLICY|"
    r"PERSPECTIVE|OUTPUT CONTRACT)(?=\s*(?:[:：]|\n|$)))",
    re.I,
)

DAILY_TIME_THEME_RE = re.compile(
    r"^(?:(?:凌晨|清晨|早晨|早上|上午|中午|午后|下午|傍晚|晚上|夜里|夜晚|深夜)"
    r"(?:时段|时间)?)(?:\s*(?:、|和|与|及|到|至|—|-|/|，|,)\s*"
    r"(?:(?:凌晨|清晨|早晨|早上|上午|中午|午后|下午|傍晚|晚上|夜里|夜晚|深夜)"
    r"(?:时段|时间)?))*$"
)

def validate_timeline_content(content):
    value = str(content or "").strip()
    if not value:
        raise ValueError("timeline_summary_empty")
    if (
        len(value.splitlines()) != 1
        or value.startswith(("{", "[", "```"))
        or OUTPUT_STRUCTURE_RE.search(value)
    ):
        raise ValueError("timeline_summary_output_contract")
    match = RELATIVE_DATE_RE.search(re.sub(r"「[^「」]*」", "", value))
    if match:
        raise ValueError(f"timeline_summary_relative_date:{match.group(0)}")
    return value

def parse_weekly_summary(raw_content):
    try:
        payload = json.loads(raw_content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("weekly_summary_invalid_json") from exc
    if not isinstance(payload, dict) or set(payload) != {"threads"}:
        raise ValueError("weekly_summary_invalid_schema")
    threads = payload["threads"]
    if not isinstance(threads, list) or not 1 <= len(threads) <= 4:
        raise ValueError("weekly_summary_invalid_threads")
    parsed = []
    for item in threads:
        if not isinstance(item, dict) or set(item) != {"thread", "summary"}:
            raise ValueError("weekly_summary_invalid_thread_schema")
        thread = item["thread"].strip() if isinstance(item["thread"], str) else ""
        summary = item["summary"].strip() if isinstance(item["summary"], str) else ""
        if not thread:
            raise ValueError("weekly_summary_empty_thread")
        if not summary:
            raise ValueError("weekly_summary_empty_summary")
        parsed.append({"thread": thread, "summary": summary})
    return {"threads": parsed}


def render_weekly_summary(parsed):
    if not isinstance(parsed, dict) or set(parsed) != {"threads"}:
        raise ValueError("weekly_summary_invalid_render_input")
    threads = parsed["threads"]
    if not isinstance(threads, list) or not 1 <= len(threads) <= 4:
        raise ValueError("weekly_summary_invalid_render_threads")
    sentences = []
    for item in threads:
        if not isinstance(item, dict) or set(item) != {"thread", "summary"}:
            raise ValueError("weekly_summary_invalid_render_thread")
        thread = item["thread"].strip() if isinstance(item["thread"], str) else ""
        summary = item["summary"].strip() if isinstance(item["summary"], str) else ""
        if not thread or not summary:
            raise ValueError("weekly_summary_empty_render_summary")
        sentences.append(summary if summary[-1] in "。！？!?" else f"{summary}。")
    return validate_timeline_content("".join(sentences))


def parse_monthly_summary(raw_content):
    try:
        payload = json.loads(raw_content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("monthly_summary_invalid_json") from exc
    if not isinstance(payload, dict) or set(payload) != {"arcs"}:
        raise ValueError("monthly_summary_invalid_schema")
    arcs = payload["arcs"]
    if not isinstance(arcs, list) or not 1 <= len(arcs) <= 4:
        raise ValueError("monthly_summary_invalid_arcs")
    parsed = []
    for item in arcs:
        if not isinstance(item, dict) or set(item) != {"arc", "summary"}:
            raise ValueError("monthly_summary_invalid_arc_schema")
        arc = item["arc"].strip() if isinstance(item["arc"], str) else ""
        summary = item["summary"].strip() if isinstance(item["summary"], str) else ""
        if not arc:
            raise ValueError("monthly_summary_empty_arc")
        if not summary:
            raise ValueError("monthly_summary_empty_summary")
        parsed.append({"arc": arc, "summary": summary})
    return {"arcs": parsed}


def render_monthly_summary(parsed):
    if not isinstance(parsed, dict) or set(parsed) != {"arcs"}:
        raise ValueError("monthly_summary_invalid_render_input")
    arcs = parsed["arcs"]
    if not isinstance(arcs, list) or not 1 <= len(arcs) <= 4:
        raise ValueError("monthly_summary_invalid_render_arcs")
    sentences = []
    for item in arcs:
        if not isinstance(item, dict) or set(item) != {"arc", "summary"}:
            raise ValueError("monthly_summary_invalid_render_arc")
        arc = item["arc"].strip() if isinstance(item["arc"], str) else ""
        summary = item["summary"].strip() if isinstance(item["summary"], str) else ""
        if not arc or not summary:
            raise ValueError("monthly_summary_empty_render_summary")
        sentences.append(summary if summary[-1] in "。！？!?" else f"{summary}。")
    return validate_timeline_content("".join(sentences))

DAY_RULES = """根据消息选择一至四个值得记住的主题。每个主题选择能保留事件、人物、条件和改口的原句。
同一件事以最新决定为准。不要把转述者当成行动者，不合并多个说话人的行为。
每个候选原句都有 event_id 和 sentence_id。只返回 JSON 对象 {"themes":[{"theme":"主题","facts":[{"event_id":1,"sentence_id":1}]}]}。
选择实际给出的编号，保留归属、否定和前提；不要改写或抄写原句。正文由系统按来源呈现。"""

PERIOD_RULES = {
    "week": '概括跨日反复或转折，保留事实的参与者、时间和条件。输出 {"threads":[{"thread":"线索","summary":"正文"}]}，一至四项。',
    "month": '概括跨周的变化、持续事项和已经结束的事项。输出 {"arcs":[{"arc":"脉络","summary":"正文"}]}，一至四项。',
}


def _citable_sentences(source):
    body = source["quotable_text"].strip()
    parts = [s.strip() for s in re.split(r"(?<=[。！？!?])", body) if s.strip()]
    if source.get("truncated") and not body.endswith(("。", "！", "？", "!", "?")):
        parts = parts[:-1]
    return parts


def render_grounded_day(raw, messages, speakers):
    payload = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(payload, dict) or set(payload) != {"themes"}:
        raise ValueError("daily_summary_invalid_schema")
    themes = payload["themes"]
    if not isinstance(themes, list) or not 1 <= len(themes) <= 4:
        raise ValueError("daily_summary_invalid_themes")
    sources = {m["event_id"]: m for m in messages}
    sentences = []
    for theme in themes:
        if (not isinstance(theme, dict) or set(theme) != {"theme", "facts"}
                or not isinstance(theme["theme"], str) or not theme["theme"].strip()
                or DAILY_TIME_THEME_RE.fullmatch(theme["theme"].strip())):
            raise ValueError("daily_summary_invalid_theme")
        if not isinstance(theme["facts"], list) or not theme["facts"]:
            raise ValueError("daily_summary_missing_evidence")
        for fact in theme["facts"]:
            if (not isinstance(fact, dict) or set(fact) not in ({"event_id", "sentence_id"}, {"event_id", "quote"})
                    or type(fact["event_id"]) is not int):
                raise ValueError("daily_summary_invalid_source")
            source = sources.get(fact["event_id"])
            if source is None:
                raise ValueError("daily_summary_invalid_source")
            parts = _citable_sentences(source)
            if "sentence_id" in fact:
                number = fact["sentence_id"]
                if type(number) is not int or not 1 <= number <= len(parts):
                    raise ValueError("daily_summary_invalid_source")
                quote = parts[number - 1]
            else:
                quote = fact["quote"]
                if not isinstance(quote, str) or not quote.strip():
                    raise ValueError("daily_summary_invalid_source")
                if quote not in parts and (source.get("truncated") or quote != source["quotable_text"].strip()):
                    raise ValueError("daily_summary_unsupported_attribution")
            display = " ".join(quote.splitlines())
            sentence = f"{speakers[source['role']]}提到「{display}」。"
            if sentence not in sentences:
                sentences.append(sentence)
    return validate_timeline_content("".join(sentences))


def generate_day(day, messages, complete, *, speakers):
    instruction = DAY_RULES + "\n" + day
    choices = [dict(event_id=m["event_id"], role=m["role"], sentence_id=i, quotable_text=q)
               for m in messages for i, q in enumerate(_citable_sentences(m), 1)]
    data = json.dumps(choices, ensure_ascii=False)
    raw = complete("daily", instruction, data, 0.3)
    try:
        return render_grounded_day(raw, messages, speakers)
    except (ValueError, TypeError):
        # 沿用原先至多一次的格式修复。只向同一次后台任务提供可核对的来源。
        raw = complete("daily", instruction + " 上次输出未通过校验，请从提供的原句编号重新选择。", data, 0.0)
        return render_grounded_day(raw, messages, speakers)


def generate_period(granularity, key, sources, complete, *, perspective=""):
    data = "\n".join(f"[{s['date']}] {s['content']}" for s in sources)
    prompt = PERIOD_RULES[granularity] + " 只返回 JSON 对象，使用绝对日期。" + perspective + " " + key
    if granularity == "week":
        return render_weekly_summary(parse_weekly_summary(complete("weekly", prompt, data, 0.2)))
    return render_monthly_summary(parse_monthly_summary(complete("monthly", prompt, data, 0.2)))
