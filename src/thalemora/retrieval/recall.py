"""单轮 Related 召回：正式 Memory 和独立 Archive 仍是唯一来源。"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
import re
import sqlite3

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


def normalized(text):
    return re.sub(r"\W+", "", text.lower())


def original_quote_request(query):
    return bool(re.search(r"(?:当时|那时|以前|之前|上次).{0,24}原话|原话.{0,12}(?:怎么说|是什么)|我(?:当时|那时|以前|之前|上次).{0,50}提过.{0,8}(?:什么|啥)", query))


def recover_original(archive, application, agent_id, app_id, channel, query,
                     visible_ids, visible_texts):
    """M07：只用明确字面锚点恢复 user 原文；不提升 Memory 为原话。"""
    if not original_quote_request(query):
        return None
    quoted = re.findall(r'[“「"]([^”」"]{4,30})[”」"]', query)
    years = re.findall(r'(?:认识|相处|交往|学了|用了|玩了)(?:了|有)?[一二三四五六七八九十百0-9]{1,3}年', query)
    bare = re.fullmatch(r'我(?:当时|那时|以前|之前|上次)提过(.{4,30}?)(?:什么|啥)[？?]?', query)
    anchors = quoted or years or ([bare[1].strip()] if bare else [])
    if len(anchors) != 1 or len(normalized(anchors[0])) < 4 or not query_terms(anchors[0]):
        return None
    anchor = anchors[0]
    # 未实现的相对窗口与年份不能偷偷退化成无时间约束的原话命中。
    if re.search(r'去年|今年|前年|上个?月|这个月|本月|最近|过去|近[一二三四五六七八九十0-9]|上周|本周|昨天|前天|今天|\d{4}年|\d{4}[-/]|[0-9一二三四五六七八九十]+[日号]', query):
        return None
    month_pattern = r'(?<![0-9])(?:1[0-2]|[1-9]|十一|十二|[一二三四五六七八九十])月'
    words = dict(zip('一二三四五六七八九十', range(1, 11)), 十一=11, 十二=12)
    def months(text):
        return {int(m[:-1]) if m[:-1].isdigit() else words[m[:-1]]
                for m in re.findall(month_pattern, text)}
    requested = months(query)
    if len(requested) > 1:
        return None
    talking_month = bool(re.search(month_pattern + r'.{0,9}(?:说|聊|提|讲|原话)', query))
    hits = application.search(agent_id, app_id, anchor, channel, limit=36)
    with closing(sqlite3.connect(archive.path)) as db:
        db.row_factory = sqlite3.Row
        for hit in hits:
            if hit['source_event_id'] in visible_ids:
                continue
            row = db.execute('SELECT * FROM history_events WHERE app=? AND channel=? AND source_event_id=?',
                             (app_id, channel, hit['source_event_id'])).fetchone()
            if (not row or row['deleted'] or row['role'] != 'user'
                    or row['version'] != hit['version'] or row['text'] != hit['text']
                    or anchor not in row['text'] or len(row['text']) > 240):
                continue
            text = row['text']
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
            if requested and (requested != {instant.month} if talking_month else not requested <= months(text)):
                continue
            norm = normalized(text)
            if any(normalized(t) and (norm in normalized(t) or normalized(t) in norm) for t in visible_texts):
                continue
            item = dict(kind='archive', id=row['source_event_id'], version=row['version'],
                        text=text, role='user', occurred_at=row['occurred_at'])
            line = f"[用户原话 {item['id']} v{item['version']} {item['occurred_at']}] {text}"
            if len(line) <= 700:
                return dict(rendered=line, memories=[], archive=[item], errors=[])
    return None


def prior_assistant_query(query, previous):
    """只解析明确编号的指代或对备选项的否认；无法确定就保留原问题。"""
    ordinal = re.fullmatch(r"第([一二三四五六七八九1-9])个(?:呢|怎么样)?[？?]?", query.strip())
    if ordinal:
        number = ordinal[1]
        wanted = int(number) if number.isdigit() else "一二三四五六七八九".index(number) + 1
        options = re.findall(r"^\s*([1-9一二三四五六七八九])[、.．）)]\s*(.+)$", previous, re.M)
        numbers = [int(n) if n.isdigit() else "一二三四五六七八九".index(n) + 1 for n, _ in options]
        if numbers == list(range(1, len(options) + 1)) and wanted <= len(options):
            text = options[wanted - 1][1].strip()
            if 0 < len(text) <= 200:
                return text
    # 被否认的选项不能当作用户偏好；只用用户明说的动作/主题查已有记忆。
    denial = re.fullmatch(r"我(?:从来|根本|压根)?(?:都)?(?:没|没有|不曾|不)"
                          r"([玩看听吃喝用去读买做打])过?[这那](?:两个|两|俩|几)"
                          r"(?:款|部|个|种|本)?([\u4e00-\u9fff]{2,5})[。！？!?]?", query.strip())
    if denial and re.search(r"还是|或者|或是|\bor\b", previous, re.I):
        verb, topic = denial.groups()
        if topic not in {"东西", "事情", "这个", "那个"}:
            return f"我平常{verb}什么{topic}？"
    return query


def trusted_previous_text(archive, agent_id, app_id, channel, host_context):
    """宿主确认前驱关系；档案复核完整来源、版本、正文与真实窗口，不补造 epoch。"""
    if not host_context:
        return None
    ref = host_context.get('previous_assistant_ref') or {}
    if ((ref.get('agent_id'), ref.get('app_id'), ref.get('channel')) != (agent_id, app_id, channel)
            or not host_context.get('conversation_id') or not host_context.get('current_epoch_id')):
        return None
    with closing(sqlite3.connect(archive.path)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM history_events WHERE app=? AND channel=? AND source_event_id=?",
                         (app_id, channel, ref.get('source_event_id'))).fetchone()
    if (row and not row['deleted'] and row['role'] == 'assistant'
            and row['version'] == ref.get('version') and row['text'] == ref.get('text')
            and row['text'] and row['conversation_id'] == host_context['conversation_id']
            and row['epoch_id'] == host_context['current_epoch_id']):
        return row['text']
    return None


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
    with closing(sqlite3.connect(archive_path)) as source_db:
        for item in evidence:
            card_id = item["card_id"]
            state = resolve_archive_evidence(source_db, item, agent_id, app_id, channel)
            key = state["source_key"]
            if key is not None:
                refs.setdefault(card_id, set()).add(key)
            status = state["resolution"]
            if status == "manual_revision" and (item["origin"], item["change_type"]) != ("manual_ui", "manual_edit"):
                status = "unsupported"
            states.setdefault(item["revision_id"], []).append(status)
    ids = {card_id for card_id, scope in scopes.items() if belongs(scope, app_id, channel)}
    by_extension = {e['extension_id']: e for e in extensions}
    unit_refs = {}
    for card_id in ids:
        unit_refs[f'memory:{card_id}'] = refs.get(card_id, set())
    for extension in extensions:
        key = f"memory:{extension['card_id']}:extension:{extension['extension_id']}"
        unit_refs[key] = {tuple(e['source_key']) for e in extension['evidence_refs'] if e['source_key']}
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
            if row['source_id'] in ids and statuses and all(s in {'current', 'manual_revision'} for s in statuses):
                rows.append(row)
    # 历史/停用扩展也只抑制确切来源版本，不让原话兜底绕过停用。
    suppressed_raw = set().union(*unit_refs.values()) if unit_refs else set()
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
                    visible_memory_ids=(), visible_archive_ids=(), visible_texts=(), host_context=None):
    archive = application.archive(agent_id, app_id, channel)  # 查询为空也必须先授权
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError("本轮问题须为不超过 200 字符的文本")
    previous = trusted_previous_text(archive, agent_id, app_id, channel, host_context)
    if previous is not None:
        query = prior_assistant_query(query, previous)
        visible_archive_ids = (*visible_archive_ids, host_context['previous_assistant_ref']['source_event_id'])
        visible_texts = (*visible_texts, previous)
    original = recover_original(archive, application, agent_id, app_id, channel, query,
                                set(visible_archive_ids), visible_texts)
    if original is not None:
        return original
    terms = query_terms(query)
    result = dict(rendered="", memories=[], archive=[], errors=[])
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
    # sources 的现有稳定行号仅用于派生文件名，分开 app 的向量缓存。
    with closing(sqlite3.connect(application.registry.registry_path)) as db:
        scope = db.execute("SELECT rowid FROM sources WHERE agent_id=? AND app_id=? AND channel=?",
                           (agent_id, app_id, channel)).fetchone()[0]
    index = RetrievalIndex(str(path.with_name(f"recall_{scope}.sqlite")),
                           memory_db_path=str(path), embedding_model=model_id, embedder=embedder)
    expected = index._generation('memory_card')
    rows, refs, suppressed_raw = scoped_rows(path, app_id, channel, archive.path, agent_id)
    try:
        ensure_current_index(index, rows, embedder, expected_generation=expected)
        candidates = index.search(query, top_k=12, pool=24, lexical_queries=terms) if rows else []
    except ProviderUnavailable as exc:
        embedding_warning(exc)
        # Re-read under a NEW fence: no stale vector/model reuse and no model retry.
        index.embedder = None
        expected = index._generation('memory_card')
        rows, refs, suppressed_raw = scoped_rows(path, app_id, channel, archive.path, agent_id)
        ensure_current_index(index, rows, None, expected_generation=expected)
        candidates = index.search(query, top_k=12, pool=24, lexical_queries=terms) if rows else []
    seen_cards, seen_raw = set(visible_memory_ids), set(visible_archive_ids)
    texts = [normalized(t) for t in visible_texts if normalized(t)]
    current = {r['source_key']: r for r in rows}
    pool = []
    for item in candidates:
        row = current.get(item['source_key'])
        if row is None or item["text"] != row["text"] or item["provenance"]["revision_id"] != row["provenance"]["revision_id"]:
            continue
        if row["source_id"] in seen_cards or any(key[3] in seen_raw for key in refs.get(row['source_key'], ())):
            continue
        lexical = sum(t in row["text"].lower() for t in terms) / len(terms)
        cosine = max((float(r.rsplit(":", 1)[1]) for r in item["retrieval_reasons"] if r.startswith("vector_rank:")), default=0)
        if lexical or cosine >= 0.65:
            memory = dict(kind='memory', id=row['source_id'], text=row['text'],
                          source_key=row['source_key'], revision_id=row['provenance']['revision_id'],
                          score=max(lexical, cosine))
            if 'extension_id' in row['provenance']:
                memory.update({k: row['provenance'][k] for k in
                               ('extension_id', 'base_revision_id', 'evidence_refs')})
            pool.append(memory)
    raw = {}
    for term in terms:
        for row in application.search(agent_id, app_id, term, channel, limit=12):
            raw[row["source_event_id"]] = row
    for source_id, row in raw.items():
        if (agent_id, app_id, channel, source_id, row["version"]) in suppressed_raw or source_id in seen_raw:
            continue
        score = sum(t in row["text"].lower() for t in terms) / len(terms)
        if score:
            pool.append(dict(kind="archive", id=source_id, version=row["version"], text=row["text"], score=score))
    pool = [p for p in pool if not any(normalized(p["text"]) in t or t in normalized(p["text"]) for t in texts)]
    pool.sort(key=lambda p: -p["score"])
    pool = pool[:12]
    if pool:
        try:
            rerank_id, reranker = configured_model(application.providers, "rerank")
            if reranker is not None:
                scores = list(reranker(query, [p["text"] for p in pool]))
                if len(scores) != len(pool) or any(not math.isfinite(float(s)) for s in scores):
                    raise ValueError("rerank 分数无效")
                pool = [p for _, p in sorted(zip(scores, pool), key=lambda pair: -float(pair[0]))]
        except Exception as exc:
            if application.providers.status("rerank")["config"].get("mode") == "local":
                application.providers.local_errors["rerank"] = type(exc).__name__
            result["errors"].append(dict(module="rerank", error=str(exc)))
    # M04：只在普通 Related 完全无候选时，用已核验正式卡片的精确关键词兜底。
    generic = STOP | set("东西 这个 那个 这些 那些 事情 生活 工作 学习 游戏 音乐 电影 朋友 家人 喜欢 讨厌 今天 昨天 明天 最近 时间 地点 人物 活动 记忆 聊天 用户 助手 心情 感觉 关系 问题 内容 信息 记录 日常 兴趣 爱好 经历 计划 目标 偏好".split())
    if not pool and re.fullmatch(r"[\u4e00-\u9fff]{2}", query) and query not in generic:
        matches = []
        for row in rows:
            provenance = row['provenance']
            if 'extension_id' in provenance or row['status'] in {'trashed', 'archived', 'retired'}:
                continue
            keywords = provenance.get('keywords') or ''
            if isinstance(keywords, str):
                try:
                    keywords = json.loads(keywords)
                except ValueError:
                    keywords = re.split(r"[,，、;；\s]+", keywords)
            if isinstance(keywords, list) and query in keywords:
                matches.append(row)
        # 先判断整个授权范围内的唯一性，不能过滤已见卡后制造唯一候选。
        if len(matches) == 1:
            row = matches[0]
            if (row['source_id'] not in seen_cards
                    and not any(key[3] in seen_raw for key in refs.get(row['source_key'], ()))
                    and not any(normalized(row['text']) in t or t in normalized(row['text']) for t in texts)):
                pool.append(dict(kind='memory', id=row['source_id'], text=row['text'],
                                 source_key=row['source_key'],
                                 revision_id=row['provenance']['revision_id'], score=0.0))
    # 模型调用可能很慢；输出前重核来源和正式修订，不把期间失效的候选注入。
    if pool:
        latest, refs, suppressed_raw = scoped_rows(path, app_id, channel, archive.path, agent_id)
        current = {r['source_key']: r for r in latest}
        checked = []
        with closing(sqlite3.connect(archive.path)) as db:
            for item in pool:
                if item["kind"] == "memory":
                    row = current.get(item['source_key'])
                    if (row and row['text'] == item['text']
                            and row['provenance']['revision_id'] == item['revision_id']
                            and row['provenance'].get('extension_id') == item.get('extension_id')):
                        if 'extension_id' in item:
                            item['evidence_refs'] = row['provenance']['evidence_refs']
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
    lines = []
    for item in pool:
        key = "memories" if item["kind"] == "memory" else "archive"
        text = normalized(item["text"])
        if result[key] or any(text in t or t in text for t in texts):
            continue
        label = "记忆" if key == "memories" else ("历史记录" if original_quote_request(query) else "历史原话")
        identity = (f"{item['id']} 扩展 {item['extension_id']} 基础修订 {item['base_revision_id']}"
                    if 'extension_id' in item else str(item['id']))
        line = f"[{label} {identity}] {item['text'][:300]}"
        lines.append(line)
        texts.append(text)
        result[key].append(item)
    result["rendered"] = "\n".join(lines)[:700]
    # 只补充本轮已复核的独立证据，不把检索相关性解释成事实关系。
    if (len(result['memories']) == 1
            and re.search(r"两(?:个|件|次|条)|前后|变化|分别", query)):
        primary = result['memories'][0]
        for item in pool:
            if (item['kind'] != 'memory' or item['id'] == primary['id']
                    or not any(t in item['text'].lower() for t in terms)):
                continue
            first_refs = refs.get(primary['source_key'], set())
            second_refs = refs.get(item['source_key'], set())
            # 无来源或共用原始事件不能算第二条独立证据。
            if (not first_refs or not second_refs
                    or {r[:4] for r in first_refs} & {r[:4] for r in second_refs}):
                continue
            text = normalized(item['text'])
            if any(text in t or t in text for t in texts):
                continue
            line = (f"[独立来源未合并：记忆 {primary['id']} 修订 {primary['revision_id']}；"
                    f"记忆 {item['id']} 修订 {item['revision_id']}] {item['text'][:300]}"
                    "（关联与冲突均未据此判定）")
            if len(result['rendered']) + 1 + len(line) <= 700:
                result['rendered'] += '\n' + line
                result['memories'].append(item)
                texts.append(text)
                break
    if re.search(r"以前|之前|后来|修改|改过|更正|纠正|前后|变化", query):
        from thalemora.lifecycle.temporal import temporal_context_hint
        for item in result['memories']:
            if 'extension_id' in item:
                continue
            hint = temporal_context_hint(query + (' 改过' if '修改' in query else ''), dict(
                text=item['text'], provenance=dict(card_id=item['id'], revision_id=item['revision_id'])), path)
            if hint:
                line = f"[记忆 {item['id']} 修订 {item['revision_id']}] {hint['note']}"
                if len(result['rendered']) + 1 + len(line) <= 700:
                    result['rendered'] += '\n' + line
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
                    or any(text in t or t in text for t in texts)):
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
    return result
