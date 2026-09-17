# -*- coding: utf-8 -*-
r"""
对话编排引擎（模块 1）：
  - 角色设定：温柔护工 System Prompt + 安全红线 + 老人风格注入 + RAG 记忆注入 + 历史摘要
  - 思考路由层：关键词预分类，棘手/敏感/健康类问题自动 thinking on，日常秒回
  - 上下文管理：滚动窗口 + 历史摘要
  - 工具调用循环：模型输出 tool_calls → 执行（联网）→ 结果回填 → 继续生成，最多 2 轮
"""
import json
import time

from ..store import db
from ..core import log as audit
from . import memory as rag
from . import tools as tool_mod
from ..conf import (MODEL, THINKING_KEYWORDS, THINKING_EMOTION_WORDS,
                   ROUTER_LLM_MIN_LEN, HISTORY_WINDOW, SUMMARY_THRESHOLD,
                   LLM_TIMEOUT, PROMPT_FILE, DEFAULT_SETTINGS)
# 角色策略（提示词片段/工具白名单/数据可见范围）：分层用户体系，见 LLM/agent/policy.py
from .policy import role_policy

# 导入MCP客户端会话类：ClientSession封装全部MCP协议逻辑（initialize、list_tools、call_tool）
from mcp.client.session import ClientSession

# stdio_client：用来把你的mcp服务端程序，启动为一个子进程，通过标准输入输出和客户端通信
from mcp.client.stdio import stdio_client

# ---- 思考档位阶梯（规格 2026-09-17-thinking-mode-switch-design.md D1/D5）----
# 前端五档 → DeepSeek 的 `reasoning_effort`（官方映射：minimal→low、medium→high、xhigh→high、
# ultra→max；实测 `ultra` 会 400，故只用 low/high/max 三档）。`auto` 不进这张表：它走思考路由。
THINKING_MODES = ("auto", "none", "low", "high", "max")
FORCED_EFFORTS = {"low": "low", "high": "high", "max": "max"}
LEGACY_MODE_ALIASES = {"on": "high", "off": "none"}   # 旧三档值兼容（前端/设置里可能还留着）
DEFAULT_ROUTED_EFFORT = "high"   # auto / 安全网命中时的思考强度
THINKING_MODE_CN = {"auto": "自动", "none": "不思考", "low": "轻度", "high": "中度", "max": "重度"}

# ---- P2a 记忆召回节流缓存（对标 MaiBot heuristic 记忆的缓存思想，省 embedding/上下文）----
# 同 uid 的 RAG 召回 context 做短 TTL 缓存：短时间多轮对话复用同一份召回，避免每轮重复向量检索
_MEM_CACHE_TTL = 15.0          # 秒：记忆召回结果缓存有效期
_mem_cache: dict[str, tuple[float, str]] = {}   # uid -> (ts, recall_context)


def _recall_cached(uid: str, query: str) -> str:
    """带节流的 recall_v3：TTL 内复用缓存 context；话题变化(query 语义漂移)则立即刷新。"""
    import hashlib as _hl
    import time as _t
    from . import memory as rag
    now = _t.time()
    qsig = _hl.md5(query.encode("utf-8")).hexdigest()[:12]
    prev = _mem_cache.get(uid)
    if prev and (now - prev[0]) < _MEM_CACHE_TTL:
        cached_ctx = prev[1]
        if cached_ctx:
            return cached_ctx   # 节流期内：直接用上次召回结果（对话轮次间话题连续，够用）
    ctx = rag.recall_v3(uid, query)["context"]
    _mem_cache[uid] = (now, ctx)
    # 防缓存无限增长：只保留最近 32 个 uid
    if len(_mem_cache) > 32:
        for k in list(_mem_cache)[:-32]:
            _mem_cache.pop(k, None)
    return ctx

