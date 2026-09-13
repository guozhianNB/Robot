# 分层用户体系（P0：用户系统）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。
> **规格：** `docs/superpowers/specs/2026-09-14-layered-user-roles-design.md`（commit `98a7e6c`）

**目标：** 让系统同时存在**管理层 / 集体层 / 老人层**三个层级，各自有独立提示词与权限；集体层以"病房用户"落地并**按小车位置自动切换**；管理员口令可改、可关。

**架构：** 新增 `LLM/session.py`（双槽会话主体 + 角色推导 + 当前病房自动切换）与 `LLM/policy.py`（角色策略包）；`profiles` 表扩 `kind/ward_id/zone_json` 承载病房用户；位姿由新增 `LLM/locator.py` 经 **rosbridge（websocket，非 MCP）** 读取并**可注入假位姿**以便无 ROS 环境测试；提示词在 `LLM/prompt.md`（共用 base）之上拼 `LLM/prompt/<role>.md`。

**技术栈：** Python 3 / FastAPI / SQLite / pytest / websocket-client（已有依赖）；前端 Vue3 + Vite + TS（pnpm monorepo）。

**红线（贯穿全程）：** R1 前端 role 不可信；R2 fail-closed（未知=集体层最小能力）；R3 急停/呼救永远放行；R4 医疗写入红线不受角色影响；R5 层级上下文单向（集体→老人可读、反向与跨病房不可读）。

**测试命令（Windows 本机，项目根目录）：**
```
.venv\Scripts\python.exe -m pytest LLM/tests -q
```

**测试隔离铁律：** 一律沿用仓内既有模式（`LLM/tests/test_memory_v4.py` 的 `d` fixture）——**只改 `db.DB_PATH` 指向临时库并在结束时还原**，不要 `importlib.reload(db)`（reload 会把 `DB_PATH` 重置回真实路径，测试会污染 `LLM/data/brain.db`）。

---

### 任务 1：`profiles` 表扩展与数据层支撑

**文件：**
- 修改：`LLM/db.py`（`init_db()` 第 111-127 行区块；`profiles` 区块第 136-200 行；`settings` 区块第 977-1015 行）
- 测试：`LLM/tests/test_ward_db.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_ward_db.py
# -*- coding: utf-8 -*-
"""病房用户与管理员口令数据层测试（临时库隔离，沿用 test_memory_v4.py 模式）。"""
import os
import tempfile

import pytest

from LLM import db


@pytest.fixture()
def d():
    from LLM import db as _db
    tmp = tempfile.mkdtemp()
    old = _db.DB_PATH
    _db.DB_PATH = os.path.join(tmp, "t.db")
    _db.init_db()
    yield _db
    _db.DB_PATH = old


def test_existing_profile_defaults_to_elder(d):
    d.upsert_profile("elder_001", name="张奶奶")
    assert d.get_profile_kind("elder_001") == "elder"
    assert d.get_profile("elder_001")["ward_id"] == ""


def test_upsert_ward_and_list_wards(d):
    d.upsert_ward("ward_101", name="101 病房",
                  zone={"frame": "map", "type": "circle", "x": 1.0, "y": 2.0, "r": 3.0})
    wards = d.list_profiles(kind="ward")
    assert [w["uid"] for w in wards] == ["ward_101"]
    assert d.get_profile_kind("ward_101") == "ward"
    assert d.get_zone("ward_101")["r"] == 3.0


def test_set_elder_ward(d):
    d.upsert_profile("elder_101_1", name="李爷爷")
    d.set_profile_ward("elder_101_1", "ward_101")
    assert d.get_profile("elder_101_1")["ward_id"] == "ward_101"


def test_float_setting_roundtrip(d):
    d.set_settings({"ward_zone_default_r": 3.5})
    assert d.get_settings()["ward_zone_default_r"] == 3.5


def test_admin_password_hash_roundtrip(d):
    assert d.get_admin_auth() == {"required": True, "hash": "", "salt": ""}
    d.set_admin_password("246810")
    assert d.verify_admin_password("246810") is True
    assert d.verify_admin_password("000000") is False
    d.set_admin_auth_required(False)
    assert d.get_admin_auth()["required"] is False
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_ward_db.py -q`
预期：FAIL（`AttributeError: module 'LLM.db' has no attribute 'get_profile_kind'`）

- [ ] **步骤 3：实现最少代码**

在 `init_db()` 的 `_ensure_columns(conn, "profiles", {...})` 字典里追加三列：

```python
            _ensure_columns(conn, "profiles", {
                "gender": "gender TEXT DEFAULT ''",
                "birthday": "birthday TEXT DEFAULT ''",
                # 分层用户体系：kind=elder|ward；ward_id=老人所属病房；zone_json=病房地图区域
                "kind": "kind TEXT DEFAULT 'elder'",
                "ward_id": "ward_id TEXT DEFAULT ''",
                "zone_json": "zone_json TEXT DEFAULT ''",
            })
```

在 `settings` 区块前新增（`profiles` 区块的 `list_profiles` 需支持 `kind` 过滤）：

```python
def list_profiles(kind: str | None = None) -> list[dict]:
    conn = _conn()
    try:
        sql = "SELECT * FROM profiles"
        args: tuple = ()
        if kind:
            sql += " WHERE kind=?"
            args = (kind,)
        rows = conn.execute(sql + " ORDER BY uid", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["profile"] = json.loads(d.pop("profile_json") or "{}")
            d["preferences"] = json.loads(d.pop("preferences_json") or "{}")
            out.append(d)
        return out
    finally:
        conn.close()


def get_profile_kind(uid: str) -> str:
    """唯一权威的角色判定依据（R1）：admin 不在 profiles 里 → 返回 ''。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT kind FROM profiles WHERE uid=?", (uid,)).fetchone()
        return (row["kind"] or "elder") if row else ""
    finally:
        conn.close()


def upsert_ward(uid: str, name: str = "", zone: dict | None = None) -> dict:
    upsert_profile(uid, name=name, kind="ward",
                   zone_json=json.dumps(zone or {}, ensure_ascii=False))
    return get_profile(uid) or {}


def set_profile_ward(uid: str, ward_id: str) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE profiles SET ward_id=?, updated_at=? WHERE uid=?",
                         (ward_id, now_iso(), uid))
            conn.commit()
        finally:
            conn.close()


def get_zone(ward_uid: str) -> dict:
    p = get_profile(ward_uid)
    return json.loads(p.get("zone_json") or "{}") if p else {}


def list_wards_with_zone() -> list[dict]:
    out = []
    for w in list_profiles(kind="ward"):
        zone = json.loads(w.get("zone_json") or "{}")
        if zone:
            out.append({"uid": w["uid"], "name": w.get("name", ""), "zone": zone})
    return out
```

`upsert_profile` 签名追加 `kind="elder"`、`zone_json=""` 两个参数，并在 INSERT/UPDATE 的列与值里带上（`kind` 用 `excluded.kind`，注意 `ON CONFLICT` 更新时**不要**覆盖已有 kind——用 `kind=COALESCE(profiles.kind, excluded.kind)`）。

管理员口令（放在 `settings` 区块末尾，**不走 `set_settings`**——它只接受 `DEFAULT_SETTINGS|TOOL_DEFAULTS` 的白名单 key）：

```python
# ---- 管理员口令（PBKDF2，绝不落明文）----
def _get_setting_raw(key: str, default: str = "") -> str:
    conn = _conn()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def _set_setting_raw(key: str, value: str) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute("INSERT INTO settings (key,value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            conn.commit()
        finally:
            conn.close()


def _hash_pw(pw: str, salt: str) -> str:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def get_admin_auth() -> dict:
    return {
        "required": _get_setting_raw("admin_auth_required", "1").lower() in ("1", "true", "yes"),
        "hash": _get_setting_raw("admin_password_hash"),
        "salt": _get_setting_raw("admin_password_salt"),
    }


def set_admin_password(pw: str) -> None:
    import os
    salt = os.urandom(16).hex()
    _set_setting_raw("admin_password_salt", salt)
    _set_setting_raw("admin_password_hash", _hash_pw(pw, salt))


def verify_admin_password(pw: str) -> bool:
    a = get_admin_auth()
    if not a["hash"] or not a["salt"]:
        return False
    return _hash_pw(pw, a["salt"]) == a["hash"]


def set_admin_auth_required(required: bool) -> None:
    _set_setting_raw("admin_auth_required", "1" if required else "0")
```

`get_settings()` 的类型转换分支里补 float（否则 `ward_zone_default_r` 读回来是字符串）：

```python
            elif isinstance(out.get(r["key"]), float):
                try:
                    v = float(v)
                except ValueError:
                    v = out[r["key"]]
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_ward_db.py -q`
预期：5 passed

- [ ] **步骤 5：跑全量回归 + Commit**

```bash
.venv\Scripts\python.exe -m pytest LLM/tests -q
git add LLM/db.py LLM/tests/test_ward_db.py
git commit -m "feat(llm): profiles 扩 kind/ward_id/zone_json + 病房与管理员口令数据层"
```

---

### 任务 2：新增配置项

