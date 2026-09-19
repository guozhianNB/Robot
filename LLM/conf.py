# -*- coding: utf-8 -*-
r"""
集中配置：路径、默认设置、常量。
所有模块从这里拿路径/默认值，避免散落魔法字符串。
"""
from pathlib import Path
import os

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent   # 项目根
load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(__file__).resolve().parent / "data"  # LLM 侧数据目录
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "brain.db"          # SQLite：档案/记忆/提醒/工具日志/对话历史/设置
AUDIT_LOG = DATA_DIR / "audit.jsonl"     # 审计日志（对话/记忆改动/提醒/工具调用，JSON Lines）
PROMPT_DIR = Path(__file__).resolve().parent / "agent" / "prompt"
PROMPT_FILE = PROMPT_DIR / "base.md"  # System Prompt 模板（人设+红线，外置便于查看/修改）
REACT_PROMPT_FILE = PROMPT_DIR / "react.md"  # ReAct 工具决策规则（每次请求实时读取）
FACTORY_PASSWORD = os.environ.get("PASSWORD", "").strip()  # 管理员出厂口令；仅用于显式恢复，不覆盖当前口令

# ---- 默认设置（与前端"设置页"一一对应，可持久化覆盖）----
DEFAULT_SETTINGS = {
    "proactive_enabled": False,     # 主动交互（目前只做广播通道，未接自动话术）
    "auto_switch_user": False,      # 自动切换老人（未接人脸/声纹，占位）
    "reminder_enabled": True,       # 定时提醒总开关
    "thinking_router_enabled": True,  # 思考路由层总开关
    "router_llm_enabled": True,     # 思考路由：规则未命中时用 LLM 快速预判兜底
    # 思考档位阶梯（规格 docs/superpowers/specs/2026-09-17-thinking-mode-switch-design.md D5）：
    # auto=照思考路由（日常快答/敏感问题自动加深）；none=不思考（**敏感词安全网保留**）；
    # low/high/max=强制思考的轻/中/重度（映射 DeepSeek 顶层 reasoning_effort=low/high/max；
    # 官方 minimal/medium/xhigh/ultra 会归并到 low/high/max，实测传 ultra 直接 400）。
    # 旧值 on→high、off→none 由 chat._resolve_thinking_mode 兼容，无需数据迁移。
    # 非特权键：kiosk 端也要能切（语音轮次不过前端，只有落库的设置才能让语音也吃到手动档位）。
    "thinking_mode": "auto",
    "memory_consolidation_enabled": True,  # 记忆整理（话题结束后批量沉淀）
    "consolidate_idle_sec": 30,     # 对话空闲多久秒后视为"话题结束"触发记忆整理
    "recycle_purge_days": 30,       # 回收站软删记忆保留多少天后物理清理
    "asr_enabled": True,            # 语音识别（真实开关）
    "asr_provider": "cloud",        # 识别引擎：cloud=火山流式识别（默认；不可用自动回退本地）/ local=sherpa（重启生效，worker 启动时读取）
    "tts_enabled": True,            # 语音合成（真实开关）
    "tts_provider": "cloud",        # 合成引擎：cloud=豆包语音(默认；不可用自动回退本地)/ local=sherpa（重启生效，worker 启动时读取）
    "voice_enabled": True,          # 语音链路总开关（启动时是否拉起 worker）
    "wakeword": "小机器人",          # 唤醒词（显示用；实际检测用 kws_keywords.txt）
    "handsfree_seconds": 30,        # 免唤醒连续对话窗口
    "spk_threshold": 0.40,          # 声纹余弦阈值（实测校准：真人 0.46-0.47 / 异人 0.14，取 0.40 留余量）
    "alarm_enabled": True,          # 报警上报开关（未确认提醒升级时写审计日志）
    "silent_start": "22:00",        # 静默时段开始（主动播报不发）
    "silent_end": "07:00",          # 静默时段结束
    "confirm_timeout_min": 30,      # 提醒送达后多少分钟未确认 → 升级"未确认"
    "migrate_done": False,        # 记忆 v3 一次性迁移是否已完成
    "mcp_enabled": False,          # MCP 外部工具总开关（开启后在启动时拉起 MCP_SERVERS）
    # ---- 地图编辑器（第三个前端 /mapeditor，见 docs/superpowers/specs/2026-09-14-map-editor-design.md）----
    "current_map": "my_map",            # **下次启动导航想用哪张**（人的意图，不是真相；真相=车实际加载的图，见 locator.current_map / map_server 的 yaml_filename）
    "map_boundary_margin_m": 0.3,       # 标点校验：距地图各边缩进（沿用 where_am_i.py 口径）
    "map_topic_fingerprint_enabled": True,  # 是否用 /map 元数据指纹**推断**"车此刻在跑哪张图"（第一权威是 map_server 的 yaml_filename，不受本开关约束）
    "mapeditor_auto_switch_map": False, # 打开编辑器时是否自动跳到识别出的那张图
    # ---- 分层用户体系（2026-09-14，规格 docs/superpowers/specs/2026-09-14-layered-user-roles-design.md）----
    "admin_auth_required": True,     # 管理员口令门开关（D13：可在 UI 直接关掉，界面须警示）
    "admin_session_ttl_s": 300,      # 管理员提权后无操作自动降权秒数（D8）
    "ward_context_window": 10,       # 集体层上下文注入条数（老人可读本病房最近 N 条，R5）
    "ward_autoswitch_enabled": True, # 病房位置自动切换总开关（关掉=退回手动；rosbridge 地址在 conf.ROSBRIDGE_URL）
    "ward_switch_debounce": 3,       # 自动切病房防抖：连续 N 次 tick 同病房才认（D18）
    "ward_zone_default_r": 3.0,      # 便捷录入病房区域的半径（米），以当前位姿为圆心采样 16 边形
    "manual_override_sec": 600,      # 手动切病房后，位置判定不覆盖的秒数（D18）
    "ward_map_source": "auto",       # 判定"车在跑哪张图"：auto=问导航（map_server 的 yaml_filename；读不到退回 /map 指纹）；setting=用 current_map 兜底（应急）
}