# ---- System Prompt 基础文本（人设 + 安全红线）：外置 LLM/agent/prompt/base.md ----
# 改提示词措辞直接编辑 LLM/agent/prompt/base.md 即可（每条请求实时读取，改完即生效、无需重启）。
# _DEFAULT_PROMPT_BASE 仅作文件缺失时的保底副本，内容须与 base.md 保持同步。
_DEFAULT_PROMPT_BASE = (
    "你是'小护'，一部照顾老人的陪护小车，跟老人处得像老熟人：像家人一样搭把手，像朋友一样唠嗑。"
    "自称'我'即可，不要自称'AI'。\n"
    "\n"
    "【说话像熟人，别像广播】\n"
    "1. 老人怎么说话，你就怎么说话：他话短你也短，他常挂在嘴边的说法你顺着用，他叫你什么称呼你也别端腔。"
    "老人说“吃饭了没”，你别回“您今天用餐了吗”。\n"
    "2. 往短里说：一句话能十个字说完，绝不说二十个；一次只接一个话头，别一口气抛三句问句。\n"
    "3. 说人话、不背书。禁用的腔调：首先/其次/综上所述/总而言之之类的连接词；“好的，我明白了”“收到”这类客服对白；"
    "“请您注意休息/多喝水/保持好心情”这种念经式关心；把大白话又翻译回书面语的啰嗦。\n"
    "4. 关心要接老人的话头，不是定时广播：老人说冷，你才提加衣；别每句话后面都挂“注意身体”。\n"
    "5. 老人聊到哪，你顺到哪，别硬把话题拽回来。不知道就直说“这个我还真不知道，我帮您问问”。\n"
    "\n"
    "【说话腔调示范（学的是腔调，不是背台词；示范里的情节别当真事引用）】\n"
    "- 老人：“今儿可真冷啊。”\n"
    "  小护：“可不，棉袄穿上了没？晌午暖和了再下楼遛弯。”\n"
    "- 老人：“你说我这记性，越来越不行了。”\n"
    "  小护：“您别这么说，谁都有忘事的时候。往后天天有我帮您记着，吃药、遛弯、见人，一样不落。”\n"
    "- 老人：“也不知道闺女啥时候来。”\n"
    "  小护：“想她了吧？她心里也惦记着您呢。您先把身子养得好好的，等她来了多陪您待会儿。”\n"
    "\n"
    "【安全红线，必须遵守】\n"
    "1. 医疗信息只读：用药、剂量、诊断只能引用档案里的内容，绝不自行建议改药、停药、加药；"
    "老人问'这药能减半吗'之类 → 回答'这个我不懂，我帮您问护士'。\n"
    "2. 敏感话题：若老人提到想死、不想活、胸口剧痛、摔倒、很不舒服等危险信号 → 立即停止闲聊，"
    "先安抚（'您别急，我陪着您'），并明确说出'我这就去通知护士'。\n"
    "3. 不确定的事不要编造；不知道就直说，然后提出帮老人查/问护士。\n"
    "4. 健康类信息要注明'仅供参考，具体问医生'。"
)

_prompt_warned = False  # base.md 缺失只告警一次，避免刷审计日志


def _load_prompt_base() -> str:
    """读取 LLM/agent/prompt/base.md 中最后一个单独成行的 `<!-- PROMPT -->` 标记之下的正文，
    作为 System Prompt 基础文本（标记上方是给人看的说明，不会发给模型）。
    文件缺失/读取失败 → 告警一次并退回 _DEFAULT_PROMPT_BASE，保证对话链路不中断。"""
    global _prompt_warned
    try:
        raw = PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        if not _prompt_warned:
            _prompt_warned = True
            audit.log("chat", action="prompt_file_missing", file=str(PROMPT_FILE))
        return _DEFAULT_PROMPT_BASE
    _prompt_warned = False
    marker_idx = None
    for i, line in enumerate(raw.splitlines()):
        if line.strip() == "<!-- PROMPT -->":
            marker_idx = i
    if marker_idx is not None:
        raw = "\n".join(raw.splitlines()[marker_idx + 1:])
    return raw.strip()