**文件：**
- 修改：`LLM/conf.py`（`DEFAULT_SETTINGS`，第 18-41 行）
- 测试：`LLM/tests/test_settings_roles.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_settings_roles.py
# -*- coding: utf-8 -*-
from LLM.conf import DEFAULT_SETTINGS


def test_role_settings_have_defaults():
    assert DEFAULT_SETTINGS["admin_auth_required"] is True
    assert DEFAULT_SETTINGS["admin_session_ttl_s"] == 300
    assert DEFAULT_SETTINGS["ward_context_window"] == 10
    assert DEFAULT_SETTINGS["ward_switch_debounce"] == 3
    assert DEFAULT_SETTINGS["ward_zone_default_r"] == 3.0
    assert DEFAULT_SETTINGS["manual_override_sec"] == 600
    assert DEFAULT_SETTINGS["rosbridge_url"] == "ws://100.65.82.93:9090"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_settings_roles.py -q`
预期：FAIL（KeyError: 'admin_auth_required'）

- [ ] **步骤 3：实现**

在 `DEFAULT_SETTINGS` 里追加：

```python
    # ---- 分层用户体系（2026-09-14，规格 docs/superpowers/specs/2026-09-14-layered-user-roles-design.md）----
    "admin_auth_required": True,    # 管理员口令门开关（可关：详见规格 D13）
    "admin_session_ttl_s": 300,     # 管理员提权无操作自动降权秒数
    "ward_context_window": 10,      # 集体层上下文注入条数（老人可见本病房的这 N 条）
    "rosbridge_url": "ws://100.65.82.93:9090",  # 位置源（空=停用病房自动切换）
    "ward_switch_debounce": 3,      # 自动切病房防抖：连续 N 次 tick 同病房才认
    "ward_zone_default_r": 3.0,     # 病房区域默认半径（米）
    "manual_override_sec": 600,     # 手动切病房后位置判定不覆盖的秒数
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_settings_roles.py LLM/tests/test_backend.py -q`
预期：全 passed

- [ ] **步骤 5：Commit**

```bash
git add LLM/conf.py LLM/tests/test_settings_roles.py
git commit -m "feat(llm): 补分层用户体系配置项（口令门/TTL/病房上下文/位置源）"
```

---

### 任务 3：`policy.py` 角色策略包

**文件：**
- 创建：`LLM/policy.py`
- 测试：`LLM/tests/test_policy_roles.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_policy_roles.py
# -*- coding: utf-8 -*-
from LLM import policy


def test_policy_keys_cover_three_roles():
    assert set(policy.POLICY_DEFAULTS) == {"admin", "ward", "elder"}


def test_ward_has_no_personal_scope_and_reads_own_ward():
    p = policy.POLICY_DEFAULTS["ward"]
    assert p["data_scope"] == "none"      # 不注入任何老人档案/私人记忆
    assert p["ward_context"] is True      # 但读本病房集体上下文
    assert p["allowed_tools"] == []       # 集体层无车控工具


def test_elder_reads_self_plus_ward_context():
    p = policy.POLICY_DEFAULTS["elder"]
    assert p["data_scope"] == "self"
    assert p["ward_context"] is True


def test_admin_reads_all_without_ward_context():
    p = policy.POLICY_DEFAULTS["admin"]
    assert p["data_scope"] == "all"
    assert p["allowed_tools"] is None     # None = 全部


def test_unknown_role_falls_back_to_ward():
    assert policy.role_policy("nope") == policy.POLICY_DEFAULTS["ward"]
    assert policy.role_policy(None) == policy.POLICY_DEFAULTS["ward"]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_policy_roles.py -q`
预期：FAIL（ModuleNotFoundError: No module named 'LLM.policy'）

- [ ] **步骤 3：实现**

```python
# -*- coding: utf-8 -*-
r"""
角色策略包（分层用户体系）：每个层级 = 一套策略 = 提示词片段 + 工具白名单 + 数据可见范围 + 语音策略。

规格：docs/superpowers/specs/2026-09-14-layered-user-roles-design.md
红线：R1 前端 role 不可信（role 只由 session.derive_role 推导）
      R2 fail-closed（未知角色一律按集体层最小能力）
设计取舍：本模块**只放纯数据 + 纯函数**，不做 IO；会话状态在 session.py。
"""
from pathlib import Path
from .conf import BASE_DIR

PROMPT_DIR = BASE_DIR / "LLM" / "prompt"

_RISK = {"destinations": "none", "risk_confirm": False, "voice": "wake"}

POLICY_DEFAULTS: dict[str, dict] = {
    # ---- 集体层：一屋子人，读得到本病房的公开对话，但读不到任何个人档案 ----
    "ward": {
        "prompt_file": PROMPT_DIR / "ward.md",
        "allowed_tools": [],
        "data_scope": "none",
        "ward_context": True,
        **_RISK,
    },
    # ---- 老人层：本人档案 + 本病房集体上下文（只读、单向）----
    "elder": {
        "prompt_file": PROMPT_DIR / "elder.md",
        "allowed_tools": ["robot_status", "robot_stop"],   # 安全动作永远在列（R3）
        "data_scope": "self",
        "ward_context": True,
        **_RISK,
    },
    # ---- 管理层：全部能力 ----
    "admin": {
        "prompt_file": PROMPT_DIR / "admin.md",
        "allowed_tools": None,
        "data_scope": "all",
        "ward_context": False,
        **_RISK,
    },
}


def role_policy(role: str | None) -> dict:
    """取角色策略；未知/None → 集体层（R2 fail-closed）。"""
    return POLICY_DEFAULTS.get(role or "", POLICY_DEFAULTS["ward"])
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_policy_roles.py -q`
预期：5 passed

- [ ] **步骤 5：Commit**

```bash
git add LLM/policy.py LLM/tests/test_policy_roles.py
git commit -m "feat(llm): 新增 policy.py 角色策略包（三层策略 + fail-closed 兜底）"
```

---

### 任务 4：`session.py` —— 双槽主体与角色推导

**文件：**
- 创建：`LLM/session.py`
- 测试：`LLM/tests/test_session_roles.py`（新建；**不要**覆盖已有的 `LLM/tests/test_session.py`）

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_session_roles.py
# -*- coding: utf-8 -*-
"""会话主体与角色推导测试（临时库隔离，沿用 test_memory_v4.py 模式）。"""
import os
import tempfile

import pytest

from LLM import db
from LLM import session


@pytest.fixture()
def d():
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    session.reset_for_test()
    db.upsert_profile("elder_101_1", name="李爷爷")
    db.upsert_ward("ward_101", name="101 病房")
    db.set_profile_ward("elder_101_1", "ward_101")
    db.set_admin_password("111111")
    yield db
    db.DB_PATH = old


def test_derive_role_by_kind(d):
    assert session.derive_role("elder_101_1") == "elder"
    assert session.derive_role("ward_101") == "ward"
    assert session.derive_role("admin") == "admin"
    assert session.derive_role("ghost_9") == "ward"     # R2 fail-closed


def test_derive_role_not_fooled_by_uid_prefix(d):
    d.upsert_profile("ward_fake", name="其实是老人")     # kind 默认 elder
    assert session.derive_role("ward_fake") == "elder"   # 权威判定查 profiles.kind


def test_two_slots_are_isolated(d):
    session.set_subject("ward_101", locked=False, slot="kiosk")
    session.login_admin("111111", slot="admin")
    assert session.get_principal("kiosk")["role"] == "ward"
    assert session.get_principal("admin")["role"] == "admin"
    assert session.get_principal("kiosk")["uid"] == "ward_101"


def test_admin_never_downgraded_by_voiceprint(d):
    session.login_admin("111111", slot="kiosk")
    session.set_subject("elder_101_1", locked=False, slot="kiosk", source="voiceprint")
    p = session.get_principal("kiosk")
    assert p["role"] == "admin" and p["uid"] == "admin"   # D8 提权只升不降


def test_wrong_password_does_not_elevate(d):
    assert session.login_admin("000000", slot="kiosk")["ok"] is False
    assert session.get_principal("kiosk")["role"] == "ward"


def test_recognizing_elder_follows_his_ward(d):
    d.upsert_ward("ward_102", name="102 病房")
    d.upsert_profile("elder_102_1", name="王奶奶")
    d.set_profile_ward("elder_102_1", "ward_102")
    session.set_subject("ward_101", slot="kiosk")
    session.set_subject("elder_102_1", slot="kiosk", source="voiceprint")
    p = session.get_principal("kiosk")
    assert p["role"] == "elder" and p["ward_uid"] == "ward_102"   # D18：当前病房跟随老人


def test_elder_without_ward_keeps_current(d):
    d.upsert_profile("elder_999", name="未分配病房的老人")
    session.set_subject("ward_101", slot="kiosk")
    session.set_subject("elder_999", slot="kiosk", source="voiceprint")
    assert session.get_principal("kiosk")["ward_uid"] == "ward_101"


def test_ttl_expiry_falls_back_to_ward(d, monkeypatch):
    session.set_subject("ward_101", locked=False, slot="kiosk")
    session.login_admin("111111", slot="kiosk", ttl_s=1)
    base = session._now_ts()
    monkeypatch.setattr(session.time, "monotonic", lambda: base + 5)
    p = session.get_principal("kiosk")
    assert p["role"] == "ward" and p["uid"] == "ward_101"  # 回落集体层
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_session_roles.py -q`
预期：FAIL（No module named 'LLM.session'）

- [ ] **步骤 3：实现**

```python
# -*- coding: utf-8 -*-
r"""
分层用户体系的会话层：**谁在说话**（角色）与**现在在哪个病房**（集体层归属）。

规格：docs/superpowers/specs/2026-09-14-layered-user-roles-design.md §3/§4
两条槽位：kiosk（车前设备，承载语音链路）与 admin（管理台）。role 按槽位隔离，
uid/locked 全端共享（沿用 2026-08-27 前端多端设计 D11）。

红线：
  R1 前端传的 role 不可信 —— 角色只由 derive_role() 从 uid 推导；
  R2 fail-closed —— 未知 uid / 未知角色一律按集体层最小能力；
  R3 急停呼救永远放行（本模块不做拦截）。
降级：rosbridge 拿不到位姿时，位置自动切换自行停用（locator 返回 None），
      会话保持当前病房不变，绝不影响对话链路。
"""
import time

