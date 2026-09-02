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

from . import db
from . import log as audit
from . import memory as rag
from . import tools as tool_mod
from .conf import (MODEL, THINKING_KEYWORDS, THINKING_EMOTION_WORDS,
                   ROUTER_LLM_MIN_LEN, HISTORY_WINDOW, SUMMARY_THRESHOLD,
                   LLM_TIMEOUT)

# 导入MCP客户端会话类：ClientSession封装全部MCP协议逻辑（initialize、list_tools、call_tool）
from mcp.client.session import ClientSession

# stdio_client：用来把你的mcp服务端程序，启动为一个子进程，通过标准输入输出和客户端通信
from mcp.client.stdio import stdio_client

PERSONA = (
    "你是'小护'，一位温柔、耐心、专业的 AI 陪护机器小车，正在照顾一位老人。"
    "说话要像护工又像家人：语气亲切温和、句子简短口语化、多用'您'、适当关心起居饮食。"
    "不要自称'AI'，自称'我'即可。不要输出与老人无关的长篇大论。"
)

SAFETY = (
    "【安全红线，必须遵守】\n"
    "1. 医疗信息只读：用药、剂量、诊断只能引用档案里的内容，绝不自行建议改药、停药、加药；"
    "老人问'这药能减半吗'之类 → 回答'这个我不懂，我帮您问护士'。\n"
    "2. 敏感话题：若老人提到想死、不想活、胸口剧痛、摔倒、很不舒服等危险信号 → 立即停止闲聊，"
    "先安抚（'您别急，我陪着您'），并明确说出'我这就去通知护士'。\n"
    "3. 不确定的事不要编造；不知道就直说，然后提出帮老人查/问护士。\n"
    "4. 健康类信息要注明'仅供参考，具体问医生'。"
)

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


def build_system(uid: str, settings: dict, query: str = "") -> str:
    """组装 System Prompt：角色 + 安全红线 + 记忆（recall_v3 已含档案 style/画像与核心记忆 persona）+ 摘要。
    query 用于向量检索相关记忆；为空时只注入结构化档案（兼容无上下文场景）。"""
    recall = rag.recall_v3(uid, query)
    parts = [
        PERSONA,
        SAFETY,
        f"\n【我了解到的关于这位老人的信息（可能不全，仅供参考）】\n{recall['context']}",
    ]
    summary = db.get_summary(uid)
    if summary:
        parts.append(f"\n【更早对话的历史摘要】\n{summary}")
    parts.append(
        "\n【当前时间】" + time.strftime("%Y-%m-%d %H:%M (%A)") +
        "\n如果老人问'现在几点/今天星期几'，按上面的时间回答。"
    )
    return "\n".join(parts)


def build_messages(uid: str, user_text: str, thinking_on: bool, settings: dict) -> list[dict]:
    """上下文管理：滚动窗口取最近 N 条 + System Prompt + 本次用户消息。"""
    history = db.load_history(uid, limit=HISTORY_WINDOW)
    system = build_system(uid, settings, query=_build_query(user_text, history))
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

def chat_stream(client, model: str, uid: str, user_text: str, thinking: str, settings: dict):
    """
    核心生成器：逐条 yield SSE 事件 dict。
      {"type":"reasoning"|"content"|"tool_start"|"tool_result"|"done"|"error", ...}
    工具循环最多 2 轮，防止模型无限调工具。
    """
    # 思考路由（规则 + 情绪词 + LLM 预判兜底）
    routed = route_thinking(user_text, settings, llm_client=client, model=model)
    thinking_on, reason, method = routed["on"], routed["reason"], routed["method"]
    if thinking == "on":
        thinking_on, reason, method = True, "用户手动开启", "manual"
    elif thinking == "off":
        thinking_on, reason, method = False, "用户手动关闭", "manual"

    yield {"type": "meta", "router": {"on": thinking_on, "reason": reason, "method": method, "uid": uid}}

    messages = build_messages(uid, user_text, thinking_on, settings)
    tools = tool_mod.effective_tools(settings)

    full_assistant = ""
    try:
        #--------------------------
        # 1、调用模型生成器，stream=True 流式输出
        for round_i in range(2):
            extra = {"thinking": {"type": "enabled"}, "reasoning_effort": "high"} if thinking_on \
                else {"thinking": {"type": "disabled"}}
            try:
                stream = client.chat.completions.create(
                    model=model, messages=messages, stream=True,
                    tools=tools or None, tool_choice="auto" if tools else None,
                    extra_body=extra,
                )
            except Exception as e:
                # thinking+工具冲突等：降级重试一次（去掉 thinking 或去掉工具）
                if thinking_on:
                    thinking_on = False
                    yield {"type": "meta", "router": {"on": False, "reason": f"重试降级：{e}", "uid": uid}}
                    extra = {"thinking": {"type": "disabled"}}
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
                    result = tool_mod.run_tool(name, args)
                    latency = int((time.time() - t0) * 1000)
                    snippet = result.get("result", result.get("message", ""))[:500]
                    db.log_tool(uid, name, args, snippet,
                                status="ok" if result.get("ok") else "error", latency_ms=latency)
                    audit.log("tool", uid=uid, tool=name, args=args,
                              ok=result.get("ok"), latency_ms=latency)
                    yield {"type": "tool_result", "tool": name, "ok": result.get("ok"),
                           "snippet": snippet}
                    messages.append({"role": "tool", "tool_call_id": slot["id"] or f"call_{round_i}_{i}",
                                     "content": json.dumps(result, ensure_ascii=False)})
                continue  # 下一轮：把工具结果交给模型

            break  # 正常结束
    except Exception as e:
        audit.log("chat", action="error", uid=uid, error=str(e))
        yield {"type": "error", "content": f"对话服务出错：{e}"}
        return

    # 落库：对话历史 + 审计
    db.append_history(uid, "user", user_text)
    if full_assistant.strip():
        db.append_history(uid, "assistant", full_assistant)
    audit.log("chat", action="turn", uid=uid, user=user_text[:200], assistant=full_assistant[:200])
    yield {"type": "done", "assistant": full_assistant}
