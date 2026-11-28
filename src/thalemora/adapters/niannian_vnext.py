"""VNext 旧请求的受信进程内桥；不提供 HTTP、工具执行或激活回执。

身份、raw_to_archive 和 previous_assistant_ref 只由宿主同步器提供，不能取自
请求正文。映射值包含 agent_id/app_id/channel/source_event_id/version。
部分可见原话缺映射时，只允许能排除这些 Raw ID 的当前卡片。trace 是内部证据包。
"""
import json
import sqlite3
from contextlib import closing, nullcontext
from functools import partial
from pathlib import Path

from thalemora.retrieval.intent import recall_for_intent
from thalemora.retrieval.recall import constrained_quote_request
from thalemora.retrieval.turn_context import acknowledged_followup


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
        db.execute('BEGIN')
        rows = db.execute(
            'SELECT rowid AS position,event_id,raw_event_id,role,content,epoch_id,status,kind,metadata_json '
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
            authored = metadata.get('authored_text')
            event = dict(event_id=row['event_id'], raw_event_id=row['raw_event_id'], role=row['role'],
                         content=row['content'], epoch_id=row['epoch_id'], status=row['status'], authored_text=authored)
            if (ids and row['event_id'] == previous and row['role'] == 'assistant'
                    and row['epoch_id'] == request.get('current_epoch_id')
                    and acknowledged_followup(request.get('user_text'), row['content'] if authored is None else authored)):
                # 只穿过一句应声。前一条必须实际可见，不能从未进入窗口的历史找选项。
                neighbor = db.execute(
                    "SELECT event_id,raw_event_id,role FROM message_records WHERE epoch_id=? AND rowid<? "
                    "AND status='active' AND kind IN ('message','proactive_message','legacy_display') "
                    "AND role IN ('user','assistant') ORDER BY rowid DESC LIMIT 1",
                    (row['epoch_id'], row['position'])).fetchone()
                if neighbor and neighbor['role'] == 'user' and neighbor['raw_event_id'] in ids:
                    event['preceding_user'] = neighbor['event_id']
            events.append(event)
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
    if previous:
        predecessor = candidates[previous['source_event_id']].get('preceding_user')
        earlier = candidates.get('canonical:' + predecessor) if predecessor else None
        if earlier and earlier['role'] == 'user':
            ref = mapping.get(earlier.get('raw_event_id'))
            if ref and ref['source_event_id'] == 'canonical:' + predecessor:
                body = earlier.get('authored_text')
                previous['acknowledged_user_ref'] = dict(ref, text=earlier['content'] if body is None else body)
    return dict(raw_to_archive=mapping, previous_assistant_ref=previous,
                conversation_id=conversation_id)


def recall_vnext_from_runtime(application, agent_id, app_id, request, *,
                              runtime_db_path, surface='normal', channel='main',
                              preaction_context=None, ambient_state=None):
    """供同机宿主使用的单一薄入口，走真实 canonical → Archive → 公共召回。"""
    events = read_canonical_events_for_vnext(runtime_db_path, request)
    mapped = map_canonical_sources(application, agent_id, app_id, request, events,
                                   channel=channel)
    return recall_vnext(application, agent_id, app_id, request, surface=surface,
                        channel=channel, preaction_context=preaction_context,
                        ambient_state=ambient_state, **mapped)


def recall_vnext(application, agent_id, app_id, request, *, surface='normal',
                 channel='main', raw_to_archive=None, previous_assistant_ref=None,
                 conversation_id=None, preaction_context=None, ambient_state=None):
    """一次公共召回；preaction_context 是宿主已有工具分类/批准结果。

    generation_key/provider_round 仅透传追踪，不据此执行、授权或写回。
    ambient_state 只接受宿主进程内的预选候选/现有评分，不能来自网络请求正文。
    决策只是提供上下文，不能将检索或注入当作真实说出口的回执。
    """
    archive = application.archive(agent_id, app_id, channel)  # 空请求也验证授权
    if surface not in {'normal', 'contact', 'activity', 'find_activity', 'preaction'}:
        raise ValueError('不支持的 VNext surface')
    scope = (agent_id, app_id, channel)

    def scoped(ref):
        return (isinstance(ref, dict)
                and tuple(ref.get(k) for k in ('agent_id', 'app_id', 'channel')) == scope
                and isinstance(ref.get('source_event_id'), str)
                and bool(ref['source_event_id']) and type(ref.get('version')) is int
                and ref['version'] > 0)

    visible = []
    missing = []
    for raw_id in request.get('visible_raw_event_ids', ()):
        ref = (raw_to_archive or {}).get(raw_id) if type(raw_id) is int else None
        if not scoped(ref):
            missing.append(raw_id)
        else:
            visible.append(ref['source_event_id'])
    trace = dict(surface=surface, generation_key=request.get('generation_key'),
                 provider_round=request.get('provider_round'),
                 scope=dict(agent_id=agent_id, app_id=app_id, channel=channel))
    if missing and (not visible or surface != 'normal' or not isinstance(application, NiannianReadOnlyRecall)
                    or any(type(raw) is not int or raw <= 0 or raw in (raw_to_archive or {}) for raw in missing)):
        trace.update(errors=[dict(code='unmapped_raw_events')], memories=[], archive=[])
        return dict(rendered='', trace=trace)

    common = dict(channel=channel, visible_archive_ids=visible,
                  visible_memory_ids=request.get('exclude_memory_card_ids', ()))
    if missing:
        common['unmapped_raw_ids'] = frozenset(missing)
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
    if missing:
        trace['errors'] = [*trace.get('errors', ()), dict(code='unmapped_raw_events')]
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
    rendered = result['rendered']
    if (ambient_state is not None and not missing and surface != 'preaction'
            and not (result.get('preference_correction') or result.get('unresolved_reference'))
            and not (surface == 'normal' and constrained_quote_request(request.get('user_text', '')))):
        mode = 'normal' if surface == 'normal' else 'proactive'
        context_text = (request.get('user_text', '') if mode == 'normal'
                        else request.get('now_text', '') or request.get('recent_user_text', ''))
        host_fields = {key: ambient_state[key] for key in (
            'candidate_ids', 'now', 'seed', 'enabled',
            'reflective_bias_by_card_id', 'reflective_affinity_by_card_id',
            'activation_bias_by_card_id', 'user_reactivated_card_ids', 'spoken_card_ids')
            if key in ambient_state}
        ambient_visible = list(visible)
        if mode == 'normal' and host:
            from thalemora.retrieval.recall import trusted_previous_text
            if trusted_previous_text(archive, agent_id, app_id, channel, host) is not None:
                ambient_visible.append(host['previous_assistant_ref']['source_event_id'])
        decision = application.ambient_for_turn(
            ambient_state['engine'], generation_key=request.get('generation_key'),
            epoch=request.get('current_epoch_id'), mode=mode, context_text=context_text,
            related_card_ids=[m['id'] for m in result.get('memories', ())],
            visible_card_ids=request.get('exclude_memory_card_ids', ()),
            visible_archive_ids=ambient_visible,
            quiet=bool(result.get('archive')) or bool(ambient_state.get('quiet')),
            **host_fields)
        if not result.get('memories') and not result.get('archive') and decision['rendered']:
            rendered = (rendered + '\n\n' + decision['rendered']).strip()[:700]
        selected = decision['ambient']
        trace['ambient'] = (dict(card_id=selected['card_id'],
                                 revision_id=selected['revision'],
                                 reason=decision['reason']) if selected else
                            dict(surfaced=False, reason=decision['reason']))
        trace['resonance'] = [dict(card_id=r['card_id'], revision_id=r['revision'],
                                   strength=r['strength']) for r in decision['resonance']]
    return dict(rendered=rendered, trace=trace)


class NiannianReadOnlyRecall:
    """宿主显式授予整份旧 main 库的读权；不注册身份、不认领卡片、不提供写接口。

    host_index_path 由宿主显式指定既有后台索引；否则 index_path 为专用派生索引。
    Archive 须已由宿主同步并建好公共词法索引；这里不安装/迁移任何源表。
    """

    def __init__(self, *, binding, owner, agent_id, app_id, memory_path, providers,
                 index_path=None, host_index_path=None):
        from thalemora.adapters.niannian_history import NiannianHistoryBinding
        if not isinstance(binding, NiannianHistoryBinding):
            raise ValueError('需要可信 NiannianHistoryBinding')
        binding.assert_source(owner, agent_id, app_id, 'main')
        self.binding, self.owner = binding, owner
        self.agent_id, self.app_id = agent_id, app_id
        self.path = binding.archive_db_path(owner, agent_id)
        self._memory_path = Path(memory_path).resolve(strict=True)
        if (index_path is None) == (host_index_path is None):
            raise ValueError('须且只能指定 index_path 或 host_index_path')
        self._host_index = host_index_path is not None
        self.index_path = Path(host_index_path if self._host_index else index_path).resolve(
            strict=self._host_index)
        if self.index_path in {self._memory_path, self.path}:
            raise ValueError('派生索引不能覆盖权威库')
        self.providers = providers

    def _authorize(self, app_id, channel):
        from thalemora.identity import AccessDenied
        self.binding.assert_source(self.owner, self.agent_id, app_id, channel)
        if app_id != self.app_id:
            raise AccessDenied('旧卡读权仅授予绑定来源')

    def archive(self, agent_id, app_id, channel='main'):
        self.binding.archive_db_path(self.owner, agent_id)
        self._authorize(app_id, channel)
        return self

    def memory_path(self, agent_id, app_id, channel='main'):
        self.archive(agent_id, app_id, channel)
        return self._memory_path

    # 生产既有 Archive 没有独立产品的 FTS/短 gram 表；只读扫描授权的现存历史。
    from thalemora.app.service import Application as _Application
    page = staticmethod(_Application.page)
    del _Application

    def search(self, agent_id, app_id, query, channel='main', *, limit=20, offset=0, user_quotes=False, quote_role=None):
        from thalemora.retrieval.core import _ro
        self.page(limit, offset)
        self.archive(agent_id, app_id, channel)
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise ValueError('词法查询须为 1..200 个字符')
        with _ro(str(self.path)) as db:
            role = quote_role or ("user" if user_quotes else None)
            role_args = (role,) if role else ()
            order = ("AND role=? ORDER BY julianday(occurred_at) DESC, source_event_id "
                     if role else "ORDER BY source_event_id ")
            return [dict(row) for row in db.execute(
                'SELECT * FROM history_events WHERE app=? AND channel=? AND deleted=0 '
                'AND instr(lower(text),lower(?))>0 ' + order + 'LIMIT ? OFFSET ?',
                (app_id, channel, query.strip(), *role_args, limit, offset))]

    def evidence(self, agent_id, app_id, card_id, channel='main', *, limit=20, offset=0):
        self.page(limit, offset)
        self.memory_path(agent_id, app_id, channel)
        return self._evidence(card_id)[offset:offset + limit]

    def _evidence(self, card_id):
        return self._evidence_by_card(card_id).get(card_id, [])

    def _evidence_by_card(self, card_id=None, *, card_ids=None, memory_db=None, archive_db=None):
        from thalemora.memory.source_validity import resolve_archive_evidence
        from thalemora.retrieval.core import _ro
        agent_id, app_id, channel = self.agent_id, self.app_id, 'main'
        ids = tuple(card_ids) if card_ids is not None else ((card_id,) if card_id is not None else None)
        if ids == ():
            return {}
        where = ' IN (' + ','.join('?' for _ in ids) + ')' if ids is not None else ''
        args = ids or ()
        with (nullcontext(memory_db) if memory_db is not None else _ro(str(self._memory_path))) as db:
            recorded = {}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='archive_card_scopes'").fetchone():
                recorded = {r[0]: (r[1], r[2]) for r in db.execute(
                    'SELECT card_id,app,channel FROM archive_card_scopes' +
                    (' WHERE card_id' + where if where else ''), args)}
            items = db.execute(
                'SELECT e.*,r.card_id,r.origin,r.change_type FROM card_evidence e JOIN card_revisions r '
                'ON r.id=e.revision_id ' + ('WHERE r.card_id' + where if where else '') +
                ' ORDER BY e.id', args).fetchall()
        grouped = {}
        for row in items:
            grouped.setdefault(row['card_id'], []).append(dict(row))
        with (nullcontext(archive_db) if archive_db is not None else _ro(str(self.path))) as db:
            for cid, evidence in grouped.items():
                outside = recorded.get(cid, (app_id, channel)) != (app_id, channel)
                for item in evidence:
                    try:
                        ref = json.loads(item['source_ref'])
                    except (TypeError, ValueError):
                        ref = None
                    if isinstance(ref, dict) and any(k in ref and ref[k] != value for k, value in
                            (('agent_id', agent_id), ('app', app_id), ('channel', channel))):
                        outside = True
                if outside:
                    grouped[cid] = []
                    continue
                for item in evidence:
                    state = resolve_archive_evidence(db, item, agent_id, app_id, channel)
                    if (state['resolution'] == 'manual_revision'
                            and (item['origin'], item['change_type']) != ('manual_ui', 'manual_edit')):
                        state['resolution'] = 'unsupported'
                    item.update(state)
                    if self._legacy_evidence(item):
                        item.update(resolution='legacy_unverified', verified=False,
                                    current_source_text=None, source_key=None)
        return grouped

    def _legacy_evidence(self, item):
        """缺旧原话证明只标未知；带公共身份的引用不能降级绕过失效核验。"""
        if item['source_kind'] not in {'user_text_exact', 'assistant_text_exact', 'raw_range',
                                       'legacy_unverified', 'manual', 'derived', 'agent_mcp', 'chenlin_mcp',
                                       'bottle_answer'}:
            return False
        try:
            ref = json.loads(item['source_ref'])
        except (TypeError, ValueError):
            ref = None
        if isinstance(ref, dict):
            if any(k in ref and ref[k] != value for k, value in
                   (('agent_id', self.agent_id), ('app', self.app_id), ('channel', 'main'))):
                return False
            if 'source_event_id' in ref or ref.get('kind') == 'mcp_explicit_manual_revision':
                return False
        return True

    @staticmethod
    def _disjoint_from_unmapped(active, raw_ids):
        # 映射不全时，只有人工独立修订或已知不相交的完整原始区间可以使用。
        return bool(active) and all(
            item['resolution'] in {'manual_note', 'manual_revision'}
            or (type(start := item.get('raw_event_start_id')) is int
                and type(end := item.get('raw_event_end_id')) is int and 0 < start <= end
                and all(not start <= raw <= end for raw in raw_ids))
            for item in active)

    def _rows(self, visible_archive_ids, *, unmapped_raw_ids=()):
        from thalemora.retrieval.recall import scoped_rows
        from thalemora.adapters.niannian_retrieval import NiannianRetrievalIndex
        from thalemora.retrieval.core import _ro
        from thalemora.memory.scopes import ownership
        visible_raw = {}
        if visible_archive_ids:
            ids = list(dict.fromkeys(visible_archive_ids))
            with _ro(str(self.path)) as db:
                visible_raw = {r['source_event_id']: r['raw_event_id'] for r in db.execute(
                    'SELECT source_event_id,raw_event_id FROM history_events '
                    'WHERE app=? AND channel=? AND source_event_id IN (' +
                    ','.join('?' for _ in ids) + ')', (self.app_id, 'main', *ids))}
        with _ro(str(self._memory_path)) as db, _ro(str(self.path)) as archive_db:
            db.execute('BEGIN')
            scopes = ownership(db, self.agent_id, 'main')
            # 全部旧卡均无独立产品来源归属时，公共 scoped_rows 必然为空。
            if any(s['state'] == 'known' for s in scopes.values()):
                rows, refs, suppressed = scoped_rows(self._memory_path, self.app_id,
                                                     'main', self.path, self.agent_id)
            else:
                rows, refs, suppressed = [], {}, set()
            current = {r['card_id']: r for r in db.execute(
                'SELECT r.id,r.card_id,r.content,c.content AS card_content FROM cards c '
                'JOIN card_revisions r ON r.id=c.current_revision_id AND r.card_id=c.id')}
            candidates = NiannianRetrievalIndex._memory_rows(str(self._memory_path), connection=db)
            evidence_by_card = self._evidence_by_card(memory_db=db, archive_db=archive_db)
            included = {r['source_key'] for r in rows}
            for row in candidates:
                cid = row['source_id']
                revision = current.get(cid)
                if (row['source_key'] in included or 'extension_id' in row['provenance']
                        or scopes[cid]['state'] != 'unknown' or row['status'] not in {'fresh', 'sunk'}
                        or not revision or revision['id'] != row['provenance']['revision_id']
                        or revision['content'] != revision['card_content']):
                    continue
                evidence = evidence_by_card.get(cid, [])
                active = [e for e in evidence if e['revision_id'] == revision['id']]
                # 所有修订中显式外域引用都不借旧宿主读权放行。
                if (not active or any(e['resolution'] == 'outside_scope' for e in evidence)
                        or any(e['resolution'] not in {'current', 'manual_revision', 'legacy_unverified'}
                               for e in active)):
                    continue
                legacy = [e for e in evidence if e['resolution'] == 'legacy_unverified']
                # shortcut: 无可核对 Raw 范围的旧证据遇已见原话时整卡排除，待宿主提供可信映射后再放宽。
                if visible_archive_ids and any(e['resolution'] == 'legacy_unverified' for e in active) and (
                        any(type(visible_raw.get(s)) is not int for s in visible_archive_ids) or any(
                        type(e['raw_event_start_id']) is not int
                        or type(e['raw_event_end_id']) is not int
                        or e['raw_event_start_id'] > e['raw_event_end_id']
                        or any(type(raw) is int and e['raw_event_start_id'] <= raw <= e['raw_event_end_id']
                               for raw in visible_raw.values()) for e in legacy)):
                    continue
                row['provenance']['legacy_unverified'] = True
                rows.append(row)
                refs[row['source_key']] = {tuple(e['source_key']) for e in active if e['source_key']}
                suppressed.update(tuple(e['source_key']) for e in evidence if e['source_key'])
            if unmapped_raw_ids:
                rows = [r for r in rows if 'extension_id' not in r['provenance']
                        and self._disjoint_from_unmapped(
                            [e for e in evidence_by_card.get(r['source_id'], ())
                             if e['revision_id'] == r['provenance']['revision_id']], unmapped_raw_ids)]
            return rows, refs, suppressed

    def _indexed_rows(self, candidates, visible_archive_ids, *, unmapped_raw_ids=()):
        """只解引用排名候选；索引过期时拒绝，不在聊天中修复后台索引。"""
        from thalemora.memory.scopes import ownership, belongs
        from thalemora.retrieval.core import _ro, _sha
        ids = tuple(dict.fromkeys(c['source_id'] for c in candidates
                    if type(c.get('source_id')) is int
                    and c['source_key'] == f"memory:{c['source_id']}"
                    and c['provenance'].get('card_id') == c['source_id']
                    and 'extension_id' not in c['provenance']))
        if not ids:
            return [], {}, set()
        marks = ','.join('?' for _ in ids)
        with _ro(str(self._memory_path)) as db, _ro(str(self.path)) as archive_db:
            db.execute('BEGIN')
            archive_db.execute('BEGIN')
            scopes = ownership(db, self.agent_id, 'main', card_ids=ids)
            current = {r['id']: r for r in db.execute(
                'SELECT c.*,r.id AS revision_id,r.content AS revision_content FROM cards c '
                'JOIN card_revisions r ON r.id=c.current_revision_id AND r.card_id=c.id '
                f'WHERE c.id IN ({marks})', ids)}
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            retired = ({r[0] for r in db.execute(
                f'SELECT from_card_id FROM memory_card_lineage WHERE from_card_id IN ({marks}) '
                'AND retired_at IS NULL', ids)} if 'memory_card_lineage' in tables else set())
            organized = ({r['card_id']: r for r in db.execute(
                f'SELECT * FROM memory_organized_content WHERE card_id IN ({marks})', ids)}
                if 'memory_organized_content' in tables else {})
            evidence = self._evidence_by_card(card_ids=ids, memory_db=db, archive_db=archive_db)
            visible_raw = {}
            if visible_archive_ids:
                visible = tuple(dict.fromkeys(visible_archive_ids))
                visible_raw = {r[0]: r[1] for r in archive_db.execute(
                    'SELECT source_event_id,raw_event_id FROM history_events WHERE app=? AND channel=? '
                    'AND source_event_id IN (' + ','.join('?' for _ in visible) + ')',
                    (self.app_id, 'main', *visible))}
            rows, refs, suppressed = [], {}, set()
            for candidate in candidates:
                cid = candidate['source_id']
                card = current.get(cid)
                scope = scopes.get(cid)
                if (not card or cid in retired or card['status'] not in {'fresh', 'sunk'}
                        or card['memory_kind'] != 'episodic'
                        or card['content'] != card['revision_content']
                        or candidate['provenance'].get('revision_id') != card['revision_id']
                        or candidate['source_key'] != f'memory:{cid}'
                        or not scope or scope['state'] == 'conflict'
                        or (scope['state'] == 'known' and not belongs(scope, self.app_id, 'main'))):
                    continue
                projection = organized.get(cid)
                body = (projection['content'] if projection is not None
                        and projection['based_on_revision_id'] == card['revision_id'] else card['content'])
                text = ((card['title'] or '').strip() + '\n' + (body or '').strip()).strip()
                if candidate['text'] != text or candidate['content_hash'] != _sha(text):
                    continue
                items = evidence.get(cid, [])
                active = [e for e in items if e['revision_id'] == card['revision_id']]
                allowed = {'current', 'manual_revision'}
                if scope['state'] == 'unknown':
                    allowed.add('legacy_unverified')
                if (not active or (unmapped_raw_ids and not self._disjoint_from_unmapped(active, unmapped_raw_ids))
                        or any(e['resolution'] == 'outside_scope' for e in items)
                        or any(e['resolution'] not in allowed for e in active)):
                    continue
                legacy = [e for e in items if e['resolution'] == 'legacy_unverified']
                if visible_archive_ids and any(e['resolution'] == 'legacy_unverified' for e in active) and (
                        any(type(visible_raw.get(s)) is not int for s in visible_archive_ids)
                        or any(type(e['raw_event_start_id']) is not int
                               or type(e['raw_event_end_id']) is not int
                               or e['raw_event_start_id'] > e['raw_event_end_id']
                               or any(type(raw) is int and e['raw_event_start_id'] <= raw <= e['raw_event_end_id']
                                      for raw in visible_raw.values()) for e in legacy)):
                    continue
                row = dict(candidate, status=card['status'])
                row['provenance'] = dict(candidate['provenance'], keywords=card['keywords'])
                if scope['state'] == 'unknown':
                    row['provenance']['legacy_unverified'] = True
                rows.append(row)
                refs[row['source_key']] = {tuple(e['source_key']) for e in active if e['source_key']}
                suppressed.update(tuple(e['source_key']) for e in items if e['source_key'])
            return rows, refs, suppressed

    def ambient_for_turn(self, engine, **host_state):
        from thalemora.adapters.niannian_ambient import ambient_for_turn
        return ambient_for_turn(self, engine, **host_state)

    def recall_for_turn(self, agent_id, app_id, query, channel='main', *, unmapped_raw_ids=(), **visible):
        from thalemora.retrieval.recall import recall_for_turn
        self.archive(agent_id, app_id, channel)
        from thalemora.retrieval.core import _ro
        if self._host_index:
            return recall_for_turn(self, agent_id, app_id, query, channel,
                                   indexed_memory_rows=(partial(self._indexed_rows, unmapped_raw_ids=unmapped_raw_ids)
                                                        if unmapped_raw_ids else self._indexed_rows),
                                   cards_only=bool(unmapped_raw_ids),
                                   index_path=self.index_path, **visible)
        # data_version 只在同一连接上可比较；缓存与观察连接均仅存活本轮。
        with _ro(str(self._memory_path)) as memory_db, _ro(str(self.path)) as archive_db:
            cached, versions = None, None

            def read_rows(effective_visible_archive_ids):
                nonlocal cached, versions
                current = (memory_db.execute('PRAGMA data_version').fetchone()[0],
                           archive_db.execute('PRAGMA data_version').fetchone()[0])
                if cached is None or current != versions:
                    cached = (self._rows(effective_visible_archive_ids, unmapped_raw_ids=unmapped_raw_ids)
                              if unmapped_raw_ids else self._rows(effective_visible_archive_ids))
                    versions = current
                return cached

            return recall_for_turn(self, agent_id, app_id, query, channel,
                                   memory_rows=read_rows, index_path=self.index_path,
                                   cards_only=bool(unmapped_raw_ids), **visible)