from . import db
from . import log as audit
from .conf import DEFAULT_SETTINGS

SLOTS = ("kiosk", "admin")
ADMIN_UID = "admin"

# slot -> {"uid": str, "role": str, "locked": bool, "source": str, "until": float|None, "ward_uid": str, "manual_until": float}
_state: dict[str, dict] = {}


def _now_ts() -> float:
    return time.monotonic()


def reset_for_test() -> None:
    """单测用：清空全部槽位状态。"""
    _state.clear()


def _slot(slot: str) -> dict:
    if slot not in SLOTS:
        slot = "kiosk"
    return _state.setdefault(slot, {
        "uid": "", "role": "ward", "locked": False, "source": "default",
        "until": None, "ward_uid": "", "manual_until": 0.0,
    })


def derive_role(uid: str | None) -> str:
    """uid → 角色（R1 唯一入口）。权威依据是 profiles.kind，不靠 uid 前缀。"""
    if not uid:
        return "ward"                      # R2
    if uid == ADMIN_UID:
        return "admin"
    kind = db.get_profile_kind(uid)
    if kind == "ward":
        return "ward"
    if kind == "elder":
        return "elder"
    return "ward"                          # R2 未知 uid


def _settings() -> dict:
    return db.get_settings()


def set_subject(uid: str, locked: bool = False, slot: str = "kiosk", source: str = "manual") -> dict:
    """切换会话主体：写 uid，role 由 uid 推导（R1）。"""
    s = _slot(slot)
    if s["role"] == "admin" and source != "manual":
        # D8 提权只升不降：管理员会话期间声纹认人不改主体，只留痕
        audit.log("session_login", action="voiceprint_ignored_in_admin", uid=uid, slot=slot)
        return get_principal(slot)
    s["uid"] = uid
    s["role"] = derive_role(uid)
    s["locked"] = bool(locked)
    s["source"] = source
    s["until"] = None
    if s["role"] == "elder":
        p = db.get_profile(uid) or {}
        if p.get("ward_id"):
            s["ward_uid"] = p["ward_id"]   # D18：识别到老人 → 当前病房跟随
    elif s["role"] == "ward":
        s["ward_uid"] = uid
    return get_principal(slot)


def login_admin(password: str | None = None, slot: str = "kiosk", ttl_s: int | None = None) -> dict:
    """口令门开启时校验口令；关闭时直接放行（D13）。失败返回 ok=False，由路由层计冷却。"""
    auth = db.get_admin_auth()
    if auth["required"]:
        if not password or not db.verify_admin_password(password):
            audit.log("session_login_fail", slot=slot)
            return {"ok": False, "error": "口令错误"}
        source = "password"
    else:
        source = "auth_disabled"
    ttl = int(ttl_s if ttl_s is not None else _settings().get("admin_session_ttl_s", 300))
    s = _slot(slot)
    s["uid"] = ADMIN_UID
    s["role"] = "admin"
    s["locked"] = False
    s["source"] = source
    s["until"] = _now_ts() + ttl
    audit.log("session_login", slot=slot, source=source, ttl_s=ttl)
    return {"ok": True, **get_principal(slot)}


def logout(slot: str = "kiosk") -> dict:
    """退出管理层 → 回落集体层（保持当前病房）。"""
    s = _slot(slot)
    uid = s.get("ward_uid") or ""
    s.update({"uid": uid, "role": "ward", "locked": False, "source": "manual", "until": None})
    audit.log("session_logout", slot=slot)
    return get_principal(slot)


def _expire_if_needed(slot: str) -> None:
    s = _slot(slot)
    if s["role"] == "admin" and s["until"] and _now_ts() >= s["until"]:
        s.update({"role": "ward", "uid": s.get("ward_uid") or "", "locked": False,
                  "source": "expired", "until": None})
        audit.log("session_expired", slot=slot)


def get_principal(slot: str = "kiosk") -> dict:
    _expire_if_needed(slot)
    s = dict(_slot(slot))
    s["slot"] = slot
    if s["role"] == "admin":
        s["uid"] = ADMIN_UID
    return s


def ttl_remain(slot: str = "kiosk") -> int | None:
    s = _slot(slot)
    if s["role"] != "admin" or not s["until"]:
        return None
    return max(0, int(s["until"] - _now_ts()))
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_session_roles.py -q`
预期：8 passed

- [ ] **步骤 5：Commit**

```bash
git add LLM/session.py LLM/tests/test_session_roles.py
git commit -m "feat(llm): session.py 双槽会话主体 + derive_role + 管理员 TTL"
```

---

### 任务 5：管理员口令的改 / 关 / 开

**文件：**
- 修改：`LLM/session.py`（追加函数）
- 测试：`LLM/tests/test_session_roles.py`（追加用例）

- [ ] **步骤 1：编写失败的测试**（追加到 `test_session_roles.py`）

```python
def test_change_password_requires_old(tmp_path, monkeypatch):
    d = _setup(tmp_path, monkeypatch)
    d.set_admin_password("111111")
    assert session.change_admin_password("wrong", "222222")["ok"] is False
    assert session.change_admin_password("111111", "222222")["ok"] is True
    assert d.verify_admin_password("222222") is True


def test_toggle_auth_required(tmp_path, monkeypatch):
    d = _setup(tmp_path, monkeypatch)
    d.set_admin_password("111111")
    assert session.set_admin_auth(True)["required"] is True
    assert session.set_admin_auth(False)["required"] is False
    r = session.login_admin(password=None, slot="kiosk")
    assert r["ok"] is True and r["source"] == "auth_disabled"   # 口令门关闭 → 直接放行


def test_first_boot_generates_random_password(tmp_path, monkeypatch):
    d = _setup(tmp_path, monkeypatch)
    pw = session.ensure_admin_password()
    assert isinstance(pw, str) and len(pw) == 6
    assert d.verify_admin_password(pw) is True
    assert session.ensure_admin_password() is None   # 已存在 → 不再生成
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_session_roles.py -q`
预期：FAIL（AttributeError: module 'LLM.session' has no attribute 'change_admin_password'）

- [ ] **步骤 3：实现**（追加到 `session.py` 末尾）

```python
def change_admin_password(old: str, new: str) -> dict:
    """管理员改口令（需旧口令；口令门关闭时允许直接设新口令）。"""
    auth = db.get_admin_auth()
    if auth["required"] and not db.verify_admin_password(old):
        return {"ok": False, "error": "旧口令错误"}
    if len(new or "") < 4:
        return {"ok": False, "error": "新口令太短（至少 4 位）"}
    db.set_admin_password(new)
    audit.log("admin_password_changed", slot="admin")
    return {"ok": True}


def set_admin_auth(required: bool) -> dict:
    """开关管理员口令门（D13）。"""
    db.set_admin_auth_required(bool(required))
    audit.log("admin_auth_changed", required=bool(required))
    if not required:
        _slot("admin").update({"role": "admin", "uid": ADMIN_UID, "source": "auth_disabled",
                               "until": None})
    return {"required": bool(required)}


def ensure_admin_password() -> str | None:
    """首次启动：库里没有口令哈希就生成 6 位随机口令，返回明文（由调用方打印+审计）。"""
    auth = db.get_admin_auth()
    if auth["hash"]:
        return None
    import secrets
    pw = "".join(secrets.choice("0123456789") for _ in range(6))
    db.set_admin_password(pw)
    return pw
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_session_roles.py -q`
预期：8 passed

- [ ] **步骤 5：Commit**

```bash
git add LLM/session.py LLM/tests/test_session_roles.py
git commit -m "feat(llm): 管理员口令可改/可开关 + 首启随机口令"
```

---

### 任务 6：`locator.py` 位置源（rosbridge，可注入假位姿）

**文件：**
- 创建：`LLM/locator.py`
- 测试：`LLM/tests/test_locator.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_locator.py
# -*- coding: utf-8 -*-
from LLM import locator


def test_inject_and_read_pose():
    locator.set_pose_for_test(1.5, -2.0, 0.3)
    p = locator.get_pose()
    assert (round(p["x"], 2), round(p["y"], 2)) == (1.5, -2.0)


def test_no_pose_returns_none():
    locator.set_pose_for_test(None)
    assert locator.get_pose() is None


def test_disabled_when_url_empty():
    locator.set_pose_for_test(1.0, 1.0, 0.0)
    assert locator.available({"rosbridge_url": ""}) is False
    assert locator.available({"rosbridge_url": "ws://x:9090"}) is True


