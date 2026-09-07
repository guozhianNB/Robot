# -*- coding: utf-8 -*-
r"""
RAG 长期记忆（模块 2）+ 半自动记忆沉淀。

混合检索路由：
  - 结构化字段（姓名/床号/病史/用药/称呼）→ 直接查档案表（SQLite），查表比向量可靠
  - 事件经历 / 喜好偏好 → 向量检索 Top-K 拼进 Prompt
  - 统一入口 recall(uid, query)

记忆沉淀（v2：批量整理，不是每轮都写）：
  - 触发时机：一段对话"话题结束"——老人空闲 N 秒不再说话，或上下文窗口已满
  - 整理时把这段时间的整段对话 + 已有记忆一起交给模型：
      · 只提取"新信息"，已有记忆不再重复入库（去重）
      · 玩笑/打趣/比喻不算事实（防"领带梗"这类假记忆）
      · 新信息与已有记忆冲突 → 标 conflict 进待处理；只是补充细节 → merge 写回原条目
      · 同时生成"老人画像"（精简档案卡）与话题摘要
  - 写入分级不变：医疗只人工 / 偏好待处理 / 事件带 TTL 自动入库
"""
import json
import threading
import time
import re

from . import db
from . import log as audit
from . import vectors
from . import graph
from . import ragstore
from .conf import (MEMORY_RULES, EVENT_TTL_DAYS, EPISODE_TTL_DAYS,
                   CORE_IMPORTANCE_THRESHOLD, IDENTITY_KEYWORDS)

# 医疗字段关键词：命中即判定为医疗信息，禁止模型写入
MEDICAL_KEYWORDS = ["药", "剂量", "病史", "诊断", "血压", "血糖", "手术", "住院", "过敏",
                    "服用", "胰岛素", "病历", "医嘱", "检查结果", "癌", "肿瘤"]

# R1 即时纠错信号词（对标 MaiBot feedback_signal_tokens）：命中才让 LLM 判断是否有纠正，省每轮 LLM 调用
CORRECT_SIGNALS = [
    "不是", "不对", "错了", "记错", "说错", "说反", "搞错", "更正", "纠正",
    "其实", "应该是", "不是的", "改一下", "更新", "忘了说", "补充一下", "以后别",
    "别再", "不要叫我", "别叫我", "我姓", "我不叫", "其实我", "我是",
]

# 记忆整理：去重向量相似度阈值（超过视为重复）
DEDUP_SIM_THRESHOLD = 0.55

# ---- 待整理对话缓冲（按 uid）：线程安全 ----
_buf_lock = threading.Lock()
_pending_turns: dict[str, list[dict]] = {}   # uid -> [{role, content}, ...]
_last_activity: dict[str, float] = {}        # uid -> 最后对话时间戳
_timers: dict[str, threading.Timer] = {}     # uid -> 空闲定时器
_in_flight: set[str] = set()                 # uid -> 正在 consolidate（租约，防并发重复整理）


def _try_acquire(uid: str) -> bool:
    """租约：同一 uid 同一时刻只允许一个 consolidate 在跑（空闲定时器/上下文满/手动 suggest 竞争时防重）。"""
    with _buf_lock:
        if uid in _in_flight:
            return False
        _in_flight.add(uid)
        return True


def _release(uid: str) -> None:
    with _buf_lock:
        _in_flight.discard(uid)


def _profile_memory(profile: dict | None, uid: str) -> list[str]:
    """结构化字段 → 纯文本片段（查表，不走向量）。"""
    if not profile:
        return [f"（老人 {uid} 暂无档案，说话时注意不要编造）"]
    lines = []
    p = profile.get("profile") or {}
    prefs = profile.get("preferences") or {}
    if profile.get("name"):
        lines.append(f"姓名：{profile['name']}（称呼：{profile.get('nickname') or profile['name']}）")
    if profile.get("bed"):
        lines.append(f"床位：{profile['bed']}")
    if profile.get("age"):
        lines.append(f"年龄：{profile['age']} 岁")
    if p.get("病史"):
        lines.append("病史：" + "、".join(p["病史"]))
    if p.get("用药"):
        meds = []
        for m in p["用药"]:
            if isinstance(m, dict):
                meds.append(f"{m.get('name','')}{m.get('dose','')} {m.get('time','')}")
            else:
                meds.append(str(m))
        if meds:
            lines.append("用药：" + "；".join(meds) + "（医疗信息只读，来自护士档案，不得自行更改）")
    if prefs.get("称呼"):
        lines.append(f"偏好称呼：{prefs['称呼']}")
    if prefs.get("话题"):
        lines.append("喜欢话题：" + "、".join(prefs["话题"]))
    if profile.get("style"):
        lines.append(f"说话风格画像：{profile['style']}")
    return lines


