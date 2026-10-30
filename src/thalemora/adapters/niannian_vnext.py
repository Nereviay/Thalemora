"""VNext 旧请求的受信进程内桥；不提供 HTTP、工具执行或激活回执。

身份、raw_to_archive 和 previous_assistant_ref 只由宿主同步器提供，不能取自
请求正文。映射值包含 agent_id/app_id/channel/source_event_id/version。
缺映射时整包保守为空，避免无法排除当前可见原话。trace 是内部证据包。
"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from thalemora.retrieval.intent import recall_for_intent


def read_canonical_events_for_vnext(runtime_db_path, request):
    """从宿主现有 Runtime 库只读取本轮所需的 canonical 事件，不另建索引。"""
    visible = request.get('visible_raw_event_ids', ())
    if not isinstance(visible, (list, tuple)) or len(visible) > 200:
        raise ValueError('visible_raw_event_ids 须为最多 200 项')
    ids = list(dict.fromkeys(v for v in visible if type(v) is int and v > 0))
    previous = request.get('previous_assistant_event_id')
    if not ids and not (isinstance(previous, str) and previous):
        return []
    clauses = []
    args = []
    if ids:
        clauses.append('raw_event_id IN (' + ','.join('?' for _ in ids) + ')')
        args.extend(ids)
    if isinstance(previous, str) and previous:
        clauses.append('event_id=?')
        args.append(previous)
    uri = Path(runtime_db_path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        rows = db.execute(
            'SELECT event_id,raw_event_id,role,content,epoch_id,status,kind,metadata_json '
            'FROM message_records WHERE status=? AND role IN (\'user\',\'assistant\') '
            'AND kind IN (\'message\',\'proactive_message\',\'legacy_display\') '
            'AND (' + ' OR '.join(clauses) + ')', ('active', *args),
        ).fetchall()
    events = []
    for row in rows:
        try:
            metadata = json.loads(row['metadata_json'] or '{}')
        except (TypeError, ValueError):
            continue
        if not isinstance(metadata, dict) or metadata.get('channel', 'main') != 'main':
            continue
        events.append(dict(event_id=row['event_id'], raw_event_id=row['raw_event_id'],
                           role=row['role'], content=row['content'], epoch_id=row['epoch_id'],
                           status=row['status'], authored_text=metadata.get('authored_text')))
    return events


def map_canonical_sources(application, agent_id, app_id, request, canonical_events, *, channel='main'):
    """把宿主可信 canonical 活跃事件与当前 Archive 版本精确对齐。

    只读本轮显式可见的 Raw ID 和可信上一条 assistant ID；没有可核对的
    正文、来源、版本或 epoch 时不做猜测。返回值仅供同进程 recall_vnext 使用。
    """
    archive = application.archive(agent_id, app_id, channel)
    wanted = {value for value in request.get('visible_raw_event_ids', ())
              if type(value) is int and value > 0}
    previous_id = request.get('previous_assistant_event_id')
    candidates = {}
    duplicate_ids = set()
    raw_id_counts = {}
    for event in canonical_events:
        if not isinstance(event, dict) or event.get('status', 'active') != 'active':
            continue
        event_id = event.get('event_id')
        raw_id = event.get('raw_event_id')
        if not isinstance(event_id, str) or not event_id:
            continue
        if not ((type(raw_id) is int and raw_id in wanted) or event_id == previous_id):
            continue
        source_id = 'canonical:' + event_id
        if source_id in candidates:
            duplicate_ids.add(source_id)
        candidates[source_id] = event
        if type(raw_id) is int and raw_id in wanted:
            raw_id_counts[raw_id] = raw_id_counts.get(raw_id, 0) + 1

    if not candidates:
        return dict(raw_to_archive={}, previous_assistant_ref=None, conversation_id=None)

    uri = Path(archive.path).resolve().as_uri() + '?mode=ro'
    names = list(candidates)
    placeholders = ','.join('?' for _ in names)
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        rows = db.execute(
            'SELECT source_event_id,version,role,text,raw_event_id,epoch_id,conversation_id,deleted '
            'FROM history_events WHERE app=? AND channel=? AND source_event_id IN (' + placeholders + ')',
            (app_id, channel, *names),
        ).fetchall()
    mapping = {}
    previous = None
    conversation_id = None
    ambiguous_raw = set()
    for row in rows:
        source_id = row['source_event_id']
        if source_id in duplicate_ids or row['deleted'] or type(row['version']) is not int:
            continue
        event = candidates[source_id]
        actual_text = event.get('authored_text')
        if actual_text is None:
            actual_text = event.get('content')
        if (not isinstance(actual_text, str) or actual_text != row['text']
                or row['role'] != event.get('role')
                or row['epoch_id'] != event.get('epoch_id')
                or event.get('role') not in ('user', 'assistant')):
            continue
        ref = dict(agent_id=agent_id, app_id=app_id, channel=channel,
                   source_event_id=source_id, version=row['version'])
        raw_id = event.get('raw_event_id')
        if (type(raw_id) is int and raw_id > 0 and raw_id == row['raw_event_id']
                and raw_id_counts.get(raw_id) == 1):
            if raw_id in mapping:
                ambiguous_raw.add(raw_id)
            mapping[raw_id] = ref
        if (event.get('event_id') == previous_id and row['role'] == 'assistant'
                and row['text'] and row['conversation_id']
                and row['epoch_id'] == request.get('current_epoch_id')):
            mapping[previous_id] = ref
            previous = dict(ref, text=row['text'])
            conversation_id = row['conversation_id']
    for raw_id in ambiguous_raw:
        mapping.pop(raw_id, None)
    return dict(raw_to_archive=mapping, previous_assistant_ref=previous,
                conversation_id=conversation_id)


def recall_vnext_from_runtime(application, agent_id, app_id, request, *,
                              runtime_db_path, surface='normal', channel='main',
                              preaction_context=None):
    """供同机宿主使用的单一薄入口，走真实 canonical → Archive → 公共召回。"""
    events = read_canonical_events_for_vnext(runtime_db_path, request)
    mapped = map_canonical_sources(application, agent_id, app_id, request, events,
                                   channel=channel)
    return recall_vnext(application, agent_id, app_id, request, surface=surface,
                        channel=channel, preaction_context=preaction_context, **mapped)


def recall_vnext(application, agent_id, app_id, request, *, surface='normal',
                 channel='main', raw_to_archive=None, previous_assistant_ref=None,
                 conversation_id=None, preaction_context=None):
    """一次公共召回；preaction_context 是宿主已有工具分类/批准结果。

    generation_key/provider_round 仅透传追踪，不据此执行、授权或写回。
    自然浮现仍由宿主既有 AmbientEngine 调度，不能将检索当成说出回执。
    """
    archive = application.archive(agent_id, app_id, channel)  # 空请求也验证授权
    if surface not in {'normal', 'contact', 'activity', 'preaction'}:
        raise ValueError('不支持的 VNext surface')
    scope = (agent_id, app_id, channel)

    def scoped(ref):
        return (isinstance(ref, dict)
                and tuple(ref.get(k) for k in ('agent_id', 'app_id', 'channel')) == scope
                and isinstance(ref.get('source_event_id'), str)
                and bool(ref['source_event_id']) and type(ref.get('version')) is int
                and ref['version'] > 0)

    visible = []
    # 没有可见的旧 RawEvent 时无须凭空要求映射表；有旧 ID 则必须完整映射。
    missing = False
    for raw_id in request.get('visible_raw_event_ids', ()):
        ref = (raw_to_archive or {}).get(raw_id) if type(raw_id) is int else None
        if not scoped(ref):
            missing = True
        else:
            visible.append(ref['source_event_id'])
    trace = dict(surface=surface, generation_key=request.get('generation_key'),
                 provider_round=request.get('provider_round'),
                 scope=dict(agent_id=agent_id, app_id=app_id, channel=channel))
    if missing:
        trace.update(errors=[dict(code='unmapped_raw_events')], memories=[], archive=[])
        return dict(rendered='', trace=trace)

    common = dict(channel=channel, visible_archive_ids=visible,
                  visible_memory_ids=request.get('exclude_memory_card_ids', ()))
    if surface == 'normal':
        host = None
        raw_id = request.get('previous_assistant_event_id')
        # 现役 Runtime 的上一条 assistant event_id 为字符串；与 raw_event_id 整数不同。
        mapped = (raw_to_archive or {}).get(raw_id) if isinstance(raw_id, str) and raw_id else None
        prior = previous_assistant_ref
        if (scoped(prior) and scoped(mapped)
                and all(prior[k] == mapped[k] for k in
                        ('agent_id', 'app_id', 'channel', 'source_event_id', 'version'))
                and conversation_id and request.get('current_epoch_id')):
            host = dict(previous_assistant_ref=prior, conversation_id=conversation_id,
                        current_epoch_id=request['current_epoch_id'])
        result = application.recall_for_turn(agent_id, app_id, request.get('user_text', ''),
                                            host_context=host, **common)
    else:
        context = {k: request[k] for k in ('now_text', 'life_state', 'open_loops',
                   'recent_user_text', 'recent_activity') if k in request}
        if surface == 'preaction':
            context = dict(preaction_context or {})
            context.update(tool_name=request.get('tool_name'), args=request.get('arguments'))
        result = recall_for_intent(application, agent_id, app_id, surface=surface,
                                   context=context, **common)
    trace.update({k: v for k, v in result.items() if k != 'rendered'})
    # 真实 context_assembly._memory_vnext_source_refs 只读取 trace.related
    # 的 memory_refs/raw_event_ids；不能仅将未消费的 memories 存进 trace。
    memory_refs = [
        dict(card_id=item['id'], revision_id=item['revision_id'])
        for item in result.get('memories', ())
        if type(item.get('id')) is int and type(item.get('revision_id')) is int
        and item['id'] > 0 and item['revision_id'] > 0
    ]
    raw_refs = []
    for item in result.get('archive', ()):
        for raw_id, ref in (raw_to_archive or {}).items():
            if (type(raw_id) is int and raw_id > 0 and scoped(ref)
                    and item.get('id') == ref['source_event_id']
                    and item.get('version') == ref['version']
                    and raw_id not in raw_refs):
                raw_refs.append(raw_id)
    # 旧原话不一定在当前可见 200 条里；按 Archive 当前版本精确取其 Raw 来源。
    if result.get('archive'):
        uri = Path(archive.path).resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            for item in result['archive']:
                if not isinstance(item.get('id'), str) or type(item.get('version')) is not int:
                    continue
                row = db.execute(
                    'SELECT raw_event_id FROM history_events '
                    'WHERE app=? AND channel=? AND source_event_id=? AND version=? AND deleted=0',
                    (app_id, channel, item['id'], item['version']),
                ).fetchone()
                if row and type(row[0]) is int and row[0] > 0 and row[0] not in raw_refs:
                    raw_refs.append(row[0])
    trace['related'] = dict(memory_refs=memory_refs, raw_event_ids=raw_refs)
    return dict(rendered=result['rendered'], trace=trace)
