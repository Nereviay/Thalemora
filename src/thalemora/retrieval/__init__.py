"""检索派生层；显式配置，不加载生产适配。"""
from .core import RetrievalIndex, RetrievalShadowError
from .bm25 import BM25Index, build_index, tokenize

__all__ = ["RetrievalIndex", "RetrievalShadowError", "BM25Index", "build_index", "tokenize"]