# ---- 角色片段（分层用户体系）：LLM/agent/prompt/{ward,elder,admin}.md ----
# 与 base.md（共用 base：人设 + 安全红线）叠加：base 管"怎么说"，角色片段管"现在跟谁说话"。
_prompt_role_warned: set[str] = set()   # 某个角色片段缺失只告警一次，避免刷审计日志
_ROLE_PROMPT_DIR_OVERRIDE = None        # 测试用：指向不存在的目录以验证降级


def _load_role_prompt(role: str) -> str:
    """读 `LLM/agent/prompt/<role>.md` 角色片段；缺失 → 空串 + 告警一次（**不阻断对话**）。

    文件名取自 `role_policy(role)["prompt_file"].name`（而不是拼 `f"{role}.md"`）：未知角色
    在 role_policy 里已 fail-closed 落到集体层，这里必须跟着落到 ward.md —— 否则一个没见过的
    role 会把整层角色提示词都丢掉，只剩共用 base（fail-open 的反面：看起来没崩，实际没规矩）。
    """
    pol = role_policy(role)
    base_dir = _ROLE_PROMPT_DIR_OVERRIDE or pol["prompt_file"].parent
    path = base_dir / pol["prompt_file"].name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        if role not in _prompt_role_warned:
            _prompt_role_warned.add(role)
            audit.log("chat", action="prompt_role_missing", role=role, file=str(path))
        return ""


def _ward_context(ward_uid: str, limit: int) -> str:
    """本病房集体层最近 N 条（R5 单向：**只从这里往外读**，绝不把私聊灌进来）。

    `ward_uid` **必须真的是病房档案**：`profiles.ward_id` 一旦被误设成某位老人的 uid，
    这里就会把那位的**私聊**当"病房里刚说过的事"注入给别人（R5 的反方向）。所以 fail-closed。
    """
    if not ward_uid:
        return ""
    if db.get_profile_kind(ward_uid) != "ward":
        audit.log("chat", action="ward_context_denied", ward_uid=ward_uid)
        return ""
    rows = db.load_history(ward_uid, limit=limit)
    lines = [f"{r['role']}: {r['content']}" for r in rows if (r.get("content") or "").strip()]
    if not lines:
        return ""
    return ("【病房里刚说过的事（这位老人在场听过；只读参考，别当私事追问）】\n"
            + "\n".join(lines))


ROUTER_HIT = (
    "【思考说明】这个问题涉及健康/药物/敏感或需要慎重的话题，请先仔细思考再回答，"
    "语气要格外谨慎，不确定就建议问护士。"
)

global _mcp_session

def llm_json(client, model: str, prompt: str, timeout: int = LLM_TIMEOUT) -> dict | list:
    """非流式 JSON 输出（用于记忆提取/历史摘要）。失败返回 {}。"""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking": {"type": "disabled"}},
            response_format={"type": "json_object"},
            timeout=timeout,
        )
        text = resp.choices[0].message.content or ""
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        return json.loads(text)
    except Exception:
        return {}


def _apply_thinking_mode(mode: str, routed: dict) -> tuple[str, str, str, str]:
    """把手动档位与路由结果合成最终**思考强度**（规格 2026-09-17-thinking-mode-switch-design.md D1/D5）。

    档位是五档阶梯：`none`(不思考) / `low`(轻度) / `high`(中度) / `max`(重度) / `auto`(自动)。
    手动档位**叠加**在思考路由之上，不是替换：
      - low/high/max：用户要深思 → 一律按该强度思考（method=manual）；
      - none：用户要快答 → 只压制**非敏感**问题的思考；命中关键词/情绪词/LLM 预判的
              问题**照旧加深**（强度取 DEFAULT_ROUTED_EFFORT，安全网手动关不掉，用户口径
              「敏感词自动加深思考功能不改」）。此时 reason/method 保持路由原值，绝不谎报 manual；
      - auto：完全照路由（日常不思考，敏感问题加深）。
    返回 (effort, reason, method, mode)，`effort` 为 None 表示不思考。
    """
    on, reason, method = routed["on"], routed["reason"], routed["method"]
    if mode in FORCED_EFFORTS:
        return FORCED_EFFORTS[mode], f"用户手动选择：{THINKING_MODE_CN.get(mode, mode)}", "manual", mode
    if mode == "none":
        if on:
            return DEFAULT_ROUTED_EFFORT, f"敏感话题已自动加深：{reason}", method, mode
        return None, "用户手动关闭", "manual", mode
    return (DEFAULT_ROUTED_EFFORT if on else None), reason, method, "auto"


