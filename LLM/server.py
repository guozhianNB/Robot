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
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import OpenAI
from pydantic import BaseModel

from . import db, bus, chat, memory as rag, reminder, tools as tool_mod, voice_api
from . import mcp_client   # MCP 桥（可选能力，内部降级，import 永远安全）
from . import session      # 分层用户体系：会话层（角色/主体/当前病房）——业务接口的角色唯一来源
from . import log as audit  # 审计：本文件的登录冷却/病房变更在多处写审计，改顶层导入
from .conf import MODEL, BASE_DIR
from . import conf

load_dotenv(BASE_DIR / ".env")

client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)

_bg = ThreadPoolExecutor(max_workers=4)   # 后台任务池：记忆沉淀 / 历史摘要，不占请求链路
_shutting_down = False                    # 退出中标志：幂等防重入（放在 _bg 定义附近）


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
    audit.log("map_io_change", mode=conf.MAPS_IO, root=(
        conf.MAPS_SSH_ROOT if conf.MAPS_IO == "ssh" else str(conf.MAPS_DIR)))
    reminder.start()          # 独立线程的定时提醒调度器
    drain_task = bus.start_drain()   # 广播扇出任务
    voice_api.start_voice(client, MODEL, _post_chat_jobs)
    mcp_client.start(db.get_settings())   # MCP 外部工具（mcp_enabled 开启时拉起）

    # 分层用户体系：首启生成管理员口令（D12）+ 位置源自检 + 每秒 tick（TTL 降权 + 病房位置自动切换）
    pw = session.ensure_admin_password()
    if pw:
        print(f"[INFO] 已生成管理员初始口令：{pw}（登录后请立即修改）")
        audit.log("admin_password_generated")
    if not db.get_admin_auth()["hash"]:
        # 半写坏库（盐在哈希没了）等场合 ensure_admin_password 会自愈；这里再兜一层可见性：
        # 口令没落地 = 管理层**永远进不去**，必须让人在启动日志里就看见
        print("[WARN] 管理员口令未初始化：管理层将无法登录！")
    ok, why = locator.available()
    if not ok:
        print(f"[WARN] 位置源不可用（{why}）→ 病房位置自动切换停用，车前屏可手动切病房")

    async def _role_tick():
        """每秒一次：admin TTL 到期降权 + 按位姿自动切病房。"""
        while True:
            await asyncio.sleep(1)
            try:
                await asyncio.to_thread(session.tick)   # tick 会碰网络/SSH，别占事件循环
            except Exception:                           # noqa: BLE001  降级：tick 出错不拖垮服务
                pass

    tick_task = asyncio.create_task(_role_tick())

    yield
    mcp_client.stop()
    voice_api.stop_voice()
    drain_task.cancel()
    tick_task.cancel()


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


def _post_chat_jobs(uid: str, user_text: str, assistant: str, role: str | None = None):
    """对话结束后的后台任务（线程池，不阻塞请求）：
    1. 本轮对话记入记忆整理缓冲，并安排"空闲 30s → 话题结束 → 批量整理记忆"定时器
    2. 上下文窗口已满时立即整理（不等到空闲）
    3. 滚动窗口历史摘要

    `role` 透传给 `rag.note_turn`：collective 层（role="ward"）的话**不沉淀成任何老人
    的记忆**（规格 §5.3）。`role=None`（语音等老调用点只传 3 个参数）时**从会话层现取**
    —— 会话层是角色的唯一权威（R1）。语音是集体层的主入口，漏了这一步就会把病房公开
    对话当成老人的话沉淀下去。`/api/chat` 路由由任务 11 显式传 `principal["role"]`。"""
    if role is None:
        # 语音等老调用点没传角色：按当前主体现取（会话层是角色的唯一权威，R1）
        from . import session as role_session
        role = role_session.get_principal("kiosk")["role"]
    settings = db.get_settings()
    try:
        rag.note_turn(uid, user_text, assistant, client, MODEL, settings, role=role)
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
    speak: bool = False


class ProfileIn(BaseModel):
    uid: str
    name: str = ""
    nickname: str = ""
    bed: str = ""
    age: int = 0
    gender: str = ""
    birthday: str = ""
    profile: dict = {}              # {"病史": [...], "用药": [{"name","dose","time"}]}
    style: str = ""
    preferences: dict = {}          # {"称呼": "...", "话题": [...]}
    notes: str = ""


class MemoryIn(BaseModel):
    uid: str
    type: str = "fact"
    content: str
    status: str = "pending"


class MemoryCorrectIn(BaseModel):
    uid: str
    old_content: str
    new_content: str = ""
    by: str = "nurse"


class MemoryImportIn(BaseModel):
    uid: str
    text: str
    split: str = "paragraph"     # paragraph | line


class PortraitIn(BaseModel):
    uid: str
    content: str


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


class SessionUserIn(BaseModel):
    uid: str
    locked: bool = True
    role: str | None = None        # 只为显式拒绝它而存在（R1）：传了就 400


class LoginIn(BaseModel):
    password: str | None = None


class PasswordIn(BaseModel):
    old: str = ""
    new: str


class AdminAuthIn(BaseModel):
    required: bool


class WardIn(BaseModel):
    uid: str
    name: str = ""
    ward_map: str = ""             # 关联区域所在的地图名（几何真相在 <图名>.tags.json）
    ward_zone: str = ""            # 关联的区域 uid（形如 z1）


class WardAssignIn(BaseModel):
    ward_id: str = ""


class AlarmIn(BaseModel):
    type: str = "sos"          # sos / fall / health / no_activity ...
    uid: str = ""
    message: str = ""


# ---------------------------------------------------------------- 地图编辑器模型
class MapMetaIn(BaseModel):
    resolution: float | None = None
    origin: list[float] | None = None
    negate: int | None = None
    occupied_thresh: float | None = None
    free_thresh: float | None = None
    confirm: bool = False


class MapNameIn(BaseModel):
    new_name: str
    confirm: bool = False


class DestinationIn(BaseModel):
    map_name: str
    name: str = ""
    aliases: list[str] | str = []
    x: float | None = None
    y: float | None = None
    yaw_deg: float | None = None
    risk: str = "low"
    elder_allowed: int = 1
    note: str = ""
    learned_by: str = "editor"


class LearnIn(BaseModel):
    map_name: str
    name: str = ""
    aliases: list[str] | str = []
    risk: str = "low"
    elder_allowed: int = 1
    note: str = ""


class ZoneIn(BaseModel):
    map_name: str
    name: str = ""
    kind: str = "room"
    shape: str = "polygon"
    polygon: list[list[float]] = []
    parent: str = ""
    note: str = ""


class ValidateIn(BaseModel):
    map_name: str
    x: float
    y: float
    margin_m: float | None = None


class MapTagsIn(BaseModel):
    version: int = 1
    map: str = ""
    resolution: float | None = None
    origin: list[float] | None = None
    destinations: list[dict] = []
    zones: list[dict] = []


class MapSaveIn(BaseModel):
    pgm_b64: str = ""
    yaml_text: str = ""
    mode: str = "saveas"        # saveas | overwrite
    new_name: str = ""
    confirm: bool = False


class PoseInjectIn(BaseModel):
    x: float | None = None
    y: float | None = None
    yaw: float | None = None
    width: int | None = None
    height: int | None = None
    resolution: float | None = None
    origin: list[float] | None = None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {"ok": True, "service": "llm-brain", "model": MODEL,
            "time": db.now_iso(), "profiles": len(db.list_profiles())}


@app.get("/api/modules/status")
async def modules_status():
    """可选模块状态聚合：语音 / embedding / RAG 存储 / 知识图谱 / MCP 工具。
    各模块缺失依赖时自行降级（available=False / status=unavailable），接口照常返回。"""
    from . import embed as e, ragstore, graph as g
    return {"ok": True, "modules": {
        "voice":    voice_api.get_status(),
        "embed":    e.status(),
        "ragstore": ragstore.status(),
        "graph":    g.status(),
        "mcp":      mcp_client.status(),
    }}


@app.get("/api/logs/warnings")
async def logs_warnings(limit: int = Query(50)):
    """最近警告/错误审计日志（服务端过滤，供前端排查用）。"""
    from . import log as audit
    return {"ok": True, "logs": audit.read_warnings(limit=limit)}


@app.post("/api/chat")
async def chat_route(req: ChatRequest, x_surface: str = Header(default="kiosk")):
    settings = db.get_settings()
    # 角色只从会话层取（R1）：前端传来的任何身份字段都不可信；槽位由 X-Surface 决定
    principal = session.get_principal(_surface(x_surface))

    def gen():
        assistant = ""
        speech = voice_api.begin_text_reply() if req.speak else None
        completed = False
        try:
            for ev in chat.chat_stream(client, MODEL, req.uid, req.message, req.thinking,
                                       settings, principal=principal):
                if ev["type"] == "content":
                    voice_api.feed_text_reply(speech, ev.get("content") or "")
                elif ev["type"] == "done":
                    assistant = ev.get("assistant", "")
                    completed = True
                yield _sse(ev)
        finally:
            voice_api.end_text_reply(speech, flush_tail=completed)
            if completed and assistant.strip():
                # 角色随本轮的 principal 一起带给沉淀任务（集体层不沉淀，规格 §5.3）
                _bg.submit(_post_chat_jobs, req.uid, req.message, assistant, principal["role"])

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
async def profiles_list(kind: str = Query("elder")):
    """老人列表（默认只列 kind='elder'）；要看病房用 /api/wards，要看全部传 ?kind=all。"""
    return {"ok": True, "profiles": db.list_profiles(kind="" if kind == "all" else kind)}