def _vector_memory(uid: str, query: str, top_k: int = 3) -> list[dict]:
    """偏好/事件类记忆 → 向量检索 Top-K。"""
    mems = db.list_memories(uid=uid, status="confirmed")
    docs = []
    for m in mems:
        if m.get("expires_at") and m["expires_at"] < db.now_iso():
            continue
        docs.append({"id": m["id"], "text": f"[{m['type']}] {m['content']}", "meta": m})
    if not docs:
        return []
    index = vectors.build_index(docs)
    hits = vectors.recall(index, query, top_k=top_k)
    return [h["doc"]["meta"] for h in hits]


def recall(uid: str, query: str) -> dict:
    """统一入口：结构化查表 + 向量 Top-K，返回 {context, sources}。"""
    profile = db.get_profile(uid)
    structured = _profile_memory(profile, uid)
    vec_hits = _vector_memory(uid, query)
    parts = structured + [f"[记忆] {m['content']}（{m['type']}，{m['ts']}）" for m in vec_hits]
    return {
        "context": "\n".join(parts),
        "sources": [{"type": "profile"} for _ in structured] + [{"type": m["type"], "id": m["id"]} for m in vec_hits],
    }


def recall_v3(uid: str, query: str) -> dict:
    """v3 检索组装：只读档案 + 核心记忆（cap）+ RAG Top-K + 图谱一跳关系。"""
    from .conf import MEMORY_TOP_K, CORE_MEMORY_CAP, CORE_MEMORY_CHAR_CAP
    parts = []
    sources = []
    profile = db.get_profile(uid)
    parts += _profile_memory(profile, uid)
    sources += [{"type": "profile"} for _ in parts]

    cores = db.list_core_memories(uid, limit=CORE_MEMORY_CAP)
    for m in cores:
        # P0c 账本定稿：AI 归纳(llm)标注"仅供参考"，护士确认(nurse)才视为可信事实
        auth = (m.get("authority") or "llm")
        tag = "" if auth in ("nurse", "claim") else "（AI 归纳，仅供参考）"
        parts.append(f"[核心] {m['content']}（{m['type']}）{tag}")
    sources += [{"type": "core", "id": m["id"]} for m in cores]

    for h in ragstore.query(uid, query, top_k=MEMORY_TOP_K):
        parts.append(f"[记忆] {h['content']}")
        sources.append({"type": "rag"})

    for eid in _query_entities(uid, query):
        for rel in graph.one_hop(eid):
            parts.append(f"[关系] {eid.split(':')[-1]} {rel['type']} {rel['target']}")
            sources.append({"type": "graph"})

    context = "\n".join(parts)
    if len(context) > CORE_MEMORY_CHAR_CAP + 3000:
        context = context[:CORE_MEMORY_CHAR_CAP + 3000]
    return {"context": context, "sources": sources}


def _query_entities(uid: str, query: str) -> list[str]:
    """从 query 匹配该 uid 已有实体名（名称出现在 query 中），返回命中实体 id。"""
    ids = []
    for ent in graph.list_entities(uid):
        name = ent.get("name") or ""
        if name and name in query:
            ids.append(ent["id"])
    return ids


