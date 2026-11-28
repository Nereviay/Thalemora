import copy
from typing import Any
from .contract import TOOL_SCHEMAS

SELF_MODEL_MUTATION_TOOLS = (
    "self_model_observe",
    "self_model_revise",
    "self_model_retire",
)


SELF_MODEL_TOOL_NAMES = (*SELF_MODEL_MUTATION_TOOLS, "self_model_review")


SELF_MODEL_STATEMENT_GUIDANCE = (
    '自我认识的 statement 用第一人称自然表达相对稳定的一个倾向；建议约 20–80 个汉字，能更短就更短。不要罗列具体事故、日志、游戏存档或排查过程；那些经历只作为证据或放入 Memory，不冒充人格。不确定时保留不确定性，不因篇幅要求而编造结论。'
)


_TOOL_DESCRIPTIONS = {
    "self_model_observe": (
        "当你自己明确形成一条可能长期成立的自我认识时记录。一次情绪、一次行为、"
        "用户的偏好或要求不能直接当成‘你是谁’。"
        + SELF_MODEL_STATEMENT_GUIDANCE
    ),
    "self_model_revise": (
        "当你对已有自我认识有新的理解、补充依据或变得不确定时修订。"
        + SELF_MODEL_STATEMENT_GUIDANCE
    ),
    "self_model_retire": "当你确认一条旧自我认识已经不再适用时退役。",
    "self_model_review": "查看你目前已有、观察中或退役的自我认识和版本；只读。",
}


def provider_tool_schemas(*, user_name="用户") -> tuple[dict[str, Any], ...]:
    """Mechanically wrap the one authoritative semantic argument contract."""
    return tuple(
        {
            "type": "function",
            "function": {
                "name": name,
                "description": _TOOL_DESCRIPTIONS[name].replace("用户的偏好或要求",user_name+"的偏好或要求"),
                "parameters": copy.deepcopy(TOOL_SCHEMAS[name]),
            },
        }
        for name in SELF_MODEL_TOOL_NAMES
    )


SELF_MODEL_PROVIDER_TOOL_SCHEMAS = provider_tool_schemas()