# ---- 地图编辑器独立服务（按需启动，见 docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md）----
MAP_EDITOR_PORT = 8010            # 编辑器服务端口（主后端 mapctl 用它拉起/探活/停止）
MAP_EDITOR_START_TIMEOUT = 20.0   # 拉起编辑器服务的最长等待秒数

# ---------------------------------------------------------------------------
# 地图文件（规格 §B十）：MarkStore / maptags / locator 共用的路径与远程 IO 配置
# ---------------------------------------------------------------------------
# 运行位置口径（2026-09-14 用户拍板「还是把地图工作放到PC上吧，因为板卡性能不是很好」）：
#   后端默认跑在 PC 上 → MAPS_IO="ssh"，地图真相仍在板卡 ros2_car/maps/，经 SSH 读写；
#   后端跑在板卡上时用 "local"。开发期板卡不可达可 `MAPS_IO=local` + 仓库副本目录。
#   ⚠️ 2026-09-14 起这只是**内置 ssh 源的种子**；编辑器实际读哪个源由 maps_sources.json
#   的命名源决定（见 MAPS_SOURCES_FILE）。ROS 端与编辑器解耦：改这里不影响 ROS 在跑的图。
MAPS_IO = os.environ.get("MAPS_IO", "ssh")            # ssh（默认）| local
MAPS_DIR = Path(os.environ.get("MAPS_DIR", str(BASE_DIR / "ros2_car" / "maps")))  # local 模式根目录
MAPS_CACHE_DIR = DATA_DIR / "mapcache"                # ssh 模式本地缓存（离线看图 + 断连降级）
MAPS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
MAPS_BACKUP_DIRNAME = ".backup"                       # 与地图同级；list() 必须排除它
# 地图源注册表（命名源：一组"地图文件在哪"的具名配置）——运行时数据，不进 git。
# 首次运行由下面的环境变量种入两个内置源（board / pc），此后以文件为准、改了即时生效。
MAPS_SOURCES_FILE = DATA_DIR / "maps_sources.json"
MAPS_SOURCE_LABEL = os.environ.get("MAPS_SOURCE_LABEL", "")   # 内置 ssh 源的显示名（空=自动生成）