# ================================================================
# 记忆沉淀 v2：话题结束批量整理
# ================================================================
def note_turn(uid: str, user_text: str, assistant_text: str, client, model: str, settings: dict):
    """每轮对话后调用：把本轮对话记入缓冲，并安排空闲定时器。
    老人 N 秒不再说话 → 触发 consolidate()（话题结束才整理记忆）。"""
    with _buf_lock:
        _pending_turns.setdefault(uid, []).append({"role": "user", "content": user_text})
        if assistant_text.strip():
            _pending_turns[uid].append({"role": "assistant", "content": assistant_text})
        _last_activity[uid] = time.time()

    # 取消旧定时器，重新计时
    old = _timers.pop(uid, None)
    if old:
        old.cancel()

    if not settings.get("memory_consolidation_enabled", True):
        return

    idle = int(settings.get("consolidate_idle_sec", 30) or 30)
    t = threading.Timer(idle, _consolidate_worker, args=[uid, client, model])
    t.daemon = True
    with _buf_lock:
        _timers[uid] = t
    t.start()


def _consolidate_worker(uid, client, model):
    """定时器回调：先检查是否真的空闲（期间没新对话），是才整理。"""
    with _buf_lock:
        idle = time.time() - _last_activity.get(uid, 0)
    if idle < 1.0:   # 刚有新对话进来，放弃本轮
        return
    try:
        consolidate(uid, client, model)
    except Exception as e:
        audit.log("memory_change", action="consolidate_error", uid=uid, error=str(e))


def _take_pending(uid: str) -> list[dict]:
    with _buf_lock:
        return _pending_turns.pop(uid, [])


def _existing_context(uid: str) -> str:
    """整理时给模型看的已有记忆（核心记忆含 id，用于去重/合并/纠错判断）。"""
    profile = db.get_profile(uid)
    parts = _profile_memory(profile, uid)
    for m in db.list_core_memories(uid)[:20]:
        parts.append(f"[核心记忆#{m['id']}] {m['type']}: {m['content']}")
    return "\n".join(parts) if parts else "（暂无）"


CONSOLIDATE_PROMPT = """你是陪护机器人的记忆管家。下面是刚结束的一段老人与机器人的对话。

{conversation}

【该老人的档案与已有记忆】
{existing}

请完成三件事，只输出一个 JSON 对象：
{{
  "entries": [...],
  "digest": "这段对话的一句话摘要（≤80字）",
  "portrait": "整合档案与已有记忆后，老人的精简画像（≤150字，含性格/习惯/偏好/说话风格（不得包含任何医疗/用药/病史信息））",
  "expressions": [{{"situation": "场景描述", "style": "老人当时的原话说法"}}]
}}

entries 规则：
1. 只提取【这段对话里新出现、有长期价值】的信息；已有记忆已包含的不要重复提取。
2. 【玩笑、打趣、比喻、假设不是事实】——机器人打趣的话、老人随口开玩笑都不能当成真实经历提取。
3. 医疗信息（药、病史、剂量、诊断）一律不提取。
4. 不确定的条目 confidence 标 low。
5. 每条 entries 格式：
   {{"type":"episodic|semantic|preference|relation|persona|style|fact",
    "content":"记忆内容", "importance":0到5的整数}}
   - episodic/semantic 属普通记忆；preference/relation/persona/style/fact 属核心记忆
   - importance 越重要分越高；核心记忆（性格/重要关系/画像/说话风格/基本事实）通常填 3 以上，
     普通事件/一般事实填 0-2
6. 若某条新信息是在【修正】已有记忆（已有记忆编号见上文《已有记忆》列表），
   用 {{"action":"correct", "correct_id":<已有记忆编号>, "content":"修正后的完整内容"}} 表示。

expressions 规则（提取老人说话风格，供机器人学口吻但保持护工身份）：
- situation=触发场景（如"夸她"、"聊到老伴儿"、"让她喝水"），style=老人原话说法（≤20字，保留原词）。
- 只提取稳定重复出现或有代表性的口吻/称呼习惯；玩笑、情绪化发泄、无意义口头语不提取。
- 不得含脏话、医疗用药内容、人名全名、个人隐私（只用"老伴儿""孙女"这类称呼即可）。
- 每次最多输出 3 条；风格不明则输出空数组。
"""