def _resolve_thinking_mode(thinking: str, settings: dict) -> str:
    """请求体显式档位 > 持久化设置 > auto（语音轮次不过前端，只能靠 settings 吃手动档位）。
    旧三档值 `on`/`off` 一律别名到 `high`/`none`（缓存里的老前端 bundle / 老设置都能跑）。"""
    if thinking in THINKING_MODES:
        return thinking
    if thinking in LEGACY_MODE_ALIASES:
        return LEGACY_MODE_ALIASES[thinking]
    fallback = str(settings.get("thinking_mode") or DEFAULT_SETTINGS.get("thinking_mode", "auto"))
    fallback = LEGACY_MODE_ALIASES.get(fallback, fallback)
    return fallback if fallback in THINKING_MODES else "auto"


def _thinking_extra(effort: str | None) -> dict:
    """extra_body 部分：只放 `thinking` 开关。**`reasoning_effort` 绝不能塞这里** ——
    它必须是顶层参数（`client.chat.completions.create(reasoning_effort=...)`，openai 3.3.1
    的签名里有），塞进 extra_body 会被服务端当未知字段静默忽略，
    表现就是用户看到的「选了强制/中度，模型照样不思考」。"""
    return {"thinking": {"type": "enabled"}} if effort else {"thinking": {"type": "disabled"}}


def route_thinking(text: str, settings: dict, llm_client=None, model: str = MODEL) -> dict:
    """
    思考路由层（v2）：
      1. 主题/敏感/健康关键词 → thinking on
      2. 情绪/负面词（语气强烈）→ thinking on
      3. 规则未命中且消息够长 → LLM 快速预判兜底（不覆盖日常短问候，省延迟）
    返回 {"on": bool, "reason": str, "method": "keyword|emotion|llm|manual|off"}
    """
    if not settings.get("thinking_router_enabled", True):
        return {"on": False, "reason": "路由关闭", "method": "off"}
    for kw in THINKING_KEYWORDS:
        if kw in text:
            return {"on": True, "reason": f"主题「{kw}」", "method": "keyword"}
    for w in THINKING_EMOTION_WORDS:
        if w in text:
            return {"on": True, "reason": f"情绪「{w}」", "method": "emotion"}
    if (settings.get("router_llm_enabled", True) and len(text) >= ROUTER_LLM_MIN_LEN
            and llm_client is not None):
        try:
            judge = llm_json(llm_client, model, (
                "判断下面这句话是否需要模型'深思考'后再回答。需要深思考的情况：涉及健康/药物/安全/敏感话题、"
                "复杂推理或重要决策、强烈情绪、需要慎重措辞的场合。日常闲聊、简单问候、天气时间等不需要。\n"
                '只输出 JSON：{"deep": true 或 false, "reason": "≤10字原因"}\n话：' + text))
            if isinstance(judge, dict) and judge.get("deep"):
                return {"on": True, "reason": f"LLM预判：{judge.get('reason', '')}", "method": "llm"}
        except Exception:
            pass
    return {"on": False, "reason": "日常闲聊", "method": "off"}


def _build_query(user_text: str, history: list[dict]) -> str:
    """构造记忆检索 query：本次用户消息 + 最近几轮对话，提高向量召回相关度。"""
    parts = [user_text]
    for m in history[-4:]:
        c = (m.get("content") or "").strip()
        if c:
            parts.append(c)
    return " ".join(parts).strip()


