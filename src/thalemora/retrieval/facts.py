"""保守的结构声明比对；只派生提示，不负责语义决策或正式修订。"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import re
import sqlite3


def statement(text):
    # shortcut: 只识别显式“主体的属性是值”；自然口语留给后续语义流程。
    text = text.strip().rstrip('。！!')
    status = 'needs_clarification'
    if text.startswith('我之前记错了，') or text.startswith('我之前记错了,'):
        text = text[7:].strip()
        status = 'explicit_correction'
    if re.search(r'假设|如果|听说|据说|别人说|例如|比如|可能|以前|之前|曾经|[？?吗呢]|[“”「」"\n，,；;。！!]', text):
        return None
    if text.startswith(('现在', '已经', '后来')):
        text = text[2:].strip()
        if status != 'explicit_correction':
            status = 'change'
    match = re.fullmatch(r'([^的是]+)的([^的是]+?)(现在|已经|后来)?是([^是]+)', text)
    if not match:
        return None
    subject, predicate, time, value = (part.strip() if part else '' for part in match.groups())
    if not all((subject, predicate, value)) or subject in {'他', '她', '它', '你', '他们', '她们'}:
        return None
    if time and status != 'explicit_correction':
        status = 'change'
    return dict(subject=subject, predicate=predicate, value=value, status=status, text=text)


def compare_statement(query, rows):
    """rows 必须已按当前权限、修订和证据复核；唯一性在可见过滤前判断。"""
    new = statement(query)
    if new is None:
        return None
    matches = []
    for row in rows:
        if 'extension_id' in row['provenance'] or row['status'] in {'trashed', 'archived', 'retired'}:
            continue
        claims = []
        for sentence in re.split(r'[\n。！!]', row['text']):
            old = statement(sentence)
            if old and (old['subject'], old['predicate']) == (new['subject'], new['predicate']):
                claims.append(old)
        if claims:
            matches.append((row, claims))
    if len(matches) != 1 or len(matches[0][1]) != 1:
        return None
    row, (old,) = matches[0]
    if old['value'] == new['value']:
        return None
    # 超长声明不是紧凑事实；不为它挤占普通召回结果预算。
    if len(old['value']) > 80 or len(new['value']) > 80:
        return None
    return dict(id=row['source_id'], revision_id=row['provenance']['revision_id'],
                subject=new['subject'], predicate=new['predicate'], old_statement=old['text'],
                new_statement=query, status=new['status'])


def render_hint(hint):
    notes = {
        'needs_clarification': '待用户确认，不自动更新；请在正常聊天中自然澄清。',
        'explicit_correction': '用户已明确纠正旧说法，交正式修订流程（06）处理；无需重复询问，不自动写入。',
        'change': '用户描述事实发生变化，不代表旧事实当时错误；交正式修订流程（06）处理，不自动写入。',
    }
    return (f"[记忆事实 {hint['status']} Memory {hint['id']} rev {hint['revision_id']}] "
            f"原事实：{hint['old_statement']}；用户本轮新说法：{hint['new_statement']}。"
            + notes[hint['status']])


# 完整的本人改口句才成立。两种说法都必须有明确旧值和新值。
_VALUE = r"[\w·-]{1,30}?"
_VERB = r"[点喝吃买用选穿听读去]"
_CHANGE = r"(?:换成|改成|换回|改回|改为)"
_CHANGES = (
    re.compile(rf"(?:我)?(?:现在|以后|已经)?{_CHANGE}(?P<new>{_VALUE})(?:了|啦)?[，,]"
               rf"(?:我)?(?:以后)?不再(?P<verb>{_VERB})(?P<old>{_VALUE})(?:了)?"),
    re.compile(rf"(?:我)?不再(?P<verb>{_VERB})(?P<old>{_VALUE})(?:了)?[，,]"
               rf"(?:我)?(?:以后|现在|已经)?(?:{_CHANGE}|改(?P=verb))(?P<new>{_VALUE})(?:了|啦)?"),
    re.compile(rf"不是(?P<old>{_VALUE})[，,]我的(?P<topic>{_VALUE})(?:现在|以后|已经)?"
               rf"{_CHANGE}(?P<new>{_VALUE})(?:了|啦)?"),
    re.compile(rf"我的(?P<topic>{_VALUE})(?:现在|以后|已经)?从(?P<old>{_VALUE})"
               rf"{_CHANGE}(?P<new>{_VALUE})(?:了|啦)?"),
)
_HABIT = re.compile(rf"我(?:平时|平常|通常|一般|一直|经常|常|习惯|喜欢)(?:都)?(?P<verb>{_VERB})(?P<value>{_VALUE})")
_RESTORED = re.compile(rf"我(?:现在|后来|已经)?又?(?:换回|改回)(?P<value>{_VALUE})(?:了|啦)?")
_TEMPORARY = re.compile(r"今天|这次|这回|暂时|临时|这几天|如果|假如|假设|可能|比如|也许|打算|想试|以前|当时|那时")


def preference_change(text):
    text = text.strip().rstrip('。！!')
    if len(text) > 100 or _TEMPORARY.search(text):
        return None
    for pattern in _CHANGES:
        match = pattern.fullmatch(text)
        if match and match['old'] != match['new']:
            return match.groupdict()
    return None


def _latest_possible_time(stamp):
    # 旧修订的建档时间可能只有本地钟面值。留足时区差，重叠时不判定先后。
    instant = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    return instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc) + timedelta(hours=12)


def preference_evidence(archive, memory_path, app_id, channel, query, item):
    """在当前习惯卡与更新的本人原话中择一。非习惯查询返回 None，同刻矛盾返回空材料。"""
    if ('extension_id' in item or re.search(r"以前|之前|原来|当时|上次|曾经|原话", query)
            or not re.search(r"我(?:的|平时|平常|通常|一般|一直|经常|常|最|习惯|喜欢|爱|[点喝吃买用选穿听读去])", query)):
        return None
    habit = _HABIT.fullmatch(item['text'].split('\n')[-1].strip().rstrip('。！!'))
    if not habit:
        return None
    old = habit['value']
    topic = next((old[i:] for i in range(len(old) - 1) if old[i:] in query), None)
    if not topic:
        return None
    # 迁移旧卡的创建时间只能证明何时入库。已修订的卡以本次修订为界。
    with closing(sqlite3.connect(memory_path)) as db:
        revision = db.execute(
            "SELECT r.origin,r.revision_no,r.created_at,r.happened_at FROM card_revisions r "
            "JOIN cards c ON c.current_revision_id=r.id AND c.id=r.card_id WHERE c.id=? AND r.id=?",
            (item['id'], item['revision_id'])).fetchone()
    if not revision:
        return {}
    origin, number, created, happened = revision
    boundaries = [happened] if happened else []
    undated_legacy = origin == 'migration' and item.get('evidence_resolution') == 'legacy_unverified'
    if number > 1 or (not item['source_refs'] and not happened and not undated_legacy):
        boundaries.append(created)
    with closing(sqlite3.connect(archive.path)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        for _, _, _, source, version in item['source_refs']:
            evidence = db.execute('SELECT occurred_at FROM history_events WHERE app=? AND channel=? '
                                  'AND source_event_id=? AND version=? AND deleted=0',
                                  (app_id, channel, source, version)).fetchone()
            if not evidence:
                return {}
            if not evidence[0]:
                return item
            boundaries.append(evidence[0])
        try:
            after = max((_latest_possible_time(t) for t in boundaries), default=None)
        except (TypeError, ValueError, AttributeError):
            return item
        rows = db.execute(
            "SELECT source_event_id,version,text,occurred_at FROM history_events "
            "WHERE app=? AND channel=? AND role='user' AND deleted=0 AND instr(text,?)>0 "
            "AND julianday(occurred_at) IS NOT NULL ORDER BY julianday(occurred_at),source_event_id",
            (app_id, channel, topic))
        value = old.removesuffix(topic) or old
        winner = None
        last_time = None
        for row in rows:
            text = row['text'].strip().rstrip('。！!')
            change = preference_change(text)
            habit_now = _HABIT.fullmatch(text)
            restored = _RESTORED.fullmatch(text)
            if change:
                if change.get('topic') and change['topic'] != topic:
                    continue
                if change.get('verb') and (change['verb'] != habit['verb'] or not change['old'].endswith(topic)):
                    continue
                before = change['old'].removesuffix(topic) or change['old']
                new = change['new']
            elif (restored or (habit_now and habit_now['verb'] == habit['verb'])) and not _TEMPORARY.search(text):
                new = (restored or habit_now)['value']
                if not new.endswith(topic):
                    continue
                before = None
            else:
                continue
            try:
                instant = datetime.fromisoformat(row['occurred_at'].replace('Z', '+00:00'))
            except (TypeError, ValueError):
                continue
            if instant.tzinfo is None:
                continue
            if after and instant <= after:
                continue
            if last_time == instant:
                # 同一时刻的两条不同陈述没有可信先后，等待下一条明确本人陈述。
                if winner and winner['text'] != row['text']:
                    winner, value = {}, None
                continue
            if before is not None and before != value:
                continue
            value = new.removesuffix(topic) or new
            last_time = instant
            winner = dict(kind='archive', id=row['source_event_id'], version=row['version'],
                          text=row['text'], role='user', occurred_at=row['occurred_at'])
        return item if winner is None else winner