@app.post("/api/profiles")
async def profiles_upsert(p: ProfileIn):
    prof = db.upsert_profile(
        p.uid, p.name, p.nickname, p.bed, p.age, p.profile, p.style, p.preferences, p.notes,
        gender=p.gender, birthday=p.birthday)
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
    op_id = db.delete_memory(mid, uid=(m or {}).get("uid", ""), reason="reject", by="nurse")
    from . import log as audit
    audit.log("memory_change", action="reject", mid=mid, op_id=op_id,
              uid=(m or {}).get("uid", ""), by="nurse")
    return {"ok": True, "op_id": op_id, "note": "已软删，可在回收站恢复"}


@app.delete("/api/memories/{mid}")
async def memories_delete(mid: int):
    m = db.get_memory(mid)
    op_id = db.delete_memory(mid, uid=(m or {}).get("uid", ""), reason="manual_delete", by="nurse")
    return {"ok": True, "op_id": op_id}


# ---------------------------------------------------------------- 回收站（软删恢复）
@app.get("/api/memories/recycle")
async def recycle_list(uid: str = Query("")):
    return {"ok": True, "operations": db.list_delete_operations(uid=uid)}


@app.post("/api/memories/recycle/{op_id}/restore")
async def recycle_restore(op_id: int):
    """恢复软删记忆。RAG 行会重建向量（chroma_id 可能变化）并回写。"""
    op = next((o for o in db.list_delete_operations(include_restored=True) if o["id"] == op_id), None)
    if not op:
        return {"ok": False, "error": "操作不存在"}
    table = op["target_table"]
    restored = db.restore_operation(op_id)
    if not restored:
        return {"ok": False, "error": "恢复失败或已恢复"}
    if table == "rag_memories":
        row = db.get_rag_memory(op["target_id"])
        if row and row.get("chroma_id"):
            from . import ragstore as _rs
            new_cid = _rs.reindex_row(row["uid"], row["type"], row["content"],
                                      importance=row.get("importance", 0),
                                      source=row.get("source", ""), old_chroma_id=row["chroma_id"])
            if new_cid and new_cid != row["chroma_id"]:
                db.set_rag_chroma_id(op["target_id"], new_cid)
    from . import log as audit
    audit.log("memory_change", action="restore", op_id=op_id, uid=op.get("uid", ""),
              table=table, by="nurse")
    return {"ok": True, "table": table, "op_id": op_id}


@app.post("/api/memories/recycle/purge")
async def recycle_purge(days: float = Query(30.0)):
    n = db.purge_soft_deleted(days=days)
    from . import log as audit
    audit.log("memory_change", action="purge", count=n, days=days, by="nurse")
    return {"ok": True, "purged": n}


@app.post("/api/memories/correct")
async def memories_correct(c: MemoryCorrectIn):
    """反馈纠错：老人/护士指出旧记忆错误 → 旧条目软删(回收站可回滚) + RAG 向量失效 + 写回正确内容。

    对标 MaiBot stale 联动：检索/画像不再命中旧条目，纠正全程留痕可回滚。
    """
    if not (c.old_content or "").strip():
        return {"ok": False, "error": "old_content 不能为空"}
    result = await asyncio.to_thread(rag.correct_from_feedback,
                                     c.uid, c.old_content, c.new_content, by=c.by)
    return result


@app.post("/api/memories/import")
async def memories_import(m: MemoryImportIn):
    """批量导入中心：粘贴文本按段落/行切分入 pending，护士审核确认（对标 MaiBot 导入中心）。"""
    if not (m.text or "").strip():
        return {"ok": False, "error": "text 不能为空"}
    return await asyncio.to_thread(db.import_memories, m.uid, m.text, by="nurse", split=m.split)


@app.post("/api/memories/portrait")
async def memories_portrait_set(p: PortraitIn):
    """护士手动维护老人画像：写入 pinned 保护，AI consolidate 不再覆盖（对标 MaiBot 画像 override）。"""
    if not (p.content or "").strip():
        return {"ok": False, "error": "content 不能为空"}
    await asyncio.to_thread(rag._upsert_portrait, p.uid, p.content, source="nurse:manual", by="nurse")
    return {"ok": True}


@app.delete("/api/memories/core/{mid}")
async def core_memories_delete(mid: int):
    m = db.get_core_memory(mid)
    op_id = db.delete_core_memory(mid, uid=(m or {}).get("uid", ""), reason="manual_delete", by="nurse")
    from . import log as audit
    audit.log("memory_change", action="core_delete", mid=mid, op_id=op_id,
              uid=(m or {}).get("uid", ""), by="nurse")
    return {"ok": True, "op_id": op_id}


@app.get("/api/memories/rag")
async def rag_memories_list(uid: str = Query("elder_001")):
    return {"ok": True, "memories": db.list_rag_memories(uid)}


@app.delete("/api/memories/rag/{rid}")
async def rag_memories_delete(rid: int):
    """删除 RAG 记忆：镜像表软删 + Chroma 向量同步清理。"""
    row = db.get_rag_memory(rid)
    if not row:
        return {"ok": False, "error": "不存在"}
    op_id = db.delete_rag_memory(rid, uid=row.get("uid", ""), reason="manual_delete", by="nurse")
    if row.get("chroma_id"):
        from . import ragstore as _rs
        _rs.delete_by_chroma_id(row.get("uid", ""), row["chroma_id"])
    from . import log as audit
    audit.log("memory_change", action="rag_delete", mid=rid, op_id=op_id,
              uid=row.get("uid", ""), by="nurse")
    return {"ok": True, "op_id": op_id}


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


@app.post("/api/memories/core/{mid}/confirm")
async def core_memories_confirm(mid: int):
    """护士确认核心记忆 = 定稿(nurse)：不再标注'AI 归纳仅供参考'，进入可信层。"""
    m = db.get_core_memory(mid)
    if not m:
        return {"ok": False, "error": "不存在"}
    db.set_core_authority(mid, "nurse")
    from . import log as audit
    audit.log("memory_change", action="core_confirm", mid=mid, uid=m.get("uid", ""),
              authority="nurse", by="nurse")
    return {"ok": True}


@app.post("/api/memories/core/{mid}/unconfirm")
async def core_memories_unconfirm(mid: int):
    """撤销定稿，退回 AI 归纳层（llm）。"""
    m = db.get_core_memory(mid)
    if not m:
        return {"ok": False, "error": "不存在"}
    db.set_core_authority(mid, "llm")
    from . import log as audit
    audit.log("memory_change", action="core_unconfirm", mid=mid, uid=m.get("uid", ""),
              authority="llm", by="nurse")
    return {"ok": True}


@app.post("/api/memories/core/{mid}/pin")
async def core_memories_pin(mid: int):
    """护士保护该核心记忆：不被自动清理、不被画像整体覆盖。"""
    m = db.get_core_memory(mid)
    if not m:
        return {"ok": False, "error": "不存在"}
    db.set_core_pinned(mid, True)
    from . import log as audit
    audit.log("memory_change", action="core_pin", mid=mid, uid=m.get("uid", ""), by="nurse")
    return {"ok": True}


@app.post("/api/memories/core/{mid}/unpin")
async def core_memories_unpin(mid: int):
    """解除保护。"""
    m = db.get_core_memory(mid)
    if not m:
        return {"ok": False, "error": "不存在"}
    db.set_core_pinned(mid, False)
    from . import log as audit
    audit.log("memory_change", action="core_unpin", mid=mid, uid=m.get("uid", ""), by="nurse")
    return {"ok": True}


@app.get("/api/memories/graph")
async def graph_view(uid: str = Query("elder_001")):
    from . import graph as g
    return {"ok": True, "status": g.status(),
            "entities": g.list_entities(uid), "relations": g.list_relations(uid)}


# ---------------------------------------------------------------- 表达习惯（风格学习产物）
@app.get("/api/memories/expressions")
async def expressions_list(uid: str = Query("elder_001")):
    return {"ok": True, "expressions": db.list_expressions(uid=uid)}


@app.post("/api/memories/expressions/{eid}/approve")
async def expressions_approve(eid: int):
    """护士审核通过 → 该语录参与对话注入（对标 MaiBot checked_only）。"""
    db.set_expression_checked(eid, True)
    from . import log as audit
    audit.log("memory_change", action="expression_approve", eid=eid, by="nurse")
    return {"ok": True}


@app.post("/api/memories/expressions/{eid}/reject")
async def expressions_reject(eid: int):
    """护士拒绝 → 软删（进回收站可恢复）。"""
    db.soft_delete_expression(eid)
    from . import log as audit
    audit.log("memory_change", action="expression_reject", eid=eid, by="nurse")
    return {"ok": True}


@app.get("/api/memories/health")


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
@app.post("/api/voice/record")
async def voice_record(body: dict = None):
    """两步式声纹第 1 步：录制并暂存（不落档），返回 recording_id。"""
    body = body or {}
    seconds = int(body.get("seconds", 15))
    uid = body.get("uid")
    return await asyncio.to_thread(voice_api.record_speaker, seconds, uid)


@app.get("/api/voice/record/{recording_id}/audio")
async def voice_record_audio(recording_id: str):
    """试听：返回暂存录音的 wav。"""
    got = await asyncio.to_thread(voice_api.get_recording_audio, recording_id)
    if got is None:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404,
                            content={"ok": False, "error": "录音已过期或不存在"})
    data, ctype = got
    from fastapi.responses import Response
    return Response(content=data, media_type=ctype)


