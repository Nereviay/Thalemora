"""单轮 Related 召回：正式 Memory 和独立 Archive 仍是唯一来源。"""
from contextlib import closing
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


def recall_for_turn(application, agent_id, app_id, query, channel="main", *,
                    visible_memory_ids=(), visible_archive_ids=(), visible_texts=()):
    archive = application.archive(agent_id, app_id, channel)  # 查询为空也必须先授权
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError("本轮问题须为不超过 200 字符的文本")
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
    # 模型调用可能很慢；输出前重核来源和正式修订，不把期间失效的候选注入。
    if pool:
        latest, _, suppressed_raw = scoped_rows(path, app_id, channel, archive.path, agent_id)
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
    lines = []
    for item in pool:
        key = "memories" if item["kind"] == "memory" else "archive"
        text = normalized(item["text"])
        if result[key] or any(text in t or t in text for t in texts):
            continue
        label = "记忆" if key == "memories" else "历史原话"
        identity = (f"{item['id']} 扩展 {item['extension_id']} 基础修订 {item['base_revision_id']}"
                    if 'extension_id' in item else str(item['id']))
        line = f"[{label} {identity}] {item['text'][:300]}"
        lines.append(line)
        texts.append(text)
        result[key].append(item)
    result["rendered"] = "\n".join(lines)[:700]
    return result