def _dedup_check(uid: str, content: str, mtype: str | None = None) -> int | None:
    """服务端兜底：新条目与已有记忆（已确认+待处理，可限定类型）向量相似度过高 → 视为重复，返回已有 id。"""
    all_mems = db.list_memories(uid=uid, status="confirmed") + db.list_memories(uid=uid, status="pending")
    if mtype:
        all_mems = [m for m in all_mems if m.get("type") == mtype]
    docs = [{"id": m["id"], "text": m["content"]} for m in all_mems]
    if not docs:
        return None
    index = vectors.build_index(docs)
    hits = vectors.recall(index, content, top_k=1)
    if hits and hits[0]["score"] >= DEDUP_SIM_THRESHOLD:
        return hits[0]["doc"]["id"]
    return None


def _rag_dedup_check(uid: str, content: str, mtype: str | None = None) -> int | None:
    """RAG 层去重：新条目与已有 rag_memories（可限定类型）向量相似度过高 → 返回命中 id。"""
    all_mems = db.list_rag_memories(uid)
    if mtype:
        all_mems = [m for m in all_mems if m.get("type") == mtype]
    docs = [{"id": m["id"], "text": m["content"]} for m in all_mems]
    if not docs:
        return None
    index = vectors.build_index(docs)
    hits = vectors.recall(index, content, top_k=1)
    if hits and hits[0]["score"] >= DEDUP_SIM_THRESHOLD:
        return hits[0]["doc"]["id"]
    return None


def _apply_v3(uid: str, e: dict) -> dict:
    """记忆 v3 分流：episodic/semantic → RAG；核心层 type 按 importance 分流；
    医疗/身份红线一律 reject。"""
    mtype = (e.get("type") or "semantic").lower()
    content = (e.get("content") or "").strip()
    try:
        importance = int(e.get("importance") or 0)
    except (ValueError, TypeError):
        importance = 0
    if not content:
        return {"route": "skip"}
    # 整理纠错：本条是在修正旧核心记忆
    if e.get("action") == "correct" and e.get("correct_id"):
        try:
            mid = int(e["correct_id"])
        except (ValueError, TypeError):
            return {"route": "skip"}
        old = db.get_core_memory(mid)
        if not old or old.get("uid") != uid:
            audit.log("memory_correct", action="blocked", uid=uid, mid=mid,
                      reason="目标不存在或不属于该老人")
            return {"route": "skip"}
        if any(k in content for k in MEDICAL_KEYWORDS) or any(k in content for k in IDENTITY_KEYWORDS):
            audit.log("memory_correct", action="blocked", uid=uid, mid=mid, reason="红线")
            return {"route": "reject"}
        db.update_core_memory(mid, content=content)
        audit.log("memory_correct", action="consolidate", uid=uid, mid=mid,
                  old=old["content"], new=content)
        return {"route": "correct"}
    if mtype == "medical" or any(k in content for k in MEDICAL_KEYWORDS):
        audit.log("memory_change", action="reject", uid=uid, type=mtype,
                  content=content, reason="医疗只读红线")
        return {"route": "reject", "reason": "医疗信息只允许人工录入"}
    if any(k in content for k in IDENTITY_KEYWORDS):
        audit.log("memory_change", action="reject", uid=uid, type=mtype,
                  content=content, reason="身份只读红线")
        return {"route": "reject", "reason": "身份信息只允许护士录入"}

    core_types = {"preference", "relation", "persona", "style", "fact"}
    if mtype in core_types and importance >= CORE_IMPORTANCE_THRESHOLD:
        db.add_core_memory(uid, mtype, content, importance=importance, source="llm:consolidate")
        audit.log("memory_change", action="core_add", uid=uid, type=mtype, content=content)
        return {"route": "core"}
    # 其余（episodic/semantic 或低 importance 核心层）→ RAG
    if _rag_dedup_check(uid, content, mtype=mtype):
        return {"route": "duplicate"}
    ragstore.add(uid, mtype, content, importance=importance, source="llm:consolidate")
    audit.log("memory_change", action="rag_add", uid=uid, type=mtype, content=content)
    return {"route": "rag"}


def _upsert_relation(uid: str, rel: dict) -> None:
    src, dst = (rel.get("src") or "").strip(), (rel.get("dst") or "").strip()
    if not src or not dst:
        return
    sid = f"{uid}:{src}"
    did = f"{uid}:{dst}"
    graph.upsert_entity(uid, sid, src, rel.get("stype", "entity"))
    graph.upsert_entity(uid, did, dst, rel.get("dtype", "entity"))
    graph.upsert_relation(uid, sid, did, rel.get("rel", "related_to"))


