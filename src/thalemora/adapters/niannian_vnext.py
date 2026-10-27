"""VNext 旧请求的受信进程内桥；不提供 HTTP、工具执行或激活回执。

身份、raw_to_archive 和 previous_assistant_ref 只由宿主同步器提供，不能取自
请求正文。映射值包含 agent_id/app_id/channel/source_event_id/version。
缺映射时整包保守为空，避免无法排除当前可见原话。trace 是内部证据包。
"""
from thalemora.retrieval.intent import recall_for_intent


def recall_vnext(application, agent_id, app_id, request, *, surface='normal',
                 channel='main', raw_to_archive=None, previous_assistant_ref=None,
                 conversation_id=None, preaction_context=None):
    """一次公共召回；preaction_context 是宿主已有工具分类/批准结果。

    generation_key/provider_round 仅透传追踪，不据此执行、授权或写回。
    自然浮现仍由宿主既有 AmbientEngine 调度，不能将检索当成说出回执。
    """
    application.archive(agent_id, app_id, channel)  # 空请求也验证授权
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
    trace['related'] = dict(memory_refs=memory_refs, raw_event_ids=raw_refs)
    return dict(rendered=result['rendered'], trace=trace)
