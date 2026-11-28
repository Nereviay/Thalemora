"""检索派生层。仅使用历史选择规则的 Runtime 无需加载向量或分词后端。"""
from importlib import import_module

__all__ = ["RetrievalIndex", "RetrievalShadowError", "BM25Index", "build_index", "tokenize"]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(name)
    module = ".core" if name in {"RetrievalIndex", "RetrievalShadowError"} else ".bm25"
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
