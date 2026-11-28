"""Deterministic contracts for turn-complete interaction affect continuity."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

TARGETS = frozenset({"user", "self", "other", "environment"})
NON_AFFECT_LABELS = frozenset({
    "调侃", "坚定", "克制", "陪伴", "认错", "退让", "安慰", "撒娇", "沉默", "保持距离",
})
AFFECT_BASES = frozenset({"assistant_explicit", "assistant_behavior", "interaction_inference", "previous_carry"})
MAX_INTENSITY = 3
MAX_UNRESOLVED_AGE_SECONDS = 2 * 3600

_CLEAR_PATTERNS = (
    re.compile(r"(?P<quote>我(?:现在|已经|真)?不(?:生气|气|烦|难受|委屈)了)"),
    re.compile(r"(?P<quote>(?:是)?我看错了)"),
    re.compile(r"(?P<quote>(?:是)?我误会了)"),
    re.compile(r"(?P<quote>刚才(?:那件事)?是(?:个)?误会)"),
)

# Each set-pattern requires a first-person self-report in the agent's emitted text.
# Deliberately omit bare mood words and behavioral/strategy descriptions.
_SET_PATTERNS = (
    (
        "生气",
        re.compile(
            r"(?P<quote>我(?:(?:现在|其实|真的|确实|还是|还|有点|很|挺|非常)\s*)*"
            r"(?:还在|在)?生(?:你)?气(?:了|呢)?)"
        ),
    ),
    (
        "生气",
        re.compile(r"(?P<quote>我(?:(?:现在|其实|真的|确实|还是|还)\s*)*没消气)"),
    ),
    (
        "烦躁",
        re.compile(
            r"(?P<quote>我(?:(?:现在|真的|确实|就是|有点|很|挺|非常)\s*)*"
            r"烦你(?:这|那)?(?:口气|语气|态度|样子|句话|说法)?)"
        ),
    ),
    (
        "委屈",
        re.compile(r"(?P<quote>我(?:(?:真的|确实|有点|很|挺|非常)\s*)+委屈(?:了|呢)?)"),
    ),
    (
        "失望",
        re.compile(r"(?P<quote>我(?:(?:真的|确实|有点|很|挺|非常)\s*)*对你(?:有点|很|挺|非常)?失望(?:了)?)"),
    ),
    (
        "难受",
        re.compile(r"(?P<quote>你[^，。！？!?\n]{0,18}让我(?:(?:真的|有点|很|挺|非常)\s*)*难受(?:了)?)"),
    ),
    (
        "伤心",
        re.compile(r"(?P<quote>你[^，。！？!?\n]{0,18}让我(?:(?:真的|有点|很|挺|非常)\s*)*伤心(?:了)?)"),
    ),
    (
        "吃醋",
        re.compile(r"(?P<quote>我(?:(?:真的|有点|很|挺)\s*)*吃醋了)"),
    ),
)

_QUOTED_OR_HYPOTHETICAL_PREFIX = re.compile(
    r"(?:你(?:刚才)?说|如果|假如|比如|不是说|别学我说)[^，。！？!?\n]{0,16}$"
)


def _explicit_intensity(quote: str) -> int:
    if any(word in quote for word in ("非常", "烦死", "特别", "真的很")):
        return 3
    if any(word in quote for word in ("有点", "一点")):
        return 1
    return 2


def extract_explicit_interaction_affect(assistant_text: str, *, actor="助手", user="用户") -> dict[str, Any] | None:
    """Extract only the agent's direct self-report from an emitted assistant reply."""
    text = str(assistant_text or "").strip()[:4000]
    if not text:
        return None
    for pattern in _CLEAR_PATTERNS:
        match = pattern.search(text)
        if match:
            quote = match.group("quote")
            return {
                "action": "clear",
                "resolution": f"{actor}明确表示相关情绪已结束或源于误会",
                "evidence_quote": quote,
            }
    for label, pattern in _SET_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        prefix = text[max(0, match.start() - 24):match.start()]
        if _QUOTED_OR_HYPOTHETICAL_PREFIX.search(prefix):
            continue
        quote = match.group("quote")
        return {
            "action": "set",
            "label": label,
            "target": "user",
            "intensity": _explicit_intensity(quote),
            "unresolved": True,
            "source": f"{actor}在已发回复中明确表示对{user}{label}",
            "evidence_quote": quote,
            "basis": "assistant_explicit",
        }
    return None


