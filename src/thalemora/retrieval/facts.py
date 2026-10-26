"""保守的结构声明比对；只派生提示，不负责语义决策或正式修订。"""
import re


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
