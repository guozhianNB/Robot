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
    """取槽位状态。**槽位名非法直接抛 ValueError**——绝不静默回落到 kiosk：那会让
    `X-Surface: TABLET` 这种笔误把"管理台的口令登录"写到车前屏上，顺带把车前屏提权，
    而且返回体/审计里的 slot 还是那个错名（追溯性也被破坏）。"""
    if slot not in SLOTS:
        raise ValueError(f"未知槽位 {slot!r}（只允许 {SLOTS}）")
    return _state.setdefault(slot, {"role": "ward", "source": "default", "until": None})


def derive_role(uid: str | None) -> str:
    """uid → 角色（**R1 的唯一入口**）。权威依据是 `profiles.kind`，不靠 uid 前缀。

    `uid == "admin"` 短路返回 `"admin"` 只表达"这个主体属于管理员层"；**拿到 admin 权限
    必须经 `login_admin()`（口令）**——`set_subject()` 会拒绝任何把槽位角色变成 admin 的调用，
    所以这里的短路不会变成免口令后门。
    """
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
    """切换会话主体：写 uid，role 由 uid 推导（R1）。

    **提权只走 `login_admin()`（口令）**：主体切换一律不许把槽位角色变成 admin ——
    否则 `POST /api/session/user {"uid": "admin"}` 就是一个免口令后门（R1 明令
    "要拿 admin 只能走 /api/session/login"。CORS 全开 + 该端点只拒绝 role 字段，
    这条后门是真实可利用的）。命中就拒绝并落审计，当前主体保持不变。
    """
    s = _slot(slot)
    _expire_if_needed(slot)          # 先让"已过期但还没被 tick 到"的 admin 会话降权，
                                     # 否则守卫会吞掉一次声纹认人并写一条误导性审计
    if s["role"] == "admin" and source != "manual":
        # D8 提权只升不降：管理员会话期间声纹认人不改主体，只留痕（事件名按规格 §4.1 = voice_spk）
        audit.log("voice_spk", action="ignored_in_admin", uid=uid, slot=slot)
        return get_principal(slot)

    role = derive_role(uid)
    if role == "admin":
        # 免口令提权通道：拒绝，保持当前主体（fail-closed）
        audit.log("policy_deny", action="admin_grant_denied", uid=uid, slot=slot, source=source)
        return get_principal(slot)

    _shared["uid"] = uid or ""
    _shared["locked"] = bool(locked)
    s["role"] = role
    s["source"] = source
    s["until"] = None
    if role == "elder":
        p = db.get_profile(uid) or {}
        if p.get("ward_id"):
            _shared["ward_uid"] = p["ward_id"]      # D18：认出老人 → 当前病房跟着他
    elif role == "ward" and db.get_profile_kind(uid) == "ward":
        # 只有"真的是病房档案"的 uid 才更新当前病房；未知 uid 一律 fail-closed 判 ward，
        # 但不许把它写进"当前病房"（否则一个幽灵 uid 会把当前病房顶掉）
        _shared["ward_uid"] = uid
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


# ---------------------------------------------------------------------------
# 口令维护（D12/D13）：可改、可整体关闭、首启自动生成 6 位随机口令
# ---------------------------------------------------------------------------
def change_admin_password(old: str, new: str) -> dict:
    """改口令（需旧口令）。口令门关着时允许直接设新口令（此时无旧口令可验）。"""
    auth = db.get_admin_auth()
    if auth["required"] and not db.verify_admin_password(old or ""):
        audit.log("admin_password_changed", ok=False, reason="old_mismatch")
        return {"ok": False, "error": "旧口令错误"}
    if len(new or "") < 4:
        return {"ok": False, "error": "新口令太短（至少 4 位）"}
    db.set_admin_password(new)
    audit.log("admin_password_changed", ok=True)
    return {"ok": True}


def set_admin_auth(required: bool) -> dict:
    """开关口令门（D13）。

    重新**开启**时把两个槽的 admin 会话立即作废：否则"先关掉门进来、再开回门"的人会
    一直留在管理员态，等于门白开了。
    """
    required = bool(required)
    db.set_admin_auth_required(required)
    audit.log("admin_auth_changed", required=required)
    if required:
        for slot in SLOTS:
            if _slot(slot)["role"] == "admin":
                _slot(slot).update({"role": "ward", "until": None, "source": "auth_reenabled"})
    return {"required": required}


def ensure_admin_password() -> str | None:
    """首启：库里没有口令哈希就生成 6 位随机口令，返回明文（调用方打印 + 审计，D12）。"""
    if db.get_admin_auth()["hash"]:
        return None
    import secrets
    pw = "".join(secrets.choice("0123456789") for _ in range(6))
    db.set_admin_password(pw)
    return pw
