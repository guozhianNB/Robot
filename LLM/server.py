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
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from openai import OpenAI
from pydantic import BaseModel

from .store import db
from .core import bus
from .agent import chat, memory as rag, reminder, tools as tool_mod
from .agent import notify          # 通知中心（护士台数据底座，模块 11）
from .voice import voice_api
from .agent import mcp_client   # MCP 桥（可选能力，内部降级，import 永远安全）
from .agent import session      # 分层用户体系：会话层（角色/主体/当前病房）——业务接口的角色唯一来源
from .maps import locator, maptags   # 病房位置自动切换 / 记录病房区域要用（编辑器路由已搬走）
from .maps import mapctl         # 地图编辑器服务（独立进程）的启停管理
from .core import log as audit  # 审计：本文件的登录冷却/病房变更在多处写审计，改顶层导入
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
    from .core import log as audit
    db.init_db()

    # 记忆 v3 迁移（幂等）+ 依赖自检
    try:
        from .store import migrate
        migrate.run()
    except Exception as e:
        audit.log("memory_change", action="migrate_error", error=str(e))

    from .store import embed as embed_mod, ragstore, graph
    audit.log("memory_degraded", embed=embed_mod.status(),
              ragstore=ragstore.status(), graph=graph.status())

    _seed_demo()
    try:
        notify.prune()            # 通知中心：启动清一次过期已处理通知（失败不得阻断启动）
    except Exception as e:
        audit.log("notify_prune_error", error=str(e))
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
    mapctl.stop()             # 编辑器服务是本进程拉起的：主后端退出不该留孤儿
    mcp_client.stop()
    voice_api.stop_voice()
    drain_task.cancel()
    tick_task.cancel()