def _append_summary_and_episode(uid: str, digest: str, external_id: str = "") -> None:
    if any(k in digest for k in MEDICAL_KEYWORDS):
        audit.log("memory_change", action="reject", uid=uid, type="digest",
                  content=digest, reason="医疗只读红线")
        return
    prev = db.get_summary(uid)
    new_sum = (prev + "\n" + f"[{db.now_iso()[:10]}] {digest}").strip()
    db.set_summary(uid, new_sum[-900:])
    # 幂等落 episode：同一段对话（由 external_id=对话内容指纹标识）只生成一次，防重复整理
    if not _rag_dedup_check(uid, digest, mtype="episodic"):
        ragstore.add(uid, "episodic", digest, source="llm:consolidate", external_id=external_id)
        audit.log("memory_change", action="episode_add", uid=uid, content=digest)


# R2 画像防退化：AI 新画像与现 persona 相似度 ≥ 该阈值 → 跳过重写（防 LLM 抖动/内容退化）
PORTRAIT_SKIP_SIM = 0.90


def _upsert_portrait(uid: str, portrait: str, source: str = "llm:consolidate",
                     by: str = "nurse") -> None:
    """画像写入核心记忆（type=persona, importance=5），旧 persona 软覆盖（删旧写新）。

    - P3: pinned（护士保护/手动维护）条目绝不覆盖
    - R2: AI 生成的新画像与现 persona 高度相似（≥PORTRAIT_SKIP_SIM）→ 跳过不重写，
          防止 consolidate 每轮 LLM 输出微小抖动导致画像反复重写、内容退化
    """
    if any(k in portrait for k in MEDICAL_KEYWORDS):
        audit.log("memory_change", action="reject", uid=uid, type="persona",
                  content=portrait, reason="医疗只读红线")
        return
    existing = [m for m in db.list_core_memories(uid) if m["type"] == "persona"]
    # 护士手动维护的画像（pinned）存在且本次为 AI 归纳 → 直接跳过，绝不覆盖
    if source.startswith("llm") and any(m.get("pinned") for m in existing):
        audit.log("memory_change", action="portrait_skip", uid=uid,
                  reason="护士已手动维护画像(pinned)，AI 不覆盖")
        return
    # 防退化：AI 归纳且与现状几乎相同 → 不重写（护士手动维护不受此限）
    if source.startswith("llm") and existing:
        prev = max(existing, key=lambda m: m.get("importance", 0) or 0)
        prev_text = prev.get("content") or ""
        if prev_text:
            idx = vectors.build_index([{"id": "p", "text": prev_text}])
            hits = vectors.recall(idx, portrait, top_k=1)
            if hits and hits[0]["score"] >= PORTRAIT_SKIP_SIM:
                audit.log("memory_change", action="portrait_skip", uid=uid,
                          reason="与现画像相似度过高")
                return
    for m in existing:
        if not m.get("pinned"):
            db.delete_core_memory_hard(m["id"])
    db.add_core_memory(uid, "persona", portrait, importance=5,
                       source=source if not source.startswith("nurse") else "nurse:manual",
                       authority="nurse" if source.startswith(("nurse", "manual")) else "llm")
    if source.startswith(("nurse", "manual")):
        # 护士手动维护的画像：pinned 防 AI 覆盖
        for m in db.list_core_memories(uid):
            if m["type"] == "persona" and m.get("content") == portrait:
                db.set_core_pinned(m["id"], True)
                break
    db.set_portrait(uid, portrait)  # 双写过渡，兼容 chat.py/server.py 旧读路径
    audit.log("memory_change", action="portrait_update", uid=uid, portrait=portrait,
              source=source, by=by)


CORRECT_PROMPT = """下面是该老人已有的核心记忆列表，以及一句老人新说的话。
判断这句话是否在纠正/更新某条已有记忆。
若是，输出要纠正的记忆 id（从列表里选）与新内容；否则 correct=false。
只输出 JSON：{{"correct": true或false, "mid": <记忆id，无则 null>, "new_content": "纠正后的内容"}}

【已有核心记忆】
{memories}

【新说的话】
{text}
"""


