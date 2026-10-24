"""召回模型接线；本地仅加载宿主对象或已经存在的模型目录。"""
import math

from thalemora.app.providers import ProviderUnavailable
from .core import RetrievalShadowError


def rerank_scores(results, count):
    if not isinstance(results, list) or len(results) != count:
        raise ValueError("rerank 必须返回每条候选的分数")
    scores = [None] * count
    for row in results:
        index, score = row["index"], row["relevance_score"]
        if type(index) is not int or not 0 <= index < count or scores[index] is not None:
            raise ValueError("rerank index 无效")
        if not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError("rerank score 无效")
        scores[index] = float(score)
    return scores


class AvailableEmbedder:
    """Host failures must be explicit; generic code/permission/DB errors propagate."""
    def __init__(self, embedder):
        self.embedder = embedder

    def _invoke(self, method, *args, **kwargs):
        try:
            return getattr(self.embedder, method)(*args, **kwargs)
        except (TimeoutError, ConnectionError) as exc:
            raise ProviderUnavailable("embedding transport unavailable") from exc

    def encode_one(self, text):
        return self._invoke("encode_one", text)

    def encode_many(self, texts, *, batch_size=16):
        return self._invoke("encode_many", texts, batch_size=batch_size)


class APIEmbedder:
    def __init__(self, providers, config):
        self.providers, self.config = providers, dict(config)

    def encode_one(self, text):
        return self.providers.call("embedding", text, expected_config=self.config)["output"]

    def encode_many(self, texts, *, batch_size=16):
        return [self.encode_one(text) for text in texts]


class LocalEmbedder:
    def __init__(self, model):
        self.model = model

    def _encode(self, *args, **kwargs):
        try:
            return self.model.encode(*args, **kwargs)
        except RetrievalShadowError:
            raise  # known numerical/model violations are never optional outages
        except (RuntimeError, TimeoutError, ConnectionError) as exc:
            raise ProviderUnavailable("local embedding runtime unavailable") from exc

    def encode_one(self, text):
        return self._encode(text)

    def encode_many(self, texts, *, batch_size=16):
        return self._encode(texts, batch_size=batch_size)


def configured_model(providers, module):
    status = providers.status(module)
    config = status["config"]
    mode = config.get("mode", "api" if config.get("enabled") else "disabled")
    if mode == "disabled":
        return "disabled", None
    # error 可由下一次明确召回重试；错误仍保留在审计/状态中。
    if status["state"] not in ("ready", "error"):
        raise ProviderUnavailable(f"{module}: {status['state']}")
    identity = f"{mode}:{config.get('base_url', '')}:{config['model']}"
    if mode == "api":
        if module == "embedding":
            return identity, APIEmbedder(providers, config)
        return identity, lambda query, docs: rerank_scores(
            providers.call("rerank", query, documents=docs)["output"], len(docs))
    try:
        local = providers.local_models.get(module)
        if local is None or local[0] != config["model"]:
            from sentence_transformers import SentenceTransformer, CrossEncoder
            if module == "embedding":
                model = LocalEmbedder(SentenceTransformer(config["model"], local_files_only=True))
            else:
                encoder = CrossEncoder(config["model"], local_files_only=True)
                model = lambda query, docs: encoder.predict([(query, doc) for doc in docs])
            local = (config["model"], model)
            providers.local_models[module] = local
        providers.local_errors.pop(module, None)
        return identity, local[1]
    except (ImportError, OSError, RuntimeError) as exc:
        if isinstance(exc, (PermissionError, RetrievalShadowError)):
            raise
        providers.local_errors[module] = type(exc).__name__
        raise ProviderUnavailable(f"{module} local: {type(exc).__name__}") from exc
