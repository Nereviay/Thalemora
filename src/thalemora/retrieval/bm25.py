"""来源 BM25 纯词法算法；接收调用方行，不持有数据库或缓存。"""
import math
import jieba
from thalemora.gatekeeper import keywords as keyword_policy

K1, B = 1.5, 0.75

FIELD_WEIGHTS = {"content": 1.0, "title": 2.0, "tags": 2.0, "keywords": 3.0}

def _keep(tok):
    """保留含中文/字母数字/下划线的词，丢纯标点、纯 emoji"""
    return any("\u4e00" <= ch <= "\u9fff" or ch.isalnum() or ch == "_" for ch in tok)

def tokenize(text):
    """jieba 分词，输出小写去重前的词列表（BM25 用词频，不去重）"""
    if not text or not text.strip():
        return []
    return [t.strip().lower() for t in jieba.cut(text) if _keep(t.strip())]

class BM25Index:
    """Okapi BM25：term -> postings[(doc_idx, tf)] 倒排，doc 长度加权归一"""

    def __init__(self, docs):
        # docs: [(card_id, content, status, {term: tf})]
        self.doc_count = len(docs)
        self.doc_ids = [d[0] for d in docs]
        self.doc_contents = [d[1] for d in docs]
        self.statuses = [d[2] for d in docs]
        self.doc_lens = [sum(d[3].values()) for d in docs]
        self.avgdl = sum(self.doc_lens) / self.doc_count if self.doc_count else 0.0
        self.postings = {}
        for di, (_, _, _, tokens) in enumerate(docs):
            for term, tf in tokens.items():
                self.postings.setdefault(term, []).append((di, tf))
        self.idf = {}
        for term, post in self.postings.items():
            df = len(post)
            self.idf[term] = math.log(1.0 + (self.doc_count - df + 0.5) / (df + 0.5))

    def rank(self, query_tokens):
        """返回 [(doc_idx, score)]，按分数降序，只含有分的卡"""
        scores = {}
        for term in query_tokens:
            idf = self.idf.get(term)
            if idf is None or self.avgdl <= 0:
                continue
            for di, tf in self.postings.get(term, ()):
                denom = tf + K1 * (1.0 - B + B * self.doc_lens[di] / self.avgdl)
                scores[di] = scores.get(di, 0.0) + idf * (tf * (K1 + 1.0)) / denom
        return sorted(scores.items(), key=lambda kv: -kv[1])

    def search(self, query, limit=10, include_sunk=False, include_archived=False):
        """沿用来源 search 的分词、状态过滤与截断，仅查询本实例派生数据。"""
        tokens = tokenize(query or "")
        if self.doc_count == 0 or not tokens:
            return []
        out = []
        for di, score in self.rank(tokens):
            if _status_ok(self.statuses[di], include_sunk, include_archived):
                out.append((self.doc_ids[di], self.doc_contents[di], score))
                if len(out) >= limit:
                    break
        return out

def _field_tokens(text, weight):
    out = {}
    for t in jieba.cut(text or ""):
        t = t.strip().lower()
        if _keep(t):
            out[t] = out.get(t, 0.0) + weight
    return out

def _status_ok(status, include_sunk, include_archived):
    """与 memory_store.search_keyword 的 status 过滤语义保持一致"""
    if status == "trashed":
        return False
    if include_sunk and include_archived:
        return True
    if include_sunk:
        return status != "archived"
    if include_archived:
        return status in ("fresh", "archived")
    return status == "fresh"

def build_index(rows):
    # 旧库的 keywords 可能很厚；建索引时先按当前策略压成少量检索锚点，
    # 不让历史关键词垃圾继续吃 3x BM25 权重。tags 仍按分类字段处理。
    normalized_keywords = {}
    for r in rows:
        kws = keyword_policy.normalize_keywords(
            r["keywords"] or "", title=r["title"] or "", tags=r["tags"] or ""
        )
        normalized_keywords[r["id"]] = ",".join(kws)
        for raw in (normalized_keywords[r["id"]], r["tags"] or ""):
            for w in raw.split(","):
                w = w.strip()
                if 1 < len(w) <= 24 and _keep(w):
                    jieba.add_word(w)
    docs = []
    for r in rows:
        tokens = {}
        for field, weight in FIELD_WEIGHTS.items():
            value = normalized_keywords[r["id"]] if field == "keywords" else r[field]
            ft = _field_tokens(value if value is not None else "", weight)
            for term, tf in ft.items():
                tokens[term] = tokens.get(term, 0.0) + tf
        if tokens:
            docs.append((r["id"], r["content"] or "", r["status"], tokens))
    return BM25Index(docs)
