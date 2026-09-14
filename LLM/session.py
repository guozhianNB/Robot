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

from . import bus
from . import db
from . import locator
from . import log as audit
from . import zonegeo

SLOTS = ("kiosk", "admin")
ADMIN_UID = "admin"

# 全局共享：当前主体（uid/locked）与"当前病房"（ward_uid/manual_until）
_shared: dict = {"uid": "", "locked": False, "ward_uid": "", "manual_until": 0.0}
# 按槽位：角色与提权状态
_state: dict[str, dict] = {}


def _now_ts() -> float:
    return time.monotonic()


def reset_for_test() -> None:
    """单测用：清空全部会话状态与病房判定缓存。"""
    _shared.update({"uid": "", "locked": False, "ward_uid": "", "manual_until": 0.0})
    _state.clear()
    _candidate.clear()
    _map_cache.update({"name": "", "at": float("-inf"), "reason": ""})


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
        bus.publish("session_expired", slot=slot)   # 前端靠它提示"管理员会话已超时"（规格 §4.1）


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
    """改口令。

    **只要已经设过口令（库里有哈希），就必须校验旧口令——与口令门开关无关。** 否则会出现
    这条可利用链：关掉口令门（此时人人可免口令进 admin）→ 攻击者把口令改成自己的 →
    管理员把门开回来（只踢会话、不回滚口令）→ 攻击者仍持有口令。首启态（还没设过口令）
    才允许无旧口令直接设，否则第一个管理员根本没法建立口令。
    """
    auth = db.get_admin_auth()
    if auth["hash"] and not db.verify_admin_password(old or ""):
        audit.log("admin_password_changed", ok=False, reason="old_mismatch")
        return {"ok": False, "error": "旧口令错误"}
    if len(new or "") < 4:
        audit.log("admin_password_changed", ok=False, reason="too_short")
        return {"ok": False, "error": "新口令太短（至少 4 位）"}
    db.set_admin_password(new)
    audit.log("admin_password_changed", ok=True)
    return {"ok": True}


def set_admin_auth(required: bool) -> dict:
    """开关口令门（D13）。

    重新**开启**时把两个槽的 admin 会话立即作废：否则"先关掉门进来、再开回门"的人会
    一直留在管理员态，等于门白开了。作废是**降权不是换人**（不动 `_shared` 的主体）。
    """
    required = bool(required)
    db.set_admin_auth_required(required)
    audit.log("admin_auth_changed", required=required)
    if required:
        for slot in SLOTS:
            if _slot(slot)["role"] == "admin":
                _slot(slot).update({"role": "ward", "until": None, "source": "auth_reenabled"})
                audit.log("session_logout", slot=slot, source="auth_reenabled")
    return {"required": required}


def ensure_admin_password() -> str | None:
    """首启：库里没有口令（哈希与盐都不全）就生成 6 位随机口令，返回明文（调用方打印 + 审计）。

    判据是 `hash and salt` 而不是只看 `hash`：半写坏状态（哈希在、盐没了）下必须能自愈，
    否则 `verify_admin_password` 恒 False、admin 面永久进不去。
    """
    auth = db.get_admin_auth()
    if auth["hash"] and auth["salt"]:
        return None
    import secrets
    pw = "".join(secrets.choice("0123456789") for _ in range(6))
    db.set_admin_password(pw)
    return pw


# ---------------------------------------------------------------------------
# 当前病房 + 位置自动切换（D17/D18）
# ---------------------------------------------------------------------------
_MAP_CACHE_S = 30.0                     # 「车在跑哪张图」的缓存时长
# 为什么是 30s 而不是 10s：`locator.current_map()` 除了可能 `drain(1.0)`，还要列地图 + **逐图**
# 做 `mapserver.map_info()`（`MAPS_IO=ssh` 下每张图一次远端 stat，未命中缓存时还要拉整幅 PGM
# 并做全图像素统计）——这是**热路径**上的重活。病房切换晚 30 秒知道完全可接受（车也不会瞬移）。
_map_cache: dict = {"name": "", "at": float("-inf"), "reason": ""}
#                                                     ^^^^^^^^^^^^ 哨兵用 -inf 而不是 0.0：
#                     `_now_ts()` 是 monotonic（开机计时），用 0.0 会让"开机后头 N 秒"被判成"缓存未过期"
_candidate: dict[str, int] = {}         # ward_uid -> 连续命中次数（防抖）


