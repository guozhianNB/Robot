# -*- coding: utf-8 -*-
r"""
分层用户体系的会话层：**谁在说话**（角色）与**现在在哪个病房**（集体层归属）。

规格：docs/superpowers/specs/2026-09-14-layered-user-roles-design.md（2026-09-14 二次修订版）

两条槽位：
  kiosk —— 车前设备，承载语音链路与车前屏；
  admin —— 管理台（浏览器）。
`uid`/`locked`/`ward_uid` 全局一份（一辆车一块屏，沿用现状）；`role` 按槽位隔离，
所以**管理台登录不会把车前屏提权**（这是双槽存在的唯一理由）。

红线：
  R1 前端传的 role 不可信 —— 角色只由 derive_role() 从 uid 推导（依据 profiles.kind，不靠前缀）；
  R2 fail-closed —— 未知 uid / 未知角色一律按集体层最小能力；
  R3 急停/呼救永远放行（本模块不做任何拦截）。
降级：拿不到位姿 / 认不出当前地图 / 本图没有病房区域 → 位置自动切换自行停用，
      会话保持当前病房不变，绝不阻塞对话。
"""
import time

from . import db
from . import log as audit

SLOTS = ("kiosk", "admin")
ADMIN_UID = "admin"

# 全局共享：当前主体（uid/locked）与"当前病房"（ward_uid/manual_until）
_shared: dict = {"uid": "", "locked": False, "ward_uid": "", "manual_until": 0.0}
# 按槽位：角色与提权状态
_state: dict[str, dict] = {}


def _now_ts() -> float:
    return time.monotonic()


def reset_for_test() -> None:
    """单测用：清空全部会话状态。"""
    _shared.update({"uid": "", "locked": False, "ward_uid": "", "manual_until": 0.0})
    _state.clear()


def _slot(slot: str) -> dict:
    if slot not in SLOTS:
        slot = "kiosk"
    return _state.setdefault(slot, {"role": "ward", "source": "default", "until": None})


def derive_role(uid: str | None) -> str:
    """uid → 角色（**R1 的唯一入口**）。权威依据是 `profiles.kind`，不靠 uid 前缀。"""
    if not uid:
        return "ward"                       # R2
    if uid == ADMIN_UID:
        return "admin"
    kind = db.get_profile_kind(uid)
    if kind == "ward":
        return "ward"
    if kind == "elder":
        return "elder"
    return "ward"                           # R2 未知 uid


def _settings() -> dict:
    return db.get_settings()


def set_subject(uid: str, locked: bool = False, slot: str = "kiosk",
                source: str = "manual") -> dict:
    """切换会话主体：写 uid，role 由 uid 推导（R1）。"""
    s = _slot(slot)
    if s["role"] == "admin" and source != "manual":
        # D8 提权只升不降：管理员会话期间声纹认人不改主体，只留痕
        audit.log("session_login", action="voiceprint_ignored_in_admin", uid=uid, slot=slot)
        return get_principal(slot)
    _shared["uid"] = uid or ""
    _shared["locked"] = bool(locked)
    s["role"] = derive_role(uid)
    s["source"] = source
    s["until"] = None
    if s["role"] == "elder":
        p = db.get_profile(uid) or {}
        if p.get("ward_id"):
            _shared["ward_uid"] = p["ward_id"]      # D18：认出老人 → 当前病房跟着他
    elif s["role"] == "ward":
        _shared["ward_uid"] = uid or ""
    return get_principal(slot)


def login_admin(password: str | None = None, slot: str = "kiosk",
                ttl_s: int | None = None) -> dict:
    """口令门开启时校验口令；门关着时直接放行（D13）。失败返回 ok=False，冷却由路由层计。"""
    auth = db.get_admin_auth()
    if auth["required"]:
        if not password or not db.verify_admin_password(password):
            audit.log("session_login_fail", slot=slot)
            return {"ok": False, "error": "口令错误"}
        source = "password"
        ttl = int(ttl_s if ttl_s is not None else _settings().get("admin_session_ttl_s", 300))
        until = _now_ts() + max(1, ttl)
    else:
        # 口令门关着：无需口令直接进，且**不再自动降权**（已经没有保护可降）
        source = "auth_disabled"
        ttl, until = None, None
    _slot(slot).update({"role": "admin", "source": source, "until": until})
    audit.log("session_login", slot=slot, source=source, ttl_s=ttl)
    out = dict(get_principal(slot))
    out["ok"] = True
    out["ttl_remain"] = ttl_remain(slot)
    return out


def logout(slot: str = "kiosk") -> dict:
    """退出管理层 → 该槽回落（非管理员槽的 role 会自动跟着当前主体走）。"""
    _slot(slot).update({"role": "ward", "source": "logout", "until": None})
    audit.log("session_logout", slot=slot)
    return get_principal(slot)


def _expire_if_needed(slot: str) -> None:
    s = _slot(slot)
    if s["role"] == "admin" and s["until"] and _now_ts() >= s["until"]:
        s.update({"role": "ward", "source": "expired", "until": None})
        audit.log("session_expired", slot=slot)


def get_principal(slot: str = "kiosk") -> dict:
    """当前主体（含角色）。**业务代码只认这个函数**，绝不读前端传来的角色（R1）。"""
    _expire_if_needed(slot)
    s = _slot(slot)
    if s["role"] != "admin":
        # 非管理员槽位的角色永远跟着全局主体走，避免"role=ward 但 uid 是某位老人"
        s["role"] = derive_role(_shared["uid"])
    return {
        "uid": ADMIN_UID if s["role"] == "admin" else _shared["uid"],
        "role": s["role"],
        "locked": bool(_shared["locked"]),
        "source": s["source"],
        "slot": slot,
        "until": s["until"],
        "ward_uid": _shared["ward_uid"],
    }


def ttl_remain(slot: str = "kiosk") -> int | None:
    _expire_if_needed(slot)
    s = _slot(slot)
    if s["role"] != "admin" or not s["until"]:
        return None
    return max(0, int(s["until"] - _now_ts()))