def test_parse_amcl_pose_message():
    msg = {"msg": {"pose": {"pose": {"position": {"x": 3.0, "y": 4.0},
                                     "orientation": {"z": 0.0, "w": 1.0}}}}}
    p = locator._parse_amcl_pose(msg)
    assert (p["x"], p["y"]) == (3.0, 4.0)
    assert round(p["yaw"], 3) == 0.0
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_locator.py -q`
预期：FAIL（No module named 'LLM.locator'）

- [ ] **步骤 3：实现**

```python
# -*- coding: utf-8 -*-
r"""
位置源（分层用户体系 §4.5）：给"现在在哪个病房"提供小车位姿。

为什么用 rosbridge 而不是 MCP：用户 2026-09-14 明确"先不管 mcp"，而 rosbridge 是
板卡上本来就跑的 websocket（`~/tools/nav_screen.sh lat`，端口 9090），订阅 /amcl_pose
即可拿到 map 系位姿；依赖 websocket-client 是**已有依赖**（asr_cloud.py 在用）。

降级（系统稳健性）：连不上/超时/未定位 → get_pose() 返回 None，位置自动切换随之停用，
绝不抛异常影响对话；另提供 set_pose_for_test() 供无 ROS 环境单测与验收注入假位姿。
"""
import json
import math
import threading
import time

try:                      # 可选依赖：缺失时整体降级
    import websocket      # websocket-client
    _WS_AVAILABLE = True
except Exception:
    _WS_AVAILABLE = False

_POSE_TTL_S = 10.0        # 位姿新鲜度：超过这么久没更新视为"拿不到"
_pose: dict | None = None
_pose_ts: float = 0.0
_lock = threading.Lock()
_started = False


def set_pose_for_test(x: float | None, y: float = 0.0, yaw: float = 0.0) -> None:
    """注入假位姿（x=None 表示"拿不到位姿"）。仅测试与诊断用。"""
    global _pose, _pose_ts
    with _lock:
        _pose = None if x is None else {"x": float(x), "y": float(y), "yaw": float(yaw)}
        _pose_ts = time.monotonic()


def get_pose() -> dict | None:
    with _lock:
        if _pose is None:
            return None
        if time.monotonic() - _pose_ts > _POSE_TTL_S:
            return None
        return dict(_pose)


def available(settings: dict) -> bool:
    """位置源是否可用（配置了 rosbridge 地址）。"""
    return bool((settings or {}).get("rosbridge_url"))


def _yaw_from_quat(z: float, w: float) -> float:
    return 2.0 * math.atan2(z, w)


def _parse_amcl_pose(msg: dict) -> dict | None:
    try:
        pose = msg["msg"]["pose"]["pose"]
        pos, ori = pose["position"], pose["orientation"]
        return {"x": float(pos["x"]), "y": float(pos["y"]),
                "yaw": _yaw_from_quat(float(ori["z"]), float(ori["w"]))}
    except (KeyError, TypeError, ValueError):
        return None


def start(settings: dict) -> None:
    """启动后台订阅线程（幂等）。不可用时静默跳过（降级）。"""
    global _started
    if _started or not _WS_AVAILABLE or not available(settings):
        return
    _started = True
    url = settings["rosbridge_url"]

    def _run():
        sub = {"op": "subscribe", "topic": "/amcl_pose", "type": "geometry_msgs/PoseWithCovarianceStamped"}
        while True:
            try:
                ws = websocket.create_connection(url, timeout=5)
                ws.send(json.dumps(sub))
                while True:
                    p = _parse_amcl_pose(json.loads(ws.recv()))
                    if p:
                        global _pose, _pose_ts
                        with _lock:
                            _pose, _pose_ts = p, time.monotonic()
            except Exception:
                time.sleep(3)      # 连不上就退避重试；不抛异常、不写审计噪音

    threading.Thread(target=_run, name="locator", daemon=True).start()
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_locator.py -q`
预期：4 passed

- [ ] **步骤 5：Commit**

```bash
git add LLM/locator.py LLM/tests/test_locator.py
git commit -m "feat(llm): locator.py 位置源（rosbridge /amcl_pose + 假位姿注入 + 降级）"
```

---

### 任务 7：当前病房与自动切换（防抖 / 手动覆盖 / 不抢私聊）

**文件：**
- 修改：`LLM/session.py`
- 测试：`LLM/tests/test_ward_autoswitch.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_ward_autoswitch.py
# -*- coding: utf-8 -*-
"""病房位置自动切换测试：全部注入假位姿，不需要 ROS / rosbridge。"""
import os
import tempfile

import pytest

from LLM import db
from LLM import locator
from LLM import session

_SETTINGS = {"rosbridge_url": "ws://x:9090", "ward_switch_debounce": 3,
             "manual_override_sec": 600, "admin_session_ttl_s": 300}


@pytest.fixture()
def d(monkeypatch):
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    session.reset_for_test()
    db.upsert_ward("ward_101", name="101", zone={"frame": "map", "type": "circle",
                                                "x": 0.0, "y": 0.0, "r": 3.0})
    db.upsert_ward("ward_102", name="102", zone={"frame": "map", "type": "circle",
                                                "x": 20.0, "y": 0.0, "r": 3.0})
    db.upsert_profile("elder_101_1", name="李爷爷")
    db.set_profile_ward("elder_101_1", "ward_101")
    monkeypatch.setattr(session, "_settings", lambda: dict(_SETTINGS))
    session.set_subject("ward_101", slot="kiosk")
    locator.set_pose_for_test(0.0, 0.0, 0.0)
    yield db
    db.DB_PATH = old


def test_debounce_requires_repeated_ticks(d):
    locator.set_pose_for_test(20.0, 0.0, 0.0)      # 进入 102
    session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"   # 第 1 次不切
    session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"   # 第 2 次不切
    session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_102"   # 第 3 次才切


def test_leaving_zone_does_not_switch(d):
    locator.set_pose_for_test(99.0, 99.0, 0.0)     # 走廊：不在任何 zone
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"


def test_does_not_steal_elder_private_chat(d):
    session.set_subject("elder_101_1", slot="kiosk", source="voiceprint")
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    p = session.get_principal("kiosk")
    assert p["role"] == "elder" and p["uid"] == "elder_101_1"    # 私聊不被打断
    assert session.current_ward() == "ward_102"                  # 但当前病房已更新


def test_no_pose_disables_autoswitch(d):
    locator.set_pose_for_test(None)
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"
    assert session.autoswitch_state()["reason"] == "no_pose"


def test_manual_override_blocks_location(d):
    session.manual_set_ward("ward_101")
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.current_ward() == "ward_101"
    assert session.autoswitch_state()["reason"] == "manual_override"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_ward_autoswitch.py -q`
预期：FAIL（AttributeError: module 'LLM.session' has no attribute 'tick'）

- [ ] **步骤 3：实现**（追加到 `session.py`；`import math` 加到文件头）

```python
from . import locator

_candidate: dict[str, int] = {}      # ward_uid -> 连续命中次数（防抖）


def _zone_hit(ward_uid: str, pose: dict) -> bool:
    z = db.get_zone(ward_uid)
    if not z or z.get("type") != "circle":
        return False
    dx, dy = pose["x"] - float(z.get("x", 0)), pose["y"] - float(z.get("y", 0))
    return (dx * dx + dy * dy) ** 0.5 <= float(z.get("r", 0))


def current_ward() -> str:
    return _slot("kiosk").get("ward_uid") or ""


def manual_set_ward(ward_uid: str) -> dict:
    """手动切病房：带 manual_override_until，期间位置判定不覆盖（D18）。"""
    s = _slot("kiosk")
    s["ward_uid"] = ward_uid
    s["manual_until"] = _now_ts() + float(_settings().get("manual_override_sec", 600))
    if s["role"] == "ward":
        s["uid"] = ward_uid
    audit.log("ward_change", source="manual", ward=ward_uid)
    return get_principal("kiosk")


def autoswitch_state() -> dict:
    """给前端/诊断用：位置自动切换当前为何没生效。"""
    s = _slot("kiosk")
    st = _settings()
    if not locator.available(st):
        return {"enabled": False, "reason": "no_url"}
    if locator.get_pose() is None:
        return {"enabled": False, "reason": "no_pose"}
    if _now_ts() < (s.get("manual_until") or 0):
        return {"enabled": False, "reason": "manual_override"}
    if s["role"] == "elder" or s["locked"]:
        return {"enabled": True, "reason": "holding_session"}
    return {"enabled": True, "reason": "active"}


def tick() -> None:
    """定时（每 ~1s 调一次）：管理员 TTL + 病房位置自动切换。"""
    for slot in SLOTS:
        _expire_if_needed(slot)

    s = _slot("kiosk")
    st = _settings()
    if not locator.available(st):
        return
    if _now_ts() < (s.get("manual_until") or 0):
        return
    pose = locator.get_pose()
    if pose is None:
        return

    debounce = int(st.get("ward_switch_debounce", 3))
    hit = ""
    for w in db.list_wards_with_zone():
        if _zone_hit(w["uid"], pose):
            hit = w["uid"]
            break
    if not hit:                       # 离开病房区（走廊）→ 不切
        _candidate.clear()
        return
    _candidate[hit] = _candidate.get(hit, 0) + 1
    for k in list(_candidate):
        if k != hit:
            _candidate.pop(k, None)
    if _candidate[hit] < debounce or s.get("ward_uid") == hit:
        return

    s["ward_uid"] = hit
    audit.log("ward_change", source="location", ward=hit)
    # D18：只在集体层且未锁定时才真正改会话主体（不抢私聊）
    if s["role"] == "ward" and not s["locked"]:
        s["uid"] = hit
        s["source"] = "location"
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_ward_autoswitch.py -q`
预期：5 passed

- [ ] **步骤 5：全量回归 + Commit**

```bash
.venv\Scripts\python.exe -m pytest LLM/tests -q
git add LLM/session.py LLM/tests/test_ward_autoswitch.py
git commit -m "feat(llm): 病房位置自动切换（防抖/手动覆盖/不抢私聊/拿不到位姿即停用）"
```

---

### 任务 8：提示词分层与集体层上下文注入

**文件：**
- 创建：`LLM/prompt/ward.md`、`LLM/prompt/elder.md`、`LLM/prompt/admin.md`
- 修改：`LLM/chat.py`（`_load_prompt_base` 之后新增 `_load_role_prompt`；`build_system` 第 195-214 行；`build_messages` 第 217-224 行；`chat_stream` 第 306 行）
- 测试：`LLM/tests/test_prompt_layers.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_prompt_layers.py
# -*- coding: utf-8 -*-
"""三层提示词分层与集体上下文单向注入测试。"""
import os
import tempfile