@app.delete("/api/voice/record/{recording_id}")
async def voice_record_discard(recording_id: str):
    """丢弃暂存录音（重录/放弃时用）。"""
    return await asyncio.to_thread(voice_api.discard_recording, recording_id)


@app.post("/api/voice/enroll")
async def voice_enroll(body: dict = None):
    body = body or {}
    uid = body.get("uid", "elder_001")
    if body.get("recording_id"):
        # 两步式第 2 步：提交暂存入档（append 默认 True = 合并平均）
        append = bool(body.get("append", True))
        return await asyncio.to_thread(voice_api.commit_speaker,
                                       body["recording_id"], uid, append)
    # 旧行为兼容：无 recording_id 直接录 seconds 秒覆盖建档
    seconds = int(body.get("seconds", 15))
    return await asyncio.to_thread(voice_api.enroll_speaker, uid, seconds)


# ---------------------------------------------------------------- 会话 / 角色（分层用户体系）
# 规格：docs/superpowers/specs/2026-09-14-layered-user-roles-design.md
# 红线 R1：角色只由 session.derive_role()（依据 profiles.kind）推导，前端传的一律不可信。
_SURFACES = ("kiosk", "admin")
_login_fail: dict[str, dict] = {}      # slot -> {"n": 连续失败次数, "until": 冷却截止}


def _surface(x_surface: str = Header(default="kiosk")) -> str:
    """端槽位：kiosk（车前/语音）| admin（管理台）。**缺头**按 kiosk，非法值显式 400。

    绝不静默回落 kiosk：那会让 `X-Surface: TABLET` 这种笔误把"管理台的口令登录"写到车前屏
    上（顺带把车前屏提权），而且返回体/审计里的 slot 还是那个错名（追溯性一并破坏）。

    空串也一律 400（"提供了非法值"与"没提供"是两回事：空串回落同样会把管理台的登录写进
    kiosk 槽）。只有**非 str** 才按缺省处理 —— 那是 handler 被直接调用（不经 FastAPI 依赖
    注入，如既有单测 `server.chat_route(req)`）时留在形参默认值里的 `Header(...)` 标记对象，
    真实请求里的请求头永远是 str。

    顺带兜底建表：会话/病房/策略这一族路由**全部**以本函数为公共入口，而它们都要读
    `profiles.kind`（R1 角色推导的唯一依据）或 `settings`（口令门/口令哈希）——"库文件在、
    表不在"的场合（首启半途中断、测试隔离出空库）必须自愈而不是 500，同
    `voice_api._ensure_schema()` 的初衷；生产上 lifespan 已建过表，这里只是一次字符串比较。
    """
    voice_api._ensure_schema()
    if not isinstance(x_surface, str):
        x_surface = "kiosk"
    if x_surface not in _SURFACES:
        raise HTTPException(status_code=400, detail=f"未知的 X-Surface：{x_surface!r}（只允许 {_SURFACES}）")
    return x_surface


def _public_policy(pol: dict) -> dict:
    """策略包转成可 JSON 序列化的形式（Path → str）。"""
    return {k: (str(v) if isinstance(v, Path) else v) for k, v in pol.items()}


def _login_cooling(slot: str) -> bool:
    st = _login_fail.get(slot) or {}
    return bool(st.get("until")) and time.time() < st["until"]


def _login_failed(slot: str) -> None:
    st = _login_fail.setdefault(slot, {"n": 0, "until": 0.0})
    st["n"] += 1
    if st["n"] >= 3:                       # 连续 3 次失败 → 该槽冷却 10s
        st["until"] = time.time() + 10
        st["n"] = 0
        audit.log("session_login_fail", slot=slot, action="cooling")


def _session_user_payload(slot: str) -> dict:
    """当前会话主体的返回体（**同步**，由路由丢进线程跑，见下）。

    形状保持老契约：`ok` 必在、没有主体时 `uid` 是 `None` —— 车前屏靠 `uid === null` 判断
    "没选人"，`frontend/packages/shared/src/api/session.ts` 与 `tests/test_session_api.py`
    都吃这个形状（`voice_api.get_session_uid()` 也是这么兜的）。
    """
    p = dict(session.get_principal(slot))       # 建表兜底在 `_surface()`（本族路由的公共入口）
    p["uid"] = p["uid"] or None
    return {"ok": True, **p,
            "ttl_remain": session.ttl_remain(slot),
            "auth_required": db.get_admin_auth()["required"],
            "autoswitch": session.autoswitch_state()}


@app.get("/api/session/user")
async def session_user_get(x_surface: str = Header(default="kiosk")):
    """当前会话主体（uid/角色/锁定/当前病房/TTL），**角色按请求槽位返回**（双槽隔离）。

    必须走线程：`autoswitch_state()` 会读位姿（`locator.get_pose()` 最多 drain 0.8s）并反查
    "车在跑哪张图"（`MAPS_IO=ssh` 下含远端 stat/整图拉取），占住事件循环会拖慢整个后端。
    """
    slot = _surface(x_surface)
    return await asyncio.to_thread(_session_user_payload, slot)


@app.post("/api/session/user")
async def session_user_set(s: SessionUserIn, x_surface: str = Header(default="kiosk")):
    """切换会话主体：**只接受 uid/locked**；`role` 与 `uid="admin"` 一律 400（R1）。

    `uid="admin"` 必须在边界拒：`session.set_subject()` 的拒绝对 admin 提权分支返回的是
    **当前主体**（与成功同形），前端会误以为"切到管理员成功了"。
    """
    if s.role:
        raise HTTPException(status_code=400, detail="role 不可由前端指定（R1）")
    if s.uid == session.ADMIN_UID:
        raise HTTPException(status_code=400,
                            detail="管理员身份只能经 /api/session/login 获取（R1）")
    slot = _surface(x_surface)
    res = dict(session.set_subject(s.uid, s.locked, slot=slot, source="manual"))
    res["ok"] = True                       # 老接口形状（voice_api.set_session_uid 同款）
    bus.publish("user_changed", uid=res["uid"], role=res["role"], slot=slot,
                locked=res["locked"], ward_uid=res["ward_uid"], source="manual")
    return res


@app.post("/api/session/login")
async def session_login(body: LoginIn | None = None, x_surface: str = Header(default="kiosk")):
    slot = _surface(x_surface)
    if _login_cooling(slot):
        return {"ok": False, "error": "口令失败次数过多，请 10 秒后重试"}
    res = session.login_admin((body.password if body else None), slot=slot)
    if res.get("ok"):
        _login_fail.pop(slot, None)
        bus.publish("user_changed", uid=res["uid"], role=res["role"], slot=slot,
                    source=res["source"])
    else:
        _login_failed(slot)
    return res


@app.post("/api/session/logout")
async def session_logout(x_surface: str = Header(default="kiosk")):
    slot = _surface(x_surface)
    res = session.logout(slot)
    bus.publish("user_changed", uid=res["uid"], role=res["role"], slot=slot, source="logout")
    return res


@app.post("/api/session/password")
async def session_password(body: PasswordIn, x_surface: str = Header(default="kiosk")):
    if session.get_principal(_surface(x_surface))["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可改口令")
    return session.change_admin_password(body.old, body.new)


@app.get("/api/session/admin-auth")
async def admin_auth_get():
    voice_api._ensure_schema()   # 本族仅此路由不经过 _surface（无端槽位语义），建表兜底单独兜一次
    return {"required": db.get_admin_auth()["required"]}


@app.post("/api/session/admin-auth")
async def admin_auth_set(body: AdminAuthIn, x_surface: str = Header(default="kiosk")):
    if session.get_principal(_surface(x_surface))["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可开关口令门")
    res = session.set_admin_auth(body.required)
    bus.publish("admin_auth_changed", required=res["required"])
    return res


def _ward_payload(w: dict) -> dict:
    """病房一条（含它关联的区域行与归属老人）——区域从**只读缓存**取，取不到就是 null。"""
    zone = db.get_zone(w.get("ward_zone") or "", w.get("ward_map") or "") \
        if w.get("ward_zone") else None
    return {"uid": w["uid"], "name": w.get("name", ""),
            "ward_map": w.get("ward_map", ""), "ward_zone": w.get("ward_zone", ""),
            "zone": zone,
            "elders": [p["uid"] for p in db.list_profiles(kind="elder")
                       if p.get("ward_id") == w["uid"]]}


@app.get("/api/wards")
async def wards_list():
    """病房用户列表（车前屏的层级栏也要用，故任何角色可读；只含名字与归属，无隐私内容）。"""
    voice_api._ensure_schema()   # 同上：本路由不经过 _surface（任何角色可读）
    return {"ok": True, "wards": [_ward_payload(w) for w in db.list_wards()]}


@app.post("/api/wards")
async def wards_upsert(w: WardIn, x_surface: str = Header(default="kiosk")):
    if session.get_principal(_surface(x_surface))["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可管理病房")
    db.upsert_ward(w.uid, name=w.name, ward_map=w.ward_map, ward_zone=w.ward_zone)
    audit.log("ward_change", source="admin", action="upsert", ward=w.uid)
    bus.publish("ward_changed", uid=w.uid, action="upsert")
    return {"ok": True, "ward": _ward_payload(db.get_profile(w.uid) or {"uid": w.uid})}