def decay_interaction_affect(
    state: Mapping[str, Any], *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Age a stored state without refreshing its source timestamp."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = _time(state.get("updated_at"))
    if stamp is None:
        return None
    age = max(0.0, (now - stamp).total_seconds())
    if age >= MAX_UNRESOLVED_AGE_SECONDS:
        return None
    out = dict(state)
    original = max(1, min(MAX_INTENSITY, int(state.get("intensity") or 1)))
    decay_step = int(age // (MAX_UNRESOLVED_AGE_SECONDS / MAX_INTENSITY))
    out["intensity"] = max(1, original - decay_step)
    out["age_seconds"] = int(age)
    return out

# Legacy shadow-evaluator fixtures below remain for compatibility tests only. The
# production runtime imports the explicit extractor/decay/renderer and makes no
# evaluator or provider call.
def evaluator_system_prompt(*, app="应用", actor="助手", user="用户"):
    return '你是{app}的互动情绪记录器，不负责决定下一句该怎么说。\n输入是一轮已经真实完成的互动：上一轮未解决情绪（可能为空）、{user}本轮消息、{actor}已经实际发出的回复。\n只记录这轮结束以后仍然成立的互动余波，不优化恋爱关系，不替任何人免责，不把沉默/短回复自动解释成体贴、克制、给空间或陪伴。\n\n输出严格 JSON：\n{"active":true|false,"label":"2-4字","intensity":1|2|3,"target":"user|self|other|environment","source":"具体触发事实","unresolved":true|false,"resolution":"若上一状态被解决，写具体依据，否则空串","confidence":0到1}\n\n规则：\n1. 只能根据已经发生的 user/assistant 原文判断，不预测下一轮，不生成回复建议、stance 或行为指令。\n2. active=true 时 target/source/intensity 必须明确；拿不准就 active=false。\n3. 上一状态 unresolved=true 时，除非本轮有明确缓和、和好、转变或{actor}自己实际表现出状态已改变，否则不要因为时间过去、用户换话题、回复很短就自动清掉。\n4. 新的明确互动情绪可以覆盖旧状态；若只是轻微背景感受，不要覆盖更强的未解决状态。\n5. “我不想理你/你烦死了/滚”等情绪性表达不等于双方已经和平结束冲突；“行/好/嗯”也不等于已经消气。\n6. 用户明确要求暂停互动时，只说明边界被执行，不自动推断{actor}内心平静、体贴或释然。\n7. 不把“她回消息了”本身解释成开心、安心或被安抚。\n8. active 只描述{actor}自己的状态，不能把{user}自己的烦躁、难过、疲惫或对环境的不满抄成{actor}的情绪；除非{actor}的已发回复或上一状态明确显示他自己因此产生了反应。\n9. 上一状态不是必须机械保留。若{user}明确提出和好/停止争执/道歉/重新亲近，而{actor}已经实际接受了这个转变，可以据此写 resolution 并清除或改写旧余波。\n10. label 必须是{actor}自身的情绪/感受，不是说话方式、立场或应对策略。像“调侃/坚定/克制/陪伴/认错/退让/安慰/撒娇/沉默/保持距离”都不是情绪，不能 active=true。\n11. 道歉、承认错误、承诺改变、说“不走”、顺着对方或短暂退让，本身都不能证明{actor}产生了某种情绪。尤其 target=self 的“自责/愧疚”必须有{actor}已发原文中明确的自我感受证据，不能仅从“我错了/是我的问题/我改”推断。\n12. basis 表示证据来源：assistant_explicit={actor}已发回复直接说出感受；assistant_behavior=已发回复中的动作/语气明确表现；interaction_inference=只能由整轮互动推断；previous_carry=没有新证据改变上一轮未解决状态，只是继续保留。\n13. 除 previous_carry 外，active=true 必须给 evidence_quote，且只能摘{actor}已经实际发出的回复原文，不能摘{user}的话。\n14. target=self/other/environment 若只是 interaction_inference 或普通应对行为，不能当成下一轮高优先互动状态；优先 active=false，除非{actor}已发回复有明确自身感受证据。\n15. 若这轮没有值得延续到下一轮的{actor}互动余波，active=false。'.replace("{app}", app).replace("{actor}", actor).replace("{user}", user)

EVALUATOR_SYSTEM_PROMPT = evaluator_system_prompt()


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _time(value: Any) -> datetime | None:
    text = _text(value, 80)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(text)
    except ValueError:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)


def evaluator_user_prompt(previous: Mapping[str, Any] | None, user_text: str, assistant_text: str, *, actor="助手", user="用户") -> str:
    prev = dict(previous) if isinstance(previous, Mapping) else None
    return (
        "上一轮未解决情绪：" + json.dumps(prev, ensure_ascii=False) + "\n"
        f"{user}本轮消息：" + _text(user_text, 1800) + "\n"
        f"{actor}已经实际发出的回复：" + _text(assistant_text, 1800)
    )


def parse_observation(raw: str, *, assistant_text: str | None = None) -> dict[str, Any]:
    text = str(raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("missing JSON object")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict) or not isinstance(data.get("active"), bool):
        raise ValueError("invalid active flag")
    if not data["active"]:
        return {
            "active": False,
            "resolution": _text(data.get("resolution"), 220),
            "confidence": _confidence(data.get("confidence")),
        }
    label = _text(data.get("label"), 16)
    target = _text(data.get("target"), 32)
    source = _text(data.get("source"), 240)
    try:
        intensity = int(data.get("intensity"))
    except (TypeError, ValueError):
        intensity = 0
    if not (2 <= len(label) <= 4):
        raise ValueError("invalid label")
    if label in NON_AFFECT_LABELS:
        raise ValueError("non-affect label")
    if target not in TARGETS:
        raise ValueError("invalid target")
    if not source:
        raise ValueError("missing source")
    if not 1 <= intensity <= MAX_INTENSITY:
        raise ValueError("invalid intensity")
    if not isinstance(data.get("unresolved"), bool):
        raise ValueError("invalid unresolved")
    basis = _text(data.get("basis"), 40)
    if basis not in AFFECT_BASES:
        raise ValueError("invalid basis")
    evidence_quote = _text(data.get("evidence_quote"), 120)
    if basis != "previous_carry" and not evidence_quote:
        raise ValueError("missing evidence quote")
    if assistant_text is not None and basis != "previous_carry" and evidence_quote not in str(assistant_text):
        raise ValueError("evidence quote not in assistant reply")
    return {
        "active": True,
        "label": label,
        "intensity": intensity,
        "target": target,
        "source": source,
        "unresolved": bool(data["unresolved"]),
        "basis": basis,
        "evidence_quote": evidence_quote,
        "resolution": _text(data.get("resolution"), 220),
        "confidence": _confidence(data.get("confidence")),
    }


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _previous_valid(previous: Mapping[str, Any] | None, now: datetime) -> bool:
    if not isinstance(previous, Mapping) or not previous.get("unresolved"):
        return False
    stamp = _time(previous.get("updated_at"))
    if stamp is None:
        return True
    return max(0.0, (now - stamp).total_seconds()) <= MAX_UNRESOLVED_AGE_SECONDS


def advance_interaction_affect(
    previous: Mapping[str, Any] | None,
    observation: Mapping[str, Any],
    *,
    now: datetime | None = None,
    turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Merge one post-turn observation. No LLM output can directly prescribe behavior."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    prev = dict(previous) if isinstance(previous, Mapping) else None
    obs = dict(observation) if isinstance(observation, Mapping) else {}
    if obs.get("active") is True:
        # Re-validate through the public parser contract shape.
        clean = parse_observation(json.dumps(obs, ensure_ascii=False))
        return {
            "label": clean["label"],
            "intensity": clean["intensity"],
            "target": clean["target"],
            "source": clean["source"],
            "unresolved": clean["unresolved"],
            "basis": clean["basis"],
            "evidence_quote": clean["evidence_quote"],
            "confidence": clean["confidence"],
            "updated_at": now.isoformat(),
            "turn_id": _text(turn_id, 120) or None,
        }

    resolution = _text(obs.get("resolution"), 220)
    if _previous_valid(prev, now) and not resolution:
        # No explicit evidence of resolution: carry the prior unresolved state.
        return prev
    return None



def interaction_dims_extras(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Encode candidate interaction metadata into mood_events.dims extras.

    This keeps the shadow design compatible with the existing append-only mood table;
    it does not require a schema migration or a fourth durable state store.
    """
    if not isinstance(state, Mapping):
        return {}
    label = _text(state.get("label"), 16)
    target = _text(state.get("target"), 32)
    source = _text(state.get("source"), 240)
    if not label or target not in TARGETS or not source:
        return {}
    try:
        intensity = int(state.get("intensity"))
    except (TypeError, ValueError):
        return {}
    if intensity not in (1, 2, 3) or not isinstance(state.get("unresolved"), bool):
        return {}
    return {
        "interaction_target": target,
        "interaction_unresolved": bool(state.get("unresolved")),
        "interaction_intensity": intensity,
        "interaction_confidence": _confidence(state.get("confidence")),
        "interaction_basis": _text(state.get("basis"), 40) or None,
        "interaction_evidence_quote": _text(state.get("evidence_quote"), 120) or None,
        "interaction_turn_id": _text(state.get("turn_id"), 120) or None,
    }

def render_affect_fact(state: Mapping[str, Any] | None, *, actor="助手", user="用户") -> str:
    if not isinstance(state, Mapping):
        return ""
    label = _text(state.get("label"), 16)
    target = _text(state.get("target"), 32)
    source = _text(state.get("source"), 180)
    if not label or target not in TARGETS or not source:
        return ""
    target_label = {
        "user": user,
        "self": actor + "自己",
        "other": "其他人",
        "environment": "外部事情",
    }[target]
    bits = [f"{actor}的感受={label}", "对象=" + target_label]
    try:
        intensity = int(state.get("intensity"))
    except (TypeError, ValueError):
        intensity = 0
    if 1 <= intensity <= MAX_INTENSITY:
        bits.append("强度=" + str(intensity) + "/3")
    bits.append("未解决=" + ("是" if state.get("unresolved") else "否"))
    evidence = _text(state.get("evidence_quote"), 80)
    if evidence:
        bits.append("原文依据=“" + evidence + "”")
    return "【互动情绪事实】" + "；".join(bits)
