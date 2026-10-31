"""念念宿主的自然浮现薄接线：候选与反思信号由宿主提供，权威仍在 Memory。"""
import json
import re

from thalemora.retrieval.core import _ro


def ambient_for_turn(adapter, engine, *, candidate_ids, generation_key, epoch, now,
                     context_text='', mode='normal', related_card_ids=(), visible_card_ids=(),
                     visible_archive_ids=(),
                     reflective_bias_by_card_id=None, reflective_affinity_by_card_id=None,
                     activation_bias_by_card_id=None, enabled=False, seed=None,
                     quiet=False, user_reactivated_card_ids=(), spoken_card_ids=()):
    """只核验宿主预选的至多 40 张卡；不维护索引、不生成模型评分或激活回执。"""
    if not enabled:
        return dict(rendered='', ambient=None, resonance=[], reason='disabled')
    if not adapter._host_index:
        raise ValueError('自然浮现需要宿主维护的只读记忆索引')
    ids = tuple(dict.fromkeys(candidate_ids))
    if len(ids) > 40 or any(type(cid) is not int or cid <= 0 for cid in ids):
        raise ValueError('需要至多 40 张受信候选卡 ID')
    adapter.archive(adapter.agent_id, adapter.app_id, 'main')
    if ids:
        with _ro(str(adapter.index_path)) as db:
            selected = db.execute(
                'SELECT source_key,source_id,source_kind,text,content_hash,status,'
                'importance,event_time,created_at,provenance_json FROM chunks '
                'WHERE source_kind=? AND source_id IN (' + ','.join('?' for _ in ids) + ')',
                ('memory_card', *ids)).fetchall()
        candidates = [dict(row, provenance=json.loads(row['provenance_json']))
                      for row in selected]
    else:
        candidates = []
    fresh, refs, _ = adapter._indexed_rows(candidates, visible_archive_ids)
    seen = set(visible_archive_ids)
    fresh = [r for r in fresh if not any(
        ref[3] in seen for ref in refs.get(r['source_key'], ()))]
    by_id = {r['source_id']: r for r in fresh}
    bias = reflective_bias_by_card_id or {}
    affinity = reflective_affinity_by_card_id or {}
    activation = activation_bias_by_card_id or {}
    eligible = []
    for row in fresh:
        cid = row['source_id']
        keywords = row['provenance'].get('keywords') or ''
        if isinstance(keywords, str):
            try:
                keywords = json.loads(keywords)
            except ValueError:
                keywords = re.split(r'[,，、\s]+', keywords)
        if not isinstance(keywords, list):
            keywords = []
        arousal = row['provenance'].get('arousal')
        eligible.append(dict(card_id=cid, revision=row['provenance']['revision_id'],
                             current_revision_id=row['provenance']['revision_id'],
                             status=row['status'], importance=row['importance'] or 0,
                             arousal=.3 if arousal is None else arousal,
                             pinned=bool(row['provenance'].get('pinned')),
                             event_time=row['event_time'], created_at=row['created_at'],
                             keywords=keywords, reflective_bias=bias.get(cid, 0),
                             affinity=affinity.get(cid),
                             activation_bias=activation.get(cid, 0)))
    outcome = engine.step(generation_key, epoch=epoch, now=now, mode=mode,
                          verified_memory_rows=eligible, context_text=context_text,
                          related_card_ids=related_card_ids, visible_card_ids=visible_card_ids,
                          enabled=True, seed=seed, quiet=quiet,
                          user_reactivated_card_ids=user_reactivated_card_ids,
                          spoken_card_ids=spoken_card_ids)
    references = ([outcome['ambient']] if outcome['ambient'] else []) + outcome['resonance']
    chosen = [row for ref in references
              if (row := by_id.get(ref['card_id'])) is not None
              and row['provenance']['revision_id'] == ref['revision']]
    # 一次独立再核验，禁止在决策等待期间被改动的卡进入最终 Context。
    valid, refs, _ = adapter._indexed_rows(chosen, visible_archive_ids)
    valid = [r for r in valid if not any(
        ref[3] in seen for ref in refs.get(r['source_key'], ()))]
    valid_by_id = {r['source_id']: r for r in valid}
    ambient = outcome['ambient']
    if ambient and ambient['card_id'] not in valid_by_id:
        ambient = None
    resonance = [ref for ref in outcome['resonance'] if ref['card_id'] in valid_by_id][:1]
    sections = []
    if ambient:
        sections.append('【偶然想起的旧记忆候选】\n' +
                        valid_by_id[ambient['card_id']]['text'][:260])
    if resonance:
        sections.append('【记忆余韵】\n' + valid_by_id[resonance[0]['card_id']]['text'][:200])
    return dict(rendered='\n\n'.join(sections)[:600], ambient=ambient,
                resonance=resonance, reason=outcome['reason'])