import pytest

from LLM import db
from LLM import chat


@pytest.fixture()
def d(monkeypatch):
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    db.upsert_ward("ward_101", name="101 病房")
    db.upsert_profile("elder_101_1", name="张奶奶")
    db.set_profile_ward("elder_101_1", "ward_101")
    for i in range(3):
        db.add_history("ward_101", "user", f"病房消息{i}", uid="ward_101")
    # 离线：不让 RAG 真去调 embedding（本测试只验装配，不验召回）
    monkeypatch.setattr(chat.rag, "recall_v3", lambda uid, q: {"context": ""})
    yield db
    db.DB_PATH = old


def _principal(role, uid):
    return {"role": role, "uid": uid, "slot": "kiosk", "ward_uid": "ward_101",
            "source": "manual", "locked": False}


def test_role_fragment_loaded(d):
    sys_p = chat.build_system("ward_101", {}, "", principal=_principal("ward", "ward_101"))
    assert "病房里" in sys_p            # ward.md 的正文出现


def test_ward_prompt_has_no_personal_profile(d):
    d.upsert_profile("elder_101_1", name="张奶奶", notes="糖尿病")
    sys_p = chat.build_system("ward_101", {}, "", principal=_principal("ward", "ward_101"))
    assert "张奶奶" not in sys_p and "糖尿病" not in sys_p


def test_elder_sees_own_ward_context(d):
    sys_p = chat.build_system("elder_101_1", {}, "", principal=_principal("elder", "elder_101_1"))
    assert "病房消息2" in sys_p


def test_ward_does_not_see_elder_private_chat(d):
    d.add_history("elder_101_1", "user", "我昨晚没睡好", uid="elder_101_1")
    sys_p = chat.build_system("ward_101", {}, "", principal=_principal("ward", "ward_101"))
    assert "我昨晚没睡好" not in sys_p