def running_map_name() -> tuple[str, str]:
    """车**此刻在跑哪张图** → `(地图名, 不可用原因)`；认不出返回 `("", reason)`。

    为什么不能拿病房自己存的 `ward_map` 当判据：位姿是**当前地图坐标系**里的数，而三张图
    坐标系不通用；若拿位姿去比"旧图上的病房多边形"，会**静默切错病房**（D17 明令禁止）。

    取值口径（`settings.ward_map_source`）：
      * `"auto"`（默认）：`locator.current_map()` 的 `/map` 四项指纹反查，唯一命中才认；
      * `"setting"`：用 `settings.current_map`（一期语义="下次启导航用哪张图"）——
        给"指纹识别不可用但现场自己知道在跑哪张图"留一条手动阀。
    结果缓存 `_MAP_CACHE_S` 秒：`current_map()` 会列地图 + 逐图读元数据，`MAPS_IO=ssh` 下很贵。
    """
    st = _settings()
    now = _now_ts()
    if now - _map_cache["at"] < _MAP_CACHE_S:
        return _map_cache["name"], _map_cache["reason"]

    name, reason = "", ""
    if str(st.get("ward_map_source") or "auto") == "setting":
        name = str(st.get("current_map") or "").strip()
        if not name:
            reason = "no_current_map"
    else:
        try:
            got = locator.current_map()
        except Exception:               # noqa: BLE001  识别失败=不可用，绝不炸 tick
            got = {}
        if got.get("source") == "map_topic" and got.get("name"):
            name = str(got["name"])
        else:
            reason = "map_unknown"
    _map_cache.update({"name": name, "at": now, "reason": reason})
    return name, reason


def _zone_hit(ward: dict, map_name: str, pose: dict) -> bool:
    """当前位姿是否落在这间病房关联的区域里（几何判定在 zonegeo）。

    降级（fail-safe）：病房没关联区域 / 关联的是别的地图 / 缓存里查不到该区域 → False。
    """
    zone_uid = str(ward.get("ward_zone") or "")
    if not zone_uid or str(ward.get("ward_map") or "") != map_name:
        return False
    zone = db.get_zone(zone_uid, map_name)
    if not zone:
        return False
    return zonegeo.zone_hit(zone, float(pose["x"]), float(pose["y"]))


def current_ward() -> str:
    """当前病房（集体层主体）的 uid；空串 = 还不知道在哪个病房。"""
    return _shared.get("ward_uid") or ""


def manual_set_ward(ward_uid: str) -> dict:
    """手动切病房：带 `manual_until`，期间位置判定不覆盖（D18）。

    只在 kiosk 为集体层且未锁定时改会话主体——正在老人私聊时手动切病房只更新背景变量。
    """
    _shared["ward_uid"] = ward_uid
    _shared["manual_until"] = _now_ts() + float(_settings().get("manual_override_sec", 600))
    _candidate.clear()               # 手动覆盖 = 判定重新开始，不许复用旧计数（否则到期后一个采样就切）
    if _holds_session():
        _shared["uid"] = ward_uid
    audit.log("ward_change", source="manual", ward=ward_uid)
    bus.publish("ward_changed", uid=ward_uid, action="manual")
    return get_principal("kiosk")


def _holds_session() -> bool:
    """kiosk 槽现在是不是"集体层 + 未锁定"——只有这种状态才允许位置/手动切换**真的改主体**。

    **必须用有效角色（`get_principal()`），不能用 `_slot("kiosk")["role"]`**：槽位里那个原始
    role 会被 `_expire_if_needed()`（admin TTL 到期直接写 `"ward"`）与 `set_admin_auth()` 改写
    而**不重算**，于是出现"派生角色=elder、槽位 role=ward"的分歧态；而 `tick()` 的第一步恰好就是
    `_expire_if_needed()`。拿原始 role 判定，就会把正在私聊的老人静默换成病房主体（D18 明令不抢私聊）。
    """
    return get_principal("kiosk")["role"] == "ward" and not _shared["locked"]