def correct_instant(uid: str, user_text: str, client, model: str) -> dict:
    """即时纠错：对话返回后异步调用。识别"纠正/更新旧记忆"，直接更新（医疗/身份红线除外）。

    R1（对标 MaiBot feedback_signal_tokens）：先做信号词规则预筛，无纠正信号直接返回，
    省去每轮一次无谓的 LLM 调用（原先每轮对话都让 LLM 判断，很费 token）。
    """
    from .chat import llm_json
    # ---- 规则预筛：纠正信号词 ----
    text = (user_text or "").strip()
    if not text or len(text) > 80:   # 超长句不预判（可能含复杂纠正，交给 consolidate）
        return {"corrected": False, "reason": "no_signal"}
    signal_hit = any(sig in text for sig in CORRECT_SIGNALS)
    if not signal_hit:
        return {"corrected": False, "reason": "no_signal"}
    cores = db.list_core_memories(uid, limit=30)
    mem_text = "\n".join(f"[#{m['id']}] {m['content']}" for m in cores) or "（暂无）"
    data = llm_json(client, model, CORRECT_PROMPT.format(memories=mem_text, text=text))
    if not data:
        # llm_json 失败/解析失败返回空 dict，区别于"无纠正"
        audit.log("memory_correct", action="instant_error", uid=uid, error="LLM 返回空或解析失败")
        return {"corrected": False, "reason": "llm_error"}
    if not isinstance(data, dict) or not data.get("correct") or data.get("mid") is None:
        return {"corrected": False, "reason": "no_correction"}
    try:
        mid = int(data["mid"])
    except (ValueError, TypeError):
        audit.log("memory_correct", action="instant_error", uid=uid, error="mid 非数值")
        return {"corrected": False, "reason": "bad_mid"}
    old = db.get_core_memory(mid)
    if not old or old.get("uid") != uid:
        audit.log("memory_correct", action="instant_error", uid=uid, mid=mid,
                  error="目标不存在或不属于该老人")
        return {"corrected": False, "reason": "no_target"}
    new_content = (data.get("new_content") or "").strip()
    if not new_content:
        return {"corrected": False, "reason": "no_target"}
    if any(k in new_content for k in MEDICAL_KEYWORDS) or any(k in new_content for k in IDENTITY_KEYWORDS):
        audit.log("memory_correct", action="blocked", uid=uid, mid=mid,
                  old=old["content"], new=new_content, reason="医疗/身份红线")
        return {"corrected": False, "reason": "redline"}
    db.update_core_memory(mid, content=new_content)
    audit.log("memory_correct", action="instant", uid=uid, mid=mid,
              old=old["content"], new=new_content)
    return {"corrected": True, "mid": mid}


def _apply_entry(uid: str, e: dict) -> dict:
    """按分级规则写入一条整理结果。返回处理摘要。"""
    action = e.get("action", "add")
    mtype = (e.get("type") or "fact").lower()
    content = (e.get("content") or "").strip()
    if action == "skip" or not content:
        return {"action": "skip"}
    if mtype == "medical" or any(k in content for k in MEDICAL_KEYWORDS):
        audit.log("memory_change", action="reject", uid=uid, type=mtype,
                  content=content, reason="医疗只读红线")
        return {"action": "reject", "reason": "医疗信息只允许人工录入"}

    if action == "merge":
        mid = e.get("merge_id")
        if mid and db.get_memory(int(mid)):
            db.update_memory_content(int(mid), content)
            audit.log("memory_change", action="merge", uid=uid, mid=int(mid), content=content)
            return {"action": "merge", "mid": mid}
        return {"action": "skip"}

    if action == "conflict":
        mid = db.add_memory(uid, mtype, content, status="pending", source="llm")
        audit.log("memory_change", action="conflict_pending", uid=uid, mid=mid,
                  type=mtype, content=content)
        return {"action": "conflict", "mid": mid}

    # add：先做服务端去重兜底
    dup = _dedup_check(uid, content)
    if dup:
        return {"action": "duplicate", "mid": dup}

    rule = MEMORY_RULES.get(mtype, "pending")
    if rule == "confirmed":
        mid = db.add_memory(uid, mtype, content, status="confirmed",
                            ttl_days=EVENT_TTL_DAYS, source="llm")
        audit.log("memory_change", action="auto_add", uid=uid, mid=mid,
                  type=mtype, content=content, ttl_days=EVENT_TTL_DAYS)
        return {"action": "add", "mid": mid}
    mid = db.add_memory(uid, mtype, content, status="pending", source="llm")
    audit.log("memory_change", action="pending_add", uid=uid, mid=mid,
              type=mtype, content=content)
    return {"action": "add_pending", "mid": mid}