MAPS_SSH_HOST = os.environ.get("MAPS_SSH_HOST") or os.environ.get("ROBOT_IP", "100.65.82.93")  # 板卡（Tailscale）
MAPS_SSH_USER = os.environ.get("MAPS_SSH_USER", "sunrise")
MAPS_SSH_PORT = int(os.environ.get("MAPS_SSH_PORT", "22"))
MAPS_SSH_ROOT = os.environ.get("MAPS_SSH_ROOT", "/home/sunrise/Robot/ros2_car/maps")
MAPS_SSH_KEY = os.environ.get("MAPS_SSH_KEY", "")      # 私钥路径（空=用 ~/.ssh/id_* 默认）
# 板卡 SSH 口令（仅 paramiko 通道能吃；cli 通道 BatchMode=yes 永远喂不了密码）。
# 别名 ROBOT_PASSWORD：.env 里既有的板卡口令变量（与 ROBOT_IP 配套），免得同一台机器
# 配两个名字；两者都在时以 MAPS_SSH_PASSWORD 为准。
MAPS_SSH_PASSWORD = (os.environ.get("MAPS_SSH_PASSWORD")
                     or os.environ.get("ROBOT_PASSWORD")
                     or "").strip()
MAPS_SSH_TRANSPORT = os.environ.get("MAPS_SSH_TRANSPORT", "auto")  # auto | paramiko | cli
MAPS_SSH_TIMEOUT = float(os.environ.get("MAPS_SSH_TIMEOUT", "10"))  # 单次连接/命令超时（秒）

MAPS_MAX_PGM_BYTES = 10 * 1024 * 1024   # 上传 pgm（解码后）上限
MAPS_MAX_YAML_BYTES = 64 * 1024         # 上传 yaml 上限
MAPS_MAX_BODY_BYTES = 16 * 1024 * 1024  # 请求体上限
MAPS_BACKUP_KEEP = 10                   # maps/.backup/ 保留组数

# 地图名白名单（规格 §B7.1 红线）：防路径穿越，且是 ssh 子进程模式**唯一的**命令注入防线。
# 另：不得含 keepout（上游编辑器会把它当掩膜文件，编辑到另一张画布上）。
MAP_RE_NAME_RE = r"^[A-Za-z0-9_-]{1,64}$"
MAP_RE_NAME_BANNED = ("keepout",)

# 位姿/当前地图识别（rosbridge，websocket 只读；不引入 rclpy —— Windows 开发机无 ROS）
ROSBRIDGE_URL = os.environ.get("ROSBRIDGE_URL", f"ws://{MAPS_SSH_HOST}:9090")
ROSBRIDGE_TIMEOUT = float(os.environ.get("ROSBRIDGE_TIMEOUT", "5"))
ROSBRIDGE_RETRY_S = float(os.environ.get("ROSBRIDGE_RETRY_S", "5"))
ROSBRIDGE_POSE_TTL_S = 10.0             # 位姿超过这么久没更新 → 视为不可用
ROSBRIDGE_MOCK_POSE = os.environ.get("ROSBRIDGE_MOCK_POSE", "")  # "x,y,yaw" 注入假位姿（无 ROS 开发/测试）
MAP_CURRENT_CACHE_TTL_S = 5.0           # 大于前端轮询周期；从扫描完成时计，避免慢 SSH 吞掉 TTL
# 「车此刻在跑哪张图」的第一权威 = **导航自己加载的那张图**：map_server 的 yaml_filename 参数。
# 为什么不让 /map 指纹当第一权威（2026-09-19 实锤）：像素编辑器另存出的副本（my_map3_edited）
# 与原图元数据（宽/高/分辨率/原点）天然完全一致，指纹反查必然"多命中"→ 按不唯一口径判 unknown
# → 车控 goto 三个工具全部 rejected「当前地图未知」。直接问 map_server 要路径则无歧义。
MAPSERVER_NODE = os.environ.get("MAPSERVER_NODE", "/map_server")
MAPSERVER_MAP_PARAM = os.environ.get("MAPSERVER_MAP_PARAM", "yaml_filename")