def autoswitch_state() -> dict:
    """给前端/诊断用：位置自动切换**当前为什么没生效**（判定顺序与 `_ward_tick` 保持一致）。

    本函数挂在车前屏的轮询端点上，而 `locator.get_pose()` / `running_map_name()`
    可能走网络/SSH：任何意外异常都不许把它变成 500。故整体兜底降级为
    `{"enabled": False, "reason": "error"}` + 审计，绝不抛穿。
    """
    try:
        st = _settings()
        if not st.get("ward_autoswitch_enabled", True):
            return {"enabled": False, "reason": "disabled"}
        if _now_ts() < (_shared.get("manual_until") or 0.0):
            return {"enabled": False, "reason": "manual_override"}
        pose = locator.get_pose()
        if pose is None or pose.get("x") is None:
            return {"enabled": False, "reason": "no_pose"}
        name, reason = running_map_name()
        if not name:
            return {"enabled": False, "reason": reason or "map_unknown"}
        if not _holds_session():
            return {"enabled": True, "reason": "holding_session"}
        return {"enabled": True, "reason": "active"}
    except Exception as e:                  # noqa: BLE001
        audit.log("session_tick_error", action="autoswitch_state", error=str(e))
        return {"enabled": False, "reason": "error"}


def _ward_tick() -> None:
    """一次位置判定（由 tick() 每秒调用一次）。

    每个 early-return 都要把 `_candidate` 清掉："连续 N 次"必须是**连续**的——否则手动覆盖
    10 分钟到期后，只要再有**一个**瞬时跳变的采样就能凑满计数、把病房改掉（防抖等于失效）。
    """
    st = _settings()
    if not st.get("ward_autoswitch_enabled", True):
        _candidate.clear()
        return
    if _now_ts() < (_shared.get("manual_until") or 0.0):
        _candidate.clear()
        return
    pose = locator.get_pose()
    if pose is None or pose.get("x") is None:
        _candidate.clear()
        return
    map_name, _why = running_map_name()
    if not map_name:
        _candidate.clear()
        return

    hit = ""
    for w in db.list_wards():               # 只读本地缓存（不走 maptags，见模块头性能约束）
        if _zone_hit(w, map_name, pose):
            hit = w["uid"]
            break
    if not hit:                             # 离开病房区（走廊/未知区域）→ 不切
        _candidate.clear()
        return
    if _shared.get("ward_uid") == hit:      # 已经在这个病房：不累加、不广播（否则计数无上限膨胀）
        _candidate.clear()
        return

    _candidate[hit] = _candidate.get(hit, 0) + 1
    for k in list(_candidate):
        if k != hit:
            _candidate.pop(k, None)         # 换目标就重新数
    debounce = max(1, int(st.get("ward_switch_debounce", 3)))
    if _candidate[hit] < debounce:
        return

    _shared["ward_uid"] = hit
    audit.log("ward_change", source="location", ward=hit)
    bus.publish("ward_changed", uid=hit, action="location")
    # D18：只在集体层且未锁定时才真正改会话主体（正在私聊/已锁定 → 只更新背景变量）
    if _holds_session():
        _shared["uid"] = hit
        _slot("kiosk")["source"] = "location"


def tick() -> None:
    """定时（server 里每秒一次）：管理员 TTL 到期降权 + 病房位置自动切换。

    整体兜底 try/except：这个函数将来挂在定时线程（或 server 的每秒任务）上，任何意外异常都会
    让这一轮乃至整条 tick 死掉 —— 连带"自动切换"与"admin TTL 降权"一起永久失效。降级原则：
    出问题只记审计，绝不中断。
    """
    for slot in SLOTS:
        try:
            _expire_if_needed(slot)
        except Exception as e:              # noqa: BLE001
            audit.log("session_tick_error", action="expire", slot=slot, error=str(e))
    try:
        _ward_tick()
    except Exception as e:                  # noqa: BLE001
        audit.log("session_tick_error", action="ward", error=str(e))