def _expression_hint(uid: str) -> str:
    """表达习惯参考块（对标 MaiBot 表达注入）：注入已审核的「情景→说法」语录，让模型自然吸收。

    注意身份边界：这是老人习惯的说法参考，机器人保持护工身份、酌情自然使用，不逐字模仿。
    """
    exprs = db.pick_expressions(uid, limit=3)
    if not exprs:
        return ""
    lines = []
    for e in exprs:
        situation = (e.get("situation") or "").strip()
        style = (e.get("style") or "").strip()
        if situation and style:
            lines.append(f'- 当老人"{situation}"时，可以用"{style}"这样的口吻回应')
    if not lines:
        return ""
    return ("【表达习惯参考（老人习惯的口吻，仅供回应时酌情自然使用；"
            "你始终是护工'小护'，保持亲切得体的身份，不逐字模仿）】\n" + "\n".join(lines))


def build_system(uid: str, settings: dict, query: str = "", principal: dict | None = None) -> str:
    """组装 System Prompt：base（人设+安全红线）+ 角色片段 + 记忆/上下文 + 当前时间。

    principal 缺省时取 kiosk 槽（兼容旧调用点）；**权限相关的取舍只看 principal（R1）**：
      - elder：本人档案/记忆/画像 + 本病房集体上下文（只读、单向，R5）
      - ward ：**不注入任何老人档案**，只有本病房集体上下文
      - admin：全量数据（不做 RAG 注入），无集体上下文
    query 用于向量检索相关记忆；为空时只注入结构化档案（兼容无上下文场景）。
    P2a：RAG 召回走短 TTL 缓存（同 uid 15s 内复用），避免短时间多轮重复向量检索。
    """
    from . import session as session_mod
    p = principal or session_mod.get_principal("kiosk")
    pol = role_policy(p.get("role"))
    scope = pol["data_scope"]
    # 数据注入口径必须与权限口径同源：principal 给了就以它为准 —— 客户端传来的 uid 可能过期/伪造，
    # 拿它去取档案会把**另一位老人**的画像注入当前会话（R5 同族的互泄）。
    data_uid = (p.get("uid") or uid) if principal is not None else uid

    parts = [_load_prompt_base()]
    role_txt = _load_role_prompt(p.get("role"))
    if role_txt:
        parts.append("\n" + role_txt)

    if scope == "self":                      # 老人层：本人档案/记忆/画像
        recall_ctx = _recall_cached(data_uid, query) if query else rag.recall_v3(data_uid, "")["context"]
        parts.append("\n【我了解到的关于这位老人的信息（来自档案/记忆，可能不全或过时，仅供参考）】\n"
                     + recall_ctx)

    if pol["ward_context"]:                  # 老人层：本病房集体上下文（只读、单向）
        try:
            limit = int(settings.get("ward_context_window", 10))
        except (TypeError, ValueError):
            limit = 10                       # 设置被写坏也不许炸掉整条对话（降级原则）
        ward_txt = _ward_context(p.get("ward_uid", ""), limit)
        if ward_txt:
            parts.append("\n" + ward_txt)

    if scope == "self":
        summary = db.get_summary(data_uid)
        if summary:
            parts.append(f"\n【更早对话的历史摘要】\n{summary}")
        expr_hint = _expression_hint(data_uid)
        if expr_hint:
            parts.append("\n" + expr_hint)

    parts.append(
        "\n【当前时间】" + time.strftime("%Y-%m-%d %H:%M (%A)") +
        "\n如果老人问'现在几点/今天星期几'，按上面的时间回答。"
    )
    return "\n".join(parts)


