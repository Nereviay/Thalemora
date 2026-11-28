"""普通聊天中由宿主核对过的前文和明确的指代。"""
from contextlib import closing
import re
import sqlite3


_DIGITS = "一二三四五六七八九"
_ACK = re.compile(r"(?:好[呀啊哒的啦]?|嗯[嗯哼]?|可以|行[呀啊]?|明白[了]?|收到)[。！!～~]?$")
_POSITION = re.compile(r"(?:不是([前后])(?:面|边)?那个[，,]\s*我说的是)?"
                       r"(?:第([一二三四五六七八九1-9])个|([前后])(?:面|边)?(?:的)?那个)"
                       r"(?:呢|怎么样|怎么(?:订|去|用|选|做)(?:来着)?)?[。！？!?]?$")
_LOCATION = re.compile(r"(?:那边|那里|那家|那个地方|那个)(?:呢|(?:怎么|如何|在哪|什么时候).{1,16})[？?]?$")
_SPLIT = re.compile(r"[，,、]?\s*(?:或者|还是|或是)\s*")
_NOUN = r"[\w·-]{1,30}"
MAX_TURN_CHARS = 8192


def question_focus(text):
    """一条消息可以铺垫很久。把明确标出的实际问题用于词法检索。"""
    parts = re.split(r"(?:^|[。！？!?；;，,\n])\s*(?:我?只(?:是)?想问(?:一下)?|我?只问(?:一下)?|所以(?:我想问)?)", text)
    if len(parts) > 1 and parts[-1].strip():
        return parts[-1].strip()[:200]
    if len(text) > 200:
        sentences = re.findall(r"[^。！？!?\n]+[？?]", text)
        return (sentences[-1] if sentences else text[-200:]).strip()[-200:]
    return text


def reference_request(query):
    return isinstance(query, str) and bool(_POSITION.fullmatch(query.strip()) or _LOCATION.fullmatch(query.strip()))


def acknowledged_followup(query, previous):
    return reference_request(query) and isinstance(previous, str) and bool(_ACK.fullmatch(previous.strip()))


def _number(value):
    return int(value) if value.isdigit() else _DIGITS.index(value) + 1


def prior_assistant_query(query, previous):
    """只使用有唯一对象的序号、二选一或明确提过的单一地点。"""
    q = query.strip()
    position = _POSITION.fullmatch(q)
    if position:
        pairs = re.findall(r"^\s*([1-9一二三四五六七八九])[、.．）)]\s*(.+)$", previous, re.M)
        if pairs:
            if [_number(n) for n, _ in pairs] != list(range(1, len(pairs) + 1)):
                return query
            choices = [text.strip() for _, text in pairs]
        else:
            body = previous.strip().rstrip("。！？!?")
            choices = _SPLIT.split(body)
            if len(choices) != 2:
                return query
            lead = re.fullmatch(r"(?:我们|你|我)?(?:可以|想要|想|要)(去|选|喝|吃|看|订)(.+)", choices[0])
            if lead:
                choices = [lead[2], choices[1].removeprefix(lead[1])]
            if not all(re.fullmatch(_NOUN, c) for c in choices):
                return query
        rejected, number, direction = position.groups()
        if (direction or rejected) and len(choices) != 2:
            return query
        index = _number(number) - 1 if number else int(direction == '后')
        if rejected and index == int(rejected == '后'):
            return query
        if index < len(choices) and 0 < len(choices[index]) <= 200:
            return choices[index]
    if _LOCATION.fullmatch(q):
        topic = re.fullmatch(r"(?:我们|你|我)?(?:刚刚|刚才)?(?:在讨论|说的是|提到的是|聊的是)"
                             r"(" + _NOUN + r")[。！!]?", previous.strip())
        if topic and not re.search(r"或者|还是|和|以及|、", topic[1]):
            return topic[1]
    # 被否认的选项不能当作用户偏好；只用用户明说的动作/主题查已有记忆。
    denial = re.fullmatch(r"我(?:从来|根本|压根)?(?:都)?(?:没|没有|不曾|不)"
                          r"([玩看听吃喝用去读买做打])过?[这那](?:两个|两|俩|几)"
                          r"(?:款|部|个|种|本)?([\u4e00-\u9fff]{2,5})[。！？!?]?", query.strip())
    if denial and re.search(r"还是|或者|或是|\bor\b", previous, re.I):
        verb, topic = denial.groups()
        if topic not in {"东西", "事情", "这个", "那个"}:
            return f"我平常{verb}什么{topic}？"
    return query


def trusted_previous_text(archive, agent_id, app_id, channel, host_context, *, role="assistant"):
    """宿主确认前驱关系；档案复核完整来源、版本、正文与真实窗口，不补造 epoch。"""
    if not host_context:
        return None
    prior = host_context.get('previous_assistant_ref') or {}
    ref = prior if role == 'assistant' else prior.get('acknowledged_user_ref') or {}
    if ((ref.get('agent_id'), ref.get('app_id'), ref.get('channel')) != (agent_id, app_id, channel)
            or not host_context.get('conversation_id') or not host_context.get('current_epoch_id')):
        return None
    with closing(sqlite3.connect(archive.path)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM history_events WHERE app=? AND channel=? AND source_event_id=?",
                         (app_id, channel, ref.get('source_event_id'))).fetchone()
    if (row and not row['deleted'] and row['role'] == role
            and row['version'] == ref.get('version') and row['text'] == ref.get('text')
            and row['text'] and row['conversation_id'] == host_context['conversation_id']
            and row['epoch_id'] == host_context['current_epoch_id']):
        return row['text']
    return None