app = FastAPI(title="AI 陪护机器人后端", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.middleware("http")
async def _no_store_entry_html(request: Request, call_next):
    """入口 HTML（/、/admin/、/kiosk/ 的 index.html）禁止缓存。

    为什么必须这样：index.html 里的 `<script src="assets/index-<hash>.js">` 是**唯一**指向
    当前构建的指针。浏览器若缓存了旧 index.html，用户按 Ctrl+F5 也只是"刷新页面"，
    加载的仍是旧 JS —— 表现就是"代码明明改好/重启了，界面还是老样子、参数还是老参数"
    （2026-09-18 排查思考档位时踩过：新代码在跑，页面却还在用 9 天前的 bundle）。
    带 hash 的 assets 可以放心长期缓存，所以只对 html 入口加 no-store。"""
    resp = await call_next(request)
    path = request.url.path.rstrip("/") or "/"
    if path in ("", "/", "/admin", "/kiosk") or path.endswith("/index.html"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp


app.include_router(mapctl.router)   # /api/mapeditor/service{,/start,/stop}（仅管理员）


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
    from .core import log as audit
    audit.log("memory_change", action="seed", uid="elder_001", note="示例数据")


def _sse(event: dict) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def _post_chat_jobs(uid: str, user_text: str, assistant: str, role: str | None = None):
    """对话结束后的后台任务（线程池，不阻塞请求）：
    1. 本轮对话记入记忆整理缓冲，并安排"空闲 30s → 话题结束 → 批量整理记忆"定时器
    2. 上下文窗口已满时立即整理（不等到空闲）
    3. 滚动窗口历史摘要

    `role` 决定本管线是否对**老人的私有记忆**生效：collective 层（role="ward"）的话**不沉淀成
    任何老人的记忆**（规格 §5.3）。`role=None`（语音等老调用点只传 3 个参数）时**从会话层
    现取** —— 会话层是角色的唯一权威（R1）。语音是集体层的主入口，漏了这一步就会把病房公开
    对话当成老人的话沉淀下去。`/api/chat` 路由显式传 `principal["role"]`。

    **非 elder/admin 一律整条管线早退**（与 `rag.note_turn` 的守卫同口径、fail-closed）：
    只早退 `note_turn` 不够 —— 同管线里的 `rag.correct_instant()` **无条件执行**，病房里的
    "不对/错了"这类话会覆盖那位老人的**核心记忆**；`summarize_old()`/`consolidate()` 同属
    "以某位老人为数据主体"的写操作。集体层（及任何未知取值）只作集体上下文：不沉淀、
    不纠错、不写摘要（规格 §5.3 / R5）。"""
    if role is None:
        # 语音等老调用点没传角色：按当前主体现取（会话层是角色的唯一权威，R1）
        # 取不到（`database is locked` 等读库异常）**不许抛出**：本函数跑在线程池里，
        # 异常会被 future 吞成静默失败 → 整条 post-chat 管线（记忆沉淀/摘要）一起丢。
        # fail-closed：取不到就按最保守的集体层（ward）处理——宁可不沉淀。
        try:
            from .agent import session as role_session
            role = role_session.get_principal("kiosk")["role"]
        except Exception as e:
            from .core import log as audit
            audit.log("memory_change", action="role_lookup_failed", uid=uid, error=str(e))
            role = "ward"
    if str(role or "").strip().lower() not in ("elder", "admin"):
        # 集体层（及任何未知取值）只作集体上下文：既不沉淀、也不纠错、也不写摘要（规格 §5.3/R5）。
        # 归一化后 fail-closed，与 `memory.note_turn` 的守卫同口径。
        return
    settings = db.get_settings()
    try:
        rag.note_turn(uid, user_text, assistant, client, MODEL, settings, role=role)
    except Exception as e:
        from .core import log as audit
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


class WardSwitchIn(BaseModel):
    ward_uid: str


class AlarmIn(BaseModel):
    type: str = "sos"          # sos / fall / health / no_activity ...
    uid: str = ""
    message: str = ""


class NoticeIn(BaseModel):
    """通知投递体（`POST /api/notifications`）：`type` 必填，其余可省。

    `type` 故意给空串默认值而不是 `str` 必填：Pydantic 必填缺失会抛 422，而本接口的契约
    是**类型问题一律 400**（与旁边 `/api/alarm` 的宽松形状同族，投递方是机器，400 更好排查）。
    """
    type: str = ""             # 空/缺失 → 路由 400
    source: str = ""
    level: str = ""            # 空/非法 → notify.ingest 按类型兜底
    uid: str = ""
    title: str = ""
    message: str = ""          # → ingest(body=…)，与 /api/alarm 的字段名保持一族
    ref: str = ""


class AckIn(BaseModel):
    """确认体：`by` 可省（默认 admin）。"""
    by: str = ""


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
    from .store import embed as e, ragstore, graph as g
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
    from .core import log as audit
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
                # 只有 content 进 TTS：reasoning（思维链）**绝不允许**喂给 voice_api，
                # 否则思考过程会被播报出来（规格 2026-09-17-thinking-mode-switch-design.md D3）。
                if ev["type"] == "content":
                    voice_api.feed_text_reply(speech, ev.get("content") or "")
                elif ev["type"] == "done":
                    assistant = ev.get("assistant", "")
                    completed = True
                yield _sse(ev)
        finally:
            voice_api.end_text_reply(speech, flush_tail=completed)
            if completed and assistant.strip():
                # 角色随本轮的 principal 一起带给沉淀任务（集体层不沉淀，规格 §5.3）。
                # uid 也必须同源：客户端传来的 uid 不可信（R1），否则管理台/别人槽位的这一轮
                # 会被沉淀进**另一位老人**的历史与记忆（R5 的旁路）。
                # admin 槽的 principal["uid"] == "admin"（管理层自己的会话），不回落到 req.uid。
                _bg.submit(_post_chat_jobs, principal["uid"] or req.uid, req.message, assistant,
                           principal["role"])

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/chat/history")
async def chat_history(uid: str = Query(""), limit: int = Query(200),
                       x_surface: str = Header(default="kiosk")):
    """回读某人的历史对话。

    主体口径与 `/api/chat` 同源：**非管理员只能读自己**（传别人的 uid 一律 400）；管理员可以
    指定 uid（管理台要按老人查看），但要落审计。
    """
    principal = session.get_principal(_surface(x_surface))
    target = uid or principal["uid"]
    if principal["role"] != "admin":
        if uid and uid != principal["uid"]:
            raise HTTPException(status_code=400, detail="只能读取自己的会话历史")
        target = principal["uid"]
    else:
        audit.log("chat", action="history_read", uid=target, by="admin")
    history = db.load_history_full(uid=target, limit=limit) if target else []
    return {"ok": True, "history": history}


@app.delete("/api/chat/history")
async def chat_history_clear(uid: str = Query(""), x_surface: str = Header(default="kiosk")):
    """清空某人的对话历史（主体口径同 `GET`：非管理员只能删自己）。"""
    principal = session.get_principal(_surface(x_surface))
    target = uid or principal["uid"]
    if principal["role"] != "admin":
        if uid and uid != principal["uid"]:
            raise HTTPException(status_code=400, detail="只能清空自己的会话历史")
        target = principal["uid"]
    n = db.clear_history(target) if target else 0
    audit.log("chat", action="clear_history", uid=target, count=n, by=principal["role"])
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
    from .core import log as audit
    audit.log("memory_change", action="profile_upsert", uid=p.uid, name=p.name, by="nurse")
    return {"ok": True, "profile": prof}


# ---------------------------------------------------------------- 记忆
@app.get("/api/memories")
async def memories_list(uid: str = Query(""), status: str = Query("")):
    return {"ok": True, "memories": db.list_memories(uid=uid or None, status=status or None)}


@app.post("/api/memories")
async def memories_add(m: MemoryIn):
    mid = db.add_memory(m.uid, m.type, m.content, status=m.status, source="manual")
    from .core import log as audit
    audit.log("memory_change", action="manual_add", uid=m.uid, mid=mid, type=m.type,
              content=m.content, by="nurse")
    return {"ok": True, "id": mid}


@app.post("/api/memories/{mid}/confirm")
async def memories_confirm(mid: int):
    m = db.get_memory(mid)
    db.set_memory_status(mid, "confirmed")
    from .core import log as audit
    audit.log("memory_change", action="confirm", mid=mid, uid=(m or {}).get("uid", ""), by="nurse")
    return {"ok": True}


@app.post("/api/memories/{mid}/reject")
async def memories_reject(mid: int):
    m = db.get_memory(mid)
    op_id = db.delete_memory(mid, uid=(m or {}).get("uid", ""), reason="reject", by="nurse")
    from .core import log as audit
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
            from .store import ragstore as _rs
            new_cid = _rs.reindex_row(row["uid"], row["type"], row["content"],
                                      importance=row.get("importance", 0),
                                      source=row.get("source", ""), old_chroma_id=row["chroma_id"])
            if new_cid and new_cid != row["chroma_id"]:
                db.set_rag_chroma_id(op["target_id"], new_cid)
    from .core import log as audit
    audit.log("memory_change", action="restore", op_id=op_id, uid=op.get("uid", ""),
              table=table, by="nurse")
    return {"ok": True, "table": table, "op_id": op_id}


@app.post("/api/memories/recycle/purge")
async def recycle_purge(days: float = Query(30.0)):
    n = db.purge_soft_deleted(days=days)
    from .core import log as audit
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
    from .core import log as audit
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
        from .store import ragstore as _rs
        _rs.delete_by_chroma_id(row.get("uid", ""), row["chroma_id"])
    from .core import log as audit
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
    from .core import log as audit
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
    from .core import log as audit
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
    from .core import log as audit
    audit.log("memory_change", action="core_pin", mid=mid, uid=m.get("uid", ""), by="nurse")
    return {"ok": True}


@app.post("/api/memories/core/{mid}/unpin")
async def core_memories_unpin(mid: int):
    """解除保护。"""
    m = db.get_core_memory(mid)
    if not m:
        return {"ok": False, "error": "不存在"}
    db.set_core_pinned(mid, False)
    from .core import log as audit
    audit.log("memory_change", action="core_unpin", mid=mid, uid=m.get("uid", ""), by="nurse")
    return {"ok": True}


@app.get("/api/memories/graph")
async def graph_view(uid: str = Query("elder_001")):
    from .store import graph as g
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
    from .core import log as audit
    audit.log("memory_change", action="expression_approve", eid=eid, by="nurse")
    return {"ok": True}


@app.post("/api/memories/expressions/{eid}/reject")
async def expressions_reject(eid: int):
    """护士拒绝 → 软删（进回收站可恢复）。"""
    db.soft_delete_expression(eid)
    from .core import log as audit
    audit.log("memory_change", action="expression_reject", eid=eid, by="nurse")
    return {"ok": True}


@app.get("/api/memories/health")


@app.get("/api/memories/health")
async def memories_health():
    from .store import embed as e, ragstore, graph as g
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
    from .core import log as audit
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
async def settings_set(body: dict, x_surface: str = Header(default="kiosk")):
    """改设置。**特权键（口令门/TTL/MCP/病房自动切换）只有管理员能改**。

    否则一个未认证的 `POST /api/settings {"admin_auth_required": false}` 就能把口令门关掉、
    整套角色保护归零（`admin_auth_required` 是 `DEFAULT_SETTINGS` 白名单键，`db.set_settings`
    照单全收），随后 `POST /api/session/login {}` 即得 `source=auth_disabled` 的 admin。
    非特权键（`asr_provider`/`tts_provider`、音量亮度等）**保持现状**：kiosk 端要能改。
    """
    patch = (body.get("settings") or body) if isinstance(body, dict) else {}
    if not isinstance(patch, dict):
        patch = {}
    privileged = {"admin_auth_required", "admin_session_ttl_s",
                  "mcp_enabled", "ward_autoswitch_enabled"}
    hit = privileged & set(patch)
    if hit:
        slot = _surface(x_surface)                 # 顺带兜底建表 + 非法端槽位显式 400
        if session.get_principal(slot)["role"] != "admin":
            audit.log("policy_deny", action="settings_privileged", slot=slot,
                      keys=sorted(hit), decision="deny")
            raise HTTPException(status_code=403, detail="仅管理员可修改特权设置")
    cur = db.set_settings(patch)
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


@app.post("/api/session/ward")
async def session_set_ward(body: WardSwitchIn, x_surface: str = Header(default="kiosk")):
    """手动切当前病房（D18）：带 `manual_until`，这段时间内位置判定不覆盖。

    与"锁定主体"不同：这里只切**集体层背景变量**，正在老人私聊时不抢会话。
    广播与审计都在 `session.manual_set_ward()` 内部完成（`ward_changed` / action=manual），
    路由层**不再重复 publish**（否则前端会收到双事件）。
    """
    _surface(x_surface)                      # 本族的公共入口：兜底建表 + 非法端槽位显式 400
    if db.get_profile_kind(body.ward_uid) != "ward":
        raise HTTPException(status_code=400, detail=f"病房 uid 不存在：{body.ward_uid}")
    res = dict(session.manual_set_ward(body.ward_uid))
    res["ok"] = True                         # 与 `/api/session/user` 同款返回形状
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


@app.post("/api/session/password/restore-factory")
async def session_password_restore_factory(x_surface: str = Header(default="kiosk")):
    """恢复 `.env` 的 PASSWORD；只允许已登录管理员，成功后全部管理员槽位立即降权。"""
    if session.get_principal(_surface(x_surface))["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可恢复出厂口令")
    return session.restore_factory_password()


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


@app.delete("/api/wards/{ward_uid}")
async def ward_delete(ward_uid: str, x_surface: str = Header(default="kiosk")):
    """删除病房档案并解除老人归属；关联的地图区域保留。"""
    if session.get_principal(_surface(x_surface))["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可管理病房")
    if not db.delete_ward(ward_uid):
        return {"ok": False, "error": f"病房不存在：{ward_uid}"}
    session.forget_ward(ward_uid)
    audit.log("ward_change", source="admin", action="delete", ward=ward_uid)
    bus.publish("ward_changed", uid=ward_uid, action="delete")
    return {"ok": True, "uid": ward_uid}


@app.post("/api/wards/{ward_uid}/zone")
async def ward_set_zone(ward_uid: str, x_surface: str = Header(default="kiosk")):
    """便捷录入：以当前位姿为圆心、`ward_zone_default_r` 为半径采样 16 边形写入**该图的
    `<图名>.tags.json`**（`maptags.record_room_polygon`，已有实现、本次补 HTTP 入口），
    再把「地图名 + 区域 uid」回填进 profiles。精确形状请到 /mapeditor 画多边形。
    """
    if session.get_principal(_surface(x_surface))["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可管理病房")
    ward = db.get_profile(ward_uid) or {}
    if ward.get("kind") != "ward":
        return {"ok": False, "error": f"uid 不存在：{ward_uid}"}
    pose = locator.get_pose()
    if not pose or pose.get("x") is None:
        return {"ok": False, "error": "拿不到小车位姿（rosbridge/定位未就绪）；可到地图编辑器手绘区域"}
    map_name, why = session.running_map_name()
    if not map_name:
        return {"ok": False,
                "error": f"认不出当前地图（{why}）：请确认导航在跑且 /map 指纹能唯一命中，"
                         f"或把设置 ward_map_source 改成 setting 并选好 current_map"}
    name = ward.get("name") or ward_uid
    r = float(db.get_settings().get("ward_zone_default_r", 3.0))
    try:
        res = maptags.record_room_polygon(map_name, name, float(pose["x"]), float(pose["y"]),
                                          radius_m=r, kind="ward")
    except Exception as e:                 # noqa: BLE001  板卡不可达/名字非法等 → 只降级不 500
        return {"ok": False, "error": f"写地图标记失败：{e}"}
    n = db.set_ward_zone(ward_uid, map_name, res["uid"])
    if not n:                              # 0 行 = uid 不存在：别把静默 no-op 报成 ok
        return {"ok": False, "error": f"uid 不存在：{ward_uid}"}
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
    if not db.set_profile_ward(uid, body.ward_id):   # 0 行 = uid 不存在
        return {"ok": False, "error": f"uid 不存在：{uid}"}
    audit.log("ward_change", source="admin", action="assign", elder=uid, ward=body.ward_id)
    bus.publish("ward_changed", uid=body.ward_id, action="assign")
    return {"ok": True, "uid": uid, "ward_id": body.ward_id}


@app.get("/api/policy/roles")
async def policy_roles(x_surface: str = Header(default="kiosk")):
    """策略矩阵：管理员看全量，其它角色只拿自己那份摘要。"""
    from .agent.policy import POLICY_DEFAULTS
    role = session.get_principal(_surface(x_surface))["role"]
    if role != "admin":
        return {role: _public_policy(POLICY_DEFAULTS.get(role, POLICY_DEFAULTS["ward"]))}
    return {k: _public_policy(v) for k, v in POLICY_DEFAULTS.items()}


# ---------------------------------------------------------------- 通知中心（模块 11）
# 设计：**投递免鉴权**（需求文档模块 11："任何模块发现异常都往该端口 POST" —— 告警源可能
# 是小车/语音/巡检等无口令的一方），但**读/确认/删除一律只给管理员**（护士台）。
def _notice_payload(n: dict) -> dict:
    """列表一条：补 `uid_name`（「姓名 · 床号」，无档案/无 uid 则空串）。

    拼接口径与广播 payload 的 `uid_name` **共用 `notify.uid_name()`**（两处必须一致，
    否则护士台的列表显示与实时 toast 会不一样）。
    """
    n["uid_name"] = notify.uid_name(n.get("uid") or "")
    return n


def _notice_admin(x_surface: str) -> None:
    """通知的读/写管理口：非管理员一律 403（与同族 admin 路由同形）。"""
    if session.get_principal(_surface(x_surface))["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可管理通知")


@app.post("/api/notifications")
async def notice_ingest(n: NoticeIn):
    """投递通知（**有意免鉴权**，见上）。同 key 在窗口内自动合并。"""
    if not (n.type or "").strip():
        raise HTTPException(status_code=400, detail="type 不能为空")
    try:
        return notify.ingest(n.source, n.type, level=n.level, uid=n.uid,
                             title=n.title, body=n.message, ref=n.ref)
    except ValueError as e:                     # type 为空 → 400（不是 5xx，也不是静默吞掉）
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/notifications")
async def notices_list(x_surface: str = Header(default="kiosk"),
                       state: str = Query("all"), limit: int = Query(0),
                       before_id: int = Query(0)):
    _notice_admin(x_surface)
    items = await asyncio.to_thread(notify.list_notices, state, limit, before_id)
    return {"ok": True, "items": [_notice_payload(i) for i in items],
            "counts": await asyncio.to_thread(notify.counts)}


@app.post("/api/notifications/{nid}/ack")
async def notice_ack(nid: int, x_surface: str = Header(default="kiosk"),
                     body: AckIn | None = None):
    _notice_admin(x_surface)
    by = (body.by if body else "") or "admin"
    if not await asyncio.to_thread(notify.ack, nid, by):
        return {"ok": False, "error": "通知不存在"}
    return {"ok": True, "id": nid}


@app.post("/api/notifications/ack-all")
async def notice_ack_all(x_surface: str = Header(default="kiosk"),
                         body: AckIn | None = None):
    _notice_admin(x_surface)
    by = (body.by if body else "") or "admin"
    return {"ok": True, "acked": await asyncio.to_thread(notify.ack_all, by)}


@app.delete("/api/notifications/{nid}")
async def notice_delete(nid: int, x_surface: str = Header(default="kiosk")):
    _notice_admin(x_surface)
    if not await asyncio.to_thread(notify.remove, nid):
        return {"ok": False, "error": "通知不存在"}
    return {"ok": True}


# ---------------------------------------------------------------- 紧急呼叫
@app.post("/api/alarm")
async def alarm_report(a: AlarmIn):
    """紧急呼叫上报（规格 D6）：审计 + 广播；微信推送留给模块 11。"""
    from .core import log as audit
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
    from .core import log as audit
    audit.log("system", action="shutdown", by="nurse")
    try:
        mapctl.stop(hard=True)               # 编辑器服务（独立进程）一起带走；马上 os._exit，
                                             # 只留 1 秒，等不起 POSIX 上的 SIGTERM 优雅期（会留孤儿）
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
    from .core import log as audit
    from fastapi.responses import JSONResponse, Response
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


def _serve_dist(dist: Path, path: str):
    """挂载单个端构建产物；SPA 回退到 index.html（无 router，实际用不到回退，防御性）。"""
    app.mount(path, StaticFiles(directory=str(dist), html=True), name=path.strip("/"))


if _KIOSK_DIST.exists():
    _serve_dist(_KIOSK_DIST, "/kiosk")
if _ADMIN_DIST.exists():
    _serve_dist(_ADMIN_DIST, "/admin")


@app.get("/")
async def root():
    """默认入口：有 admin 产物则给 admin，否则提示构建。"""
    if _ADMIN_DIST.exists():
        return FileResponse(str(_ADMIN_DIST / "index.html"))
    return {"ok": True, "message": "前端未构建。运行 scripts/build_frontend.ps1 生成产物。"}
