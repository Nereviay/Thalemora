"""历史召回、稳定事实、时间线与内在状态选择的公共默认配置。"""

import re


WHISPER_RECALL_CANDIDATES = 3

WHISPER_RECALL_COOLDOWN = 6 * 60 * 60

WHISPER_INJECT_MAX = 3

WHISPER_CONTEXT_WINDOW = 14 * 24 * 3600

_CONTEMPLATION_RECALL_INTERVAL = 60

_CONTEMPLATION_TRIGGER_KEYWORDS = ("沉思", "想", "发呆", "放空", "冥想", "发愣")

MEMORY_MIN_SCORE = 0.8

MEMORY_EXPLICIT_FACT_MIN_SCORE = 0.75

MEMORY_EXPLICIT_FACT_MIN_IMPORTANCE = 8.5

MEMORY_INJECT_TOP_N = 1

MEMORY_INJECT_MAX_CHARS = 220

TIMELINE_RECALL_MAX_CHARS = 1600

STABLE_USER_FACT_MIN_IMPORTANCE = 9.0

STABLE_USER_FACT_MAX_CARDS = 2

STABLE_USER_FACT_MAX_CHARS = 420

STABLE_USER_FACT_CACHE_TTL = 600

MEMORY_QUERY_TRIGGER_KEYWORDS = (
    "记得", "记不记得", "记住", "忘了", "以前", "之前", "上次", "那次",
    "哪次", "什么时候", "哪天", "几号", "生日", "名字", "喜欢", "讨厌",
    "害怕", "约定", "答应", "说过", "提过", "聊过", "挑食", "忌口", "口味",
    "不吃", "爱吃", "吃什么",
    "昨天", "今天", "昨晚", "昨夜", "今晚", "前天", "大前天",
    "凌晨", "早晨", "早上", "今早", "上午", "中午", "下午",
    "傍晚", "晚上", "夜晚", "夜里", "深夜", "白天",
)

HISTORY_REFERENCE_PHRASES = (
    "不是和你说过", "不是跟你说过", "我不是说过", "我之前说过",
    "我以前说过", "我跟你说过", "我和你说过", "之前跟你说过",
    "之前和你说过", "上次说过", "你还记得", "还记得我",
    "记不记得", "你记得我", "你忘了", "忘了吗", "说过吗", "提过吗", "聊过吗",
)

HISTORY_RECALL_RECENT_EXCLUDE_IDS = 24

HISTORY_RECALL_MAX_WINDOWS = 2

HISTORY_RECALL_CONTEXT_RADIUS = 3

HISTORY_RECALL_MAX_CHARS = 1600

HISTORY_RECALL_STOPWORDS = frozenset({
    "不是", "说过", "提过", "聊过", "记得", "忘了", "以前", "之前", "上次",
    "这个", "那个", "事情", "东西", "一个", "一个人", "人去", "个人", "一起", "自己",
    "我们", "你们", "怎么", "什么", "为什么", "时候",
    "已经", "还是", "就是", "现在", "后来",
})

TOPIC_HISTORY_PAST_CUES = ("那次", "当时", "以前", "之前", "上次", "后来")

TOPIC_HISTORY_GENERIC_TERMS = frozenset({
    "包子", "早餐", "午饭", "晚饭", "吃饭", "睡觉", "衣服", "鞋子", "手机",
    "电脑", "工作", "上班", "回家", "出门", "聊天", "消息", "照片", "图片",
    "演唱会", "考试", "电影", "旅行", "比赛", "活动", "见面", "约会",
    "好吃", "难吃", "好看", "不好看", "喜欢", "讨厌",
})

TOPIC_HISTORY_FILLER_TERMS = frozenset({
    "应该", "感觉", "本来", "过来", "要是", "怎么办", "这么", "莫名其妙", "这才", "几点",
    "有时候", "不想", "看着", "给你个", "不好", "关键", "太好了", "只有",
    "两个", "他们", "以为", "这种", "里面", "多次", "第一次", "是因为",
    "听说", "真的", "看看", "是不是", "小时", "分钟", "秒钟", "回来", "不到", "两口", "世界",
})

TOPIC_HISTORY_CONTEXTUAL_GENERIC_TERMS = frozenset({
    "演唱会", "考试", "电影", "旅行", "比赛", "活动", "见面", "约会",
})

TOPIC_HISTORY_RECENT_CONTEXT_MESSAGES = 6

TOPIC_HISTORY_CONTEXT_TURNS = 2

TOPIC_HISTORY_MAX_MESSAGES = 10

TOPIC_HISTORY_MAX_CHARS = 1400

TOPIC_HISTORY_CONTEXT_MAX_GAP_SECONDS = 30 * 60

TOPIC_HISTORY_STAGE_DIRECTION_RE = re.compile(
    r"（[^（）]{0,160}）|\([^()]{0,160}\)"
)

TOPIC_HISTORY_RELATIVE_TIME_RE = re.compile(
    r"^[一二三四五六七八九十百千万两几\d]+(?:秒|分钟|小时|天|周|个月|月|年)$"
)

_INJECT_CARD_COOLDOWN = 1200

_INJECT_TOPIC_COOLDOWN = 900
