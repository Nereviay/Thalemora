"""单轮 Related 召回：正式 Memory 和独立 Archive 仍是唯一来源。"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
import sqlite3
from zoneinfo import ZoneInfo

from .facts import statement, compare_statement, render_hint, preference_evidence, preference_change
from .turn_context import (MAX_TURN_CHARS, acknowledged_followup, prior_assistant_query,
                           question_focus, reference_request, trusted_previous_text)
from .bm25 import tokenize
from .core import RetrievalIndex
from .models import configured_model, AvailableEmbedder
from thalemora.app.providers import ProviderUnavailable


STOP = set("我 你 他 她 它 的 了 啊 呢 吗 吧 是 在 有 和 与 就 都 也 又 很 不 一 下 个 这 那 什么 怎么 如何 哪 哪里 哪儿 现在 以前 之前 记得 请 告诉 说说 帮忙 帮 我们 你们 一下 想 要 问 问一下 看 看看 知道 知道了 明白 收到 麻烦 能 不能 能不能 可以 还 随便 没有 没事 事情 帮我 讲讲 聊聊 你好 您好 在吗 好 好的 好吧 嗯 嗯嗯 哦 哈哈 继续 谢谢 the a an is are was i you my me what where how please tell about".split())


def public_references(result):
    """HTTP-safe identifiers only; no text, score, evidence envelope or exceptions."""
    return {kind: [{k: item[k] for k in fields if k in item} for item in result[kind]]
            for kind, fields in (('memories', ('id', 'revision_id', 'extension_id', 'base_revision_id')),
                                 ('archive', ('id', 'version')))}


def query_terms(query):
    return list(dict.fromkeys(t for t in tokenize(query) if t not in STOP))[:12]


def _row_keywords(row):
    keywords = row['provenance'].get('keywords') or ''
    if isinstance(keywords, str):
        try:
            keywords = json.loads(keywords)
        except ValueError:
            keywords = re.split(r"[,，、;；\s]+", keywords)
    return keywords if isinstance(keywords, list) else []


def normalized(text):
    return re.sub(r"\W+", "", text.lower())


def separately_named_titles(query, rows):
    marker = re.search(r"分别|各自", query)
    if not marker:
        return ()
    prefix = query[:marker.start()].rstrip()
    titles = {row['title'].strip() for row in rows if row.get('title') and len(row['title'].strip()) >= 2}
    pairs = set()
    for second in titles:
        if not prefix.endswith(second):
            continue
        join = re.search(r"(?:和|与|以及|、|跟)\s*$", prefix[:-len(second)])
        if join:
            first_part = prefix[:join.start()].rstrip()
            pairs.update((first, second) for first in titles if first != second and first_part.endswith(first))
    return pairs.pop() if len(pairs) == 1 else ()


def excerpt(text, terms, limit=300):
    """展示最相关原句及附近正文；没有字面命中时保留原来的开头。"""
    if len(text) <= limit:
        return text
    start, best = 0, 0
    for sentence in re.finditer(r'[^。！？!?\n]+[。！？!?]?', text):
        body = sentence[0].lower()
        hits = [t for t in terms if t in body]
        if len(hits) <= best:
            continue
        best = len(hits)
        if len(body) <= limit:
            # 窗口至少包含整句，避免恰好在命中词与答案之间截断。
            start = 0 if sentence.end() <= limit else sentence.start()
        else:
            anchor = sentence.start() + body.index(max(hits, key=len))
            start = max(sentence.start(), anchor - limit // 2)
        if best == len(terms):
            break
    return text[start:start + limit]


_LAST_AGREEMENT = re.compile(
    r"(?:上次|上回|最近一次)[，,\s]*(?:我们|咱们)?(?:关于)?"
    r"(?P<topic>[\w·-]{2,30}?)(?<!的)(?:约好|约定|说好|商量好)(?:的|了)?"
    r"(?P<question>[^。！？!?]{0,40}(?:哪|什么|怎么|几|谁)[^。！？!?]{0,12})[？?]?$")
_QUOTE_SUBJECT = re.compile(
    r'(?<![跟对向和])(?P<who>助手|用户|你|我)(?=的(?:那句)?原话|关于|当时|那时|上次|上回|'
    r'以前|之前|昨天|前天|今天|(?:在)?[0-9一二三四五六七八九十]{1,3}月|(?:怎么)?(?:说|提|讲))')


def _asks_past_words(query):
    return (('“' in query or '「' in query or '"' in query)
            and bool(re.search(r'怎么(?:说|讲)的|(?:说|讲|提)(?:了|过)?(?:什么|啥)', query))
            and bool(_QUOTE_SUBJECT.search(query)))


def constrained_quote_request(query):
    return (current_conversation_quote(query)
            or ('原话' in query and bool(re.search(r'我|你的|助手|上次|当时|那时|以前|之前|月|年|[“「"]', query)))
            or bool(re.search(r'我(?:当时|那时|以前|之前|上次).{0,50}提过.{0,8}(?:什么|啥)', query))
            or bool(_LAST_AGREEMENT.search(query)) or _asks_past_words(query))


def original_quote_request(query):
    return constrained_quote_request(query) or bool(re.search(r'原话.{0,12}(?:怎么说|是什么)', query))


def recover_original(archive, application, agent_id, app_id, channel, query,
                     visible_ids, visible_texts, conversation_id=None):
    """按说话人、字面锚点及本地日历恢复原始事件。"""
    if not original_quote_request(query):
        return None
    quoted = re.findall(r'[“「"]([^”」"]{2,30})[”」"]', query)
    request_text = re.sub(r'[“「"][^”」"]+[”」"]', '', query)
    roles = {'assistant' if who in {'助手', '你'} else 'user' for who in _QUOTE_SUBJECT.findall(request_text)}
    if len(roles) > 1:
        return None
    role = next(iter(roles), 'user')
    years = re.findall(r'(?:认识|相处|交往|学了|用了|玩了)(?:了|有)?[一二三四五六七八九十百0-9]{1,3}年', query)
    bare = re.fullmatch(r'我(?:当时|那时|以前|之前|上次)提过(.{4,30}?)(?:什么|啥)[？?]?', query)
    agreement = _LAST_AGREEMENT.search(query)
    anchors = quoted or years
    action = ''
    if not anchors and bare:
        anchors = [bare[1].strip()]
    elif not anchors and agreement:
        anchors = [agreement['topic']]
        question_head = re.split(r'哪|什么|怎么|几|谁', agreement['question'], maxsplit=1)[0]
        action = re.sub(r'^(?:要|准备|打算|应该)|(?:到|在|给|向|往)$', '', question_head).strip()
    if len(anchors) != 1 or len(normalized(anchors[0])) < 4 or not query_terms(anchors[0]):
        return None
    anchor = anchors[0]
    # 未实现的相对窗口与年份不能偷偷退化成无时间约束的原话命中。
    if re.search(r'去年|今年|前年|上个?月|这个月|本月|最近(?!一次)|过去|(?<!最)近[一二三四五六七八九十0-9]|上周|本周|\d{4}年|\d{4}[-/]|[0-9一二三四五六七八九十]+[日号]', request_text):
        return None
    month_pattern = r'(?<![0-9])(?:1[0-2]|[1-9]|十一|十二|[一二三四五六七八九十])月'
    words = dict(zip('一二三四五六七八九十', range(1, 11)), 十一=11, 十二=12)
    def months(text):
        return {int(m[:-1]) if m[:-1].isdigit() else words[m[:-1]]
                for m in re.findall(month_pattern, text)}
    requested = months(request_text)
    days = set(re.findall(r'今天|昨天|前天', request_text))
    if len(requested) > 1 or len(days) > 1:
        return None
    talking_month = bool(re.search(month_pattern + r'.{0,9}(?:说|聊|提|讲|原话)', request_text))
    zone_name = os.environ.get('THALEMORA_RECALL_TIMEZONE')
    zone = ZoneInfo(zone_name) if zone_name and (days or talking_month) else None
    day = None
    if days:
        days_ago = {'今天': 0, '昨天': 1, '前天': 2}[days.pop()]
        day = datetime.now(zone).date() - timedelta(days=days_ago)

    def candidates():
        offset = 0
        while True:
            hits = application.search(agent_id, app_id, anchor, channel, limit=36, offset=offset,
                                      quote_role=role)
            yield from hits
            if len(hits) < 36:
                return
            offset += len(hits)

    with closing(sqlite3.connect(archive.path)) as db:
        db.row_factory = sqlite3.Row
        for hit in candidates():
            row = db.execute('SELECT * FROM history_events WHERE app=? AND channel=? AND source_event_id=?',
                             (app_id, channel, hit['source_event_id'])).fetchone()
            if (not row or row['deleted'] or row['role'] != role
                    or (conversation_id is not None and row['conversation_id'] != conversation_id)
                    or row['version'] != hit['version'] or row['text'] != hit['text']
                    or anchor not in row['text']):
                continue
            text = row['text']
            if action and action not in text and not re.search(r'取消|作废|不算数', text):
                continue
            if ('朋友' in query and ('朋友' not in text
                    or ('朋友们' in query) != ('朋友们' in text))):
                continue
            if any(word in query and word not in text for word in ('他们', '她们', '他', '她')):
                continue
            if ('他们' not in query and '他' in query and '他们' in text
                    or '她们' not in query and '她' in query and '她们' in text):
                continue
            try:
                instant = datetime.fromisoformat(row['occurred_at'].replace('Z', '+00:00'))
            except (AttributeError, ValueError):
                continue
            if instant.tzinfo is None:
                continue
            local_date = instant.astimezone(zone).date()
            if day and local_date != day:
                continue
            if requested and (requested != {local_date.month} if talking_month else not requested <= months(text)):
                continue
            norm = normalized(text)
            if (len(text) > 240 or row['source_event_id'] in visible_ids
                    or any(norm in normalized(t) for t in visible_texts)):
                if '上次' in query or agreement:
                    return None
                continue
            item = dict(kind='archive', id=row['source_event_id'], version=row['version'],
                        text=text, role=role, occurred_at=row['occurred_at'])
            label = '助手原话' if role == 'assistant' else '用户原话'
            line = f"[{label} {item['id']} v{item['version']} {item['occurred_at']}] {text}"
            if len(line) <= 700:
                return dict(rendered=line, memories=[], archive=[item], errors=[])
    return None


def current_conversation_quote(query):
    outside_quotes = re.sub(r'[“「"]([^”」"]+)[”」"]', '', query)
    scope = r'(?:这(?:一)?(?:段|个|次|轮)|当前|本(?:次|段|轮)?)(?:对话|会话|聊天)'
    outside_quotes = re.sub(r'(?:不限于?|不局限于|不限定在?|不只看)' + scope, '', outside_quotes)
    return bool(re.search(scope, outside_quotes)
                and ('原话' in outside_quotes or _asks_past_words(query) or _LAST_AGREEMENT.search(outside_quotes)
                     or re.search(r'我.{0,12}提过.{0,8}(?:什么|啥)', outside_quotes)))


def scoped_rows(path, app_id, channel, archive_path, agent_id):
    """当前修订证据全有效才自动使用；历史引用仅抑制同范围、同版本原话。"""
    from thalemora.memory.source_validity import resolve_archive_evidence
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        from thalemora.memory.scopes import ownership, belongs
        db.execute('BEGIN')
        scopes = ownership(db, agent_id, channel)
        evidence = db.execute(
            "SELECT r.card_id,r.id AS revision_id,r.revision_no,r.origin,r.change_type,"
            "e.source_kind,e.source_ref FROM card_revisions r "
            "JOIN card_evidence e ON e.revision_id=r.id ORDER BY r.revision_no").fetchall()
        memory_rows = RetrievalIndex._memory_rows(str(path), connection=db)
        from thalemora.memory.extensions import scoped_extensions
        extensions = scoped_extensions(path, archive_path, agent_id, app_id, channel, connection=db)
    refs, states = {}, {}
    revisions = {r['source_id']: r['provenance']['revision_id'] for r in memory_rows
                 if 'extension_id' not in r['provenance']}
    ids = {card_id for card_id, scope in scopes.items() if belongs(scope, app_id, channel)}
    suppressed_raw = set()
    with closing(sqlite3.connect(archive_path)) as source_db:
        for item in evidence:
            card_id = item["card_id"]
            state = resolve_archive_evidence(source_db, item, agent_id, app_id, channel)
            key = state["source_key"]
            if key is not None and card_id in ids:
                suppressed_raw.add(key)
                if item['revision_id'] == revisions.get(card_id):
                    refs.setdefault(card_id, set()).add(key)
            status = state["resolution"]
            if status == "manual_note" and (item["origin"], item["change_type"]) != ("manual_ui", "create"):
                status = "unsupported"
            if status == "manual_revision" and (item["origin"], item["change_type"]) != ("manual_ui", "manual_edit"):
                status = "unsupported"
            states.setdefault(item["revision_id"], []).append(status)
    by_extension = {e['extension_id']: e for e in extensions}
    unit_refs = {}
    for card_id in ids:
        unit_refs[f'memory:{card_id}'] = refs.get(card_id, set())
    for extension in extensions:
        key = f"memory:{extension['card_id']}:extension:{extension['extension_id']}"
        unit_refs[key] = {tuple(e['source_key']) for e in extension['evidence_refs'] if e['source_key']}
        suppressed_raw.update(unit_refs[key])
    rows = []
    for row in memory_rows:
        extension_id = row['provenance'].get('extension_id')
        if extension_id is not None:
            extension = by_extension.get(extension_id)
            if extension and extension['eligible']:
                row['provenance']['evidence_refs'] = extension['evidence_refs']
                rows.append(row)
        else:
            statuses = states.get(row['provenance']['revision_id'], [])
            if row['source_id'] in ids and statuses and all(s in {'current', 'manual_note', 'manual_revision'} for s in statuses):
                rows.append(row)
    # 历史/停用扩展也只抑制确切来源版本，不让原话兜底绕过停用。
    return rows, unit_refs, suppressed_raw


def ensure_current_index(index, rows, embedder, *, expected_generation=None):
    """Fence BEFORE reading sources; repair missing/unbound vectors after restart.

    Internal callers pass a supplier, or a fence captured before their snapshot.
    A precomputed low-level snapshot has the same caller responsibility.
    """
    expected = index._generation('memory_card') if expected_generation is None else expected_generation
    rows = list(rows() if callable(rows) else rows)
    with index._connect() as db:
        existing = {row['source_key']: row for row in db.execute(
            "SELECT * FROM chunks WHERE source_kind='memory_card'")}
    changed = len(existing) != len(rows)
    if not changed:
        for row in rows:
            old = existing.get(row['source_key'])
            if (old is None or old['content_hash'] != row['content_hash']
                    or old['text'] != row['text']
                    or old['status'] != row.get('status', '')
                    or json.loads(old['provenance_json']) != row['provenance']
                    or (embedder is not None and not index._vector_bound(old, index.embedding_model))):
                changed = True
                break
    if changed:
        return index._replace_lane('memory_card', rows, source_path=index.memory_db_path,
                                   embedder=embedder, expected_generation=expected)
    return None


def archive_context(archive, app_id, channel, anchor):
    """只解引用已准入原话的真实邻居；缺少可信全序时不推测上下文。"""
    archive._authorize(app_id, channel)
    with closing(sqlite3.connect(archive.path)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        hit = db.execute(
            "SELECT * FROM history_events WHERE app=? AND channel=? AND source_event_id=?",
            (app_id, channel, anchor['id']),
        ).fetchone()
        if (not hit or hit['deleted'] or hit['version'] != anchor['version']
                or hit['text'] != anchor['text'] or hit['role'] not in {'user', 'assistant'}
                or not (hit['conversation_id'] or '').strip()
                or not (hit['epoch_id'] or '').strip()):
            return []
        # 包括 tombstone 和不可展示行：它们占据真实位置，不越过它们寻找替代邻句。
        rows = db.execute(
            "SELECT source_event_id, occurred_at FROM history_events WHERE app=? AND channel=? "
            "AND conversation_id=? AND epoch_id=?",
            (app_id, channel, hit['conversation_id'], hit['epoch_id']),
        )
        try:
            anchor_time = datetime.fromisoformat(hit['occurred_at'].replace('Z', '+00:00'))
        except (AttributeError, ValueError):
            return []
        if anchor_time.tzinfo is None:
            return []
        anchor_time = anchor_time.astimezone(timezone.utc)
        timeline = set()
        before = after = None
        # 仍校验整个窗口的可信全序，但只传输 ID/时间，单遍找相邻项，不排序正文。
        for row in rows:
            try:
                instant = datetime.fromisoformat(row['occurred_at'].replace('Z', '+00:00'))
            except (AttributeError, ValueError):
                return []
            if instant.tzinfo is None:
                return []
            instant = instant.astimezone(timezone.utc)
            if instant in timeline:
                return []
            timeline.add(instant)
            if instant < anchor_time and (before is None or instant > before[0]):
                before = (instant, row['source_event_id'])
            if instant > anchor_time and (after is None or instant < after[0]):
                after = (instant, row['source_event_id'])
        neighbors = []
        for direction, candidate in (('前文', before), ('后文', after)):
            if candidate is None or abs(candidate[0] - anchor_time) > timedelta(minutes=30):
                continue
            row = db.execute(
                "SELECT * FROM history_events WHERE app=? AND channel=? AND source_event_id=?",
                (app_id, channel, candidate[1]),
            ).fetchone()
            neighbors.append((direction, row, hit['role']))
        return neighbors


def recall_for_turn(application, agent_id, app_id, query, channel="main", *,
                    visible_memory_ids=(), visible_archive_ids=(), visible_texts=(), host_context=None,
                    legacy_graph_db_path=None, max_memory=None, max_archive=None,
                    memory_rows=None, index_path=None, indexed_memory_rows=None, cards_only=False):
    archive = application.archive(agent_id, app_id, channel)  # 查询为空也必须先授权
    if not isinstance(query, str) or len(query) > MAX_TURN_CHARS:
        raise ValueError(f"本轮问题须为不超过 {MAX_TURN_CHARS} 字符的文本")
    bounded = max_memory is not None or max_archive is not None
    for limit in (max_memory, max_archive):
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 2):
            raise ValueError("可信召回数量须为 1 或 2")
    result = dict(rendered="", memories=[], archive=[], errors=[])
    if preference_change(query) or (len(query) > 200 and preference_change(question_focus(query))):
        result['preference_correction'] = True
        return result
    previous = trusted_previous_text(archive, agent_id, app_id, channel, host_context)
    earlier = None
    utterance = query
    if previous is not None:
        prior_ref = host_context['previous_assistant_ref']
        user_ref = prior_ref.get('acknowledged_user_ref') or {}
        if acknowledged_followup(query, previous) and user_ref.get('source_event_id') in visible_archive_ids:
            earlier = trusted_previous_text(archive, agent_id, app_id, channel, host_context, role='user')
        query = prior_assistant_query(query, earlier or previous)
        visible_archive_ids = (*visible_archive_ids, prior_ref['source_event_id'])
        visible_texts = (*visible_texts, previous)
        if earlier is not None:
            visible_texts = (*visible_texts, earlier)
    if query == utterance and reference_request(query):
        result['unresolved_reference'] = True
        return result
    scoped_quote = current_conversation_quote(query)
    if (scoped_quote and previous is None) or (cards_only and original_quote_request(query)):
        return result
    original = recover_original(archive, application, agent_id, app_id, channel, query,
                                set(visible_archive_ids), visible_texts,
                                host_context['conversation_id'] if scoped_quote and previous is not None else None)
    if original is not None:
        if scoped_quote and trusted_previous_text(archive, agent_id, app_id, channel, host_context) != previous:
            return result
        return original
    # 指定本人、时间或字面锚点的引语查证失败，普通相关资料不能替代原话。
    if constrained_quote_request(query):
        return result
    focus = question_focus(query)
    terms = query_terms(focus)
    if not terms:
        return result
    path = application.memory_path(agent_id, app_id, channel)
    def embedding_warning(exc):
        if application.providers.status("embedding")["config"].get("mode") == "local":
            application.providers.local_errors["embedding"] = type(exc).__name__
        result["errors"].append(dict(module="embedding", code="embedding_unavailable",
                                     error=type(exc).__name__))

    try:
        model_id, embedder = configured_model(application.providers, "embedding")
        if embedder is None:
            raise ProviderUnavailable("embedding disabled or unconfigured")
        embedder = AvailableEmbedder(embedder)
    except ProviderUnavailable as exc:
        embedding_warning(exc)
        model_id, embedder = "unavailable", None
    # 可信进程内宿主可提供只读候选与派生索引路径；独立产品仍按 registry 隔离。
    if index_path is None:
        with closing(sqlite3.connect(application.registry.registry_path)) as db:
            scope = db.execute("SELECT rowid FROM sources WHERE agent_id=? AND app_id=? AND channel=?",
                               (agent_id, app_id, channel)).fetchone()[0]
        index_path = path.with_name(f"recall_{scope}.sqlite")
    read_rows = ((lambda: memory_rows(visible_archive_ids)) if memory_rows is not None
                 else (lambda: scoped_rows(path, app_id, channel, archive.path, agent_id)))
    if indexed_memory_rows is not None:
        # 宿主旧索引用本地模型的原始名称，非 Provider 配置的 local: 前缀。
        config = application.providers.status('embedding')['config']
        if embedder is not None and config.get('mode') == 'local':
            model_id = config['model']
        index = RetrievalIndex.open_read_only(str(index_path), memory_db_path=str(path),
                                             embedding_model=model_id, embedder=embedder)
        try:
            candidates = index.search(focus, top_k=24, pool=24, lexical_queries=terms)
        except ProviderUnavailable as exc:
            embedding_warning(exc)
            index.embedder = None
            candidates = index.search(focus, top_k=24, pool=24, lexical_queries=terms)
        read_rows = lambda: indexed_memory_rows(candidates, visible_archive_ids)
        rows, refs, suppressed_raw = read_rows()
    else:
        index = RetrievalIndex(str(index_path),
                               memory_db_path=str(path), embedding_model=model_id, embedder=embedder)
        expected = index._generation('memory_card')
        rows, refs, suppressed_raw = read_rows()
        try:
            ensure_current_index(index, rows, embedder, expected_generation=expected)
            candidates = index.search(focus, top_k=12, pool=24, lexical_queries=terms) if rows else []
        except ProviderUnavailable as exc:
            embedding_warning(exc)
            # Re-read under a NEW fence: no stale vector/model reuse and no model retry.
            index.embedder = None
            expected = index._generation('memory_card')
            rows, refs, suppressed_raw = read_rows()
            ensure_current_index(index, rows, None, expected_generation=expected)
            candidates = index.search(focus, top_k=12, pool=24, lexical_queries=terms) if rows else []
    seen_cards, seen_raw = set(visible_memory_ids), set(visible_archive_ids)
    visible = [value for t in visible_texts if (value := normalized(t))]
    texts = []

    def already_present(text):
        # 插件也会传入问题本身。主题词或一句“好”不能证明整条候选已经展示。
        return any(text in t for t in visible) or any(text in t or t in text for t in texts)
    current = {r['source_key']: r for r in rows}
    named_titles = separately_named_titles(focus, rows)
    pool = []
    asks_history = any(cue in query for cue in
                       ('之前', '以前', '上次', '那次', '当时', '还记得', '记不记得', '曾经'))
    # 宿主 normal 仅在追溯历史时查 Archive；意图召回由宿主传入数量上限。
    archive_allowed = (indexed_memory_rows is None or bounded or asks_history
                       or '原话' in query
                       or bool(re.search(r'(?:说过|提过).{0,8}(?:什么|啥|哪)', query))
                       or bool(re.search(r'(?:昨天|前天|刚才|那天).{0,32}(?:聊|谈|说|提|讲)'
                                         r'.{0,40}(?:什么|啥|哪|怎么|谁|呢)', query)))
    generic = STOP | set("东西 这个 那个 这些 那些 事情 生活 工作 学习 游戏 音乐 电影 朋友 家人 喜欢 讨厌 今天 昨天 明天 最近 时间 地点 人物 活动 记忆 聊天 用户 助手 心情 感觉 关系 问题 内容 信息 记录 日常 兴趣 爱好 经历 计划 目标 偏好".split())
    short_chat = not archive_allowed and len(normalized(query)) <= 12
    topic_terms = {t for t in terms if len(t) >= 2 and t not in generic}

    def has_chat_topic(row):
        if not short_chat:
            return True
        # 具体查问可由正文直接回答；单独一个情绪或动作词仍沿用标题/关键词限制。
        if (topic_terms and re.search(r'哪|什么|谁|多少|几', query)
                and all(t in row['text'].lower() for t in terms)):
            return True
        first_line = row['text'].partition('\n')[0]
        # 无标题旧卡保留以完整复合主题开头的召回，正文零散命中不足以选卡。
        if len(topic_terms) >= 2 and first_line.lower().startswith(query.strip().lower()):
            return True
        if row['title'] and not topic_terms.isdisjoint(tokenize(first_line)):
            return True
        return not topic_terms.isdisjoint(k.strip().lower() for k in _row_keywords(row)
                                          if isinstance(k, str))

    for item in candidates:
        row = current.get(item['source_key'])
        if row is None or item["text"] != row["text"] or item["provenance"]["revision_id"] != row["provenance"]["revision_id"]:
            continue
        if row["source_id"] in seen_cards or any(key[3] in seen_raw for key in refs.get(row['source_key'], ())):
            continue
        lexical = sum(t in row["text"].lower() for t in terms) / len(terms)
        cosine = max((float(r.rsplit(":", 1)[1]) for r in item["retrieval_reasons"] if r.startswith("vector_rank:")), default=0)
        # 宿主旧索引按词拆查询时，命中一个泛词不应直接注入整张旧卡。
        # 非宿主独立召回保持原有词面规则。
        lexical_allowed = lexical >= 0.5 if indexed_memory_rows is not None else bool(lexical)
        semantic_floor = 0.59 if indexed_memory_rows is not None and asks_history else 0.65
        if (row.get('title') or '').strip() in named_titles or ((lexical_allowed or cosine >= semantic_floor) and has_chat_topic(row)):
            memory = dict(kind='memory', id=row['source_id'], text=row['text'],
                          source_key=row['source_key'], revision_id=row['provenance']['revision_id'],
                          score=max(lexical, cosine))
            if 'extension_id' in row['provenance']:
                memory.update({k: row['provenance'][k] for k in
                               ('extension_id', 'base_revision_id', 'evidence_refs')})
            pool.append(memory)
    # 用户明确提到的唯一非泛化旧关键词可补候选；语义解释仍交宿主聊天模型。
    # 有界索引候选不代表整库，不能据此推断关键词在授权全库中唯一。
    keyword_rows = {}
    for row in rows:
        if 'extension_id' in row['provenance'] or row['status'] in {'trashed', 'archived', 'retired'}:
            continue
        for keyword in set(k.lower().strip() for k in _row_keywords(row) if isinstance(k, str)):
            if len(keyword) < 2 or keyword in generic:
                continue
            if re.search(r'(?<![a-z0-9_])' + re.escape(keyword) + r'(?![a-z0-9_])', query.lower()):
                keyword_rows.setdefault(keyword, []).append(row)
    for matches in ([] if indexed_memory_rows is not None or re.fullmatch(r"[\u4e00-\u9fff]{2}", query) else keyword_rows.values()):
        # 可见过滤前判断唯一性，不能靠排除已见卡制造确定性。
        if len(matches) != 1:
            continue
        row = matches[0]
        if (row['source_id'] in seen_cards
                or any(k[3] in seen_raw for k in refs.get(row['source_key'], ()))
                or any(p['source_key'] == row['source_key'] for p in pool)):
            continue
        pool.append(dict(kind='memory', id=row['source_id'], text=row['text'],
                         source_key=row['source_key'],
                         revision_id=row['provenance']['revision_id'], score=1.0))
    raw = {}
    for term in terms if archive_allowed and not cards_only else ():
        for row in application.search(agent_id, app_id, term, channel, limit=12):
            raw[row["source_event_id"]] = row
    for source_id, row in raw.items():
        if (agent_id, app_id, channel, source_id, row["version"]) in suppressed_raw or source_id in seen_raw:
            continue
        scoring_text = row["text"][:300] if indexed_memory_rows is not None else row["text"]
        score = sum(t in scoring_text.lower() for t in terms) / len(terms)
        # 旧宿主原话的单个泛词命中不能把整段无关历史注入 Context。
        # 与上面宿主 Memory 的最低词面覆盖率一致；独立产品原规则不变。
        if score > 0 and (indexed_memory_rows is None or score >= 0.5):
            pool.append(dict(kind="archive", id=source_id, version=row["version"], text=row["text"], score=score))
    pool = [p for p in pool if not already_present(normalized(p['text']))]
    if named_titles and not bounded:
        pool = [p for p in pool if p['kind'] == 'memory'
                and current[p['source_key']]['title'].strip() in named_titles]
    pool.sort(key=lambda p: -p["score"])
    pool = pool[:12]
    if pool:
        try:
            rerank_id, reranker = configured_model(application.providers, "rerank")
            if reranker is not None:
                scores = list(reranker(focus, [p["text"] for p in pool]))
                if len(scores) != len(pool) or any(not math.isfinite(float(s)) for s in scores):
                    raise ValueError("rerank 分数无效")
                pool = [p for _, p in sorted(zip(scores, pool), key=lambda pair: -float(pair[0]))]
        except Exception as exc:
            if application.providers.status("rerank")["config"].get("mode") == "local":
                application.providers.local_errors["rerank"] = type(exc).__name__
            result["errors"].append(dict(module="rerank", error=str(exc)))
    # M04：只在普通 Related 完全无候选时，用已核验正式卡片的精确关键词兜底。
    if indexed_memory_rows is None and not pool and re.fullmatch(r"[\u4e00-\u9fff]{2}", query) and query not in generic:
        matches = []
        for row in rows:
            provenance = row['provenance']
            if 'extension_id' in provenance or row['status'] in {'trashed', 'archived', 'retired'}:
                continue
            if query in _row_keywords(row):
                matches.append(row)
        # 先判断整个授权范围内的唯一性，不能过滤已见卡后制造唯一候选。
        if len(matches) == 1:
            row = matches[0]
            if (row['source_id'] not in seen_cards
                    and not any(key[3] in seen_raw for key in refs.get(row['source_key'], ()))
                    and not already_present(normalized(row['text']))):
                pool.append(dict(kind='memory', id=row['source_id'], text=row['text'],
                                 source_key=row['source_key'],
                                 revision_id=row['provenance']['revision_id'], score=0.0))
    # 模型调用可能很慢；输出前重核来源和正式修订，不把期间失效的候选注入。
    if pool:
        latest, refs, suppressed_raw = read_rows()
        current = {r['source_key']: r for r in latest}
        checked = []
        with closing(sqlite3.connect(archive.path)) as db:
            for item in pool:
                if item["kind"] == "memory":
                    row = current.get(item['source_key'])
                    if (row and row['text'] == item['text']
                            and row['provenance']['revision_id'] == item['revision_id']
                            and row['provenance'].get('extension_id') == item.get('extension_id')
                            and ((row.get('title') or '').strip() in named_titles or has_chat_topic(row))
                            and (indexed_memory_rows is None or not any(
                                key[3] in seen_raw for key in refs.get(row['source_key'], ())))):
                        if 'extension_id' in item:
                            item['evidence_refs'] = row['provenance']['evidence_refs']
                        if row['provenance'].get('legacy_unverified'):
                            item.update(ownership='unknown', evidence_resolution='legacy_unverified',
                                        verified=False)
                        item['source_refs'] = sorted(refs.get(row['source_key'], ()))
                        checked.append(item)
                else:
                    row = db.execute("SELECT version,deleted FROM history_events WHERE app=? AND channel=? "
                                     "AND source_event_id=?", (app_id, channel, item["id"])).fetchone()
                    key = (agent_id, app_id, channel, item["id"], item["version"])
                    if row and row[0] == item["version"] and not row[1] and key not in suppressed_raw:
                        checked.append(item)
        pool = checked
    # Provider 等待期间前驱若已被修订/删除，不继续使用失效指代。
    if previous is not None and trusted_previous_text(archive, agent_id, app_id, channel, host_context) != previous:
        return result
    if earlier is not None and trusted_previous_text(archive, agent_id, app_id, channel, host_context, role='user') != earlier:
        return result
    if named_titles and not bounded:
        # 一对象一张当前卡；同名多张时不替用户消歧，也不将两张卡标成有事实关系。
        selected = []
        for title in named_titles:
            matches = [item for item in pool if item['kind'] == 'memory'
                       and current[item['source_key']]['title'].strip() == title]
            if len(matches) == 1:
                selected.extend(matches)
        pool = selected
    primary = next((item for item in pool if item['kind'] == 'memory'), None)
    if primary and not bounded:
        evidence = preference_evidence(archive, path, app_id, channel, query, primary)
        if evidence is primary:
            # 泛搜到的其他说话者、临时选择和早期引语不能充当当前习惯。
            pool = [primary]
        elif evidence is not None:
            result['preference_correction'] = True
            if not evidence:
                return result
            source = (agent_id, app_id, channel, evidence['id'], evidence['version'])
            line = f"[用户原话 {evidence['id']} v{evidence['version']} {evidence['occurred_at']}] {evidence['text']}"
            if (not cards_only and source not in suppressed_raw and evidence['id'] not in seen_raw
                    and not already_present(normalized(evidence['text'])) and len(line) <= 700):
                result.update(rendered=line, archive=[evidence])
            return result
    lines = []
    for item in pool:
        key = "memories" if item["kind"] == "memory" else "archive"
        text = normalized(item["text"])
        if (len(result[key]) >= ((max_memory or len(named_titles) or 1) if key == "memories" else (max_archive or 1))
                or already_present(text)):
            continue
        label = "记忆" if key == "memories" else ("历史记录" if original_quote_request(query) else "历史原话")
        if item.get('evidence_resolution') == 'legacy_unverified':
            label = '旧记忆候选，证据未核实'
        identity = (f"{item['id']} 扩展 {item['extension_id']} 基础修订 {item['base_revision_id']}"
                    if 'extension_id' in item else str(item['id']))
        fragment = excerpt(item['text'], terms)
        if already_present(normalized(fragment)):
            continue
        line = f"[{label} {identity}] {fragment}"
        if len("\n".join([*lines, line])) > 700:
            continue
        lines.append(line)
        texts.append(text)
        result[key].append(item)
    result["rendered"] = "\n".join(lines)[:700]
    if bounded or cards_only or named_titles:
        return result
    # 只补充本轮已复核的独立证据，不把检索相关性解释成事实关系。
    if (len(result['memories']) == 1
            and re.search(r"两(?:个|件|次|条)|前后|变化|分别", query)):
        primary = result['memories'][0]
        for item in pool:
            if (item.get('evidence_resolution') == 'legacy_unverified'
                    or primary.get('evidence_resolution') == 'legacy_unverified'
                    or item['kind'] != 'memory' or item['id'] == primary['id']
                    or not any(t in item['text'].lower() for t in terms)):
                continue
            first_refs = refs.get(primary['source_key'], set())
            second_refs = refs.get(item['source_key'], set())
            # 无来源或共用原始事件不能算第二条独立证据。
            if (not first_refs or not second_refs
                    or {r[:4] for r in first_refs} & {r[:4] for r in second_refs}):
                continue
            text = normalized(item['text'])
            if already_present(text):
                continue
            line = (f"[独立来源未合并：记忆 {primary['id']} 修订 {primary['revision_id']}；"
                    f"记忆 {item['id']} 修订 {item['revision_id']}] {excerpt(item['text'], terms)}"
                    "（关联与冲突均未据此判定）")
            if len(result['rendered']) + 1 + len(line) <= 700:
                result['rendered'] += '\n' + line
                result['memories'].append(item)
                texts.append(text)
                break
    # 已存图和关键词派生候选保留不同证据标签，不把相似性冒充图边。
    if (len(result['memories']) == 1
            and re.search(r"两(?:个|件|次|条)|前后|分别|比较|对比", query)):
        primary = result['memories'][0]
        related = index.stored_related_candidates(
            primary, current.values(), legacy_graph_db_path=legacy_graph_db_path)
        related += index.derived_related_candidates(primary, current.values(),
                                                    excluded_keywords=generic)
        for row, relation in related:
            # 已存边也只是“相关”而非事实合并；至少有本轮文字锚点。
            if (relation['kind'] == 'stored_graph'
                    and not any(k in query for k in relation['shared_keywords'])
                    and not any(t in row['text'].lower() for t in terms)):
                continue
            first_refs = refs.get(primary['source_key'], set())
            second_refs = refs.get(row['source_key'], set())
            text = normalized(row['text'])
            if (row['provenance'].get('legacy_unverified')
                    or primary.get('evidence_resolution') == 'legacy_unverified'
                    or row['source_id'] in seen_cards or not first_refs or not second_refs
                    or {r[:4] for r in first_refs} & {r[:4] for r in second_refs}
                    or any(r[3] in seen_raw for r in second_refs)
                    or already_present(text)):
                continue
            label = ('已记录关联候选' if relation['kind'] == 'stored_graph'
                     else '共享关键词派生候选（未保存关联图）')
            line = (f"[{label}：记忆 {primary['id']} "
                    f"修订 {primary['revision_id']}；记忆 {row['source_id']} "
                    f"修订 {row['provenance']['revision_id']}] {excerpt(row['text'], terms)}"
                    "（两条独立来源；尚未证实关系类型，不推断因果或合并事实）")
            if len(result['rendered']) + 1 + len(line) > 700:
                continue
            relation.update(source_a=sorted(first_refs), source_b=sorted(second_refs))
            result['memories'].append(dict(
                kind='memory', id=row['source_id'], source_key=row['source_key'],
                revision_id=row['provenance']['revision_id'], text=row['text'], score=0.0,
                related_evidence=relation))
            result['rendered'] += '\n' + line
            texts.append(text)
            break
    if re.search(r"以前|之前|后来|修改|改过|更正|纠正|前后|变化", query):
        from thalemora.lifecycle.temporal import temporal_context_hint
        for item in result['memories']:
            if 'extension_id' in item or item.get('evidence_resolution') == 'legacy_unverified':
                continue
            hint = temporal_context_hint(query + (' 改过' if '修改' in query else ''), dict(
                text=item['text'], provenance=dict(card_id=item['id'], revision_id=item['revision_id'])), path)
            if hint:
                line = f"[记忆 {item['id']} 修订 {item['revision_id']}] {hint['note']}"
                if len(result['rendered']) + 1 + len(line) <= 700:
                    result['rendered'] += '\n' + line
    # 明确追问更正历史时，才按来源有效性补前一版原话；普通问答绝不混入旧事实。
    if re.search(r"旧版本|旧记录|旧记忆|原话|(?:以前|之前|原来).*(?:说|什么|怎么|哪|几|是)|更正.*(?:经过|过程|历史)", query):
        for item in result['memories'][:1]:
            if (item.get('kind') != 'memory' or 'extension_id' in item
                    or item.get('evidence_resolution') == 'legacy_unverified'):
                continue
            with closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                old = db.execute(
                    "SELECT r.id,r.content FROM card_revisions r JOIN cards c ON c.id=r.card_id "
                    "JOIN card_revisions current ON current.id=c.current_revision_id "
                    "WHERE c.id=? AND c.current_revision_id=? AND r.revision_no<current.revision_no "
                    "AND r.content!=current.content ORDER BY r.revision_no DESC LIMIT 1",
                    (item['id'], item['revision_id'])).fetchone()
            if old is None:
                continue
            # 旧修订不是另一条现役事实；仅当其原文来源仍存在且同域授权时展示。
            old_evidence = next((e for e in application.evidence(
                agent_id, app_id, item['id'], channel=channel, limit=100)
                if e.get('revision_id') == old['id']
                and e.get('resolution') == 'current'
                and e.get('current_source_text')), None)
            if old_evidence is None:
                continue
            try:
                old_ref = json.loads(old_evidence['source_ref'])
            except (ValueError, TypeError):
                continue
            source_id = old_ref.get('source_event_id') if isinstance(old_ref, dict) else None
            if not source_id or source_id in seen_raw:
                continue
            quote = old_evidence['current_source_text']
            line = (f"[记忆 {item['id']} 历史修订 {old['id']}（已被更正，不代表当前事实；"
                    f"旧原话 {source_id} v{old_ref.get('version')}）] "
                    f"旧记录：{old['content'][:120]}；当时原话：{quote[:160]}")
            if len(result['rendered']) + 1 + len(line) <= 700:
                result['rendered'] += '\n' + line
                result.setdefault('history_revisions', []).append(dict(
                    card_id=item['id'], revision_id=old['id'],
                    source_event_id=source_id, source_version=old_ref.get('version')))
                break
    # 先完整保留原有 Memory/Raw 渲染，再使用剩余预算；邻居不进入候选/重排。
    anchors = list(result['archive'])
    seen_raw.update(item['id'] for item in anchors)
    for anchor in anchors:
        if len(result['rendered']) >= 700:
            break
        for direction, row, role in archive_context(archive, app_id, channel, anchor):
            source_key = (agent_id, app_id, channel, row['source_event_id'], row['version'])
            text = normalized(row['text'] or '')
            if (row['deleted'] or row['role'] not in {'user', 'assistant'} or not text
                    or source_key in suppressed_raw or row['source_event_id'] in seen_raw
                    or already_present(text)):
                continue
            line = (f"[原话 {anchor['id']} ({role}) 的{direction} "
                    f"{row['source_event_id']} v{row['version']} ({row['role']})] {row['text']}")
            if len(result['rendered']) + 1 + len(line) > 700:
                continue
            result['rendered'] += '\n' + line
            result['archive'].append(dict(kind='archive', id=row['source_event_id'],
                                          version=row['version'], text=row['text']))
            seen_raw.add(row['source_event_id'])
            texts.append(text)
    if statement(query) is not None:
        latest, latest_refs, _ = read_rows()
        hint = compare_statement(query, latest)
        if hint:
            row = next(r for r in latest if r['source_id'] == hint['id'])
            if (not row['provenance'].get('legacy_unverified')
                    and row['source_id'] not in set(visible_memory_ids)
                    and not any(k[3] in set(visible_archive_ids)
                                for k in latest_refs.get(row['source_key'], ()))
                    and not any(normalized(row['text']) in t
                                or normalized(hint['old_statement']) in t
                                for t in visible)):
                line = render_hint(hint)
                rendered = '\n'.join(filter(None, (result['rendered'], line)))
                if len(rendered) <= 700:
                    result['rendered'] = rendered
                    result['fact_hints'] = [hint]
    return result
