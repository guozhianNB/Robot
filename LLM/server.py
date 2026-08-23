# -*- coding: utf-8 -*-
r"""
AI 对话后端（FastAPI + SSE 流式）—— 大模型端"大脑与嘴"的 HTTP 出口。

能力（对应 docs/2.pre/大模型端开发目标.md）：
  - /api/chat      多轮对话（流式 SSE）：护工角色 + 安全红线 + RAG 记忆注入 + 思考路由 + 工具调用（联网）
  - /api/profiles  老人档案 CRUD（含用药 → 自动同步每日服药提醒）
  - /api/memories  RAG 记忆查看 / 审核（已确认 / 待处理）/ 人工录入 / 沉淀触发
  - /api/reminders 定时提醒（护士建议录入 / 确认 / 状态机）
  - /api/tools/log 工具调用日志（审计可追溯）
  - /api/settings  功能开关（一键开关，持久化）
  - /api/events    提醒/告警广播（SSE，前端实时 toast）
  - /api/context   查看某位老人当前记住了什么（演示/调试用）

运行方式（在项目根目录执行，包方式导入）：
  .venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000
"""
import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import OpenAI
from pydantic import BaseModel

from . import db, bus, chat, memory as rag, reminder, tools as tool_mod, voice_api
from .conf import MODEL, BASE_DIR

load_dotenv(BASE_DIR / ".env")

client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)

_bg = ThreadPoolExecutor(max_workers=4)   # 后台任务池：记忆沉淀 / 历史摘要，不占请求链路


# ---------------------------------------------------------------------------
# 应用生命周期
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    from . import log as audit
    db.init_db()

    # 记忆 v3 迁移（幂等）+ 依赖自检
    try:
        from . import migrate
        migrate.run()
    except Exception as e:
        audit.log("memory_change", action="migrate_error", error=str(e))

    from . import embed as embed_mod, ragstore, graph
    audit.log("memory_degraded", embed=embed_mod.status(),
              ragstore=ragstore.status(), graph=graph.status())

    _seed_demo()
    reminder.start()          # 独立线程的定时提醒调度器
    drain_task = bus.start_drain()   # 广播扇出任务
    voice_api.start_voice(client, MODEL, _post_chat_jobs)
    yield
    voice_api.stop_voice()
    drain_task.cancel()


