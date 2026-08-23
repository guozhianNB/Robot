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

# 记忆整理：去重向量相似度阈值（超过视为重复）
DEDUP_SIM_THRESHOLD = 0.55

# ---- 待整理对话缓冲（按 uid）：线程安全 ----
_buf_lock = threading.Lock()
_pending_turns: dict[str, list[dict]] = {}   # uid -> [{role, content}, ...]
_last_activity: dict[str, float] = {}        # uid -> 最后对话时间戳
_timers: dict[str, threading.Timer] = {}     # uid -> 空闲定时器


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
        parts.append(f"[核心] {m['content']}（{m['type']}）")
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
    """整理时给模型看的"已有记忆"，用于判断去重/冲突/合并。"""
    profile = db.get_profile(uid)
    parts = _profile_memory(profile, uid)
    portrait = db.get_portrait(uid)
    if portrait:
        parts.append(f"【老人画像（此前整理）】{portrait}")
    for m in db.list_memories(uid=uid, status="confirmed")[-15:]:
        parts.append(f"[记忆#{m['id']}] {m['type']}: {m['content']}（{m['ts']}）")
    for m in db.list_memories(uid=uid, status="pending")[-5:]:
        parts.append(f"[待审核#{m['id']}] {m['type']}: {m['content']}")
    return "\n".join(parts) if parts else "（暂无）"


CONSOLIDATE_PROMPT = """你是陪护机器人的记忆管家。下面是刚结束的一段老人与机器人的对话。

{conversation}

【该老人的档案与已有记忆】
{existing}

请完成三件事，只输出一个 JSON 对象：
{{
  "entries": [...],
  "digest": "这段对话的一句话摘要（≤80字）",
  "portrait": "整合档案与已有记忆后，老人的精简画像（≤150字，含性格/习惯/偏好/健康注意事项/说话风格）"
}}

entries 规则：
1. 只提取【这段对话里新出现、有长期价值】的信息；已有记忆已包含的不要重复提取。
2. 【玩笑、打趣、比喻、假设不是事实】——机器人打趣的话、老人随口开玩笑都不能当成真实经历提取。
3. 医疗信息（药、病史、剂量、诊断）一律不提取。
4. 不确定的条目 confidence 标 low。
5. 每条 entries 格式：
   {{"action":"add|skip|merge|conflict", "type":"preference|event|fact",
     "content":"记忆内容", "merge_id":<数字，merge 时填已有记忆编号>, "confidence":"high|low"}}
   - add：新信息
   - skip：不值得记（重复/玩笑/废话）
   - merge：已有记忆的补充细节 → merge_id 填编号，content 写合并后的完整内容
   - conflict：与已有记忆矛盾 → 照常写入 content，动作标 conflict（会进人工待处理）
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


def _apply_v3(uid: str, e: dict) -> dict:
    """记忆 v3 分流：episodic/semantic → RAG；核心层 type 按 importance 分流；
    医疗/身份红线一律 reject。"""
    mtype = (e.get("type") or "semantic").lower()
    content = (e.get("content") or "").strip()
    importance = int(e.get("importance") or 0)
    if not content:
        return {"route": "skip"}
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
    ragstore.add(uid, mtype, content, importance=importance, source="llm:consolidate")
    audit.log("memory_change", action="rag_add", uid=uid, type=mtype, content=content)
    return {"route": "rag"}


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
    """即时纠错：对话返回后异步调用。识别"纠正/更新旧记忆"，直接更新（医疗/身份红线除外）。"""
    from .chat import llm_json
    cores = db.list_core_memories(uid, limit=30)
    mem_text = "\n".join(f"[#{m['id']}] {m['content']}" for m in cores) or "（暂无）"
    data = llm_json(client, model, CORRECT_PROMPT.format(memories=mem_text, text=user_text))
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
    """整理某位老人这段时间的对话：提取记忆（去重/合并/冲突）+ 话题摘要 + 画像。"""
    turns = _take_pending(uid)
    if not turns:
        return {"ok": True, "skipped": True, "reason": "无待整理对话"}
    conversation = "\n".join(f"{m['role']}: {m['content']}" for m in turns[-20:])
    if len(conversation) > 6000:
        conversation = conversation[-6000:]

    from .chat import llm_json
    prompt = CONSOLIDATE_PROMPT.format(conversation=conversation, existing=_existing_context(uid))
    data = llm_json(client, model, prompt)

    stats = {"add": 0, "add_pending": 0, "skip": 0, "duplicate": 0,
             "merge": 0, "conflict": 0, "reject": 0}
    if isinstance(data, dict):
        for e in data.get("entries", []) or []:
            r = _apply_entry(uid, e)
            key = r["action"] if r["action"] in stats else "skip"
            stats[key] = stats.get(key, 0) + 1
        digest = (data.get("digest") or "").strip()
        if digest:
            prev = db.get_summary(uid)
            new_sum = (prev + "\n" + f"[{db.now_iso()[:10]}] {digest}").strip()
            db.set_summary(uid, new_sum[-900:])
            # 摘要同时作为 Episode 记忆入库，供后续按话题检索（修"摘要不进检索"）
            if not _dedup_check(uid, digest, mtype="episode"):
                db.add_memory(uid, "episode", digest, status="confirmed",
                              ttl_days=EPISODE_TTL_DAYS, source="llm:consolidate")
                audit.log("memory_change", action="episode_add", uid=uid, content=digest)
        portrait = (data.get("portrait") or "").strip()
        if portrait:
            db.set_portrait(uid, portrait)
            audit.log("memory_change", action="portrait_update", uid=uid, portrait=portrait)
    else:
        audit.log("memory_change", action="consolidate_error", uid=uid, error="解析失败")

    audit.log("memory_change", action="consolidate", uid=uid, turns=len(turns), stats=stats)
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
            db.delete_memory(m["id"])
            audit.log("memory_change", action="expire", uid=m["uid"], mid=m["id"],
                      content=m["content"], reason="TTL 到期")