@app.post("/api/wards/{ward_uid}/zone")
async def ward_set_zone(ward_uid: str, x_surface: str = Header(default="kiosk")):
    """便捷录入：以当前位姿为圆心、`ward_zone_default_r` 为半径采样 16 边形写入**该图的
    `<图名>.tags.json`**（`maptags.record_room_polygon`，已有实现、本次补 HTTP 入口），
    再把「地图名 + 区域 uid」回填进 profiles。精确形状请到 /mapeditor 画多边形。
    """
    if session.get_principal(_surface(x_surface))["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可管理病房")
    pose = locator.get_pose()
    if not pose or pose.get("x") is None:
        return {"ok": False, "error": "拿不到小车位姿（rosbridge/定位未就绪）；可到地图编辑器手绘区域"}
    map_name, why = session.running_map_name()
    if not map_name:
        return {"ok": False,
                "error": f"认不出当前地图（{why}）：请确认导航在跑且 /map 指纹能唯一命中，"
                         f"或把设置 ward_map_source 改成 setting 并选好 current_map"}
    ward = db.get_profile(ward_uid) or {}
    name = ward.get("name") or ward_uid
    r = float(db.get_settings().get("ward_zone_default_r", 3.0))
    try:
        res = maptags.record_room_polygon(map_name, name, float(pose["x"]), float(pose["y"]),
                                          radius_m=r, kind="ward")
    except Exception as e:                 # noqa: BLE001  板卡不可达/名字非法等 → 只降级不 500
        return {"ok": False, "error": f"写地图标记失败：{e}"}
    db.set_ward_zone(ward_uid, map_name, res["uid"])
    audit.log("ward_change", source="admin", action="zone", ward=ward_uid,
              map=map_name, zone=res["uid"])
    bus.publish("ward_changed", uid=ward_uid, action="zone")
    return {"ok": True, "map": map_name, "zone_uid": res["uid"],
            "zone": maptags.get_zone(map_name, res["uid"])}


@app.post("/api/profiles/{uid}/ward")
async def profile_set_ward(uid: str, body: WardAssignIn,
                           x_surface: str = Header(default="kiosk")):
    """把老人归入/移出病房（写 profiles.ward_id）。

    单独一条路由是**故意的**：`upsert_profile` 不许碰病房字段，否则档案编辑会静默清掉关联。
    """
    if session.get_principal(_surface(x_surface))["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可分配病房")
    db.set_profile_ward(uid, body.ward_id)
    audit.log("ward_change", source="admin", action="assign", elder=uid, ward=body.ward_id)
    bus.publish("ward_changed", uid=body.ward_id, action="assign")
    return {"ok": True, "uid": uid, "ward_id": body.ward_id}


@app.get("/api/policy/roles")
async def policy_roles(x_surface: str = Header(default="kiosk")):
    """策略矩阵：管理员看全量，其它角色只拿自己那份摘要。"""
    from .policy import POLICY_DEFAULTS
    role = session.get_principal(_surface(x_surface))["role"]
    if role != "admin":
        return {role: _public_policy(POLICY_DEFAULTS.get(role, POLICY_DEFAULTS["ward"]))}
    return {k: _public_policy(v) for k, v in POLICY_DEFAULTS.items()}


# ---------------------------------------------------------------- 紧急呼叫
@app.post("/api/alarm")
async def alarm_report(a: AlarmIn):
    """紧急呼叫上报（规格 D6）：审计 + 广播；微信推送留给模块 11。"""
    from . import log as audit
    audit.log("alarm", action="report", type=a.type, uid=a.uid,
              message=a.message[:200], by="nurse")
    # 注意：payload 键用 alarm_type 而非 type —— bus.publish 内部构造
    # {"type": event_type, **payload}，payload 里再用 type 会覆盖事件类型，
    # 导致广播的事件 type 变成 "sos" 而非 "alarm"，前端会丢弃该事件
    bus.publish("alarm", level="critical", alarm_type=a.type, uid=a.uid, message=a.message)
    return {"ok": True}


@app.get("/api/voice/status")
async def voice_status():
    return voice_api.get_status()


@app.get("/api/voice/speakers")
async def voice_speakers():
    if not voice_api._VOICE_AVAILABLE:
        return {"ok": True, "status": "unavailable", "reason": voice_api._degraded_msg(),
                "speakers": [], "details": {}}
    return {"ok": True, "speakers": voice_api.list_speakers(),
            "details": voice_api.list_speaker_details()}


@app.delete("/api/voice/speakers/{uid}")
async def voice_speaker_delete(uid: str):
    """删除老人声纹（注销/重录时用）。"""
    return await asyncio.to_thread(voice_api.delete_speaker, uid)


@app.get("/api/face/status")
async def face_status():
    """人脸录入占位：本期未实现，返回 unavailable（前端据此置灰按钮）。"""
    return {"ok": True, "status": "unavailable",
            "reason": "人脸录入尚未接入（占位接口，见 docs/temp/face-recognition-notes.md）"}