def test_missing_role_file_degrades(d, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(chat, "_ROLE_PROMPT_DIR_OVERRIDE", Path("/nonexistent"), raising=False)
    sys_p = chat.build_system("ward_101", {}, "", principal=_principal("ward", "ward_101"))
    assert "小护" in sys_p              # base 仍在，没抛异常
```

> 为让最后一个用例能生效，`_load_role_prompt` 实现里用**模块级可覆盖目录**（见步骤 3 的代码）：
> `_ROLE_PROMPT_DIR_OVERRIDE = None`（测试 monkeypatch 成不存在目录即可验证降级）。

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_prompt_layers.py -q`
预期：FAIL（TypeError: build_system() got an unexpected keyword argument 'principal'）

- [ ] **步骤 3：实现**

新建 `LLM/prompt/ward.md`（正文，直接生效）：

```markdown
你现在是在**一个病房里**跟"大家"说话，不是跟某一位老人私聊。屋里可能有好几个人，也可能有人刚进来、有人正躺着。

1. 说给大家听：打招呼、报时、提醒吃药时间、公布消息（"今天下午三点体检"），有事说事，一句话讲完。
2. 别问谁是谁，也别替谁说他的私事。有人问"我的药""我是谁"这类个人问题 → 回他"这个我得单独跟本人说"。
3. 认出了具体是哪位老人，你就会被切到他的私聊里——那时候再聊他的事，现在不用打听。
4. 你不认识屋里的人，所以别用"张奶奶"这种称呼点名，用"您""大家"。
5. 安全红线照旧：有人说不舒服、胸口疼、摔倒 → 先安抚，再说"我这就去通知护士"。
```

新建 `LLM/prompt/elder.md`：

```markdown
你现在是跟一位具体的老人单独说话（不是在病房里跟大家广播）。

1. 称呼他，聊他的事：吃什么、睡得好不好、想谁了、身上哪儿不舒服。
2. 〔病房里刚说过的事〕那一段，是他也在场听过的，可以顺着接着聊，不用从头再说一遍。
3. 他私下跟你说的话**只留在你俩之间**——不要拿到病房里当众说，也不要主动提"您刚才说的大家都知道了"。
4. 说话短、说人话、别背书，红线照旧：医疗只读、危险信号先安抚再叫护士。
```

新建 `LLM/prompt/admin.md`：

```markdown
你现在是跟**管理员**说话，不是跟老人。

1. 直接给结论和数字：状态、参数、结果，一句话报完，不用寒暄、不用哄、不用卖萌。
2. 可以用术语和缩写（位姿、AMCL、/cmd_vel、TTL 等），管理员看得懂。
3. 管理员问"现在系统是什么状态"时，按事实回答：哪个层级在用、当前病房、口令门是否开着、位置源是否可用。
4. 危险动作执行完要回报结果（成功/失败/为什么失败），失败要给出下一步建议。
5. 老人隐私红线不因身份而取消：医疗信息仍只读。
```

在 `chat.py` 里：`from .policy import role_policy`（新增 import），并追加：

```python
_prompt_role_warned: set[str] = set()
_ROLE_PROMPT_DIR_OVERRIDE = None      # 测试用：指向不存在的目录以验证降级


def _load_role_prompt(role: str) -> str:
    """读 LLM/prompt/<role>.md 角色片段；缺失 → 空串 + 告警一次（不阻断对话）。"""
    base_dir = _ROLE_PROMPT_DIR_OVERRIDE or role_policy(role)["prompt_file"].parent
    path = base_dir / f"{role or 'ward'}.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        if role not in _prompt_role_warned:
            _prompt_role_warned.add(role)
            audit.log("chat", action="prompt_role_missing", role=role, file=str(path))
        return ""


def _ward_context(ward_uid: str, limit: int) -> str:
    """本病房集体层最近 N 条（R5 单向：只从这里往外读，绝不把私聊灌进来）。"""
    if not ward_uid:
        return ""
    rows = db.load_history(ward_uid, limit=limit)
    if not rows:
        return ""
    lines = [f"{r['role']}: {r['content']}" for r in rows if (r.get("content") or "").strip()]
    if not lines:
        return ""
    return ("【病房里刚说过的事（这位老人在场听过；只读参考，别当私事追问）】\n"
            + "\n".join(lines))
```

`build_system` 改为（保持 `uid` 老签名兼容，新增 `principal`）：

```python
def build_system(uid: str, settings: dict, query: str = "", principal: dict | None = None) -> str:
    from . import session as session_mod
    p = principal or session_mod.get_principal("kiosk")
    pol = role_policy(p.get("role"))
    scope = pol["data_scope"]

    parts = [_load_prompt_base()]
    role_txt = _load_role_prompt(p.get("role"))
    if role_txt:
        parts.append("\n" + role_txt)

    if scope == "self":                      # 老人层：本人档案/记忆/语录
        recall_ctx = _recall_cached(uid, query) if query else rag.recall_v3(uid, "")["context"]
        parts.append(f"\n【我了解到的关于这位老人的信息（来自档案/记忆，可能不全或过时，仅供参考）】\n{recall_ctx}")
    elif scope == "all" and query:           # 管理层：默认不注入老人记忆
        pass

    if pol["ward_context"]:                  # 老人层：本病房集体上下文（只读）
        ward_txt = _ward_context(p.get("ward_uid", "") if p.get("role") == "elder" else p.get("uid", ""),
                                 int(settings.get("ward_context_window", 10)))
        if ward_txt:
            parts.append("\n" + ward_txt)

    if scope == "self":
        summary = db.get_summary(uid)
        if summary:
            parts.append(f"\n【更早对话的历史摘要】\n{summary}")
        expr_hint = _expression_hint(uid)
        if expr_hint:
            parts.append("\n" + expr_hint)

    parts.append(
        "\n【当前时间】" + time.strftime("%Y-%m-%d %H:%M (%A)") +
        "\n如果老人问'现在几点/今天星期几'，按上面的时间回答。"
    )
    return "\n".join(parts)
```

`build_messages` / `chat_stream` 加 `principal` 透传：

```python
def build_messages(uid: str, user_text: str, thinking_on: bool, settings: dict,
                   principal: dict | None = None) -> list[dict]:
    history = db.load_history(uid, limit=HISTORY_WINDOW)
    system = build_system(uid, settings, query=_build_query(user_text, history), principal=principal)
    ...

def chat_stream(client, model: str, uid: str, user_text: str, thinking: str, settings: dict,
                principal: dict | None = None):
    ...
    messages = build_messages(uid, user_text, thinking_on, settings, principal=principal)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_prompt_layers.py LLM/tests/test_backend.py -q`
预期：全 passed

- [ ] **步骤 5：Commit**

```bash
git add LLM/prompt LLM/chat.py LLM/tests/test_prompt_layers.py
git commit -m "feat(llm): 三层提示词分层 + 集体层上下文单向注入"
```

---

### 任务 9：工具角色白名单（闸门 2）

**文件：**
- 修改：`LLM/tools.py`（`tool()` 装饰器第 31-44 行；`effective_tools` 第 83-94 行；`run_tool` 第 118 行起）
- 修改：`LLM/chat.py`（第 323 行 `effective_tools(settings)`、第 392 行 `run_tool(name, args)`）
- 测试：`LLM/tests/test_policy_tools.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_policy_tools.py
# -*- coding: utf-8 -*-
from LLM import tools


def _p(role):
    return {"role": role, "uid": "u", "slot": "kiosk", "ward_uid": "", "locked": False}


def test_ward_gets_no_global_tools():
    names = [t["function"]["name"] for t in tools.effective_tools({}, _p("ward"))]
    assert names == []


def test_elder_only_gets_safety_tools():
    names = [t["function"]["name"] for t in tools.effective_tools({}, _p("elder"))]
    assert set(names) <= {"robot_status", "robot_stop"}


def test_unknown_role_falls_back_to_ward():
    assert tools.effective_tools({}, {"role": "??"}) == []


def test_run_tool_denies_out_of_whitelist():
    res = tools.run_tool("__nope__", {}, _p("ward"))
    assert res["ok"] is False and "不允许" in res["error"]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_policy_tools.py -q`
预期：FAIL（TypeError: effective_tools() takes 1 positional argument but 2 were given）

- [ ] **步骤 3：实现**

`tools.py` 里 `@tool(...)` 增加 `roles=None` 参数（`None`=不限角色，保持现有工具行为兼容）并存入 registry；`effective_tools` 改为：

```python
def effective_tools(settings: dict, principal: dict | None = None) -> list[dict]:
    """按 per-tool 开关 ∩ 角色白名单过滤（闸门 2）。principal 缺省 → 集体层（R2）。"""
    from .policy import role_policy
    role = (principal or {}).get("role")
    allow = role_policy(role)["allowed_tools"]      # None = 全部；[] = 无
    local_names = set(_TOOL_REGISTRY)
    out = []
    for name, reg in _TOOL_REGISTRY.items():
        if not settings.get(f"{name}_enabled", reg["enabled"]):
            continue
        if allow is not None and name not in allow:
            continue
        out.append(reg["schema"])
    if settings.get("mcp_enabled"):
        mcp_roles = {"elder", "admin"}              # MCP 默认从严：集体层不给
        if allow is None or (role in mcp_roles):
            out += _mcp_tools(settings)             # 既有 MCP 合并逻辑，函数名按现状
    return out
```

`run_tool` 改为在入口做二次校验：

```python
def run_tool(name: str, args: dict, principal: dict | None = None) -> dict:
    """统一分发（闸门 2 二次校验）：角色白名单不放行的工具一律拒绝并审计。"""
    from .policy import role_policy
    from . import log as audit
    reg = _TOOL_REGISTRY.get(name)
    allow = role_policy((principal or {}).get("role"))["allowed_tools"]
    if allow is not None and name not in allow:
        audit.log("policy_deny", tool=name, role=(principal or {}).get("role"),
                  reason="out_of_role_whitelist")
        return {"ok": False, "error": f"当前身份不允许调用工具 {name}"}
    if reg is None:
        return _run_mcp_tool(name, args)            # 既有 MCP 分支（按现状保留）
    return _run_fn(reg["fn"], args)
```

（若现有 `run_tool` 内部结构不同，**等义改写**：在函数最前面插入白名单校验，其余逻辑不动。）

`chat.py` 两处调用改为带 principal：

```python
    tools = tool_mod.effective_tools(settings, principal)
    ...
                    result = tool_mod.run_tool(name, args, principal)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests -q`
预期：全 passed（既有 83 项不回归）

- [ ] **步骤 5：Commit**

```bash
git add LLM/tools.py LLM/chat.py LLM/tests/test_policy_tools.py
git commit -m "feat(llm): 工具角色白名单（闸门 2，含 run_tool 二次校验与审计）"
```

---

### 任务 10：集体层不沉淀记忆 + 语音链路接入角色

**文件：**
- 修改：`LLM/memory.py`（`note_turn()`）
- 修改：`LLM/voice_api.py`（`set_session_uid`/`get_session_uid` 第 51-85 行转发到 `session`；`_stream_fn` 第 103-108 行传 principal）
- 修改：`LLM/voice/worker.py`（`_handle_speech` 第 341-365 行）
- 测试：`LLM/tests/test_ward_memory.py`（新建）、`LLM/tests/test_worker_roles.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_ward_memory.py
# -*- coding: utf-8 -*-
"""集体层对话不沉淀成任何老人的记忆（规格 §5.3）。"""
import os
import tempfile

import pytest

from LLM import db
from LLM import memory


@pytest.fixture()
def d():
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    db.upsert_ward("ward_101", name="101 病房")
    yield db
    db.DB_PATH = old


def test_ward_turn_is_never_buffered(d):
    memory.note_turn("ward_101", "小车", "明天九点体检", None, "", {}, role="ward")
    assert memory._pending_turns.get("ward_101") is None     # 连缓冲都不进
    assert db.list_memories("ward_101") == []


def test_elder_turn_is_still_buffered(d):
    memory.note_turn("elder_101_1", "我", "我不爱吃甜的", None, "",
                     {"memory_consolidation_enabled": False}, role="elder")
    assert memory._pending_turns.get("elder_101_1")          # 老人层照旧进缓冲
```

```python
# LLM/tests/test_worker_roles.py
# -*- coding: utf-8 -*-
import os
import tempfile

import pytest

from LLM import db
from LLM import session
from LLM import voice_api


@pytest.fixture()
def d():
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    session.reset_for_test()
    db.upsert_ward("ward_101", name="101")
    yield db
    db.DB_PATH = old


def test_voice_api_forwards_to_session(d):
    res = voice_api.set_session_uid("ward_101", False)
    assert res["uid"] == "ward_101" and res["role"] == "ward"
    assert voice_api.get_session_uid()["uid"] == "ward_101"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_ward_memory.py LLM/tests/test_worker_roles.py -q`
预期：FAIL（`note_turn() got an unexpected keyword argument 'role'`）

- [ ] **步骤 3：实现**

`memory.py::note_turn` 签名加 `role: str = "elder"`，函数体开头：

```python
def note_turn(uid: str, user_text: str, assistant_text: str, client, model: str,
              settings: dict, role: str = "elder"):
    if role == "ward":
        # 集体层对话只作病房公开上下文，不沉淀成任何老人的记忆（规格 §5.3）
        return
```

**调用点同步**：`LLM/server.py:117` 的 `rag.note_turn(uid, user_text, assistant, client, MODEL, settings)`
改为 `rag.note_turn(uid, user_text, assistant, client, MODEL, settings, role=principal["role"])`
（`principal` 由任务 11 在同一处注入）。

`voice_api.py`：把 `_session_uid/_session_locked` 的持有权交给 `session`（保留同名函数，调用点不变）：

```python
def set_session_uid(uid: str, locked: bool) -> dict:
    """转发到 session（分层用户体系后，会话主体与角色统一由 session 持有）。"""
    from . import session as session_mod
    if not locked:
        # 解锁 = 恢复声纹自动判定（沿用 I-1 语义：清残留，回到集体层）
        return session_mod.set_subject(session_mod.current_ward() or uid, False, "kiosk", "manual")
    return session_mod.set_subject(uid, True, "kiosk", "manual")


def get_session_uid() -> dict:
    from . import session as session_mod
    p = session_mod.get_principal("kiosk")
    return {"uid": p["uid"], "locked": p["locked"], "role": p["role"],
            "slot": p["slot"], "source": p["source"], "ward_uid": p["ward_uid"],
            "ttl_remain": session_mod.ttl_remain("kiosk"),
            "autoswitch": session_mod.autoswitch_state()}
```

`_stream_fn` 传 principal：

```python
    def _fn(uid, text):
        settings = db.get_settings()
        from . import session as session_mod
        return chat.chat_stream(client, model, uid, text, "auto", settings,
                                principal=session_mod.get_principal("kiosk"))
```

`worker.py::_handle_speech` 按规格 §4.3 改：

```python
        principal = session.get_principal("kiosk")
        if principal["role"] == "admin":
            uid = principal["uid"]                      # 管理员：语音按 admin 走，且不降权
        else:
            vote = ...
            recognized = id_mod.effective_uid(vote, self.current_uid, self.locked_uid)
            uid = recognized or (principal["uid"] or session.current_ward())
            session.set_subject(uid, bool(self.locked_uid), "kiosk", "voiceprint")
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests -q`
预期：全 passed

- [ ] **步骤 5：Commit**

```bash
git add LLM/memory.py LLM/voice_api.py LLM/voice/worker.py LLM/tests/test_ward_memory.py LLM/tests/test_worker_roles.py
git commit -m "feat(llm): 集体层不沉淀记忆 + 语音链路接入角色（管理员不降权）"
```

---

### 任务 11：REST 接口（登录/口令/病房/策略）+ 业务接口取 principal

**文件：**
- 修改：`LLM/server.py`（`lifespan`；会话状态区块第 658-670 行；`/api/chat` 第 252 行）
- 测试：`LLM/tests/test_server_roles_routes.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_server_roles_routes.py
# -*- coding: utf-8 -*-
"""会话/病房/策略路由测试（临时库隔离，直接改 db.DB_PATH，不 reload）。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def c():
    from LLM import db
    from LLM import session
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    session.reset_for_test()
    db.upsert_ward("ward_101", name="101 病房")
    db.set_admin_password("111111")
    import LLM.server as server
    client = TestClient(server.app)       # 不进 lifespan（不启动语音/轮询）
    yield client
    db.DB_PATH = old


def test_session_user_rejects_role_field(c):
    r = c.post("/api/session/user", json={"uid": "ward_101", "locked": False, "role": "admin"})
    assert r.status_code == 400            # R1


def test_login_and_logout_flow(c):
    assert c.post("/api/session/login", json={"password": "bad"},
                  headers={"X-Surface": "admin"}).json()["ok"] is False
    ok = c.post("/api/session/login", json={"password": "111111"},
                headers={"X-Surface": "admin"}).json()
    assert ok["ok"] is True and ok["role"] == "admin"
    assert c.get("/api/session/user", headers={"X-Surface": "admin"}).json()["role"] == "admin"
    assert c.get("/api/session/user", headers={"X-Surface": "kiosk"}).json()["role"] == "ward"
    c.post("/api/session/logout", headers={"X-Surface": "admin"})
    assert c.get("/api/session/user", headers={"X-Surface": "admin"}).json()["role"] == "ward"


def test_wards_crud_and_admin_auth_toggle(c):
    c.post("/api/session/login", json={"password": "111111"}, headers={"X-Surface": "admin"})
    assert c.get("/api/wards", headers={"X-Surface": "admin"}).json()["wards"][0]["uid"] == "ward_101"
    r = c.post("/api/wards", json={"uid": "ward_102", "name": "102 病房"},
               headers={"X-Surface": "admin"})
    assert r.json()["ok"] is True
    r = c.post("/api/session/admin-auth", json={"required": False},
               headers={"X-Surface": "admin"})
    assert r.json()["required"] is False


def test_non_admin_cannot_manage_wards(c):
    r = c.post("/api/wards", json={"uid": "ward_999", "name": "越权"},
               headers={"X-Surface": "kiosk"})
    assert r.status_code == 403
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_server_roles_routes.py -q`
预期：FAIL（404 / 422）

- [ ] **步骤 3：实现**

`server.py` 顶部加导入与 surface 依赖、Pydantic 模型（放在其他 `*In` 模型旁）：

```python
from fastapi import Header, HTTPException
from . import locator
from . import session
from . import log as audit      # 若文件内已有该行则不要重复导入

_SURFACES = ("kiosk", "admin")


def _surface(x_surface: str = Header(default="kiosk")) -> str:
    """端槽位：kiosk（车前/语音）| admin（管理台）。缺省按 kiosk。"""
    return x_surface if x_surface in _SURFACES else "kiosk"


def _public_policy(pol: dict) -> dict:
    """把策略包转成可 JSON 序列化的形式（Path → str）。"""
    return {k: (str(v) if isinstance(v, Path) else v) for k, v in pol.items()}


class SessionUserIn(BaseModel):        # 既有模型：加 role 只为显式拒绝它（R1）
    uid: str
    locked: bool = False
    role: str | None = None


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
    zone: dict | None = None
```

会话状态区块替换为：

```python
@app.get("/api/session/user")
async def session_user_get(x_surface: str = Header(default="kiosk")):
    """当前会话主体（uid/role/锁定/TTL/当前病房），按请求槽位返回角色。"""
    slot = _surface(x_surface)
    p = session.get_principal(slot)
    return {**p, "ttl_remain": session.ttl_remain(slot),
            "auth_required": db.get_admin_auth()["required"],
            "autoswitch": session.autoswitch_state()}


@app.post("/api/session/user")
async def session_user_set(s: SessionUserIn, x_surface: str = Header(default="kiosk")):
    """切换会话主体：只接受 uid/locked —— 传 role 一律 400（R1）。"""
    if s.role:
        raise HTTPException(status_code=400, detail="role 不可由前端指定（R1）")
    slot = _surface(x_surface)
    res = session.set_subject(s.uid, s.locked, slot, "manual")
    bus.publish("user_changed", uid=res["uid"], role=res["role"], slot=slot,
                ward_uid=res.get("ward_uid", ""), locked=res["locked"], source="manual")
    return res


@app.post("/api/session/login")
async def session_login(body: LoginIn | None = None, x_surface: str = Header(default="kiosk")):
    slot = _surface(x_surface)
    res = session.login_admin((body.password if body else None), slot=slot)
    if res.get("ok"):
        bus.publish("user_changed", uid=res["uid"], role=res["role"], slot=slot,
                    source=res["source"])
    return res


@app.post("/api/session/logout")
async def session_logout(x_surface: str = Header(default="kiosk")):
    slot = _surface(x_surface)
    res = session.logout(slot)
    bus.publish("user_changed", uid=res["uid"], role=res["role"], slot=slot, source="logout")
    return res


@app.post("/api/session/password")
async def session_password(body: PasswordIn):
    return session.change_admin_password(body.old, body.new)


@app.get("/api/session/admin-auth")
async def admin_auth_get(x_surface: str = Header(default="kiosk")):
    return {"required": db.get_admin_auth()["required"]}


@app.post("/api/session/admin-auth")
async def admin_auth_set(body: AdminAuthIn, x_surface: str = Header(default="kiosk")):
    slot = _surface(x_surface)
    if session.get_principal(slot)["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可开关口令门")
    res = session.set_admin_auth(body.required)
    bus.publish("admin_auth_changed", required=res["required"])
    return res


@app.get("/api/wards")
async def wards_list(x_surface: str = Header(default="kiosk")):
    return {"wards": [{"uid": w["uid"], "name": w.get("name", ""),
                       "zone": json.loads(w.get("zone_json") or "{}"),
                       "elders": [p["uid"] for p in db.list_profiles(kind="elder")
                                  if p.get("ward_id") == w["uid"]]}
                      for w in db.list_profiles(kind="ward")]}


@app.post("/api/wards")
async def wards_upsert(w: WardIn, x_surface: str = Header(default="kiosk")):
    if session.get_principal(_surface(x_surface))["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可管理病房")
    db.upsert_ward(w.uid, name=w.name, zone=w.zone)
    audit.log("ward_change", source="admin", ward=w.uid)
    bus.publish("ward_changed", uid=w.uid, action="upsert")
    return {"ok": True, "ward": db.get_profile(w.uid)}


@app.post("/api/wards/{ward_uid}/zone")
async def ward_set_zone(ward_uid: str, x_surface: str = Header(default="kiosk")):
    """把 locator 当前位姿记为该病房区域（圆心 + 默认半径）。"""
    if session.get_principal(_surface(x_surface))["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可管理病房")
    pose = locator.get_pose()
    if pose is None:
        return {"ok": False, "error": "拿不到小车位姿（rosbridge/定位未就绪），可改为手填坐标"}
    r = float(db.get_settings().get("ward_zone_default_r", 3.0))
    zone = {"frame": "map", "type": "circle", "x": pose["x"], "y": pose["y"], "r": r}
    db.upsert_ward(ward_uid, name=(db.get_profile(ward_uid) or {}).get("name", ""), zone=zone)
    bus.publish("ward_changed", uid=ward_uid, action="zone")
    return {"ok": True, "zone": zone}


@app.get("/api/policy/roles")
async def policy_roles(x_surface: str = Header(default="kiosk")):
    from .policy import POLICY_DEFAULTS
    role = session.get_principal(_surface(x_surface))["role"]
    if role != "admin":
        return {role: _public_policy(POLICY_DEFAULTS[role])}
    return {k: _public_policy(v) for k, v in POLICY_DEFAULTS.items()}
```

（`_public_policy` 只把 `prompt_file` 换成字符串路径，避免 JSON 序列化 `Path`。）

`lifespan` 里加：`session.ensure_admin_password()` 打印+审计、`locator.start(db.get_settings())`、后台 tick 线程：

```python
    pw = session.ensure_admin_password()
    if pw:
        print(f"[INFO] 已生成管理员初始口令：{pw}（请登录后立即修改）")
        audit.log("admin_password_generated")
    locator.start(db.get_settings())

    async def _ward_tick():
        while True:
            await asyncio.sleep(1)
            try:
                await asyncio.to_thread(session.tick)
            except Exception:
                pass

    asyncio.create_task(_ward_tick())
```

`/api/chat` 改为按 surface 取 principal（第 252 行附近）：

```python
            principal = session.get_principal(_surface(x_surface))
            for ev in chat.chat_stream(client, MODEL, req.uid, req.message, req.thinking,
                                       settings, principal=principal):
```

`/api/chat` 的路由签名补 `x_surface: str = Header(default="kiosk")`。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests -q`
预期：全 passed

- [ ] **步骤 5：Commit**

```bash
git add LLM/server.py LLM/tests/test_server_roles_routes.py
git commit -m "feat(llm): 会话/病房/策略 REST 接口 + 业务接口按槽位取 principal"
```

---

### 任务 12：前端 —— shared 会话层与事件

**文件：**
- 修改：`frontend/packages/shared/src/api/client.ts`（加 `X-Surface` 支持）
- 修改：`frontend/packages/shared/src/api/session.ts`（扩展现有文件，**不新建** src/session.ts）
- 修改：`frontend/packages/shared/src/events.ts`（事件枚举 + `parseBusPayload`）

- [ ] **步骤 1：实现**（前端无单测框架，用类型检查 + 构建验证）

`api/client.ts` 全量替换为：

```ts
// 统一 REST client：所有 /api 调用走这里（规格 §4）
// 分层用户体系：带 X-Surface 头区分端槽位（kiosk=车前/语音，admin=管理台），
// 后端据此返回该槽位的角色（role 绝不由前端指定，规格 R1）。
export type Surface = "kiosk" | "admin";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`API ${res.status}: ${url}`);
  return (await res.json()) as T;
}