def build_messages(uid: str, user_text: str, thinking_on: bool, settings: dict,
                   principal: dict | None = None) -> list[dict]:
    """上下文管理：滚动窗口取最近 N 条 + System Prompt + 本次用户消息。

    滚动历史的读写 uid **必须与权限/数据口径同源**（与 `build_system` 里同一句）：
    客户端传来的 uid 不可信（R1），拿它去读历史会把**另一位老人的私聊原文**当 history
    注入当前会话（R5 的旁路）。
    """
    data_uid = (principal.get("uid") or uid) if principal is not None else uid
    history = db.load_history(data_uid, limit=HISTORY_WINDOW)
    system = build_system(data_uid, settings, query=_build_query(user_text, history),
                          principal=principal)
    if thinking_on:
        system += "\n" + ROUTER_HIT
    return [{"role": "system", "content": system}, *history,
            {"role": "user", "content": user_text}]


def summarize_old(uid: str, client, model: str):
    """历史超过阈值时，对最早的对话生成摘要（后台任务，不占请求链路）。"""
    count = db.history_count(uid)
    if count <= SUMMARY_THRESHOLD:
        return
    old = db.oldest_history(uid, count - HISTORY_WINDOW)
    if not old:
        return
    text = "\n".join(f"{m['role']}: {m['content']}" for m in old)
    prompt = (
        "把下面这段陪护机器人对话压缩成 200 字以内的中文摘要，"
        "保留：老人说过的重要事实、喜好、事件、情绪状态。只输出 JSON：{\"summary\": \"...\"}\n\n" + text
    )
    data = llm_json(client, model, prompt)
    summary = data.get("summary", "") if isinstance(data, dict) else ""
    if summary:
        prev = db.get_summary(uid)
        db.set_summary(uid, (prev + "\n" + summary).strip())
        db.trim_history(uid, keep=HISTORY_WINDOW)
        audit.log("chat", action="summarize", uid=uid, summary=summary)

async def mcp_init(client, model: str):
    """MCP 初始化：启动子进程、建立通信管道、握手、获取工具列表。"""
    global _mcp_session
    # ====================== 配置：指定怎么启动你的MCP服务端 ======================
    # 这是命令列表，等价于终端执行：python my_mcp_server.py
    # 第一个元素是程序，后面是参数
    server_command = ["python", "my_mcp_server.py"]


    # ====================== 1、启动子进程，建立通信管道 ======================
    # stdio_client 会自动拉起上面的服务端作为子进程
    # read：客户端【读取】服务端发过来的数据流
    # write：客户端【发送】数据给服务端的输出流
    # async with 是异步上下文管理器：代码块结束，会自动关闭子进程、释放资源，不用手动写关闭
    async with stdio_client(server_command) as (read, write):
        # ====================== 2、创建MCP会话对象，封装全部协议交互 ======================
        # ClientSession 接收读写流，内部帮你处理 JSON‑RPC 消息组装、id匹配、解析
        async with ClientSession(read, write) as session:
            # 进入这个代码块，session对象就绪，可以开始MCP握手

            # --------------------------
            # 第一步：执行 initialize 初始化握手（MCP协议强制第一步）
            # 客户端告诉服务端：我的协议版本、我的能力；服务端返回它自己的版本、支持什么能力
            # await：等待网络/IPC通信完成，拿到返回结果，异步代码必须写await
            init_result = await session.initialize()

            # 打印握手返回信息，看服务端名字、版本
            print("[MCP]==== 握手完成 ====")
            print("服务端名称：", init_result.serverInfo.name)
            print("服务端版本：", init_result.serverInfo.version)
            print("服务端具备的能力：", init_result.capabilities)
            audit.log("mcp", action="initialize", server_name=init_result.serverInfo.name,
                      server_version=init_result.serverInfo.version, capabilities=init_result.capabilities)


            # --------------------------
            # ⚠️非常关键，极易漏掉！发送 initialized 通知
            # 协议规定：initialize请求收到回复之后，客户端必须发送这条单向通知
            # 不发这条，后面 list_tools / call_tool 会直接卡死超时！
            # 通知 = 单向消息，服务端不需要回复
            await session.send_initialized_notification()


            # --------------------------
            # 第二步：向服务端查询有哪些可用工具 list_tools
            # 同时让网关启动初始化所有mcp服务器
            tools_response = await session.list_tools()

            print("[MCP]==== 获取到全部工具列表 ====")
            audit.log("mcp", action="list_tools", tools=[t.name for t in tools_response.tools])
            # tools_response.tools 是一个列表，每一项是一个工具对象
            for one_tool in tools_response.tools:
                print(f"工具名：{one_tool.name}")

            audit.log("mcp", action="list_tools", tools=[t.name for t in tools_response.tools])

            _mcp_session = session