def consolidate(uid: str, client, model: str) -> dict:
    """整理某位老人这段时间的对话：提取记忆（去重/合并/冲突）+ 话题摘要 + 画像。

    P1a 租约：同 uid 正在整理时直接跳过（空闲定时器/上下文满/手动触发并发竞争防重）。
    """
    if not _try_acquire(uid):
        return {"ok": True, "skipped": True, "reason": "该老人正在整理中（租约占用）"}
    try:
        return _consolidate_locked(uid, client, model)
    finally:
        _release(uid)


def _apply_expression(uid: str, e: dict) -> dict:
    """一条老人说话风格表达 → 自审 → 落 expressions 待审（checked=0，护士审核后参与注入）。"""
    situation = (e.get("situation") or "").strip()
    style = (e.get("style") or "").strip()
    if not situation or not style:
        return {"action": "skip"}
    if len(style) > 40:
        return {"action": "skip", "reason": "style 过长"}
    # 自审：脏话/医疗/身份/隐私红线（服务端兜底，模型也可能漏）
    # 注意用多字词避免误伤（"操"会误杀"操心"）
    banned = MEDICAL_KEYWORDS + IDENTITY_KEYWORDS + \
        ["妈的", "他妈", "混蛋", "滚蛋", "去死", "放屁", "傻逼", "贱人", "妈的逼", "草泥马", "日你", "操你"]
    if any(k in situation for k in banned) or any(k in style for k in banned):
        return {"action": "reject", "reason": "红线"}
    if re.search(r"[\u4e00-\u9fff]{4,}", situation) is None:
        situation = "日常对话"  # 兜底场景描述
    eid = db.upsert_expression(uid, situation, style, authority="llm", source="llm:consolidate")
    return {"action": "add", "eid": eid}


def _consolidate_locked(uid: str, client, model: str) -> dict:
    turns = _take_pending(uid)
    if not turns:
        return {"ok": True, "skipped": True, "reason": "无待整理对话"}
    conversation = "\n".join(f"{m['role']}: {m['content']}" for m in turns[-20:])
    if len(conversation) > 6000:
        conversation = conversation[-6000:]

    # P0a 幂等锚点：本段对话的稳定指纹作为 external_id（含 uid，同段对话重复整理只落一次 episode）
    import hashlib as _hl
    ext_id = f"consolidate:{uid}:{_hl.sha256(conversation.encode('utf-8')).hexdigest()[:20]}"
    from .chat import llm_json
    prompt = CONSOLIDATE_PROMPT.format(conversation=conversation, existing=_existing_context(uid))
    data = llm_json(client, model, prompt)

    stats = {"skip": 0, "reject": 0, "core": 0, "rag": 0, "correct": 0, "duplicate": 0}
    if isinstance(data, dict):
        for e in data.get("entries", []) or []:
            try:
                r = _apply_v3(uid, e)
                key = r["route"]
                stats[key] = stats.get(key, 0) + 1
            except Exception as exc:  # noqa: BLE001
                audit.log("memory_change", action="entry_error", uid=uid, error=str(exc))
                stats["skip"] = stats.get("skip", 0) + 1

        for rel in data.get("relations", []) or []:
            _upsert_relation(uid, rel)

        expr_stats = {"skip": 0, "reject": 0, "add": 0}
        for e in data.get("expressions", []) or []:
            try:
                r = _apply_expression(uid, e)
                expr_stats[r.get("action", "skip")] = expr_stats.get(r.get("action", "skip"), 0) + 1
            except Exception as exc:  # noqa: BLE001
                audit.log("memory_change", action="expression_error", uid=uid, error=str(exc))
                expr_stats["skip"] += 1

        digest = (data.get("digest") or "").strip()
        if digest:
            _append_summary_and_episode(uid, digest, external_id=ext_id)

        portrait = (data.get("portrait") or "").strip()
        if portrait:
            _upsert_portrait(uid, portrait)
    else:
        audit.log("memory_change", action="consolidate_error", uid=uid, error="解析失败")

    audit.log("memory_change", action="consolidate", uid=uid, turns=len(turns),
              stats=stats, expressions=expr_stats)
    return {"ok": True, "stats": stats}


