# -*- coding: utf-8 -*-
r"""
通知中心（护士台数据底座，需求文档模块 11）：
  - **任何模块**发现异常都往 `POST /api/notifications` 投递（该口有意免鉴权）；
  - 同 `(source,type,uid,正文)` 在 `conf.NOTIFY_DEDUP_S` 窗口内的**未处理**通知做合并
    （count+1、last_at 刷新，**保留最早原文**、级别**只升不降**），避免"同一次失联刷 80 条"；
    正文进合并键是 2026-09-18 的修订（少了它，同一分钟内**不同的事**会互相吞掉、紧急级别被静默降级）。
  - 所有状态变更写审计（`notify_ingest` / `notify_ack` / `notify_delete`），
    并广播总线事件（`notification` / `notification_ack`）让护士台实时收到。

分层：本模块属 agent 层，只 import `..store` / `..core` / `..conf`，**绝不** import server。
"""
from datetime import datetime, timedelta

from ..store import db
from ..core import bus
from ..core import log as audit
from .. import conf

LEVELS = ("info", "warning", "critical")
_LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}     # info=0 < warning=1 < critical=2


def _higher_level(a: str, b: str) -> str:
    """取更紧急的那个级别（未知值按 info 计）。合并时必须**只升不降**，见 `ingest` 注释。

    `a` 会先归一到 `LEVELS`：库里若存着词表外的级别（今天不可达 —— 写入侧只由 `ingest` 传校验过的
    值、列默认 `'info'`），也不能把它当"更紧急"原样返回再写回库。
    """
    a = a if a in _LEVEL_RANK else "info"
    b = b if b in _LEVEL_RANK else "info"
    return b if _LEVEL_RANK[b] > _LEVEL_RANK[a] else a

DEFAULT_LEVEL = {
    "sos": "critical", "fall": "critical", "help": "critical",
    "health": "warning", "no_activity": "warning",
    "reminder_unconfirmed": "warning", "reminder_missed": "warning",
    "low_battery": "warning", "offline": "warning", "task_failed": "warning",
}

TITLES = {
    "sos": "紧急呼叫", "fall": "疑似跌倒", "help": "呼救", "health": "身体异常",
    "no_activity": "长时间无活动", "reminder_unconfirmed": "提醒未确认",
    "reminder_missed": "提醒漏做", "low_battery": "电量低", "offline": "小车失联",
    "task_failed": "任务失败", "task_done": "任务完成", "patrol_done": "巡房完成",
    "arrived": "已到达", "message": "通知",
}
TITLE_FALLBACK = "通知"

_TITLE_MAX = 80


def uid_name(uid: str) -> str:
    """老人显示名 = 「姓名 · 床号」（如 `张建国 · 301床`）；无 uid / 无档案 / 都为空 → 空串。

    只读 `profiles`（姓名+床号），**只给前端多一个展示字段**，不越权带出隐私内容。
    """
    if not uid:
        return ""
    p = db.get_profile(uid) or {}
    name = (p.get("name") or "").strip()
    bed = (p.get("bed") or "").strip()
    if name and bed:
        return f"{name} · {bed}"
    return name or bed


