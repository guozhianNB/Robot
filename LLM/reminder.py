# -*- coding: utf-8 -*-
r"""
定时提醒调度器（模块 6）：
  - 独立线程运行，与对话进程/线程完全解耦 —— 对话卡死不影响"到点必须响"。
  - 触发源：档案用药自动同步的每日提醒 + 护士建议（一次性/每日）。
  - 错过补触发：服务重启/晚于触发点醒来时，不静默丢弃，补报并标 missed。
  - 送达状态机：pending → triggered(广播) → confirmed / unconfirmed(超时升级，审计+告警事件)。
  - 静默时段（默认 22:00–07:00）内不主动播报（紧急告警除外，这里只做记录）。
"""
import threading
import time
from datetime import datetime, timedelta

from . import db
from . import bus
from . import log as audit
from .conf import REMINDER_STATUS

_tick = 15          # 扫描间隔（秒）
_miss_window = 300  # 错过判定窗口：超过触发点 5 分钟后才触发 → 视为错过补报

_status_labels = {
    "pending": "待触发", "triggered": "已触发", "unconfirmed": "未确认",
    "confirmed": "已确认", "missed": "错过补报",
}


def _now():
    return datetime.now()


def _is_silent(settings: dict) -> bool:
    """判断当前时刻是否在静默时段内。"""
    try:
        start = datetime.strptime(settings["silent_start"], "%H:%M").time()
        end = datetime.strptime(settings["silent_end"], "%H:%M").time()
    except Exception:
        return False
    t = _now().time()
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end   # 跨天（22:00–07:00）


def _broadcast(rem, settings, missed: bool):
    """推送提醒事件给前端（前端负责展示与确认）。"""
    silent = _is_silent(settings)
    bus.publish(
        "reminder",
        id=rem["id"], uid=rem["uid"], kind=rem["kind"],
        title=rem["title"], content=rem["content"],
        status=rem["status"], missed=missed, silent=silent,
        time=rem["trigger_time"],
    )
    audit.log("reminder", action="trigger", rid=rem["id"], uid=rem["uid"],
              title=rem["title"], missed=missed, silent=silent)


def _escalate(rem, settings):
    """确认超时 → 升级为未确认（报警模块对接点：目前写审计日志 + 广播告警事件）。"""
    if settings.get("alarm_enabled", True):
        bus.publish("alarm", level="warning", uid=rem["uid"], rid=rem["id"],
                    message=f"提醒未确认：{rem['title']}")
    audit.log("alarm", level="warning", rid=rem["id"], uid=rem["uid"],
              title=rem["title"], reason="确认超时未回应")


def _tick_once(settings: dict):
    now = _now()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    for rem in db.list_reminders():
        if rem["status"] not in ("pending", "triggered", "unconfirmed"):
            continue
        due = None
        missed = False
        if rem["trigger_type"] == "daily":
            if hhmm >= rem["trigger_time"] and rem["last_trigger_date"] != today:
                due = datetime.strptime(today + " " + rem["trigger_time"], "%Y-%m-%d %H:%M")
        elif rem["trigger_type"] == "once":
            if rem["status"] == "pending" and rem["trigger_date"]:
                try:
                    due = datetime.strptime(f"{rem['trigger_date']} {rem['trigger_time']}", "%Y-%m-%d %H:%M")
                except Exception:
                    due = None
                if due and now < due:
                    due = None
        if not due:
            continue
        missed = (now - due).total_seconds() > _miss_window
        new_status = "missed" if missed else "triggered"
        if missed:
            rem["missed_count"] = (rem.get("missed_count") or 0) + 1
        db.update_reminder(rem["id"], status=new_status, last_trigger_date=today,
                           triggered_at=now.strftime("%Y-%m-%d %H:%M:%S"),
                           missed_count=rem["missed_count"])
        rem = db.get_reminder(rem["id"])
        _broadcast(rem, settings, missed)

    # 确认超时升级：triggered 且超过 confirm_timeout_min 未确认 → unconfirmed
    for rem in db.list_reminders():
        if rem["status"] != "triggered" or not rem.get("triggered_at"):
            continue
        try:
            triggered_at = datetime.strptime(rem["triggered_at"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        timeout = int(rem.get("confirm_timeout_min") or settings.get("confirm_timeout_min", 30))
        if (now - triggered_at).total_seconds() > timeout * 60:
            db.update_reminder(rem["id"], status="unconfirmed")
            _escalate(db.get_reminder(rem["id"]), settings)


def _run():
    while True:
        try:
            settings = db.get_settings()
            if settings.get("reminder_enabled", True):
                _tick_once(settings)
            # 事件记忆 TTL 清理（不占对话链路）
            expired = db.cleanup_expired_memories()
            if expired:
                audit.log("memory_change", action="expire", count=expired, note="TTL 到期自动清除")
        except Exception as e:
            audit.log("reminder", action="tick_error", error=str(e))
        time.sleep(_tick)


def start():
    """启动调度线程（幂等）。"""
    t = threading.Thread(target=_run, name="reminder-scheduler", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- 对外操作
def confirm(rid: int, uid: str = "") -> dict:
    rem = db.get_reminder(rid)
    if not rem:
        return {"ok": False, "message": "提醒不存在"}
    if rem["status"] in ("confirmed",):
        return {"ok": True, "reminder": rem, "message": "已经确认过"}
    db.update_reminder(rid, status="confirmed", confirmed_at=db.now_iso())
    rem = db.get_reminder(rid)
    bus.publish("reminder_confirmed", id=rid, uid=rem["uid"], title=rem["title"])
    audit.log("reminder", action="confirm", rid=rid, uid=rem["uid"], by=uid or "nurse")
    return {"ok": True, "reminder": rem, "message": "已确认"}


def dismiss(rid: int) -> dict:
    rem = db.get_reminder(rid)
    if not rem:
        return {"ok": False, "message": "提醒不存在"}
    db.delete_reminder(rid)
    audit.log("reminder", action="dismiss", rid=rid, uid=rem["uid"])
    return {"ok": True, "message": "已删除"}


def status_label(status: str) -> str:
    return _status_labels.get(status, status)