export function apiGet<T = any>(url: string, surface?: Surface): Promise<T> {
  return request<T>(url, {
    headers: { Accept: "application/json", ...(surface ? { "X-Surface": surface } : {}) },
  });
}

export function apiPost<T = any>(url: string, body?: unknown, surface?: Surface): Promise<T> {
  return request<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(surface ? { "X-Surface": surface } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
```

`api/session.ts` 全量替换为：

```ts
import { apiGet, apiPost, type Surface } from "./client";

export type Role = "admin" | "ward" | "elder";

export interface SessionUser {
  ok?: boolean;
  uid: string;
  role: Role;
  locked: boolean;
  slot: Surface;
  source: string;
  ward_uid: string;
  ttl_remain: number | null;
  auth_required: boolean;
  autoswitch: { enabled: boolean; reason: string };
}

export interface Ward {
  uid: string;
  name: string;
  zone: Record<string, unknown>;
  elders: string[];
}

export function getSessionUser(surface: Surface): Promise<SessionUser> {
  return apiGet<SessionUser>("/api/session/user", surface);
}

export function setSessionUser(uid: string, locked: boolean, surface: Surface): Promise<SessionUser> {
  return apiPost<SessionUser>("/api/session/user", { uid, locked }, surface);
}

export function login(password: string | null, surface: Surface) {
  return apiPost<{ ok: boolean; error?: string; role?: Role; ttl_remain?: number }>(
    "/api/session/login", { password }, surface);
}

export function logout(surface: Surface): Promise<SessionUser> {
  return apiPost<SessionUser>("/api/session/logout", {}, surface);
}

export function changePassword(oldPw: string, newPw: string) {
  return apiPost<{ ok: boolean; error?: string }>("/api/session/password", { old: oldPw, new: newPw });
}

export function getAdminAuth(surface: Surface) {
  return apiGet<{ required: boolean }>("/api/session/admin-auth", surface);
}

export function setAdminAuth(required: boolean, surface: Surface) {
  return apiPost<{ required: boolean }>("/api/session/admin-auth", { required }, surface);
}

export function listWards(surface: Surface) {
  return apiGet<{ wards: Ward[] }>("/api/wards", surface);
}

export function upsertWard(uid: string, name: string, surface: Surface) {
  return apiPost<{ ok: boolean }>("/api/wards", { uid, name }, surface);
}

export function recordWardZone(wardUid: string, surface: Surface) {
  return apiPost<{ ok: boolean; error?: string; zone?: Record<string, unknown> }>(
    `/api/wards/${wardUid}/zone`, {}, surface);
}
```

`events.ts`：`user_changed` 载荷加 `role?: Role`、`slot?: Surface`、`ward_uid?: string`；事件类型枚举加 `"session_expired" | "admin_auth_changed" | "ward_changed"`；`parseBusPayload` 加三个分支（分别返回 `{slot}` / `{required}` / `{uid, action}`）。

- [ ] **步骤 2：类型检查**

运行：`cd frontend && pnpm --filter shared exec tsc --noEmit`
预期：无类型错误（若 shared 无独立 tsconfig，则用 `pnpm --filter admin build` 触发依赖编译）

- [ ] **步骤 3：Commit**

```bash
git add frontend/packages/shared
git commit -m "feat(frontend): shared 会话层（X-Surface/登录/口令/病房）+ 事件类型扩展"
```

---

### 任务 13：前端 —— kiosk 左侧层级栏

**文件：**
- 修改：`frontend/packages/kiosk/src/components/UserSwitcher.vue`（改为三组层级抽屉）
- 修改：`frontend/packages/kiosk/src/components/VoiceStatusBar.vue`（按角色换徽标）
- 修改：`frontend/packages/kiosk/src/App.vue`（第 170-180 行切人逻辑改走 `setSessionUser(uid, locked, "kiosk")`；监听 `ward_changed`/`session_expired`）

- [ ] **步骤 1：实现层级抽屉**（模板骨架，样式沿用现有弹层）

```vue
<template>
  <div class="drawer">
    <section>
      <h4>🛡 管理层</h4>
      <button v-if="role !== 'admin'" @click="showPw = true">输入口令进入</button>
      <template v-else>
        <p>当前（剩 {{ fmt(ttl) }}）</p>
        <button @click="$emit('logout')">退出</button>
        <button @click="showPwChange = true">口令设置</button>
      </template>
      <p v-if="!authRequired" class="warn">⚠️ 当前无口令保护，点「输入口令进入」可直接进入</p>
    </section>
    <section>
      <h4>🏠 集体层</h4>
      <p v-if="!autoswitch.enabled" class="hint">位置未知 · 手动切病房（{{ autoswitch.reason }}）</p>
      <ul>
        <li v-for="w in wards" :key="w.uid" :class="{ active: w.uid === wardUid }"
            @click="$emit('pick', w.uid)">{{ w.name || w.uid }}</li>
      </ul>
    </section>
    <section>
      <h4>👴 老人层</h4>
      <ul>
        <li v-for="p in elders" :key="p.uid" :class="{ active: p.uid === current }"
            @click="$emit('pick', p.uid)">{{ p.nickname || p.name || p.uid }}</li>
      </ul>
    </section>
  </div>
</template>
```

- [ ] **步骤 2：构建验证**

运行：`cd frontend && pnpm --filter kiosk build`
预期：构建成功，无 TS 报错

- [ ] **步骤 3：Commit**

```bash
git add frontend/packages/kiosk
git commit -m "feat(kiosk): 左侧层级栏（管理层/集体层/老人层）+ 角色状态条"
```

---

### 任务 14：前端 —— admin 登录门与两个新页签

**文件：**
- 修改：`frontend/packages/admin/src/App.vue`（登录门 + Header 身份）
- 创建：`frontend/packages/admin/src/pages/RolesPage.vue`（身份与权限：策略矩阵只读 + 口令设置）
- 创建：`frontend/packages/admin/src/pages/WardsPage.vue`（病房管理：列表/新建/记录房间区域/归入老人）

- [ ] **步骤 1：实现**
  - `App.vue`：`onMounted` 调 `getSession("admin")`；`role !== "admin"` 且 `auth_required` → 只渲染登录卡；`auth_required === false` → 直接进入并在顶部渲染红条「当前无口令保护」。
  - `RolesPage.vue`：`GET /api/policy/roles` 渲染三行（角色/提示词文件/工具白名单/数据范围/是否读病房上下文）；口令区两个按钮：改口令（旧+新）、开关口令门（`setAdminAuth`）。
  - `WardsPage.vue`：`GET /api/wards` 列表；「新建病房」输入 uid/名称调 `POST /api/wards`；「记录当前房间为病房区域」调 `POST /api/wards/{uid}/zone`，失败时展示后端返回的 `error`（拿不到位姿）。

- [ ] **步骤 2：构建验证**

运行：`cd frontend && pnpm --filter admin build && pnpm --filter kiosk build`
预期：两个包均构建成功

- [ ] **步骤 3：Commit**

```bash
git add frontend/packages/admin
git commit -m "feat(admin): 登录门 + 身份与权限页签 + 病房管理页签"
```

---

### 任务 15：文档、降级自检与端到端验收

**文件：**
- 修改：`AGENTS.md`（「LLM/ 后端」模块表加 `session.py`/`policy.py`/`locator.py`/`prompt/`；「关键约定」补 R1–R5）
- 修改：`docs/log.md`（按日期追加本次实现）
- 修改：`docs/superpowers/specs/2026-09-14-layered-user-roles-design.md`（状态改「已实现 P0」）

- [ ] **步骤 1：跑规格 §11 的 12 条验收**（全部本机，不用动车）

逐条执行并把结果记到 `docs/log.md`，其中：
- 第 13-16 条（病房自动切换）用 `locator.set_pose_for_test()` 在 Python 交互里注入位姿，观察 `GET /api/session/user` 的 `ward_uid` 与审计 `ward_change`；
- 第 15 条额外验证：把设置里 `rosbridge_url` 置空 → `autoswitch.enabled == false`，对话仍正常。

- [ ] **步骤 2：降级自检**

运行：`.venv\Scripts\python.exe -c "import LLM.server; print('import ok')"`
预期：`import ok`（可选依赖缺失也不许炸；新代码不得有顶层硬 import `websocket` 之外的第三方）

- [ ] **步骤 3：Commit**

```bash
git add AGENTS.md docs/log.md docs/superpowers/specs/2026-09-14-layered-user-roles-design.md
git commit -m "docs: 分层用户体系 P0 实现日志 + AGENTS 红线 R1-R5"
```

---

## 交付说明

- 本计划覆盖规格的 **P0**（用户系统）。规格 §6.3 动作分级、§7 地点白名单/`robot_goto`/二次确认为 **P1 暂缓**（用户 2026-09-14：先不管 MCP）。
- 全程**不需要动车、不需要板卡**：病房自动切换用注入假位姿验证；真机联调（rosbridge 实际位姿）留到 MCP/导航线重启后一并做。
- 验收通过后，把 P1 的动作约束按规格 §7 另立计划。