# ---------------------------------------------------------------- 广播（提醒/告警 SSE）
@app.get("/api/events")
async def events_stream():
    return StreamingResponse(
        bus.stream_events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------- 系统
@app.post("/api/system/shutdown")
async def system_shutdown():
    """系统退出：停提醒线程 → 停语音（释放音频设备）→ 停广播 → 停线程池，
    返回响应后延迟 1 秒 os._exit(0)，保证前端先收到 200 再杀进程。
    幂等：重复调用直接返回；任何 stop 步骤抛异常也保证退出任务被调度。"""
    global _shutting_down
    if _shutting_down:
        return {"ok": True, "message": "系统正在退出…"}
    _shutting_down = True
    from . import log as audit
    audit.log("system", action="shutdown", by="nurse")
    try:
        reminder.stop()                      # 1. 提醒调度线程（不再触发新提醒）
        voice_api.stop_voice()               # 2. 语音 worker（释放麦克风/扬声器）
        bus.stop()                           # 3. 事件总线扇出
        _bg.shutdown(wait=False)             # 4. 后台任务线程池（不等待，进程将退出）
    finally:
        asyncio.create_task(_delayed_exit()) # 5. 无论上述步骤是否抛异常，1 秒后真正退出
    return {"ok": True, "message": "系统正在退出…"}


async def _delayed_exit():
    """延迟退出：给 uvicorn 留出时间把上面这个响应发回前端。"""
    await asyncio.sleep(1.0)
    os._exit(0)


# ---------------------------------------------------------------- 工具
@app.get("/api/tools")
async def tools_list():
    """返回当前可用工具清单 + 每工具开关状态（前端展示/切换用）。"""
    return {"ok": True, "tools": tool_mod.tools_with_state(db.get_settings())}


# ---------------------------------------------------------------------------
# 地图编辑器（第三个前端 /mapeditor）
# 规格：docs/superpowers/specs/2026-09-14-map-editor-design.md
#   红线（§4）：标记的唯一真相是地图文件夹里的 <图名>.tags.json，brain.db 只是只读索引缓存；
#               所有写接口都是「改文件 → maptags.sync_map() → 审计」，绝不"只改库不改文件"。
# ---------------------------------------------------------------------------
from . import mapserver, maptags, mapstore, locator   # noqa: E402  （放此处便于阅读，import 无副作用）
from fastapi.responses import Response                 # noqa: E402

_MAP_EXTS = ("yaml", "pgm", "tags")


def _store(source: str = ""):
    """「地图源」依赖：``source`` 空 = 用默认源（``mapsources.default``）。

    **源未知一律转 HTTP 400**（附可用源清单）——这是用户可纠正的输入错误，不能变 500
    （实测：未知源原来会抛 500）。作为 FastAPI 依赖使用时（``store=Depends(_store)``）
    异常自动变成 400 响应；直接调用时抛 ``HTTPException``，路由无需各自 try/except。
    """
    from fastapi import HTTPException
    from . import mapsources
    try:
        return mapstore.get_store(source)
    except (mapstore.MapStoreError, mapsources.SourceError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _store_or_err(source: str = ""):
    """``(store, None)`` 或 ``(None, 400 响应)``：所有带 ``?source=`` 的路由统一用它。

    未知源是用户可纠正的输入错误，必须 400 + 可用源清单（实测直接抛会变 500）。
    """
    from . import mapsources
    try:
        return mapstore.get_store(source), None
    except (mapstore.MapStoreError, mapsources.SourceError) as e:
        return None, _err(str(e))


def _err(msg: str, code: int = 400, **extra):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=code, content={"ok": False, "error": msg, **extra})


def _name_of(path_name: str) -> str:
    """路径参数 → 合法地图名；不合法直接抛（由各路由统一转 400）。"""
    return mapstore.check_name(path_name)


def _tag_counts(names: list[str]) -> dict[str, dict]:
    """批量取各图的标记数量（读缓存，缺失即 0，不触发远程读）。"""
    out: dict[str, dict] = {}
    for n in names:
        try:
            out[n] = db.count_map_tags(n)
        except Exception:      # noqa: BLE001
            out[n] = {"destinations": 0, "zones": 0}
    return out


# ---------------------------------------------------------------- 地图源（sources）
@app.get("/api/map/sources")
async def map_sources_list(test: bool = Query(False)):
    """列出全部地图源（只读；界面只做"选"、不手填路径）。

    ``test=true`` 时对每个源真连一次（等价于 ``/test``），慢（ssh 每个源一次往返）；
    默认只给配置态，连通性由前端按需点"自检"或对当前源调用 ``/test``。
    """
    from . import mapsources
    doc = mapsources.load()
    store_map = {}
    if test:
        for s in doc["items"]:
            try:
                store_map[s["id"]] = mapstore.get_store(s["id"])
            except Exception:      # noqa: BLE001  单个源造不出来不影响列表
                pass
    return mapsources.view_list(store_map or None)


@app.post("/api/map/sources")
async def map_sources_upsert(body: dict = None):
    """新增/修改一条源。**这是配置入口，不是给界面自由填路径用的**：界面只做选择。"""
    from . import log as audit, mapsources
    body = body or {}
    src = body.get("source") if isinstance(body.get("source"), dict) else body
    try:
        doc = mapsources.upsert(src)
    except mapsources.SourceError as e:
        return _err(str(e))
    mapstore.reset_store()                 # 源变了 → 丢掉缓存实例，下次按新配置重建
    audit.log("map_source", action="upsert", source=src.get("id"), kind=src.get("kind"))
    return {"ok": True, **mapsources.view_list(), "default": doc["default"]}


@app.post("/api/map/sources/{sid}/default")
async def map_sources_set_default(sid: str):
    """把某源设为默认源（不传 ``?source=`` 时用它）。"""
    from . import log as audit, mapsources
    try:
        doc = mapsources.set_default(sid)
    except mapsources.SourceError as e:
        return _err(str(e), 404)
    mapstore.reset_store()
    audit.log("map_source", action="set_default", source=sid)
    return {"ok": True, "default": doc["default"], **mapsources.view_list()}


@app.post("/api/map/sources/{sid}/test")
async def map_sources_test(sid: str):
    """对某个源做连通性自检（``list()`` 一次，**不改任何文件**）。"""
    from . import mapsources
    try:
        mapstore.resolve_source(sid)
    except (mapstore.MapStoreError, mapsources.SourceError) as e:
        return _err(str(e), 404)
    return await asyncio.to_thread(mapstore.io_test, sid)


@app.delete("/api/map/sources/{sid}")
async def map_sources_delete(sid: str):
    """删一条源（默认源不允许删，见 mapsources.remove 的说明）。"""
    from . import log as audit, mapsources
    try:
        mapsources.remove(sid)
    except mapsources.SourceError as e:
        return _err(str(e))
    mapstore.reset_store()
    audit.log("map_source", action="delete", source=sid)
    return {"ok": True, **mapsources.view_list()}


@app.get("/api/map/list")
async def map_list(source: str = Query("", alias="source")):
    """列地图（**必须排除 .backup/**，规格 §〇 第 4 条）。带尺寸/元数据/未知率/标记数/残缺态。"""
    return await asyncio.to_thread(_map_list_sync, source)


def _map_list_sync(source: str = ""):
    """同步地图扫描在线程池执行，避免 SSH/PGM 工作阻塞 ASGI 事件循环。"""
    from . import log as audit
    from . import mapsources
    try:
        store = mapstore.get_store(source)
    except (mapstore.MapStoreError, mapsources.SourceError) as e:
        # 未知源 = 用户可纠正的输入错误 → 明确 400（含可用源），不要 500
        return _err(str(e))
    ok, why = store.available()
    if not ok:
        return {"ok": True, "status": "unavailable", "reason": why, "maps": [],
                "source": mapstore.current_source().get("id", ""),
                "mode": conf.MAPS_IO, "root": store.root}
    try:
        entries = store.list()
    except mapstore.MapStoreError as e:
        return {"ok": True, "status": "unavailable", "reason": str(e), "maps": [],
                "source": mapstore.current_source().get("id", ""),
                "mode": conf.MAPS_IO, "root": store.root}
    counts = _tag_counts([e["name"] for e in entries])
    maps = []
    for e in entries:
        item = dict(e)
        item["counts"] = counts.get(e["name"], {"destinations": 0, "zones": 0})
        item["status"] = ("残缺：有 pgm 没 yaml" if e["has_pgm"] and not e["has_yaml"]
                          else "残缺：有 yaml 没 pgm" if e["has_yaml"] and not e["has_pgm"]
                          else "ok")
        item["meta_ok"] = False
        item["width"] = item["height"] = None
        item["resolution"] = item["origin"] = None
        item["unknown_ratio"] = None
        if e["has_yaml"]:
            try:
                info = mapserver.map_info(e["name"], store)
                item.update({"width": info.get("width"), "height": info.get("height"),
                             "resolution": info.get("resolution"), "origin": info.get("origin"),
                             "unknown_ratio": info.get("unknown_ratio"),
                             "meta_ok": bool(info.get("meta_ok")),
                             "problems": info.get("problems") or [],
                             "stale": info.get("stale"), "cached_at": info.get("cached_at")})
            except mapstore.MapStoreError as ex:
                item["problems"] = [str(ex)]
        item["current"] = (db.get_settings().get("current_map") == e["name"])
        maps.append(item)
    audit.log("map_change", action="list", count=len(maps), mode=conf.MAPS_IO)
    return {"ok": True, "maps": maps, "mode": conf.MAPS_IO, "root": store.root,
            "current_map": db.get_settings().get("current_map")}


@app.get("/api/map/current")
async def map_current(source: str = Query("", alias="source"), store=Depends(_store)):
    """车此刻在跑哪张图（**不靠人工声明**，指纹反查，规格 §5.3）。"""
    return await asyncio.to_thread(locator.current_map, store)


@app.get("/api/map/{name}/meta")
async def map_meta(name: str):
    return await asyncio.to_thread(_map_meta_sync, name)


def _map_meta_sync(name: str, store):
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    try:
        info = mapserver.map_info(n, store)
    except mapstore.MapStoreError as e:
        return _err(str(e), 404)
    counts = db.count_map_tags(n)
    got = maptags.resolve(n, store)
    return {"ok": True, "meta": info, "counts": counts,
            "tags_exists": got["exists"], "tags_warnings": got.get("warnings") or [],
            "tags_path": maptags.tags_path(n, store),
            "fingerprint": got.get("fingerprint"),
            "tags_mtime": maptags.file_mtime_iso(n, store)}


@app.post("/api/map/{name}/meta")
def map_meta_set(name: str, body: MapMetaIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """改 resolution/origin 等元数据：**先返回将失效的标记数并要求 confirm=true**（规格 §7.2）。"""
    from . import log as audit
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    try:
        info = mapserver.map_info(n, store)
    except mapstore.MapStoreError as e:
        return _err(str(e), 404)
    counts = db.count_map_tags(n)
    affects = counts["destinations"] + counts["zones"]
    patch: dict = {}
    for field in ("resolution", "origin", "negate", "occupied_thresh", "free_thresh"):
        v = getattr(body, field)
        if v is not None:
            patch[field] = v
    if not patch:
        return _err("没有要修改的字段", 400)
    if body.resolution is not None and body.resolution <= 0:
        return _err("resolution 必须为正数", 400)
    if body.origin is not None and len(body.origin) < 2:
        return _err("origin 至少要 2 个分量", 400)
    if body.origin is not None and len(body.origin) > 2 and abs(float(body.origin[2])) > 1e-9:
        return _err("origin 的 yaw 必须为 0（本项目不支持旋转地图）", 400)
    if affects and not body.confirm:
        return _err(f"本图有 {counts['destinations']} 个地点、{counts['zones']} 个区域，"
                    f"改 resolution/origin 会让它们的坐标含义改变", 409,
                    need_confirm=True, affects=counts)
    try:
        text = store.read(n, "yaml").decode("utf-8", "replace")
        y = mapserver.parse_yaml_flat(text)
        changes = []
        for k, v in patch.items():
            old = y.get(k)
            if k == "origin":
                v = [float(x) for x in v]
            y[k] = v
            changes.append(f"{k}: {old} → {v}")
        new_text = _rewrite_yaml_fields(text, patch)
        # 改元数据 = 覆盖语义 → 强制备份
        backup = store.backup(n)
        store.write(n, "yaml", new_text.encode("utf-8"))
        mapserver.clear_cache()
        locator.clear_current_map_cache()
    except mapstore.MapStoreError as e:
        return _err(str(e), 500)
    # §7.2 第 3 条：把"已确认改元数据"这件事也记录进 tags.json（用**新**的指纹覆写）
    tags_updated = False
    if affects:
        try:
            got = maptags.resolve(n, store)
            if got["ok"] and got["exists"]:
                tags = got["tags"]
                tags["resolution"] = float(y.get("resolution")) if y.get("resolution") else None
                org = y.get("origin")
                if isinstance(org, list) and len(org) >= 2:
                    tags["origin"] = [float(org[0]), float(org[1]),
                                      float(org[2]) if len(org) > 2 else 0.0]
                maptags.save(n, tags, store, action="meta_fingerprint_confirm",
                             changes="; ".join(changes))
                tags_updated = True
        except mapstore.MapStoreError as e:
            return _err(f"元数据已改，但刷新 tags.json 指纹失败：{e}", 500)
    maptags.sync_map(n, store, force=True)
    audit.log("map_change", action="meta_update", map=n, changes=changes,
              backup=",".join(backup), affected=affects, tags_updated=tags_updated)
    return {"ok": True, "changed": changes, "backup": backup, "affected": affects,
            "tags_updated": tags_updated, "meta": mapserver.map_info(n, store)}


def _rewrite_yaml_fields(text: str, patch: dict) -> str:
    """在 yaml 原文上逐字段替换（保留注释、缩进与键顺序）。缺字段则追加。"""
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if any(ln.endswith("\r\n") for ln in lines) else "\n"
    if not lines:
        lines = []
    done = set()
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key = stripped.split(":", 1)[0].strip()
        if key not in patch:
            continue
        val = patch[key]
        if isinstance(val, list):
            val = "[" + ", ".join(str(v) for v in val) + "]"
        indent = ln[: len(ln) - len(ln.lstrip())]
        tail = newline if ln.endswith(("\n", "\r")) else ""
        lines[i] = f"{indent}{key}: {val}{tail}"
        done.add(key)
    for key, val in patch.items():
        if key in done:
            continue
        if isinstance(val, list):
            val = "[" + ", ".join(str(v) for v in val) + "]"
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] = lines[-1] + newline
        lines.append(f"{key}: {val}{newline}")
    return "".join(lines)


@app.post("/api/map/{name}/rename")
def map_rename(name: str, body: MapNameIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """重命名成对文件（.pgm/.yaml/.tags.json）并同步改 yaml 的 image: 与 tags 的 map:。"""
    from . import log as audit
    try:
        n, new = _name_of(name), mapstore.check_name(body.new_name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    if n == new:
        return _err("新名字与原图相同")
    if store.exists(new, "yaml") or store.exists(new, "pgm"):
        return _err(f"目标已存在：{new}", 409)
    try:
        store.rename(n, new)
        # yaml 里的 image: 必须跟着走，否则 map_server 找不到 pgm
        try:
            text = store.read(new, "yaml").decode("utf-8", "replace")
            store.write(new, "yaml", mapserver.update_yaml_image(text, f"{new}.pgm").encode("utf-8"))
        except mapstore.MapStoreError:
            pass
        # tags.json 的 map 字段与文件名对齐（指纹不变）
        if store.exists(new, "tags"):
            got = maptags.resolve(new, store)
            if got["ok"] and got["exists"]:
                tags = got["tags"]
                tags["map"] = new
                tags["updated_at"] = db.now_iso()
                import json as _json
                store.write(new, "tags",
                         _json.dumps(tags, ensure_ascii=False, indent=2).encode("utf-8"))
        mapserver.clear_cache()
        locator.clear_current_map_cache()
        db.drop_map_tags(n)
        maptags.sync_map(new, store, force=True)
        if db.get_settings().get("current_map") == n:
            db.set_settings({"current_map": new})
    except mapstore.MapStoreError as e:
        return _err(str(e), 500)
    audit.log("map_change", action="rename", old=n, new=new)
    return {"ok": True, "old": n, "new": new}


@app.post("/api/map/{name}/copy")
def map_copy(name: str, body: MapNameIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """复制成对文件 → **标记随行**（指纹一致才复制，规格 §B5.2 第 6 步 / §B九 坑 13）。"""
    from . import log as audit
    try:
        n, new = _name_of(name), mapstore.check_name(body.new_name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    if n == new:
        return _err("新名字与原图相同")
    warnings: list[str] = []
    try:
        got = maptags.resolve(n, store)
    except mapstore.MapStoreError as e:
        return _err(f"读取原图标记失败：{e}", 500)
    try:
        store.copy(n, new)                 # 会连带复制 tags.json（若存在）
    except mapstore.MapStoreError as e:
        # 目标已存在 / 源不存在 → 都是"用户可纠正"的冲突，给 409 而不是 500
        code = 409 if "已存在" in str(e) else 404
        return _err(str(e), code)
    try:
        try:
            text = store.read(new, "yaml").decode("utf-8", "replace")
            store.write(new, "yaml", mapserver.update_yaml_image(text, f"{new}.pgm").encode("utf-8"))
        except mapstore.MapStoreError:
            pass
        # 指纹比对：一致则保留随行的标记，不一致就删掉新图的 tags（绝不静默错配）
        copied_tags = False
        if got["ok"] and got["exists"] and store.exists(new, "tags"):
            try:
                info = mapserver.map_info(new, store)
                fp = maptags.fingerprint_check(got["tags"], info)
                if fp.get("changed"):
                    warnings.append(f"该图元数据与标记指纹不一致（{'; '.join(fp.get('reasons') or [])}），"
                                    f"标记未随行")
                    store.remove(new, "tags")
                else:
                    tags = got["tags"]
                    tags["map"] = new
                    tags["updated_at"] = db.now_iso()
                    import json as _json
                    store.write(new, "tags",
                             _json.dumps(tags, ensure_ascii=False, indent=2).encode("utf-8"))
                    copied_tags = True
            except mapstore.MapStoreError as e:
                warnings.append(f"标记随行失败：{e}")
        mapserver.clear_cache()
        locator.clear_current_map_cache()
        maptags.sync_map(new, store, force=True)
    except mapstore.MapStoreError as e:
        return _err(str(e), 500)
    if not copied_tags:
        db.drop_map_tags(new)
    audit.log("map_change", action="copy", src=n, dst=new, tags_copied=copied_tags,
              warnings="; ".join(warnings))
    return {"ok": True, "src": n, "dst": new, "tags_copied": copied_tags, "warnings": warnings}


@app.delete("/api/map/{name}")
def map_delete(name: str, confirm: bool = Query(False), source: str = Query("", alias="source"), store=Depends(_store)):
    """删除成对文件（含 .tags.json）。本图有标记时必须 ``confirm=true``（规格 §5.1）。"""
    from . import log as audit
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    counts = db.count_map_tags(n)
    affects = counts["destinations"] + counts["zones"]
    if affects and not confirm:
        return _err(f"本图有 {affects} 个标记（{counts['destinations']} 地点 / {counts['zones']} 区域），"
                    f"删除会一并移除", 409, need_confirm=True, affects=counts)
    removed = []
    try:
        for ext in _MAP_EXTS:
            if store.exists(n, ext):
                store.remove(n, ext)
                removed.append(ext)
    except mapstore.MapStoreError as e:
        return _err(str(e), 500)
    if not removed:
        return _err(f"地图不存在：{n}", 404)
    mapserver.clear_cache()
    locator.clear_current_map_cache()
    db.drop_map_tags(n)
    audit.log("map_change", action="delete", map=n, removed=",".join(removed), affected=affects)
    return {"ok": True, "removed": removed, "affected": affects}


@app.get("/api/map/{name}/download")
def map_download(name: str, file: str = Query("yaml"), source: str = Query("", alias="source"), store=Depends(_store)):
    """下载原始文件；``file`` **只接受 yaml/pgm/tags**（其他值报错，避免变成任意文件读取）。"""
    if file not in _MAP_EXTS:
        return _err(f"file 只接受 {'/'.join(_MAP_EXTS)}，收到 {file!r}")
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    try:
        data, stale, at = store.read_with_meta(n, file)
    except mapstore.MapStoreError as e:
        return _err(str(e), 404)
    media = {"yaml": "text/yaml; charset=utf-8", "pgm": "image/x-portable-graymap",
             "tags": "application/json; charset=utf-8"}[file]
    fn = {"yaml": f"{n}.yaml", "pgm": f"{n}.pgm", "tags": f"{n}.tags.json"}[file]
    headers = {"Content-Disposition": f'attachment; filename="{fn}"'}
    if stale:
        # 断连时回落到本地缓存 → 前端显示「当前离线，显示缓存（时间）」（规格 §B4.2）
        headers["X-Map-Stale"] = "1"
        if at:
            headers["X-Map-Cached-At"] = str(at)
    return Response(content=data, media_type=media, headers=headers)


@app.get("/api/map/{name}/image.png")
async def map_image(name: str, source: str = Query("", alias="source"), store=Depends(_store)):
    """**后端把 PGM 转成灰度 PNG**（前端按阈值着色）；按 mtime+size 缓存（规格 §7.4）。"""
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    try:
        png, stale, at = await asyncio.to_thread(mapserver.image_png, n, store)
    except mapstore.MapStoreError as e:
        return _err(str(e), 404)
    headers = {"Cache-Control": "no-cache"}
    if stale:
        headers["X-Map-Stale"] = "1"
        if at:
            headers["X-Map-Cached-At"] = str(at)
    return Response(content=png, media_type="image/png", headers=headers)


# ---------------------------------------------------------------- 地点与区域（§5.2）
@app.get("/api/destinations")
async def destinations_list(map: str = Query("", alias="map"), source: str = Query("", alias="source"), store=Depends(_store)):
    if not map:
        return _err("缺少 map 参数")
    try:
        n = _name_of(map)
        rows = await asyncio.to_thread(maptags.get_destinations, n, store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    return {"ok": True, "map": n, "destinations": rows}


@app.post("/api/destinations")
def destinations_add(d: DestinationIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """新增地点：服务端完整校验（名称唯一、坐标在地图内、障碍检查）并返回 ``warnings[]``。"""
    from . import log as audit
    try:
        n = _name_of(d.map_name)
        out = maptags.upsert_destination(n, d.model_dump(), store=store)
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", action="destination_add", map=d.map_name, error=str(e))
        return _err(str(e))
    return {"ok": True, "uid": out["uid"], "warnings": out["warnings"], "save": out["save"]}


@app.post("/api/destinations/validate")
def destinations_validate(v: ValidateIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """标点即校验（§7.6）：越界 / 障碍 / 未知 / 距障碍余量 —— **只警告不阻止**。

    ⚠️ 本路由必须**定义在** ``POST /api/destinations/{uid}`` **之前**：否则 FastAPI 会先匹配
    到 ``{uid}``，把 "validate" 当成一个地点 uid（实测就是 "地点不存在：validate"）。
    """
    try:
        n = _name_of(v.map_name)
        out = mapserver.validate_point(n, v.x, v.y, store, v.margin_m)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    return out


@app.post("/api/destinations/{uid}")
def destinations_update(uid: str, d: DestinationIn, source: str = Query("", alias="source"), store=Depends(_store)):
    from . import log as audit
    try:
        n = _name_of(d.map_name)
        out = maptags.upsert_destination(n, d.model_dump(), uid=uid, store=store)
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", action="destination_update", map=d.map_name,
                  uid=uid, error=str(e))
        return _err(str(e))
    return {"ok": True, "uid": out["uid"], "warnings": out["warnings"], "save": out["save"]}


@app.delete("/api/destinations/{uid}")
def destinations_delete(uid: str, map: str = Query("", alias="map"), source: str = Query("", alias="source"), store=Depends(_store)):
    from . import log as audit
    if not map:
        return _err("缺少 map 参数")
    try:
        n = _name_of(map)
        maptags.delete_destination(n, uid, store=store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    audit.log("map_change", action="destination_delete", map=n, uid=uid)
    return {"ok": True, "uid": uid}


@app.post("/api/destinations/learn")
def destinations_learn(body: LearnIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """取**当前位姿**写入该图 tags.json（位姿不可用 → 明确失败并提示"可改为在图上点选"）。"""
    from . import log as audit
    try:
        n = _name_of(body.map_name)
        pose = locator.get_pose()
        out = maptags.learn_here(n, body.model_dump(), pose, store=store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    audit.log("map_change", action="destination_learn", map=n, uid=out["uid"],
              pose=out.get("pose"))
    return {"ok": True, "uid": out["uid"], "warnings": out["warnings"], "pose": out.get("pose"),
            "save": out["save"]}


@app.get("/api/zones")
async def zones_list(map: str = Query("", alias="map"), source: str = Query("", alias="source"), store=Depends(_store)):
    if not map:
        return _err("缺少 map 参数")
    try:
        n = _name_of(map)
        rows = await asyncio.to_thread(maptags.get_zones, n, store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    return {"ok": True, "map": n, "zones": rows}


@app.post("/api/zones")
def zones_add(z: ZoneIn, source: str = Query("", alias="source"), store=Depends(_store)):
    from . import log as audit
    try:
        n = _name_of(z.map_name)
        out = maptags.upsert_zone(n, z.model_dump(), store=store)
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", action="zone_add", map=z.map_name, error=str(e))
        return _err(str(e))
    return {"ok": True, "uid": out["uid"]}


@app.post("/api/zones/{uid}")
def zones_update(uid: str, z: ZoneIn, source: str = Query("", alias="source"), store=Depends(_store)):
    try:
        n = _name_of(z.map_name)
        out = maptags.upsert_zone(n, z.model_dump(), uid=uid, store=store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    return {"ok": True, "uid": out["uid"]}


@app.delete("/api/zones/{uid}")
def zones_delete(uid: str, map: str = Query("", alias="map"), source: str = Query("", alias="source"), store=Depends(_store)):
    from . import log as audit
    if not map:
        return _err("缺少 map 参数")
    try:
        n = _name_of(map)
        out = maptags.delete_zone(n, uid, store=store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    audit.log("map_change", action="zone_delete", map=n, uid=uid, orphaned=out.get("orphaned"))
    return {"ok": True, "uid": uid, "orphaned": out.get("orphaned") or []}


@app.get("/api/map/{name}/tags")
def map_tags_get(name: str, source: str = Query("", alias="source"), store=Depends(_store)):
    """**直读该图 .tags.json 原文**（调试 / 迁移 / 人工核对用）。"""
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    got = maptags.resolve(n, store)
    return {"ok": got["ok"], "map": n, "exists": got["exists"], "tags": got["tags"],
            "warnings": got.get("warnings") or [], "stale": got.get("stale"),
            "cached_at": got.get("cached_at"), "error": got.get("error") or "",
            "fingerprint": got.get("fingerprint"), "path": maptags.tags_path(n, store)}


@app.put("/api/map/{name}/tags")
def map_tags_put(name: str, body: MapTagsIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """整份替换标记文件（高级用途，做 schema 校验）；写文件 + sync_map()。"""
    try:
        n = _name_of(name)
        out = maptags.replace_all(n, body.model_dump(), store=store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    return out


@app.post("/api/map/{name}/tags/reindex")
def map_tags_reindex(name: str, source: str = Query("", alias="source"), store=Depends(_store)):
    """强制重建该图的索引缓存（缓存丢失或怀疑不一致时用）。"""
    try:
        n = _name_of(name)
        out = maptags.reindex(n, store=store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    return {"ok": True, **out}


@app.post("/api/map/reindex-all/tags")
def map_tags_reindex_all(source: str = Query("", alias="source"), store=Depends(_store)):
    """重建**所有**图的索引缓存（缓存整个丢了时的恢复入口，验收红线 3 用）。

    路径特意避开 ``/api/map/{name}/...`` 前缀，免得和 ``{name}`` 参数路由抢匹配。
    """
    from . import log as audit
    try:
        out = maptags.reindex_all(store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    audit.log("map_change", action="tags_reindex_all", maps=len(out))
    return {"ok": True, "results": out}


# ---------------------------------------------------------------- 位姿 / 当前地图 / IO 状态
@app.get("/api/robot/pose")
async def robot_pose():
    """位姿（只读，rosbridge）。降级时 ``{"ok": True, "status": "unavailable"}``。"""
    return await asyncio.to_thread(lambda: locator.pose_payload(db.get_settings()))


@app.get("/api/mapeditor/status")
async def mapeditor_status(source: str = Query("", alias="source")):
    """编辑器顶部状态条的一份汇总：位姿 + rosbridge + 当前地图 + IO 模式。"""
    store = _store(source)
    io, loc = await asyncio.gather(
        asyncio.to_thread(mapstore.io_status, "", source),
        asyncio.to_thread(locator.status, store),
    )
    return {"ok": True, "io": io, "locator": loc}


@app.get("/api/mapeditor/io")
async def mapeditor_io(name: str = Query(""), source: str = Query("", alias="source")):
    return {"ok": True, **(await asyncio.to_thread(mapstore.io_status, name, source))}


@app.post("/api/mapeditor/io/test")
async def mapeditor_io_test(source: str = Query("", alias="source")):
    """主动连通性自检（``list()`` 一次），返回耗时与错误原因；**不改任何文件**。"""
    return await asyncio.to_thread(mapstore.io_test, source)


@app.post("/api/mapeditor/pose/inject")
def mapeditor_pose_inject(body: PoseInjectIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """注入假位姿 / 假地图元数据（**无 ROS 环境开发与测试用**，规格 §5.4 / §nine 4）。"""
    if body.x is None and body.width is None:
        locator.clear_injection()
        return {"ok": True, "cleared": True}
    if body.x is not None:
        locator.set_pose_for_test(body.x, body.y, body.yaw)
    if body.width is not None:
        locator.set_map_for_test(body.width, body.height, body.resolution, body.origin)
    return {"ok": True, "pose": locator.pose_payload(), "current_map": locator.current_map(store)}


# ---------------------------------------------------------------- 像素修图保存（B 篇 §B5.2）
@app.post("/api/map/{name}/save")
def map_save(name: str, body: MapSaveIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """保存像素改动：白名单 → 体积 → yaml 白名单校验 → 备份 → 标记随行 → 原子写 → 审计。"""
    from . import log as audit
    import base64
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", map=name, error=str(e), stage="name")
        return _err(str(e))
    store = _store(source)

    # 2) 体积校验
    try:
        pgm = base64.b64decode(body.pgm_b64 or "", validate=False)
    except Exception as e:      # noqa: BLE001
        audit.log("map_edit_reject", map=n, error=f"pgm base64 解码失败：{e}", stage="decode")
        return _err(f"pgm base64 解码失败：{e}")
    if not pgm:
        return _err("pgm_b64 为空（编辑器没有回传像素数据）")
    if len(pgm) > conf.MAPS_MAX_PGM_BYTES:
        return _err(f"pgm 过大：{len(pgm)} B > {conf.MAPS_MAX_PGM_BYTES} B", 413)
    yaml_in = body.yaml_text or ""
    if len(yaml_in.encode("utf-8")) > conf.MAPS_MAX_YAML_BYTES:
        return _err(f"yaml 过大：>{conf.MAPS_MAX_YAML_BYTES} B", 413)
    if pgm[:2] not in (b"P5", b"P2"):
        return _err(f"pgm 魔数不被接受：{pgm[:2]!r}（只认 P2/P5，与上游 parsePGM 一致）")

    # 1) 白名单校验（红线 §B7.1）—— 必须**最早**做：它是 ssh 子进程模式唯一的一道命令注入防线，
    #    也是唯一一处"连试都不该试"的输入。放在体积校验之前，避免"超大 body + 非法名字"绕过。
    if body.mode not in ("saveas", "overwrite"):
        return _err(f"mode 只接受 saveas/overwrite，收到 {body.mode!r}")
    try:
        target = (mapstore.check_name(body.new_name or f"{n}_edited")
                  if body.mode == "saveas" else mapstore.check_name(n))
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", map=n, error=str(e), stage="name")
        return _err(str(e))
    if body.mode == "overwrite" and not body.confirm:
        return _err("覆盖原图必须带 confirm=true", 409, need_confirm=True)

    # 3) yaml 白名单校验：除 image 外任何字段与磁盘原值不一致 → 409
    try:
        disk_yaml = store.read(n, "yaml").decode("utf-8", "replace")
    except mapstore.MapStoreError as e:
        return _err(f"读磁盘原 yaml 失败：{e}", 404)
    from_disk = mapserver.parse_yaml_flat(disk_yaml)
    from_up = mapserver.parse_yaml_flat(yaml_in) if yaml_in.strip() else {}
    diffs = []
    for k, v in from_up.items():
        if k == "image":
            continue
        if k not in from_disk:
            diffs.append(f"{k}（磁盘上没有该字段，回传值 {v!r}）")
        elif _same_value(from_disk[k], v) is False:
            diffs.append(f"{k}（磁盘 {from_disk[k]!r} vs 回传 {v!r}）")
    if diffs:
        audit.log("map_edit_reject", map=n, mode=body.mode, diffs="; ".join(diffs), stage="yaml")
        return _err("回传 yaml 与磁盘原值不一致（只允许改 image 字段）：" + "; ".join(diffs), 409,
                    diffs=diffs)

    # 5) 备份（覆盖：强制；另存：目标已存在时也备份）
    backup: list[str] = []
    warnings: list[str] = []
    try:
        if body.mode == "overwrite":
            backup = store.backup(target)
        elif store.exists(target, "yaml") or store.exists(target, "pgm"):
            backup = store.backup(target)
    except mapstore.MapStoreError as e:
        if body.mode == "overwrite":
            # 备份是覆盖的前置条件，不允许"备份失败但继续"（规格 §B八）
            audit.log("map_edit_reject", map=n, error=str(e), stage="backup")
            return _err(f"备份失败，已拒绝覆盖保存：{e}", 500)
        warnings.append(f"目标已存在但备份失败：{e}")

    # 6) 标记随行
    tags_copied = False
    if body.mode == "overwrite":
        try:
            got = maptags.resolve(n, store)
            if got["ok"] and got["exists"]:
                got["tags"]["updated_at"] = db.now_iso()
                import json as _json
                store.write(n, "tags",
                         _json.dumps(got["tags"], ensure_ascii=False, indent=2).encode("utf-8"))
                tags_copied = True
        except mapstore.MapStoreError as e:
            warnings.append(f"标记指纹更新失败：{e}")
    else:
        try:
            got = maptags.resolve(n, store)
            if got["ok"] and got["exists"]:
                # 用**本次要写出的** pgm 元数据与原名指纹比对（像素改不了 resolution/origin）
                info = mapserver.map_info(n, store)
                fp = maptags.fingerprint_check(got["tags"], info)
                if fp.get("changed"):
                    warnings.append(f"该图元数据已变（{'; '.join(fp.get('reasons') or [])}），标记未随行")
                else:
                    tags = got["tags"]
                    tags["map"] = target
                    tags["updated_at"] = db.now_iso()
                    import json as _json
                    store.write(target, "tags",
                             _json.dumps(tags, ensure_ascii=False, indent=2).encode("utf-8"))
                    tags_copied = True
        except mapstore.MapStoreError as e:
            warnings.append(f"标记随行失败：{e}")

    # 7) 写 pgm（原子）
    try:
        store.write(target, "pgm", pgm)
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", map=n, target=target, error=str(e), stage="pgm")
        return _err(f"写 pgm 失败：{e}", 500)

    # 8) 写 yaml：以磁盘原文为本，只替换 image: 一行
    try:
        out_yaml = mapserver.update_yaml_image(disk_yaml, f"{target}.pgm")
        store.write(target, "yaml", out_yaml.encode("utf-8"))
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", map=n, target=target, error=str(e), stage="yaml_write")
        return _err(f"写 yaml 失败：{e}", 500)

    mapserver.clear_cache()
    locator.clear_current_map_cache()
    if body.mode == "overwrite":
        try:
            maptags.sync_map(target, store, force=True)
        except mapstore.MapStoreError as e:
            warnings.append(f"刷新索引缓存失败：{e}")
    else:
        try:
            maptags.sync_map(target, store, force=True)
        except mapstore.MapStoreError:
            pass

    wrote = [f"{target}.pgm", f"{target}.yaml"]
    if tags_copied:
        wrote.append(f"{target}.tags.json")
    audit.log("map_edit_save", map=n, target=target, mode=body.mode,
              pgm_bytes=len(pgm), backup=",".join(backup), tags_copied=tags_copied,
              warnings="; ".join(warnings))
    return {"ok": True, "wrote": wrote, "backup": backup, "target": target,
            "tags_copied": tags_copied, "warnings": warnings,
            "restart_hint": f"~/tools/nav_screen.sh nav {target}",
            "note": "map_server 启动时一次性读入地图，改完必须重启导航才生效"}


def _same_value(a, b) -> bool:
    """yaml 字段值比较：数字容忍 int/float 差异，其余按字符串比。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same_value(x, y) for x, y in zip(a, b))
    return str(a) == str(b)


# ------------------------------------------------------------------ 摄像头 HTTP 桥
# 摄像头共享服务（vision/camera_server.py）本身是裸 TCP，浏览器说不了那套协议；
# 这里桥成 HTTP，**让上位机 PC 浏览器直接看板卡画面**（不必登录板卡/到现场）。
# 摄像头没起时全部降级：查询类返回 status=unavailable 且 ok=True，写操作 ok=False。
@app.get("/api/vision/status")
async def vision_status():
    """摄像头共享服务状态（含通道/帧计数/模式）。"""
    from vision import webbridge
    return webbridge.status()


@app.get("/api/vision/snapshot")
async def vision_snapshot(channel: int = Query(1), quality: int = Query(80),
                          token: str = Query(None)):
    """单帧 JPEG 快照。`<img src="/api/vision/snapshot?channel=1">` 可直接显示。"""
    from . import log as audit
    from fastapi.responses import JSONResponse
    from vision import webbridge
    try:
        jpg, _w, _h, source = await asyncio.to_thread(
            webbridge.get_jpeg, channel, None, None, quality)
    except Exception as e:  # noqa: BLE001  （连不上/无帧都按 503 语义返回 JSON）
        audit.log("vision_snapshot_failed", channel=channel, error=str(e))
        return JSONResponse({"ok": False, "error": f"取帧失败：{e}"},
                            status_code=503)
    return Response(content=jpg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store",
                             "X-Vision-Source": source})


@app.get("/api/vision/stream")
async def vision_stream(channel: int = Query(1), fps: int = Query(10),
                        quality: int = Query(80)):
    """MJPEG 连续流：`<img src="/api/vision/stream">` 即可看到动态画面。

    摄像头不可用时**直接结束流**（返回空 body），由前端显示占位，避免无限空转。
    """
    from vision import webbridge
    gen = webbridge.mjpeg_stream(channel=channel, fps=fps, quality=quality)
    return StreamingResponse(
        (chunk for chunk in gen),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------- 前端静态托管
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

_FRONTEND_DIST = BASE_DIR / "frontend" / "packages"
_KIOSK_DIST = _FRONTEND_DIST / "kiosk" / "dist"
_ADMIN_DIST = _FRONTEND_DIST / "admin" / "dist"
_MAPEDITOR_DIST = _FRONTEND_DIST / "mapeditor" / "dist"
_MAPEDITOR_PUBLIC = _FRONTEND_DIST / "mapeditor" / "public"


def _serve_dist(dist: Path, path: str):
    """挂载单个端构建产物；SPA 回退到 index.html（无 router，实际用不到回退，防御性）。"""
    app.mount(path, StaticFiles(directory=str(dist), html=True), name=path.strip("/"))


if _KIOSK_DIST.exists():
    _serve_dist(_KIOSK_DIST, "/kiosk")
if _ADMIN_DIST.exists():
    _serve_dist(_ADMIN_DIST, "/admin")
def _mount_editor():
    """挂 `/mapeditor`：判据是**入口页 index.html 存在**，不是目录存在。

    为什么这么判：`frontend/packages/mapeditor/dist/` 会被 dev 构建留下来，但如果它里面没有
    `index.html`（例如只把 `public/` 拷过去、或 dist 只装了 `pixel-editor.html`），
    直接挂 dist 会让 `/mapeditor/` 变 404 —— 此时必须回退到 `public/`。
    两个候选都没有 `index.html` 时，退一步挂 `public/`（至少 `pixel-editor.html` 能打开），
    并在启动日志里说清楚「前端未构建」，别让人对着 404 猜。
    """
    for cand, label in ((_MAPEDITOR_DIST, "dist（构建产物）"), (_MAPEDITOR_PUBLIC, "public（未构建回退）")):
        if (cand / "index.html").exists():
            _serve_dist(cand, "/mapeditor")
            print(f"[mapeditor] 挂载 {label}：{cand}")
            return
    if _MAPEDITOR_PUBLIC.exists():
        _serve_dist(_MAPEDITOR_PUBLIC, "/mapeditor")
        print(f"[WARN] [mapeditor] 没有 index.html —— 只挂了 public/ 使 pixel-editor.html 可用："
              f"{_MAPEDITOR_PUBLIC}\n"
              f"        要打开地图编辑器主界面请先构建：cd frontend && pnpm --filter mapeditor build")


_mount_editor()


@app.get("/")
async def root():
    """默认入口：有 admin 产物则给 admin，否则提示构建。"""
    if _ADMIN_DIST.exists():
        return FileResponse(str(_ADMIN_DIST / "index.html"))
    return {"ok": True, "message": "前端未构建。运行 scripts/build_frontend.ps1 生成产物。"}
