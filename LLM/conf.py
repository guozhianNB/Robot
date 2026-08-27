# -*- coding: utf-8 -*-
r"""
集中配置：路径、默认设置、常量。
所有模块从这里拿路径/默认值，避免散落魔法字符串。
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent   # 项目根
DATA_DIR = Path(__file__).resolve().parent / "data"  # LLM 侧数据目录
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "brain.db"          # SQLite：档案/记忆/提醒/工具日志/对话历史/设置
AUDIT_LOG = DATA_DIR / "audit.jsonl"     # 审计日志（对话/记忆改动/提醒/工具调用，JSON Lines）
FEEDS_FILE = DATA_DIR / "feeds.json"     # 新闻 RSS 源配置

# ---- 默认设置（与前端"设置页"一一对应，可持久化覆盖）----
DEFAULT_SETTINGS = {
    "web_search_enabled": True,     # 联网搜索/新闻工具开关
    "proactive_enabled": False,     # 主动交互（目前只做广播通道，未接自动话术）
    "auto_switch_user": False,      # 自动切换老人（未接人脸/声纹，占位）
    "reminder_enabled": True,       # 定时提醒总开关
    "thinking_router_enabled": True,  # 思考路由层总开关
    "router_llm_enabled": True,     # 思考路由：规则未命中时用 LLM 快速预判兜底
    "memory_consolidation_enabled": True,  # 记忆整理（话题结束后批量沉淀）
    "consolidate_idle_sec": 30,     # 对话空闲多久秒后视为"话题结束"触发记忆整理
    "asr_enabled": True,            # 语音识别（真实开关）
    "tts_enabled": True,            # 语音合成（真实开关）
    "voice_enabled": True,          # 语音链路总开关（启动时是否拉起 worker）
    "wakeword": "小机器人",          # 唤醒词（显示用；实际检测用 kws_keywords.txt）
    "handsfree_seconds": 30,        # 免唤醒连续对话窗口
    "spk_threshold": 0.40,          # 声纹余弦阈值（实测校准：真人 0.46-0.47 / 异人 0.14，取 0.40 留余量）
    "alarm_enabled": True,          # 报警上报开关（未确认提醒升级时写审计日志）
    "silent_start": "22:00",        # 静默时段开始（主动播报不发）
    "silent_end": "07:00",          # 静默时段结束
    "confirm_timeout_min": 30,      # 提醒送达后多少分钟未确认 → 升级"未确认"
    "migrate_done": False,        # 记忆 v3 一次性迁移是否已完成
}

# 声纹录制
VOICE_ENROLL_SECONDS = 15        # 注册/追加默认录制秒数
VOICE_PENDING_TTL_S = 600        # 录制暂存（特征+音频）内存保留时长

# 思考路由 · 第一层：主题/敏感/健康关键词（命中即深思考）
THINKING_KEYWORDS = [
    # 健康/药物
    "药", "剂量", "副作用", "血压", "血糖", "心脏", "肿瘤", "癌", "过敏", "疫苗",
    "手术", "住院", "检查", "体检", "失眠", "头晕", "恶心", "呕吐", "胸口", "心慌",
    "摔倒", "救命", "不舒服", "难受", "病", "痛", "疼",
    # 敏感/安全
    "想死", "不想活", "自杀", "遗嘱", "遗产", "怎么办", "为什么",
    # 时事/政治/复杂话题
    "政治", "国家", "政府", "党", "革命", "国际", "美国", "形势", "时事", "政策",
    "改革", "经济", "军事", "战争", "选举", "股票", "基金", "投资", "官司", "法院",
    "养老", "退休", "补贴", "敏感",
]

# 思考路由 · 第二层：情绪/负面词（语气强烈时即使主题不在关键词表也深思考）
# 注意：只用多字词和含义明确的单字（恨/哭/骂），避免"气"误中"天气"这类中性词
THINKING_EMOTION_WORDS = [
    "生气", "气死", "气人", "受气", "讨厌", "心烦", "烦死", "委屈", "孤独",
    "寂寞", "害怕", "担心", "难过", "伤心", "哭", "骂", "恨", "愤怒",
    "欺负", "看不起", "心慌", "焦虑", "烦躁", "憋屈",
]

# LLM 预判触发条件：规则未命中且消息足够长（短问候语不做预判，省延迟）
ROUTER_LLM_MIN_LEN = 10

# 上下文管理：滚动窗口保留最近多少条消息；超过多少条触发历史摘要
HISTORY_WINDOW = 20       # 单次请求携带的最近消息数
SUMMARY_THRESHOLD = 30    # 累计超过该条数 → 后台生成摘要并裁剪

# 记忆分级：哪些类型自动入库、哪些必须人工
MEMORY_RULES = {
    "medical": "manual_only",      # 医疗字段只允许人工录入
    "preference": "pending",       # 偏好 → 待处理，人工确认
    "event": "confirmed",          # 事件 → 直接入库，带 TTL
    "fact": "pending",             # 一般事实 → 待处理
}
EVENT_TTL_DAYS = 30                # 事件记忆默认时效
EPISODE_TTL_DAYS = 90              # 经历片段（Episode）记忆时效：一段对话摘要，比单条事件更久

# 提醒状态机
REMINDER_STATUS = ["pending", "triggered", "unconfirmed", "confirmed", "missed"]

# ---- 记忆系统 v3：embedding ----
EMBED_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
EMBED_MODEL = "text-embedding-v3"
EMBED_DIM = 1024          # 阿里 text-embedding-v3 默认维度；n-gram 回退也映射到此维度
EMBED_TIMEOUT = 10

# ---- 记忆系统 v3：检索与分流 ----
MEMORY_TOP_K = 3                # RAG 检索 Top-K
CORE_MEMORY_CAP = 30            # 核心记忆全量注入条数上限
CORE_MEMORY_CHAR_CAP = 2000     # 核心记忆注入字符上限
CORE_IMPORTANCE_THRESHOLD = 3   # importance >= 3 且核心层 type 才进 core_memories
GRAPH_REL_TYPES = ["likes", "dislikes", "family", "related_to", "happened_at"]

# 身份字段红线：模型永不写、永不改（护士只读档案）
IDENTITY_KEYWORDS = ["姓名", "年龄", "生日", "性别", "床位", "床号", "昵称", "称呼"]

# LLM 参数
MODEL = "deepseek-v4-flash"
LLM_TIMEOUT = 60