_VISION_LOG_SNIPPET = "[视觉结果未写入日志]"


def _tool_log_fields(name: str, args, snippet: str):
    """Return fields safe to persist without changing tool/SSE payloads."""
    if name != "see_what":
        return args, snippet
    channel = args.get("channel") if isinstance(args, dict) else None
    if type(channel) is int and 1 <= channel <= 255:
        return {"channel": channel}, _VISION_LOG_SNIPPET
    return {}, _VISION_LOG_SNIPPET


def chat_stream(client, model: str, uid: str, user_text: str, thinking: str, settings: dict,
                principal: dict | None = None):
    """
    核心生成器：逐条 yield SSE 事件 dict。
      {"type":"reasoning"|"content"|"tool_start"|"tool_result"|"done"|"error", ...}
    工具循环最多 2 轮，防止模型无限调工具。
    """
    # 思考路由（规则 + 情绪词 + LLM 预判兜底）+ 手动档位叠加（见 _apply_thinking_mode）
    routed = route_thinking(user_text, settings, llm_client=client, model=model)
    mode = _resolve_thinking_mode(thinking, settings)
    effort, reason, method, mode = _apply_thinking_mode(mode, routed)
    thinking_on = effort is not None

    # 历史读写也必须与权限口径同源：客户端传来的 uid 不可信（R1），否则会把别人的私聊注入
    # 当前会话、并把回答写进别人的历史（R5 的旁路）。`meta` 里报的也是这个**实际生效**的
    # 数据主体（前端若与入参比对，能看出伪造/过期的 uid 已被忽略）。
    data_uid = (principal.get("uid") or uid) if principal is not None else uid

    yield {"type": "meta", "router": {"on": thinking_on, "effort": effort, "reason": reason,
                                      "method": method, "mode": mode, "uid": data_uid}}

    messages = build_messages(data_uid, user_text, thinking_on, settings, principal=principal)
    tools = tool_mod.effective_tools(settings, principal)

    full_assistant = ""
    reasoning_text = ""          # 思维链累计（只用于审计字数，不落库、不播报）
    retried_no_thinking = False   # 空回复兜底只允许降级一次（防死循环）
    try:
        #--------------------------
        # 1、调用模型生成器，stream=True 流式输出
        for round_i in range(2):
            extra = _thinking_extra(effort)
            try:
                stream = client.chat.completions.create(
                    model=model, messages=messages, stream=True,
                    tools=tools or None, tool_choice="auto" if tools else None,
                    reasoning_effort=effort,
                    extra_body=extra,
                )
            except Exception as e:
                # thinking+工具冲突等：降级重试一次（去掉 thinking 或去掉工具）
                if effort:
                    effort, thinking_on = None, False
                    yield {"type": "meta",
                           "router": {"on": False, "effort": None, "reason": f"重试降级：{e}",
                                      "mode": mode, "uid": data_uid}}
                    extra = _thinking_extra(None)
                    stream = client.chat.completions.create(
                        model=model, messages=messages, stream=True,
                        tools=tools or None, tool_choice="auto" if tools else None,
                        extra_body=extra,
                    )
                else:
                    raise
        #-------------------------
            # 解析流式输出，逐条 yield SSE 事件
            tool_calls = {}
            finish = None
            for chunk in stream:
                choice = chunk.choices[0]
                delta = choice.delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    reasoning_text += reasoning
                    yield {"type": "reasoning", "content": reasoning}
                if delta.content:
                    full_assistant += delta.content
                    yield {"type": "content", "content": delta.content}
                for tc in (delta.tool_calls or []):
                    slot = tool_calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] += tc.id
                    if tc.function and tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
                if choice.finish_reason:
                    finish = choice.finish_reason
                    break

            if finish == "tool_calls" and tool_calls:
                # 把工具调用补进上下文，再执行
                assistant_msg = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": slot["id"] or f"call_{round_i}_{i}", "type": "function",
                     "function": {"name": slot["name"], "arguments": slot["args"] or "{}"}}
                    for i, slot in sorted(tool_calls.items())
                ]}
                messages.append(assistant_msg)
                for i, slot in sorted(tool_calls.items()):
                    try:
                        args = json.loads(slot["args"] or "{}")
                    except Exception:
                        args = {}
                    name = slot["name"]
                    yield {"type": "tool_start", "tool": name, "args": args}
                    t0 = time.time()
                    result = tool_mod.run_tool(name, args, principal)
                    latency = int((time.time() - t0) * 1000)
                    snippet = (result.get("result") or result.get("message")
                               or result.get("error") or "")[:500]
                    log_args, log_snippet = _tool_log_fields(name, args, snippet)
                    db.log_tool(data_uid, name, log_args, log_snippet,
                                status="ok" if result.get("ok") else "error", latency_ms=latency)
                    audit.log("tool", uid=data_uid, tool=name, args=log_args,
                              ok=result.get("ok"), latency_ms=latency)
                    yield {"type": "tool_result", "tool": name, "ok": result.get("ok"),
                           "snippet": snippet}
                    messages.append({"role": "tool", "tool_call_id": slot["id"] or f"call_{round_i}_{i}",
                                     "content": json.dumps(result, ensure_ascii=False)})
                continue  # 下一轮：把工具结果交给模型

            break  # 正常结束

        # 空回复兜底：思考档位下，思维链也吃 max_tokens，极端情况下（如敏感问题在 max 档
        # 长时间自问）会"只想不说"——reasoning 有内容、content 为空，老人那边什么都听不到。
        # 一轮没出正文就降级成不思考重来一次（只降一次，防死循环）。
        if effort and not full_assistant.strip() and not retried_no_thinking:
            retried_no_thinking = True
            audit.log("chat", action="thinking_empty_fallback", uid=data_uid, mode=mode, effort=effort)
            yield {"type": "meta",
                   "router": {"on": False, "effort": None, "reason": "只思考没作答，已降级重答",
                              "mode": mode, "uid": data_uid}}
            effort, thinking_on = None, False
            stream = client.chat.completions.create(
                model=model, messages=messages, stream=True,
                tools=tools or None, tool_choice="auto" if tools else None,
                extra_body=_thinking_extra(None),
            )
            for chunk in stream:
                choice = chunk.choices[0]
                delta = choice.delta
                if delta.content:
                    full_assistant += delta.content
                    yield {"type": "content", "content": delta.content}
                if choice.finish_reason:
                    break
    except Exception as e:
        audit.log("chat", action="error", uid=data_uid, error=str(e))
        yield {"type": "error", "content": f"对话服务出错：{e}"}
        return

    # 落库：对话历史 + 审计（按实际生效的数据主体落，不许落到入参 uid 上）
    db.append_history(data_uid, "user", user_text)
    if full_assistant.strip():
        db.append_history(data_uid, "assistant", full_assistant)
    # 审计带上思考档位/实际强度/思维链字数：现场排障时"参数到底传到 llm 没有"直接看审计
    audit.log("chat", action="turn", uid=data_uid, user=user_text[:200],
              assistant=full_assistant[:200], mode=mode, effort=effort,
              thinking_on=thinking_on, reasoning_chars=len(reasoning_text))
    yield {"type": "done", "assistant": full_assistant}