# 摄像头共享服务（vision/camera_server.py，裸 TCP）—— webbridge 连它的地址
# 默认**本机**：摄像头服务与后端通常一起跑（PC 上用 webcam 调试、板卡上用 MIPI）。
#   - 与 ROSBRIDGE_URL 默认指向板卡不同：那里板卡是唯一来源；而摄像头服务在
#     PC（usb 摄像头）和板卡（MIPI）上都可能跑，默认本机才不会让"PC 调试"要先改配置。
# 「后端跑在 PC、摄像头在板卡」时：把 VISION_HOST 设成板卡地址（同 MAPS_SSH_HOST），
# 并在板卡上以 `--bind 0.0.0.0` 启动服务。
def _parse_vision_port(raw, default: int = 9540) -> int:
    """Parse a TCP port without allowing malformed environment input to abort import."""
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


VISION_HOST = (os.environ.get("VISION_HOST") or "127.0.0.1").strip() or "127.0.0.1"
VISION_PORT = _parse_vision_port(os.environ.get("VISION_PORT"))

# vision MCP 子进程需要显式继承的视觉参数。可选项仅在环境变量实际设置时
# 传入，避免用空串覆盖 vision_client 内的默认值。
_VISION_SEE_ENV_NAMES = (
    "VISION_SEE_MODEL",
    "VISION_SEE_BASE_URL",
    "VISION_SEE_TIMEOUT",
    "VISION_SEE_MAX_TOKENS",
    "VISION_SEE_MAX_WIDTH",
    "VISION_SEE_QUALITY",
    "VISION_SEE_MAX_BYTES",
    "VISION_SEE_IMAGE_DIRS",
    "VISION_SEE_GRAB_TIMEOUT",
    "VISION_SEE_CONNECT_TIMEOUT",
    "VISION_SEE_CHANNEL",
)


def _vision_mcp_env() -> dict[str, str]:
    """Build the environment overrides for the vision MCP child process."""
    env = {
        # An empty key is intentional: see_server reports cloud unavailability.
        "DASHSCOPE_API_KEY": os.environ.get("DASHSCOPE_API_KEY", ""),
        "VISION_HOST": str(VISION_HOST),
        "VISION_PORT": str(VISION_PORT),
    }
    for name in _VISION_SEE_ENV_NAMES:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


# 人脸：检测（vision/face.py）+ 稳定判定（LLM/face_api.py 的"连续 N 帧一致"）
# 检测输入尺寸：**640 是实测工作点，别盲目调大** —— 本机双人场景 16/16 帧全中；
# 同一批帧改 1280 反而只检到 1 人（远处那位分数本就 0.46~0.53，尺度一变掉到阈值下），
# 且耗时 2.6 倍（203ms → 525ms）。要提升远处小脸检出率应做"裁脸放大"而非整体放大输入。
FACE_DETECT_IMGSZ = int(os.environ.get("FACE_DETECT_IMGSZ", "640"))
FACE_DETECT_CONF = float(os.environ.get("FACE_DETECT_CONF", "0.25"))   # 模型侧阈值
FACE_CAMERA_CHANNEL = int(os.environ.get("FACE_CAMERA_CHANNEL", "1"))  # 取哪一路通道
# "连续 N 帧一致"：稳定判定所需的连续帧数 / 所需平均置信度 / 跨帧关联 IoU / 允许丢帧数
FACE_STABLE_FRAMES = int(os.environ.get("FACE_STABLE_FRAMES", "5"))
# 检测框置信度门槛（"框得多准"）。**2026-09-18 真机实测后从 0.60 降到 0.45**：
# 同一台 PC 摄像头在弱光/稍远时 YOLO 检测分只有 0.44~0.53，而身份相似度高达 0.95+；
# 把"能不能切换"压在检测分 0.60 上会让系统永远不动作。切换的决定权交给下面那道
# **身份分门槛**（"认得多像"），检测门槛只负责筛掉明显不可靠的框。
FACE_STABLE_CONF = float(os.environ.get("FACE_STABLE_CONF", "0.45"))
# 身份相似度门槛：`switchable` 要求同一身份连续 N 帧且**平均相似度**达标。
# 与 `FACE_MATCH_THRESHOLD`（0.45，判"是不是库里的人"）分开：这里是"够不够确定到可以动作"。
FACE_IDENTITY_CONF = float(os.environ.get("FACE_IDENTITY_CONF", "0.55"))
FACE_TRACK_IOU = float(os.environ.get("FACE_TRACK_IOU", "0.30"))
FACE_TRACK_MAX_AGE = int(os.environ.get("FACE_TRACK_MAX_AGE", "5"))
# 跨帧关联的**兜底**：中心位移容差（单位 = 人脸框短边）。
# 为什么需要：后端是"轮询一帧算一帧"（实测 ~0.8 s/轮），人在 0.8 秒里自然会晃，
# IoU 对位移极敏感、会频繁掉到 FACE_TRACK_IOU 之下 → 同一人被当成新目标 →
# "连续 N 帧"反复归零、switchable 刚亮就灭（2026-09-18 真机踩到）。
# 判据：中心位移 ≤ 1.0×框短边 **且** 尺寸比在 [0.5, 2] → 仍算同一个人。
FACE_TRACK_MAX_JUMP = float(os.environ.get("FACE_TRACK_MAX_JUMP", "1.0"))