app = FastAPI(title="AI 陪护机器人后端", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


def _seed_demo():
    """首次启动：种一个示例老人档案（文档里的张建国示例）+ 示例护士建议。"""
    if db.list_profiles():
        return
    profile = {
        "病史": ["高血压"],
        "用药": [{"name": "降压药", "dose": "1片", "time": "08:00"}],
    }
    prefs = {"称呼": "闺女", "话题": ["京剧", "孙子"]}
    db.upsert_profile(
        uid="elder_001", name="张建国", nickname="张爷爷", bed="3-12", age=78,
        profile=profile, style="亲切北方口吻，爱用'闺女''老伴儿'称呼，话简短",
        preferences=prefs, notes="演示示例档案，可修改/删除后重建",
    )
    for med in profile["用药"]:
        db.upsert_medication_reminder("elder_001", med["name"], med["dose"], med["time"])
    db.add_reminder("elder_001", "nurse", "护士建议", "今天记得多喝水，天气转凉注意保暖",
                    "once", "18:00", db.now_iso()[:10], created_by="nurse")
    from . import log as audit
    audit.log("memory_change", action="seed", uid="elder_001", note="示例数据")


def _sse(event: dict) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def _post_chat_jobs(uid: str, user_text: str, assistant: str):
    """对话结束后的后台任务（线程池，不阻塞请求）：
    1. 本轮对话记入记忆整理缓冲，并安排"空闲 30s → 话题结束 → 批量整理记忆"定时器
    2. 上下文窗口已满时立即整理（不等到空闲）
    3. 滚动窗口历史摘要"""
    settings = db.get_settings()
    try:
        rag.note_turn(uid, user_text, assistant, client, MODEL, settings)
    except Exception as e:
        from . import log as audit
        audit.log("memory_change", action="note_error", uid=uid, error=str(e))
    try:
        if db.history_count(uid) >= chat.SUMMARY_THRESHOLD:
            rag.consolidate(uid, client, MODEL)   # 上下文满了，话题基本结束，立即整理
    except Exception:
        pass
    try:
        chat.summarize_old(uid, client, MODEL)
    except Exception:
        pass
    try:
        rag.correct_instant(uid, user_text, client, MODEL)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    uid: str = "elder_001"
    message: str
    thinking: str = "auto"          # auto / on / off


class ProfileIn(BaseModel):
    uid: str
    name: str = ""
    nickname: str = ""
    bed: str = ""
    age: int = 0
    profile: dict = {}              # {"病史": [...], "用药": [{"name","dose","time"}]}
    style: str = ""
    preferences: dict = {}          # {"称呼": "...", "话题": [...]}
    notes: str = ""


class MemoryIn(BaseModel):
    uid: str
    type: str = "fact"
    content: str
    status: str = "pending"


class SuggestIn(BaseModel):
    uid: str
    user_text: str = ""
    assistant_text: str = ""


class ReminderIn(BaseModel):
    uid: str
    kind: str = "nurse"
    title: str = ""
    content: str
    trigger_type: str = "once"      # daily / once
    trigger_time: str = "08:00"
    trigger_date: str = ""
    confirm_timeout_min: int = 30


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {"ok": True, "service": "llm-brain", "model": MODEL,
            "time": db.now_iso(), "profiles": len(db.list_profiles())}


@app.post("/api/chat")
async def chat_route(req: ChatRequest):
    settings = db.get_settings()

    def gen():
        assistant = ""
        for ev in chat.chat_stream(client, MODEL, req.uid, req.message, req.thinking, settings):
            if ev["type"] == "done":
                assistant = ev.get("assistant", "")
            yield _sse(ev)
        if assistant.strip():
            _bg.submit(_post_chat_jobs, req.uid, req.message, assistant)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/chat/history")
async def chat_history(uid: str = Query("elder_001"), limit: int = Query(200)):
    """回读某位老人的历史对话（前端刷新后恢复显示）。"""
    return {"ok": True, "history": db.load_history_full(uid=uid, limit=limit)}


@app.delete("/api/chat/history")
async def chat_history_clear(uid: str = Query("elder_001")):
    """清空某位老人的对话历史。"""
    n = db.clear_history(uid)
    from . import log as audit
    audit.log("chat", action="clear_history", uid=uid, count=n, by="nurse")
    return {"ok": True, "cleared": n}


# ---------------------------------------------------------------- 老人档案
@app.get("/api/profiles")
async def profiles_list():
    return {"ok": True, "profiles": db.list_profiles()}


@app.post("/api/profiles")
async def profiles_upsert(p: ProfileIn):
    prof = db.upsert_profile(
        p.uid, p.name, p.nickname, p.bed, p.age, p.profile, p.style, p.preferences, p.notes)
    # 用药 → 自动同步每日服药提醒
    meds = (p.profile or {}).get("用药") or []
    for m in meds:
        if isinstance(m, dict) and m.get("name") and m.get("time"):
            db.upsert_medication_reminder(p.uid, m["name"], m.get("dose", ""), m["time"])
    from . import log as audit
    audit.log("memory_change", action="profile_upsert", uid=p.uid, name=p.name, by="nurse")
    return {"ok": True, "profile": prof}


# ---------------------------------------------------------------- 记忆
@app.get("/api/memories")
async def memories_list(uid: str = Query(""), status: str = Query("")):
    return {"ok": True, "memories": db.list_memories(uid=uid or None, status=status or None)}


@app.post("/api/memories")
async def memories_add(m: MemoryIn):
    mid = db.add_memory(m.uid, m.type, m.content, status=m.status, source="manual")
    from . import log as audit
    audit.log("memory_change", action="manual_add", uid=m.uid, mid=mid, type=m.type,
              content=m.content, by="nurse")
    return {"ok": True, "id": mid}


@app.post("/api/memories/{mid}/confirm")
async def memories_confirm(mid: int):
    m = db.get_memory(mid)
    db.set_memory_status(mid, "confirmed")
    from . import log as audit
    audit.log("memory_change", action="confirm", mid=mid, uid=(m or {}).get("uid", ""), by="nurse")
    return {"ok": True}


@app.post("/api/memories/{mid}/reject")
async def memories_reject(mid: int):
    m = db.get_memory(mid)
    db.delete_memory(mid)
    from . import log as audit
    audit.log("memory_change", action="reject", mid=mid, uid=(m or {}).get("uid", ""), by="nurse")
    return {"ok": True}


@app.delete("/api/memories/{mid}")
async def memories_delete(mid: int):
    db.delete_memory(mid)
    return {"ok": True}


@app.post("/api/memories/suggest")
async def memories_suggest(s: SuggestIn):
    result = await asyncio.to_thread(rag.suggest_from_chat,
                                     s.uid, s.user_text, s.assistant_text, client, MODEL)
    return {"ok": True, **result}


@app.get("/api/context")
async def context_view(uid: str = Query("elder_001")):
    """演示/调试：看某位老人当前记住了什么。"""
    profile = db.get_profile(uid)
    confirmed = db.list_memories(uid=uid, status="confirmed")
    pending = db.list_memories(uid=uid, status="pending")
    return {
        "ok": True, "uid": uid, "profile": profile,
        "portrait": db.get_portrait(uid),
        "summary": db.get_summary(uid),
        "memories": {"confirmed": confirmed, "pending": pending},
    }


@app.get("/api/memories/core")
async def core_memories_list(uid: str = Query("elder_001")):
    return {"ok": True, "memories": db.list_core_memories(uid)}


@app.delete("/api/memories/core/{mid}")
async def core_memories_delete(mid: int):
    db.delete_core_memory(mid)
    return {"ok": True}


@app.get("/api/memories/rag")
async def rag_memories_list(uid: str = Query("elder_001")):
    return {"ok": True, "memories": db.list_rag_memories(uid)}


@app.get("/api/memories/graph")
async def graph_view(uid: str = Query("elder_001")):
    from . import graph as g
    return {"ok": True, "status": g.status(),
            "entities": g.list_entities(uid), "relations": g.list_relations(uid)}


@app.get("/api/memories/health")
async def memories_health():
    from . import embed as e, ragstore, graph as g
    return {"ok": True, "embed": e.status(), "ragstore": ragstore.status(), "graph": g.status()}


# ---------------------------------------------------------------- 提醒
@app.get("/api/reminders")
async def reminders_list(uid: str = Query("")):
    rows = db.list_reminders(uid=uid or None)
    for r in rows:
        r["status_label"] = reminder.status_label(r["status"])
    return {"ok": True, "reminders": rows}


@app.post("/api/reminders")
async def reminders_add(r: ReminderIn):
    rid = db.add_reminder(
        r.uid, r.kind, r.title or (r.content[:12]), r.content,
        r.trigger_type, r.trigger_time, r.trigger_date,
        confirm_timeout_min=r.confirm_timeout_min, created_by="nurse")
    from . import log as audit
    audit.log("reminder", action="create", rid=rid, uid=r.uid, kind=r.kind,
              content=r.content[:200], by="nurse")
    return {"ok": True, "id": rid}


@app.post("/api/reminders/{rid}/confirm")
async def reminders_confirm(rid: int, body: dict = None):
    return reminder.confirm(rid, uid=(body or {}).get("uid", ""))


@app.delete("/api/reminders/{rid}")
async def reminders_delete(rid: int):
    return reminder.dismiss(rid)


# ---------------------------------------------------------------- 工具日志
@app.get("/api/tools/log")
async def tools_log(uid: str = Query(""), limit: int = Query(100)):
    return {"ok": True, "logs": db.list_tool_log(uid=uid or None, limit=limit)}


# ---------------------------------------------------------------- 设置
@app.get("/api/settings")
async def settings_get():
    return {"ok": True, "settings": db.get_settings()}


@app.post("/api/settings")
async def settings_set(body: dict):
    patch = body.get("settings") or body
    cur = db.set_settings(patch)
    from . import log as audit
    audit.log("settings", change=json.dumps(patch, ensure_ascii=False), by="nurse")
    return {"ok": True, "settings": cur}


# ---------------------------------------------------------------- 语音
@app.post("/api/voice/enroll")
async def voice_enroll(body: dict):
    uid = (body or {}).get("uid", "elder_001")
    seconds = int((body or {}).get("seconds", 15))
    return await asyncio.to_thread(voice_api.enroll_speaker, uid, seconds)


@app.get("/api/voice/status")
async def voice_status():
    return voice_api.get_status()


@app.get("/api/voice/speakers")
async def voice_speakers():
    return {"ok": True, "speakers": voice_api.list_speakers()}


# ---------------------------------------------------------------- 广播（提醒/告警 SSE）
@app.get("/api/events")
async def events_stream():
    return StreamingResponse(
        bus.stream_events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------- 工具
@app.get("/api/tools")
async def tools_list():
    """返回当前可用工具清单（前端展示用）。"""
    return {"ok": True, "tools": tool_mod.TOOLS}
