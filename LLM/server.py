# -*- coding: utf-8 -*-
r"""
AI 对话后端（方案 A：FastAPI + 流式转发）
========================================
职责：
  1. 接收前端发来的对话消息（messages + thinking 开关）
  2. 调用 DeepSeek 的流式接口（stream=True）
  3. 把返回的每个"小纸条"(chunk) 转成 SSE 事件，实时推回给前端

数据流：
  前端 --POST /api/chat--> 本文件 --stream=True--> DeepSeek API
  前端 <--SSE 逐条推送--- 本文件 <--增量 chunk------ DeepSeek API

运行方式（在 LLM 目录下执行）：
  ..\.venv\Scripts\python.exe -m uvicorn server:app --reload --port 8000
"""

import os
import json
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import OpenAI
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# 1. 加载环境变量（.env 里的 DEEPSEEK_API_KEY）
# ---------------------------------------------------------------------------
# server.py 在 LLM/ 目录下，项目根是它的上一级。用绝对路径定位 .env，
# 这样无论从哪个目录启动 uvicorn，都能找到 key。
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# 创建 DeepSeek 客户端（OpenAI 兼容接口，所以用 openai SDK + 自定义 base_url）
client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)

# ---------------------------------------------------------------------------
# 2. 创建 FastAPI 应用 + 跨域配置
# ---------------------------------------------------------------------------
app = FastAPI(title="AI Chat Backend")

# CORS（跨域）：前端可能是 file:// 直接打开的（Origin 为 null），
# 也可能是 localhost:8000 起的静态页，统一放行，方便本地开发。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # 本地开发：允许所有来源
    allow_methods=["*"],        # 允许所有方法（GET/POST...）
    allow_headers=["*"],        # 允许所有请求头
)


# ---------------------------------------------------------------------------
# 3. 定义请求体的数据结构（Pydantic 会自动校验 + 转成 Python 对象）
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    messages: list[dict]      # 多轮对话历史：[{"role": "user", "content": "..."}, ...]
    thinking: bool = False    # 是否开启"思考模式"（显示模型的内心戏）


# ---------------------------------------------------------------------------
# 4. 核心：把 DeepSeek 的流式 chunk 逐个转成 SSE 事件
# ---------------------------------------------------------------------------
def stream_chat(req: ChatRequest):
    """
    这是一个"生成器"函数（用了 yield），FastAPI 会把它包装成
    StreamingResponse，边生成边推给浏览器。

    SSE 事件格式：以 "data: " 开头，以空行 \n\n 结尾
      data: {"type":"reasoning","content":"..."}   ← 思考过程增量
      data: {"type":"content","content":"..."}     ← 正式回答增量
      data: {"type":"done"}                        ← 结束标记
    """
    # 根据 thinking 开关，决定传给 DeepSeek 的参数
    if req.thinking:
        # 开启思考：多传一个 reasoning_effort（low/high/max），让模型"想"得多一点
        extra_body = {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    else:
        # 关闭思考：模型直接出答案，更快
        extra_body = {"thinking": {"type": "disabled"}}

    try:
        # stream=True：让 DeepSeek 变成"边说边传"模式，返回迭代器
        stream = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=req.messages,   # 把完整历史一起发过去 → 多轮记忆
            stream=True,
            extra_body=extra_body,
        )

        # 逐条接收"小纸条"(chunk)
        for chunk in stream:
            choice = chunk.choices[0]
            delta = choice.delta

            # 关键技巧：getattr(对象, 属性, 兜底值)
            # thinking 关闭时，delta 上根本没有 reasoning_content 属性，
            # 直接访问会抛 AttributeError，用 getattr 拿不到就返回 None。
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield _sse("reasoning", reasoning)

            # content 是正式回答的增量（thinking 阶段它可能是 None）
            if delta.content:
                yield _sse("content", delta.content)

            # finish_reason == "stop" 表示模型说完了，收尾
            if choice.finish_reason:
                yield _sse("done")
                break

    except Exception as e:
        # 任何异常都推给前端一个 error 事件，方便前端提示用户
        yield _sse("error", str(e))


def _sse(type_: str, content: str = ""):
    """把事件拼成 SSE 文本：data: {...}\n\n"""
    # ensure_ascii=False：保留中文原文，前端不用转码
    payload = json.dumps({"type": type_, "content": content}, ensure_ascii=False)
    return f"data: {payload}\n\n"


# ---------------------------------------------------------------------------
# 5. 路由：POST /api/chat
# ---------------------------------------------------------------------------
@app.post("/api/chat")
async def chat(req: ChatRequest):
    """
    接收前端请求，返回一个"流式响应"。
    media_type="text/event-stream" 告诉浏览器：这是 SSE 流，请边收边显示。
    """
    return StreamingResponse(
        stream_chat(req),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