def ingest(source: str, type: str, *, level: str = "", uid: str = "",
           title: str = "", body: str = "", ref: str = "") -> dict:
    """投递一条通知（唯一的写入口）。顺序固定，见模块 docstring。

    时间一律取**服务器时间**（`db.now_iso()`）—— 外部传入的时间不可信，也不给传入口。
    """
    # 1. type 必填（非字符串一律当缺失）；source 空 → system
    if not isinstance(type, str) or not type.strip():
        raise ValueError("type 不能为空")
    type = str(type).strip()
    source = source.strip() if isinstance(source, str) and source.strip() else "system"

    # 2. level 不合法 → 按类型兜底，最后回 info
    lvl = level if level in LEVELS else DEFAULT_LEVEL.get(type, "info")

    # 3. 标题/正文兜底与截断
    title_ = title.strip() if isinstance(title, str) else ""
    if not title_:
        title_ = TITLES.get(type, TITLE_FALLBACK)
    title_ = title_[:_TITLE_MAX]
    body_ = (body or "")[:conf.NOTIFY_BODY_MAX]
    ref_ = ref if isinstance(ref, str) else ""
    uid_ = uid or ""

    # 4. 服务器时间，同时写 created_at 与 last_at
    now = db.now_iso()

    # 5. 去重合并：窗口内同 (source,type,uid,正文) 的未处理通知
    since = (datetime.now() - timedelta(seconds=conf.NOTIFY_DEDUP_S)).strftime("%Y-%m-%d %H:%M:%S")
    old = db.find_unacked_notification(source, type, uid_, since, body_)
    # 合并级别**只升不降**：同正文的同一件事由 info 改报 critical 时，降级会把红卡+蜂鸣一起抹掉。
    eff = _higher_level(old.get("level") or "", lvl) if old else lvl
    # 竞态守卫：find 与 bump 各自持锁、不在同一临界区 —— 护士可能在这两步之间把该行 ack 了。
    # bump 的 UPDATE 带 `ack_at=''`，rowcount=0 即"刚被处理"：此时**必须回落到新增一行**，
    # 否则新上报会被并进"已处理"行（不出未读卡、不响蜂鸣、角标不变）→ 静默丢失。
    deduped = bool(old) and db.bump_notification(old["id"], now, level=eff) > 0
    if deduped:
        nid = old["id"]
        cnt = int(old.get("count") or 1) + 1        # 不改 body/title：保留最早原文
        # 广播与**库里那一行**对齐（规格 M1）：合并时正文/标题/级别一律取库里的值，
        # 否则实时 toast 与 30s 后的列表刷新会给出两套内容（护士看到卡片自己变了）。
        title_ = old.get("title") or title_
        body_ = old.get("body") or body_
    else:
        eff = lvl                                   # 新行就用本次上报的级别（别继承那个没被合并的旧行）
        nid = db.add_notification(eff, source, type, uid=uid_, title=title_,
                                  body=body_, ref=ref_, ts=now)
        cnt = 1

    # 6. 审计 + 广播
    audit.log("notify_ingest", source=source, type=type, level=lvl, stored_level=eff,
              uid=uid_, id=nid, deduped=deduped)
    # 注意：payload 键必须是 `kind` 而非 `type` —— bus.publish 内部构造
    # {"type": event_type, **payload}，payload 里再用 type 会把事件类型覆盖成业务类型，
    # 前端会丢弃该事件（同 `/api/alarm` 的 alarm_type 注释）。
    # `uid_name` 一并带出（规格 §4.5）：前端收到实时事件不必再拉一次档案就能显示老人。
    bus.publish("notification", id=nid, level=eff, source=source, kind=type, uid=uid_,
                uid_name=uid_name(uid_), title=title_, body=body_, count=cnt, last_at=now)

    # 7. 返回（level 是**库里实际生效**的级别：合并升级后与本次上报可能不同）
    return {"ok": True, "id": nid, "deduped": deduped, "level": eff}


def list_notices(state: str = "all", limit: int = 0, before_id: int = 0) -> list[dict]:
    """通知列表：`limit<=0` 用 `conf.NOTIFY_LIST_LIMIT`；`state="unread"` 只回未处理。"""
    return db.list_notifications(state=state,
                                 limit=limit if limit and limit > 0 else conf.NOTIFY_LIST_LIMIT,
                                 before_id=before_id)


def counts() -> dict:
    """角标计数：`{"unread": int, "critical": int}`。"""
    return db.notification_counts()


def ack(nid: int, by: str = "admin") -> bool:
    """确认一条通知；不存在或已确认过 → False。"""
    if db.ack_notification(nid, by) <= 0:
        return False
    audit.log("notify_ack", id=nid, by=by)
    bus.publish("notification_ack", id=nid, by=by)
    return True


def ack_all(by: str = "admin") -> int:
    """确认全部未处理通知，返回条数。"""
    n = db.ack_all_notifications(by)
    audit.log("notify_ack", all=True, n=n, by=by)
    bus.publish("notification_ack", all=True, by=by, n=n)
    return n


def remove(nid: int) -> bool:
    """删除单条通知（路由限管理员）。"""
    ok = db.delete_notification(nid) > 0
    audit.log("notify_delete", id=nid, by="admin")   # 无论行在不在都留痕（删除请求本身就是审计事实）
    return ok


def prune(days: int = 0) -> int:
    """清理已处理且过期的通知（**只删已 ack 的**），返回删除行数。`days=0` 用配置默认。"""
    days = days or conf.NOTIFY_KEEP_DAYS
    before = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return db.prune_notifications(before)
