"""召回模型接线；本地仅加载宿主对象或已经存在的模型目录。"""
import math

from thalemora.app.providers import ProviderUnavailable


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


class APIEmbedder:
    def __init__(self, providers):
        self.providers = providers

    def encode_one(self, text):
        return self.providers.call("embedding", text)["output"]

    def encode_many(self, texts, *, batch_size=16):
        return [self.encode_one(text) for text in texts]


class LocalEmbedder:
    def __init__(self, model):
        self.model = model

    def encode_one(self, text):
        return self.model.encode(text)

    def encode_many(self, texts, *, batch_size=16):
        return self.model.encode(texts, batch_size=batch_size)


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
            return identity, APIEmbedder(providers)
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
    except Exception as exc:
        providers.local_errors[module] = type(exc).__name__
        raise ProviderUnavailable(f"{module} local: {type(exc).__name__}") from exc