# 人脸识别（ArcFace）与样本库
# 模型变体：mbf=MobileFaceNet（13.6MB，本机实测 27ms/张）；r50=ResNet50（174MB，324ms/张）
# 默认轻量版（整条链还要叠加检测 ~190ms）；要最高精度可设 FACE_EMBED_VARIANT=r50。
# **阈值按模型分别标定**：两个模型的余弦分布不同，换模型必须重新标。
FACE_EMBED_VARIANT = os.environ.get("FACE_EMBED_VARIANT", "mbf")
FACE_EMBED_DIM = 512                       # ArcFace 输出维度（用于样本维度校验）
FACE_ALIGN_MARGIN = float(os.environ.get("FACE_ALIGN_MARGIN", "0.25"))  # 框外扩比例
FACE_DIR = (Path(os.environ["FACE_DIR"]) if os.environ.get("FACE_DIR")
            else DATA_DIR / "faces")       # 可用 FACE_DIR 重定位（换加密盘/临时目录，测试也用）
# 比对两道闸门：绝对阈值 + 与第二名的差距（养老场景"两位老人长得像"要防误认）
FACE_MATCH_THRESHOLD = float(os.environ.get("FACE_MATCH_THRESHOLD", "0.45"))
FACE_MATCH_MIN_MARGIN = float(os.environ.get("FACE_MATCH_MIN_MARGIN", "0.03"))
FACE_LIB_MAX_UIDS = int(os.environ.get("FACE_LIB_MAX_UIDS", "200"))          # 人数上限
FACE_LIB_MAX_SAMPLES_PER_UID = int(os.environ.get("FACE_LIB_MAX_SAMPLES_PER_UID", "20"))
# 每人建议最少样本数（低于它会在库状态里被点名，注册引导据此提示"多拍几张"）
FACE_MIN_SAMPLES_PER_UID = int(os.environ.get("FACE_MIN_SAMPLES_PER_UID", "3"))


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

# ---- 通知中心（护士台数据底座，模块 11）----
NOTIFY_DEDUP_S = 60          # 去重合并窗口（秒）
NOTIFY_KEEP_DAYS = 30        # 已处理通知保留天数
NOTIFY_LIST_LIMIT = 50       # 列表默认条数上限
NOTIFY_BODY_MAX = 500        # 正文截断长度

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

# ---- MCP 外部工具服务器（可选能力，见 mcp_client.py）----
# 服务器名 -> {"command", "args", "env"(可选,None=继承), "enabled"(可选,默认True)}
# 由后台独立线程常驻拉起（stdio 子进程），工具自动并入 OpenAI function-calling 循环。
# 新增 MCP 服务器 = 在这里加一条配置，无需改任何代码；启动后自动握手、自动转 schema、
# run_tool 自动分发、前端 /api/tools 自动显示。
# 注意：Windows 上 npx 是 .cmd 脚本，command 须写 npx.cmd（Linux 写 npx）；
# 下面用 _NPX 按平台自动选择，直接照抄即可。
import sys as _sys
_NPX = "npx.cmd" if _sys.platform == "win32" else "npx"

