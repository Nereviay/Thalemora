"""单轮 Related 召回：正式 Memory 和独立 Archive 仍是唯一来源。"""
from contextlib import closing
import json
import math
import re
import sqlite3

from .bm25 import tokenize
from .core import RetrievalIndex
from .models import configured_model


STOP = set("我 你 他 她 它 的 了 啊 呢 吗 吧 是 在 有 和 与 就 都 也 又 很 不 一 下 个 这 那 什么 怎么 如何 哪 哪里 哪儿 现在 以前 之前 记得 请 告诉 说说 帮忙 帮 我们 你们 一下 想 要 问 问一下 看 看看 知道 知道了 明白 收到 麻烦 能 不能 能不能 可以 还 随便 没有 没事 事情 帮我 讲讲 聊聊 你好 您好 在吗 好 好的 好吧 嗯 嗯嗯 哦 哈哈 继续 谢谢 the a an is are was i you my me what where how please tell about".split())


def query_terms(query):
    return list(dict.fromkeys(t for t in tokenize(query) if t not in STOP))[:12]


def normalized(text):
    return re.sub(r"\W+", "", text.lower())


def scoped_rows(path, app_id, channel):
    """沿用 Archive scope；旧人工卡从首修订证据确认来源，不推测无来源卡。"""
    with closing(sqlite3.connect(path)) as db:
        scopes = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='archive_card_scopes'").fetchone():
            scopes = {r[0]: (r[1], r[2]) for r in db.execute("SELECT card_id,app,channel FROM archive_card_scopes")}
        refs = {}
        for card_id, revision_no, source_ref in db.execute(
                "SELECT r.card_id,r.revision_no,e.source_ref FROM card_revisions r "
                "JOIN card_evidence e ON e.revision_id=r.id ORDER BY r.revision_no"):
            try:
                ref = json.loads(source_ref)
            except (ValueError, TypeError):
                continue
            if not isinstance(ref, dict):
                continue
            if revision_no == 1 and ref.get("app") and ref.get("channel"):
                scopes.setdefault(card_id, (ref["app"], ref["channel"]))
            if ref.get("app") == app_id and ref.get("channel") == channel and ref.get("source_event_id"):
                refs.setdefault(card_id, set()).add(ref["source_event_id"])
    ids = {card_id for card_id, scope in scopes.items() if scope == (app_id, channel)}
    rows = [row for row in RetrievalIndex._memory_rows(str(path)) if row["source_id"] in ids]
    # 被修订、删除、lineage 淘汰的卡所依据的旧原话也不再作为补充注入。
    suppressed_raw = set().union(*(refs.get(card_id, set()) for card_id in ids)) if ids else set()
    return rows, refs, suppressed_raw


def ensure_current_index(index, rows, embedder):
    """Reuse the existing index on unchanged turns; rebuild only on source/model change."""
    with index._connect() as db:
        existing = {
            row["source_key"]: row
            for row in db.execute(
                "SELECT source_key,content_hash,provenance_json,"
                "vector IS NOT NULL AS has_vector,embedding_model "
                "FROM chunks WHERE source_kind='memory_card'"
            )
        }
    changed = len(existing) != len(rows)
    if not changed:
        for row in rows:
            old = existing.get(row["source_key"])
            if (old is None or old["content_hash"] != row["content_hash"]
                    or json.loads(old["provenance_json"]) != row["provenance"]
                    or (embedder is not None and
                        (not old["has_vector"] or old["embedding_model"] != index.embedding_model))):
                changed = True
                break
    if changed:
        index._replace_lane("memory_card", rows, source_path=index.memory_db_path,
                            embedder=embedder)


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
    rows, refs, suppressed_raw = scoped_rows(path, app_id, channel)
    model_id, embedder = configured_model(application.providers, "embedding")
    # sources 的现有稳定行号仅用于派生文件名，分开 app 的向量缓存。
    with closing(sqlite3.connect(application.registry.registry_path)) as db:
        scope = db.execute("SELECT rowid FROM sources WHERE agent_id=? AND app_id=? AND channel=?",
                           (agent_id, app_id, channel)).fetchone()[0]
    index = RetrievalIndex(str(path.with_name(f"recall_{scope}.sqlite")),
                           memory_db_path=str(path), embedding_model=model_id, embedder=embedder)
    try:
        ensure_current_index(index, rows, embedder)
        candidates = index.search(query, top_k=12, pool=24, lexical_queries=terms) if rows else []
    except Exception as exc:
        if model_id.startswith("local:"):
            application.providers.local_errors["embedding"] = type(exc).__name__
        raise
    seen_cards, seen_raw = set(visible_memory_ids), set(visible_archive_ids)
    texts = [normalized(t) for t in visible_texts if normalized(t)]
    current = {r["source_id"]: r for r in rows}
    pool = []
    for item in candidates:
        row = current.get(item["source_id"])
        if row is None or item["text"] != row["text"] or item["provenance"]["revision_id"] != row["provenance"]["revision_id"]:
            continue
        if row["source_id"] in seen_cards or refs.get(row["source_id"], set()) & seen_raw:
            continue
        lexical = sum(t in row["text"].lower() for t in terms) / len(terms)
        cosine = max((float(r.rsplit(":", 1)[1]) for r in item["retrieval_reasons"] if r.startswith("vector_rank:")), default=0)
        if lexical or cosine >= 0.65:
            pool.append(dict(kind="memory", id=row["source_id"], text=row["text"],
                             revision_id=row["provenance"]["revision_id"], score=max(lexical, cosine)))
    raw = {}
    for term in terms:
        for row in application.search(agent_id, app_id, term, channel, limit=12):
            raw[row["source_event_id"]] = row
    for source_id, row in raw.items():
        if source_id in suppressed_raw or source_id in seen_raw:
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
    lines = []
    for item in pool:
        key = "memories" if item["kind"] == "memory" else "archive"
        text = normalized(item["text"])
        if result[key] or any(text in t or t in text for t in texts):
            continue
        label = "记忆" if key == "memories" else "历史原话"
        line = f"[{label} {item['id']}] {item['text'][:300]}"
        lines.append(line)
        texts.append(text)
        result[key].append(item)
    result["rendered"] = "\n".join(lines)[:700]
    return result
