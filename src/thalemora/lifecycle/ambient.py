"""受信宿主调用的自然浮现；SQLite 仅保存派生决策，不保存 Memory 正文。"""
from __future__ import annotations

from contextlib import closing
import datetime as dt
import hashlib
import json
import math
import random
import re
import sqlite3

from .activation import parse_time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ambient_turns (
    epoch TEXT NOT NULL, generation_key TEXT NOT NULL, result TEXT NOT NULL,
    PRIMARY KEY(epoch, generation_key)
);
CREATE TABLE IF NOT EXISTS ambient_cooldowns (
    card_id INTEGER PRIMARY KEY, surfaced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ambient_echoes (
    epoch TEXT NOT NULL, card_id INTEGER NOT NULL, revision INTEGER NOT NULL,
    strength REAL NOT NULL, unrelated INTEGER NOT NULL,
    PRIMARY KEY(epoch, card_id)
);
"""


def _quiet(text):
    # 只识别明确的场景信号；宿主可用 quiet 显式传递其已知场景。
    for reason, pattern in (
        ('acute_distress', r'崩溃|受不了|撑不住|难受|哭了|委屈|焦虑|难过|伤心|很生气|烦死|害怕|恐惧|打击|不想说以前|不想被转移话题'),
        ('correction', r'根本没|从来没|我没说|我不是|记反了|弄混|理解错|我说的是|不是.*是|猜错|否定过|说的.*相反|不是一个|不是让你'),
        ('focused_task', r'检查|核对|报错|测试|修复|排查|部署|服务器|日志|接口|函数|代码|数据库|构建|执行|任务单|请直接解释|写.{0,8}文档|设计.{0,6}方案'),
    ):
        if re.search(pattern, text):
            return reason
    return ''


def _score(row, now, last):
    importance = min(1., max(0., row.get('importance', 0) / 10))
    activation = min(.20, max(0., row.get('activation_bias', 0)))
    status = row['status']
    if status == 'fresh':
        if importance < .5 and not row.get('pinned'):
            return 0., []
        multiplier = 1.
    else:
        if importance < .8 and activation < .12:
            return 0., []
        multiplier = min(.64, .24 + 2 * activation)
    gap = 1.
    if last:
        days = (now - parse_time(last)).total_seconds() / 86400
        if days < 3:
            return 0., []
        gap = 1 - math.exp(-days / 30)
    reference = parse_time(row.get('activation_last_at')) or parse_time(row.get('created_at'))
    age = max(0., (now - reference).total_seconds() / 86400) if reference else 365.
    recency = math.exp(-age / 45)
    event = parse_time(row.get('event_time'))
    anniversary = bool(event and (event.month, event.day) == (now.month, now.day))
    reflection = min(.1, max(-.1, row.get('reflective_bias', 0)))
    affinity = row.get('affinity')
    score = (.52 * importance + .10 * recency + .14 * gap + .10 * anniversary
             + reflection + activation + .08 * bool(row.get('pinned'))
             + .12 * bool(row.get('boosted')))
    score *= multiplier * (1 if affinity is None else .45 + .55 * min(1., affinity))
    return max(0., score), [f'importance:{importance:.3f}', f'recency:{recency:.3f}',
                           f'surface_gap:{gap:.3f}', f'status_multiplier:{multiplier:.3f}',
                           f'reflection:{reflection:.3f}', f'activation:{activation:.3f}',
                           f'anniversary:{int(anniversary)}']


def _eligible(rows):
    result = {}
    for row in rows:
        cid, revision = int(row['card_id']), int(row['revision'])
        if cid in result:
            raise ValueError('one current revision per card required')
        affinity = row.get('affinity')
        if (cid <= 0 or revision <= 0 or row.get('deleted')
                or revision != row['current_revision_id']
                or row.get('status') not in {'fresh', 'sunk'}
                or (affinity is not None and affinity < .35)
                or (affinity is None and row.get('arousal', .3) >= .85)):
            continue
        result[cid] = row
    return result


class AmbientEngine:
    """每个授权 Agent/来源范围使用独立的派生库，可与其 ActivationLedger 共库。

    构造不建库，enabled 默认 False。verified_memory_rows 必须是宿主在本轮
    核验授权、来源、删除和 canonical revision 后的完整候选（含仍存活的余韵）。
    行字段：card_id/revision/current_revision_id/status/importance，及可选
    arousal/affinity/keywords/created_at/event_time/reflective_bias/activation_bias。
    keywords 是宿主供给的具体话题词，不从正文猜测身份或私人关键词。
    epoch、generation_key 均由宿主提供；同 epoch 的 key 必须唯一且不可复用。

    user_reactivated_card_ids / spoken_card_ids 仅接受宿主已核验并通过现有
    ActivationLedger.record 写入的真实 user_reactivated / used 回执；updated
    同样由原写入方记录。本模块不写激活，不将 Related、检索或 injected 当回执。
    输出仅含引用；宿主组装最终 Context 前仍须复核版本与权限。
    """

    def __init__(self, db_path):
        self.db_path = str(db_path)

    def step(self, generation_key, *, epoch, verified_memory_rows, now,
             mode='normal', context_text='', related_card_ids=(), visible_card_ids=(),
             enabled=False, seed=None, quiet=False, user_reactivated_card_ids=(),
             spoken_card_ids=()):
        empty = dict(ambient=None, resonance=[], reason='disabled')
        if not enabled:
            return empty
        if not generation_key or not epoch or mode not in {'normal', 'proactive'}:
            raise ValueError('trusted epoch/key and normal or proactive mode required')
        now = parse_time(now)
        if now is None:
            raise ValueError('valid now required')
        rows = _eligible(verified_memory_rows)
        visible, related = set(visible_card_ids), set(related_card_ids)
        blocked = 'host_quiet' if quiet else _quiet(context_text)
        if related:
            blocked = blocked or 'related_present'

        def current(ref):
            row = rows.get(ref['card_id'])
            return (row is not None and row['revision'] == ref['revision']
                    and ref['card_id'] not in visible | related)

        def output(result):
            # 重放也重核当前候选，绝不从旧决策恢复失效正文或旧版本。
            result = dict(result)
            if result['ambient'] and (blocked or not current(result['ambient'])):
                result['ambient'] = None
                result['reason'] = blocked or 'reference_unavailable'
            result['resonance'] = [r for r in result['resonance'] if not blocked and current(r)]
            return result

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                prior = conn.execute('SELECT result FROM ambient_turns WHERE epoch=? AND generation_key=?',
                                     (epoch, generation_key)).fetchone()
                if prior:
                    return output(json.loads(prior['result']))
                echoes = []
                for saved in conn.execute('SELECT * FROM ambient_echoes WHERE epoch=?', (epoch,)):
                    echo = dict(saved)
                    if not current(echo):
                        continue
                    echo['strength'] *= .72
                    echo['unrelated'] += 1
                    if echo['card_id'] in user_reactivated_card_ids:
                        echo['strength'] = min(1., echo['strength'] + .25)
                        echo['unrelated'] = 0
                    if echo['card_id'] in spoken_card_ids:
                        echo['strength'] *= .45
                    if echo['strength'] >= .18 and echo['unrelated'] < 4:
                        echoes.append(echo)
                echoes.sort(key=lambda e: (-e['strength'], e['card_id']))
                result = dict(ambient=None, resonance=[], reason=blocked or 'probability_gate')
                rng = random.Random(int.from_bytes(hashlib.sha256(
                    str(generation_key if seed is None else seed).encode()).digest()[:8], 'big'))
                probability = (.08 if mode == 'normal' else .25) * (0 if len(echoes) >= 2 else .35 if echoes else 1)
                if not blocked and rng.random() < probability:
                    cooldowns = dict(conn.execute('SELECT card_id,surfaced_at FROM ambient_cooldowns'))
                    pool = []
                    for cid, row in sorted(rows.items()):
                        if cid in visible | related or any(e['card_id'] == cid for e in echoes):
                            continue
                        score, reasons = _score(row, now, cooldowns.get(cid))
                        cues = [term for term in row.get('keywords', ()) if len(term) >= 2 and term in context_text]
                        event = parse_time(row.get('event_time'))
                        calendar = bool(event and (event.month, event.day) == (now.month, now.day)
                                        and re.search(r'生日|纪念|日历|日期|这个日子', context_text))
                        cue = min(1., .70 + .04 * len(max(cues, key=len))) if cues else .82 if calendar else 0
                        if score > 0:
                            pool.append(dict(card_id=cid, revision=row['revision'], score=score * (1 + cue),
                                             reason=reasons + ['topic_cue' if cue else 'rare_unprompted'], cue=cue))
                    aligned = [p for p in pool if p['cue']]
                    if aligned:
                        pool = aligned
                    elif rng.random() < .08:
                        pool = [p for p in pool if rows[p['card_id']].get('arousal', .3) < .55
                                and rows[p['card_id']].get('importance', 0) >= 8]
                    else:
                        pool = []
                    result['reason'] = 'no_eligible_candidate'
                    if pool:
                        pool.sort(key=lambda p: (-p['score'], p['card_id']))
                        pool = pool[:40]
                        chosen = rng.choices(pool, weights=[p['score'] ** 2 for p in pool], k=1)[0]
                        chosen.pop('cue')
                        result.update(ambient=chosen, reason='surfaced')
                        conn.execute('INSERT OR REPLACE INTO ambient_cooldowns VALUES (?,?)',
                                     (chosen['card_id'], now.isoformat()))
                        echoes.append(dict(epoch=epoch, card_id=chosen['card_id'], revision=chosen['revision'],
                                           strength=1., unrelated=0))
                conn.execute('DELETE FROM ambient_echoes WHERE epoch=?', (epoch,))
                for echo in echoes[:2]:
                    conn.execute('INSERT INTO ambient_echoes VALUES (?,?,?,?,?)',
                                 (epoch, echo['card_id'], echo['revision'], echo['strength'], echo['unrelated']))
                surfaced = result['ambient']['card_id'] if result['ambient'] else None
                result['resonance'] = [dict(card_id=e['card_id'], revision=e['revision'],
                                            strength=e['strength'], reason='lingering')
                                       for e in echoes if e['card_id'] != surfaced
                                       and (mode != 'normal' or e['unrelated'] <= 1)][:1 if mode == 'normal' else 2]
                conn.execute('INSERT INTO ambient_turns VALUES (?,?,?)',
                             (epoch, generation_key, json.dumps(result)))
                return output(result)