# 护士后台投递口地址（LLM/notice_mcp 子进程用；规格 docs/superpowers/specs/2026-09-18-llm-notify-nurse-mcp-design.md）。
# 默认**本机**：后端与它同机跑（PC 上的常规部署）；「LLM 不跟后端同机」时把 NOTICE_BACKEND_URL
# 设成后端地址即可（跨机部署代码零改动）。投递口 `POST /api/notifications` 免鉴权，见通知中心规格 D4。
NOTICE_BACKEND_URL = ((os.environ.get("NOTICE_BACKEND_URL") or "").strip()
                      or "http://127.0.0.1:8000").rstrip("/")

# notice MCP 子进程需要显式继承的可选项（照 `_vision_mcp_env()` 的惯例）：只在环境变量实际
# 设置时传下去，避免用空串覆盖 notice_client / notice_server 内的默认值。
_NOTICE_ENV_NAMES = ("NOTICE_TIMEOUT_S", "NOTICE_MCP_LOG")


def _notice_mcp_env() -> dict[str, str]:
    """Build the environment overrides for the notice MCP child process."""
    env = {"NOTICE_BACKEND_URL": NOTICE_BACKEND_URL}
    for name in _NOTICE_ENV_NAMES:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


_CAR_MCP_ENV_NAMES = (
    "CAR_PROBE_TIMEOUT_S",
    "CAR_ACTION_TIMEOUT_S",
    "CAR_HEARTBEAT_TTL_S",
    "CAR_MCP_LOG",
)


def _car_mcp_env() -> dict[str, str]:
    """Build explicit environment overrides for the car MCP child process."""
    env = {"ROSBRIDGE_URL": os.environ.get("ROSBRIDGE_URL", ROSBRIDGE_URL)}
    for name in _CAR_MCP_ENV_NAMES:
        if name in os.environ:
            env[name] = os.environ[name]
    return env

# MCP工具列表
MCP_SERVERS: dict[str, dict] = {
    # 网页抓取（需要本机有 node/npx，首次会自动 npx 下载包）：
    # 工具名 fetch_html —— 可用来替代原 web_search/get_news 的联网能力
    "fetch": {"command": _NPX, "args": ["-y", "@tokenizin/mcp-npx-fetch"], "enabled": True},
    "tavily": {
        "command": "node",
        "args": [str(BASE_DIR / "LLM" / "mcp_servers" / "node_modules" / "tavily-mcp" / "build" / "index.js")],
        # env 值留空串 = 运行时从 os.environ 继承（.env 由 server.py 加载后才有值）
        "env": {"TAVILY_API_KEY": ""},
        "enabled": True,
    },
    "vision": {
        "command": _sys.executable,
        "args": [str(BASE_DIR / "LLM" / "vision_mcp" / "see_server.py")],
        "env": _vision_mcp_env(),
        "enabled": True,
        "roles": ["elder", "admin"],
    },
    # 护士传达（把一条话推给护士台）：让"我这就去通知护士"这句话真的能做到。
    # roles 三层都给 —— 声纹识别失败会 fail-closed 落到 ward 层，那里堵死等于"老人求助喊不出来"。
    # 代价：未识别的说话人也能刷护士台（有 60s 去重兜底）；不想要就删掉 "ward"。
    "notice": {
        "command": _sys.executable,
        "args": [str(BASE_DIR / "LLM" / "notice_mcp" / "notice_server.py")],
        "env": _notice_mcp_env(),
        "enabled": True,
        "roles": ["elder", "ward", "admin"],
    },
    # 默认登记，但仍受 settings.mcp_enabled 总开关控制；总开关关闭时不会拉起子进程。
    "car": {
        "command": _sys.executable,
        "args": [str(BASE_DIR / "LLM" / "car_mcp" / "car_server.py")],
        "env": _car_mcp_env(),
        "enabled": True,
        "roles": ["ward", "elder", "admin"],
    },
}

MCP_TOOL_TIMEOUT = 30             # 单次 MCP 工具调用超时（秒）
MCP_CONNECT_TIMEOUT = 60          # 单台服务器握手超时（秒）

# LLM 参数
MODEL = "deepseek-v4-flash"
LLM_TIMEOUT = 60
REACT_MAX_TOOL_ROUNDS = 4
