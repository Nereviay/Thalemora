"""受信 Python 宿主的意图召回；不分类工具、不批准或执行动作。

context 中参数必须已经由宿主去敏。preaction 要求 needs_precheck=True，
args 仅采纳明确对象字段；开关、重放、轮次和最终 Context 注入归宿主。
不调用 AmbientEngine，也不写激活回执。
"""
import json
import re
from collections.abc import Mapping

from .recall import query_terms


_SENSITIVE = re.compile(r'api[_-]?key|token|authorization|password|secret|bearer', re.I)
_OBJECTS = ('project', 'repository', 'path', 'file', 'target', 'resource',
            'document', 'recipient', 'url', 'command')
_GENERIC = set('事情 活动 任务 项目 现在 今天 明天 最近 日常 工作 生活 空闲 无 暂无 routine contact activity preaction'.split())


def _text(value):
    if not isinstance(value, str) or _SENSITIVE.search(value):
        return ''
    return ' '.join(value.split())[:200]


def _items(value):
    if isinstance(value, str):
        return [_text(value)]
    if isinstance(value, Mapping):
        if value.get('status') in {'done', 'completed', 'closed', 'cancelled'}:
            return []
        # VNext 的 open_loop 和 activity 可写在 content/summary/description；
        # 同一事项只取第一段真实正文，不因多字段重复扩大查询。
        return [text for k in ('content', 'text', 'summary', 'title', 'description', 'topic')
                if (text := _text(value.get(k)))][:1]
    if isinstance(value, (list, tuple)):
        return [text for item in value[:4] for text in _items(item)]
    return []


def recall_for_intent(application, agent_id, app_id, *, surface, context,
                      channel='main', visible_memory_ids=(),
                      visible_archive_ids=(), visible_texts=()):
    """返回原召回信封；最多 2+2（行动前 1+1）、700 字，无行动回执。

    contact/activity 接受 recent_user_text/open_loops/now_text/life_state；
    activity 另接受 recent_activity/unfinished_tasks。无主题时不检索。
    敏感标记所在片段及含敏感标记的完整结果均丢弃，不输出部分凭据。
    """
    if surface not in {'contact', 'activity', 'find_activity', 'preaction'}:
        raise ValueError('不支持的召回意图')
    empty = dict(rendered='', memories=[], archive=[], errors=[])
    if surface == 'preaction':
        if (not context.get('needs_precheck')
                or any(context.get(k) for k in ('read_only', 'routine', 'already_checked'))):
            return empty
        args = context.get('args') or {}
        parts = [_text(args.get(k)) for k in _OBJECTS]
    else:
        parts = _items(context.get('open_loops'))
        if surface in {'activity', 'find_activity'}:
            parts += _items(context.get('unfinished_tasks')) + _items(context.get('recent_activity'))
        parts += [_text(context.get('recent_user_text'))]
        state = context.get('life_state') or {}
        parts += [_text(state.get(k)) for k in ('location', 'activity', 'scene', 'focus', 'current_activity')]
    # 时间或工具名自身不是对象锚点。
    parts = [p for p in parts if any(t not in _GENERIC and len(t) >= 2 for t in query_terms(p))]
    if not parts:
        return empty
    suffix = _text(context.get('tool_name' if surface == 'preaction' else 'now_text'))
    query = '；'.join(dict.fromkeys(parts + ([suffix] if suffix else [])))[:200]
    limit = 1 if surface == 'preaction' else 2
    result = application.recall_for_turn(
        agent_id, app_id, query, channel, visible_memory_ids=visible_memory_ids,
        visible_archive_ids=visible_archive_ids, visible_texts=visible_texts,
        max_memory=limit, max_archive=limit)
    # 原始证据不做文本改写；含凭据标记时整体拒绝，避免伪造引用。
    if _SENSITIVE.search(json.dumps(result, ensure_ascii=False)):
        return empty
    return result