def suggest_from_chat(uid: str, user_text: str, assistant_text: str, client, model: str) -> dict:
    """手动触发（/api/memories/suggest）：把给定对话当一段待整理内容立即整理。"""
    with _buf_lock:
        _pending_turns.setdefault(uid, []).append({"role": "user", "content": user_text})
        if assistant_text.strip():
            _pending_turns[uid].append({"role": "assistant", "content": assistant_text})
        _last_activity[uid] = time.time()
    return consolidate(uid, client, model)


async def suggest_from_chat_async(uid: str, user_text: str, assistant_text: str, llm_client, model: str):
    import asyncio
    return await asyncio.to_thread(suggest_from_chat, uid, user_text, assistant_text, llm_client, model)


def _clean_expired():
    """清理过期事件记忆（TTL 到期自动降权/清除）。"""
    for m in db.list_memories():
        if m.get("expires_at") and m["expires_at"] < db.now_iso() and m["status"] == "confirmed":
            db.delete_memory_hard(m["id"])
            audit.log("memory_change", action="expire", uid=m["uid"], mid=m["id"],
                      content=m["content"], reason="TTL 到期")


# ---------------------------------------------------------------- P1b 反馈纠错（stale 联动）
def correct_from_feedback(uid: str, old_content: str, new_content: str,
                          target: str = "auto", by: str = "nurse") -> dict:
    """老人/护士反馈"记错了"时的纠错写回（对标 MaiBot reject→FORGET + correct 写回语义）：

    - 定位旧记忆（core_memories / rag_memories 精确/包含匹配）
    - 旧条目软删（进回收站可回滚）—— rag 条目同步清 Chroma 向量（检索侧立即失效）
    - 新内容按 authority 语义写入 core（nurse 定稿）；无旧条目命中则当作新增
    - 全程审计留痕；纯服务端实现，不依赖 LLM
    """
    from . import ragstore as _rs
    results = db.find_memories_by_content(uid, old_content,
                                          tables=("core_memories", "rag_memories"))
    stats = {"core_softdel": 0, "rag_softdel": 0, "added": 0, "redline": 0}
    new_text = (new_content or "").strip()
    if any(k in new_text for k in MEDICAL_KEYWORDS) or any(k in new_text for k in IDENTITY_KEYWORDS):
        audit.log("memory_correct", action="blocked", uid=uid, content=new_text,
                  reason="医疗/身份红线")
        return {"ok": False, "error": "医疗/身份信息不允许通过此入口修改", "stats": stats}

    hits = list(results.get("core_memories", [])) + list(results.get("rag_memories", []))
    for m in hits:
        table = "core_memories" if m.get("chroma_id") is None else "rag_memories"
        # rag 行特征：有 chroma_id 字段 → 走 rag 软删
        if "chroma_id" in m and m.get("chroma_id"):
            db.delete_rag_memory(m["id"], uid=uid, reason="feedback_correct", by=by)
            _rs.delete_by_chroma_id(uid, m["chroma_id"])
            stats["rag_softdel"] += 1
        else:
            db.delete_core_memory(m["id"], uid=uid, reason="feedback_correct", by=by)
            stats["core_softdel"] += 1

    if new_text:
        # 纠错后的正确内容 → core 记忆（type=fact），护士入口即定稿
        if _rag_dedup_check(uid, new_text):
            stats["added"] = 0
        else:
            mid = db.add_core_memory(uid, "fact", new_text, confidence=1.0, importance=3,
                                     source=f"correct:{by}")
            stats["added"] = 1 if mid else 0

    audit.log("memory_correct", action="feedback", uid=uid, old=old_content[:200],
              new=new_text[:200], by=by, stats=stats)
    return {"ok": True, "stats": stats}
