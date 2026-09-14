# 分层用户体系（P0：用户系统）实现计划 · v2（2026-09-14 复审修订版）

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。
> **规格：** `docs/superpowers/specs/2026-09-14-layered-user-roles-design.md`（2026-09-14 **二次修订版**；冲突清单与处置见其 **§14 复审结论**——本计划已按 §14 全部改正，读规格时以 §14 为准）

**目标：** 让系统同时存在**管理层 / 集体层 / 老人层**三个层级，各有独立提示词与权限；集体层以"病房用户"落地并按小车位置自动切换；管理员口令可改、可关。

**架构：** 新增 `LLM/session.py`（双槽会话主体 + 角色推导 + 当前病房自动切换）与 `LLM/policy.py`（角色策略包）与 `LLM/zonegeo.py`（点是否在区域内，纯几何）；`profiles` 加 `kind`/`ward_id`/`ward_map`/`ward_zone` 承载病房用户——**病房区域几何的唯一真相是地图文件夹里的 `<图名>.tags.json`**（`LLM/maptags.py`，一期已落地），`profiles` 只记「地图名 + 区域 uid」，`brain.db.zones` 只是只读索引缓存；位姿与当前地图**复用一期已落地的** `LLM/locator.py` + `LLM/roslink.py`；提示词在 `LLM/prompt.md`（共用 base）之上拼 `LLM/prompt/<role>.md`。

**技术栈：** Python 3 / FastAPI / SQLite / pytest / websocket-client（已有依赖，不新增）；前端 Vue3 + Vite + TS（pnpm monorepo）+ vitest（`shared` 包已有）。

**红线（贯穿全程，规格 §1.3）：** R1 前端 role 不可信；R2 fail-closed（未知=集体层最小能力）；R3 急停/呼救永远放行；R4 医疗写入红线不受角色影响；R5 层级上下文单向（集体→老人可读、反向与跨病房不可读）。

---

## v2 修订说明（相对旧版计划 `cc23e3b` / `8e64fa7`）

旧版计划写于 2026-09-14 白天，之后有三件事变了，**照旧版写会撞车**：

| # | 旧版写法 | 已核实的事实 | v2 改法 |
|---|---|---|---|
| 1 | 任务 1 要**新建 `zones` 表**（自增 `id`）+ `profiles.zone_id`；新区间函数 `add_zone`/`get_zone(int)`/`list_zones(map, kind)` | `zones` 表**一期已建**（`db.py:117-129`，复合主键 `(map_name, uid)`，**只读缓存**）；`db.get_zone(uid)`/`db.list_zones(map_name, uid)` 已存在且签名不同；几何真相是 `<图名>.tags.json` | 任务 2 改为**加列 + 向后兼容扩参**，不建表、不加 `add_zone`；病房关联改为 `ward_map` + `ward_zone`（`z<N>`） |
| 2 | 任务 6 要**新增 `locator.py`**（自写 rosbridge 订阅 + `available(settings) -> bool`） | `locator.py`（240 行）与 `roslink.py`（255 行）**一期已落地**；`available()` 返回 `(bool, reason)`；`get_pose()` 拿不到返回 `None`；已有 `current_map()`/`set_pose_for_test()` | 任务 7 直接**复用**，不重写；删除自写订阅线程那段 |
| 3 | 任务 2 加设置项 `rosbridge_url` | 它不是 settings 项（`conf.ROSBRIDGE_URL` 模块级常量），`set_settings` 白名单会拒收；`current_map` 已存在 | 改为 `ward_autoswitch_enabled` + `ward_map_source`；`current_map` 不重复添加 |
| 4 | 任务 7 用 `settings.current_map` 当判定地图 | 一期已有 `/map` 指纹反查 `locator.current_map()`；用设置值会在换图后静默切错病房（D17 禁止） | 默认用指纹反查，认不出**不判命中**；`ward_map_source="setting"` 才用设置值 |
| 5 | 任务 12 要"新建 `shared/src/session.ts`" | 实际是 `shared/src/api/session.ts`（已存在）；前端 `X-Surface` 零实现 | 改为**扩展现有文件**并写清真实落点（kiosk 换人入口 `VoiceStatusBar.vue:34`、admin 页签数组在 `App.vue:14-23`） |
| 6 | 各任务"预期：全 passed（既有 83 项不回归）"；测试里用 `db.add_history` | 实测基线 **`4 failed, 191 passed`**（红态在 `tests/test_modules_status.py` 与 `tests/test_unlock_switch.py`，与本次无关）；历史函数叫 **`append_history`** | 全篇改成真实基线与真实函数名，并声明"4 个红态不许修" |
| 7 | （旧版没有）热路径性能 | `maptags.get_zones()` 会触发 `_ensure_fresh → sync_map`（`MAPS_IO=ssh` 下可能秒级阻塞）；`locator.current_map()` 会列图 + 逐图读元数据 | tick **只读本地 `db.zones` 缓存**；`current_map()` 结果做 10s TTL 缓存 |
| 8 | （旧版没有）档案编辑清病房关联的坑 | git 历史 `f6f6e54` 专门修过这个 bug | `ward_id`/`ward_map`/`ward_zone` **只由 `upsert_ward`/`set_ward_zone`/`set_profile_ward` 写**，`upsert_profile` 不碰 |
| 9 | （旧版没有）两条被 revert 版本暴露的历史隐患 | ① `kind=COALESCE(...)` 兜底永不触发 → 把已有 uid 提升成病房会静默失败；② `list_profiles()` 不过滤 kind → 病房行混进"老人列表" | 任务 2 用 `SET kind='ward'` 直写并加回归用例；任务 11 把 `GET /api/profiles` 默认改成只列 `kind='elder'` 并加回归用例 |

**测试命令（Windows 本机，项目根目录）：**
```
.venv\Scripts\python.exe -m pytest LLM/tests -q
```

**测试基线（必须先认下来，改代码前先跑一次）：**
```
.venv\Scripts\python.exe -m pytest LLM/tests tests -q
→ 4 failed, 191 passed in ~78s
```
4 个红态是**既有基线漂移，与本次无关**：`tests/test_modules_status.py::test_modules_status_shape`（模块集合没算 `mcp`）与 `tests/test_unlock_switch.py` 3 例（`VoiceWorker` 旧签名 `chat_fn`，现签名是 `stream_fn`）。**不要去修，也不要当成自己打坏的。**

**测试隔离铁律：** 一律沿用仓内既有模式（`LLM/tests/test_memory_v4.py` 的 `d` fixture）——**只改 `db.DB_PATH` 指向临时库并在结束时还原**，不要 `importlib.reload(db)`（reload 会把 `DB_PATH` 重置回真实路径，测试会污染 `LLM/data/brain.db`）。

---

## 文件结构（先锁定职责，再拆任务）

| 文件 | 状态 | 职责 |
|---|---|---|
| `LLM/zonegeo.py` | **新增**（~70 行） | 纯几何：点是否在区域内（多边形射线法 / `rect` 用外接矩形）。零 IO、零外部依赖、不抛异常 |
| `LLM/session.py` | **新增**（~330 行） | 会话层：双槽角色、`derive_role`、管理员登录/登出/TTL、当前病房与位置自动切换 |
| `LLM/policy.py` | **新增**（~60 行） | 纯数据 + 纯函数：三角色策略包（提示词文件 / 工具白名单 / 数据可见范围 / 是否读病房上下文） |
| `LLM/prompt/{ward,elder,admin}.md` | **新增** | 角色提示词片段（`prompt.md` 继续作共用 base，不动） |
| `LLM/db.py` | 修改 | `profiles` 加 4 列；病房 CRUD；口令哈希 raw-key 读写；`list_zones`/`get_zone` 向后兼容扩参 |
| `LLM/conf.py` | 修改 | 8 个新设置项 |
| `LLM/chat.py` | 修改 | `principal` 透传；`_load_role_prompt`；集体层上下文注入 |
| `LLM/tools.py` | 修改 | `@tool(roles=)`；`effective_tools`/`run_tool` 角色白名单（闸门 2） |
| `LLM/memory.py` | 修改 | `note_turn(..., role=)`：集体层不沉淀 |
| `LLM/voice_api.py` | 修改 | 会话持有权移交 `session.py`（同名函数转发，调用点不变） |
| `LLM/voice/worker.py` | 修改 | `_handle_speech` 按角色分支（规格 §4.3） |
| `LLM/server.py` | 修改 | 新增 session/wards/policy 路由；业务接口按 `X-Surface` 取 principal；`lifespan` 挂 tick |
| `LLM/locator.py` `LLM/roslink.py` `LLM/maptags.py` `LLM/mapstore.py` | **不改** | 一期已落地，本计划只调用 |
| `frontend/packages/shared/src/{api/client.ts,api/session.ts,events.ts}` + `tests/events.test.ts` | 修改 | `X-Surface`、会话/病房 API、3 个新事件 |
| `frontend/packages/kiosk/src/{components/UserSwitcher.vue,components/VoiceStatusBar.vue,App.vue}` | 修改 | 左侧层级栏 + 角色徽标 + 管理员登录/口令设置 |
| `frontend/packages/admin/src/{App.vue,pages/RolesPage.vue,pages/WardsPage.vue,pages/RegisterPage.vue}` | 修改/新增 | 登录门 + 「身份与权限」「病房管理」页签 + 注册向导加病房归属 |

> ⚠️ **两个 `session` 不要搞混**：`LLM/session.py`（本计划**新增**，角色会话层）与 `LLM/voice/session.py`
> （**早已存在**，语音状态机 IDLE/LISTENING/SPEAKING，worker 里 import 为 `session_mod`）。新代码
> 一律用 `role_session` 别名引用角色会话层。

---

### 任务 1：`zonegeo.py` —— 点是否在区域内（纯几何，无依赖）

**文件：**
- 创建：`LLM/zonegeo.py`
- 测试：`LLM/tests/test_zonegeo.py`（新建）

先做这一件：它零依赖、可独立测试，任务 7 的病房判定要用它。**为什么单独一个模块**：这个判定有两处使用者（本设计的"小车在哪个病房"、将来地图编辑器的"点位回读"），且**只吃数据不吃 IO**；塞进 `db.py` 会脏化 SQLite 层，塞进 `session.py` 则无法独立单测。口径照抄前端既有实现 `frontend/packages/mapeditor/src/lib/coords.ts:44 pointInPolygon()`（射线法），保证前后端对"点是否在区域内"给出一致答案。

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_zonegeo.py
# -*- coding: utf-8 -*-
"""区域几何判定测试（纯函数，无 DB、无 IO、无 ROS）。"""
from LLM import zonegeo


def test_point_inside_and_outside_square():
    poly = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]
    assert zonegeo.point_in_polygon(1.0, 1.0, poly) is True
    assert zonegeo.point_in_polygon(3.0, 1.0, poly) is False
    assert zonegeo.point_in_polygon(-1.0, -1.0, poly) is False


def test_concave_polygon_notch_is_outside():
    """L 形：缺口里那点必须判在外（射线法要能处理凹多边形）。"""
    poly = [[0.0, 0.0], [4.0, 0.0], [4.0, 1.0], [1.0, 1.0], [1.0, 4.0], [0.0, 4.0]]
    assert zonegeo.point_in_polygon(0.5, 3.0, poly) is True
    assert zonegeo.point_in_polygon(2.0, 2.0, poly) is False


def test_degenerate_input_is_false_never_raises():
    """点数 <3 / 空 / None 一律 False（fail-safe：判"不在"，不抛异常打断对话）。"""
    assert zonegeo.point_in_polygon(0.0, 0.0, [[0.0, 0.0], [1.0, 1.0]]) is False
    assert zonegeo.point_in_polygon(0.0, 0.0, []) is False
    assert zonegeo.point_in_polygon(0.0, 0.0, None) is False


def test_broken_points_are_dropped_not_fatal():
    poly = [[0.0, 0.0], ["x", "y"], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]
    assert zonegeo.point_in_polygon(1.0, 1.0, poly) is True


def test_rect_shape_uses_bbox():
    zone = {"shape": "rect", "polygon": [[0.0, 0.0], [4.0, 0.0], [4.0, 2.0], [0.0, 2.0]]}
    assert zonegeo.zone_hit(zone, 3.9, 1.9) is True
    assert zonegeo.zone_hit(zone, 5.0, 1.0) is False


def test_zone_hit_accepts_cache_row_with_polygon_json_string():
    """db.list_zones 会给 polygon（已解析），但表里存的是字符串；两种都得能吃。"""
    zone = {"shape": "polygon", "polygon_json": "[[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]"}
    assert zonegeo.zone_hit(zone, 1.0, 1.0) is True
    assert zonegeo.zone_hit({"shape": "polygon", "polygon_json": "坏了"}, 1.0, 1.0) is False
    assert zonegeo.zone_hit(None, 1.0, 1.0) is False
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_zonegeo.py -q`
预期：FAIL（`ModuleNotFoundError: No module named 'LLM.zonegeo'`）

- [ ] **步骤 3：编写实现**

```python
# LLM/zonegeo.py
# -*- coding: utf-8 -*-
r"""
区域几何判定（点在不在某个区域内）—— 纯 stdlib、无 IO、无外部依赖、不抛异常。

规格：docs/superpowers/specs/2026-09-14-layered-user-roles-design.md §4.5
口径照抄前端既有实现 frontend/packages/mapeditor/src/lib/coords.ts:44 pointInPolygon()
（射线法），保证前后端对"点是否在区域内"给出一致答案：
  * shape == "polygon"：射线法（顶点顺序不限，凸凹多边形都行）；
  * shape == "rect"   ：用 polygon 的**外接矩形**判定（矩形区域只存 4 个角，容差更稳）；
  * 顶点数 < 3 / 数据损坏 → False —— fail-safe：宁可判"不在"，也不抛异常打断对话。
"""
import json


def _points(poly) -> list[tuple[float, float]]:
    """[[x, y], ...] → [(x, y), ...]；坏点直接丢弃（不抛异常）。"""
    out = []
    for pt in poly or []:
        try:
            out.append((float(pt[0]), float(pt[1])))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return out


def point_in_polygon(x: float, y: float, poly) -> bool:
    """射线法：点 `(x, y)` 是否在多边形 `poly`（`[[x, y], ...]`，**米坐标**）内。"""
    pts = _points(poly)
    n = len(pts)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_bbox(x: float, y: float, poly) -> bool:
    """点是否落在 `poly` 的外接矩形内（`shape='rect'` 的判定口径）。"""
    pts = _points(poly)
    if not pts:
        return False
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys)


def _as_poly(zone: dict) -> list:
    """从区域行里取几何：优先已解析的 `polygon`，退回 `polygon_json` 字符串。"""
    poly = zone.get("polygon")
    if poly is None:
        poly = zone.get("polygon_json") or []
    if isinstance(poly, str):
        try:
            poly = json.loads(poly or "[]")
        except ValueError:
            poly = []
    return poly if isinstance(poly, list) else []


def zone_hit(zone: dict, x: float, y: float) -> bool:
    """点是否落在这个区域行里（`zone` = `db.list_zones()` 返回的行，含 `shape`/`polygon`）。"""
    if not zone:
        return False
    poly = _as_poly(zone)
    if str(zone.get("shape") or "polygon") == "rect":
        return in_bbox(x, y, poly)
    return point_in_polygon(x, y, poly)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_zonegeo.py -q`
预期：`6 passed`

- [ ] **步骤 5：Commit**

```bash
git add LLM/zonegeo.py LLM/tests/test_zonegeo.py
git commit -m "feat(llm): 新增 zonegeo.py（点是否在区域内，纯几何，照抄前端 coords.ts 口径）"
```

---

### 任务 2：`profiles` 扩列 + 病房 CRUD + 管理员口令数据层

**文件：**
- 修改：`LLM/db.py`（`init_db()` 的 `_ensure_columns(conn, "profiles", …)` 第 153-156 行；`list_profiles` 第 231 行；`list_zones` 第 1231 行；`get_zone` 第 1258 行；`settings` 区块尾部）
- 测试：`LLM/tests/test_ward_db.py`（新建）

**四条硬约束（每条都有实测依据）：**
1. **不建表**：`zones`/`destinations`/`map_tags_manifest` 三张缓存表一期已建（`db.py:91-129`，复合主键 `(map_name, uid)`），唯一写入口是 `maptags.sync_map()`；本任务只读。
2. **病房区域几何不在这层**：几何真相是 `<图名>.tags.json`；`profiles` 只记 `ward_map` + `ward_zone`。
3. **`upsert_profile` 一个参数都不加**：`ward_id`/`ward_map`/`ward_zone` 只允许专用函数写，否则管理台编辑老人档案时会静默清掉病房关联（git 历史 `f6f6e54` 就是修这个 bug）。
4. **口令哈希不走 `set_settings`**：它只接受 `DEFAULT_SETTINGS | TOOL_DEFAULTS` 白名单 key，`admin_password_hash` 不在其中 → 用 raw key 读写。

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_ward_db.py
# -*- coding: utf-8 -*-
"""病房用户 / 管理员口令 / 区域缓存的数据层测试（临时库隔离，沿用 test_memory_v4.py 模式）。"""
import json
import os
import tempfile

import pytest

from LLM import db


@pytest.fixture()
def d():
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    yield db
    db.DB_PATH = old


def _seed_zones(map_name: str, zones: list[dict]) -> None:
    """往**只读缓存表**里种区域（等价于 maptags.sync_map 的结果；测试直接铺数据）。

    注意 `replace_map_tags` 是「整图重建」——**一次要把该图所有区域一起传进来**，
    分两次调用会把前一次的行删掉。
    """
    db.replace_map_tags(
        map_name,
        {"file_mtime": 0, "file_size": 0, "sha1": "", "resolution": 0.05,
         "origin": {"x": 0.0, "y": 0.0}, "warnings": []},
        [],
        zones,
    )


def _zone(uid: str, name: str, poly: list, kind: str = "ward", shape: str = "polygon") -> dict:
    return {"uid": uid, "name": name, "kind": kind, "shape": shape,
            "polygon_json": json.dumps(poly), "parent": "", "note": "",
            "created_at": "", "updated_at": ""}


def test_existing_profile_defaults_to_elder(d):
    d.upsert_profile("elder_001", name="张奶奶")
    assert d.get_profile_kind("elder_001") == "elder"
    assert d.get_profile("elder_001")["ward_id"] == ""
    assert d.get_profile("elder_001")["ward_map"] == ""
    assert d.get_profile("elder_001")["ward_zone"] == ""


def test_unknown_uid_kind_is_empty(d):
    assert d.get_profile_kind("ghost_9") == ""       # 查不到 → ""，由 session 兜底成 ward（R2）


def test_upsert_ward_and_list(d):
    d.upsert_ward("ward_101", name="101 病房", ward_map="my_map", ward_zone="z1")
    wards = d.list_wards()
    assert [w["uid"] for w in wards] == ["ward_101"]
    assert d.get_profile_kind("ward_101") == "ward"
    assert wards[0]["ward_map"] == "my_map" and wards[0]["ward_zone"] == "z1"
    assert d.list_profiles(kind="elder") == []       # kind 过滤生效


def test_upsert_ward_does_not_wipe_linkage_on_rename(d):
    """重复 upsert_ward（只改名）不许把已有关联清掉——调用方传空串=保持原值。"""
    d.upsert_ward("ward_101", name="101", ward_map="my_map", ward_zone="z1")
    d.upsert_ward("ward_101", name="101 病房")
    p = d.get_profile("ward_101")
    assert p["name"] == "101 病房"
    assert p["ward_map"] == "my_map" and p["ward_zone"] == "z1"


def test_upsert_profile_never_touches_ward_linkage(d):
    """**核心回归**：档案编辑（upsert_profile）绝不许清掉病房关联（f6f6e54 的教训）。"""
    d.upsert_ward("ward_101", name="101 病房", ward_map="my_map", ward_zone="z1")
    d.upsert_profile("elder_101_1", name="李爷爷")
    d.set_profile_ward("elder_101_1", "ward_101")
    d.upsert_profile("elder_101_1", name="李爷爷（改名）", age=79)
    p = d.get_profile("elder_101_1")
    assert p["name"] == "李爷爷（改名）" and p["ward_id"] == "ward_101"
    w = d.get_profile("ward_101")
    assert w["ward_map"] == "my_map" and w["ward_zone"] == "z1"
    assert w["kind"] == "ward"


def test_set_ward_zone_roundtrip(d):
    d.upsert_ward("ward_101", name="101 病房")
    assert d.get_profile("ward_101")["ward_zone"] == ""
    d.set_ward_zone("ward_101", "my_map", "z3")
    p = d.get_profile("ward_101")
    assert (p["ward_map"], p["ward_zone"]) == ("my_map", "z3")


def test_set_profile_ward(d):
    d.upsert_profile("elder_101_1", name="李爷爷")
    d.set_profile_ward("elder_101_1", "ward_101")
    assert d.get_profile("elder_101_1")["ward_id"] == "ward_101"


def test_upsert_ward_promotes_existing_profile(d):
    """**回归（历史隐患①）**：把一个已存在的 uid 提升成病房，`kind` 必须真的变成 ward。

    旧版实现用 `kind=COALESCE(profiles.kind, excluded.kind)` 兜底，而旧行早已被回填成
    `'elder'`、永不为空 → 兜底分支**永不触发** → 提升静默失败却照写区域关联（半状态）。
    本版用 `SET kind='ward'` 直写，这条测试就是钉住它。
    """
    d.upsert_profile("ward_101", name="其实是先建错的老人档案")   # 先以 elder 存在
    assert d.get_profile_kind("ward_101") == "elder"
    d.upsert_ward("ward_101", name="101 病房", ward_map="my_map", ward_zone="z1")
    assert d.get_profile_kind("ward_101") == "ward"
    assert d.get_profile("ward_101")["ward_map"] == "my_map"


def test_upsert_ward_keeps_name_when_omitted(d):
    """**回归**：只改关联（不传名字）时，已有病房名不许被抹成空串。

    `upsert_profile` 的 ON CONFLICT 是 `name=excluded.name`，把空串喂进去会**静默清掉名字**；
    `/api/wards/{uid}/zone` 这类"只改区域关联"的端点最自然的写法就是不传名字。
    """
    d.upsert_ward("ward_101", name="101 病房", ward_map="my_map", ward_zone="z1")
    d.upsert_ward("ward_101", ward_map="my_map2", ward_zone="z9")
    p = d.get_profile("ward_101")
    assert p["name"] == "101 病房"
    assert (p["ward_map"], p["ward_zone"]) == ("my_map2", "z9")


def test_get_settings_never_leaks_password_keys(d):
    """口令哈希/盐存在 settings 表里，但绝不许随 get_settings() 外泄（GET /api/settings 会透出）。"""
    d.set_admin_password("246810")
    leaked = [k for k in d.get_settings() if k.startswith("admin_password_")]
    assert leaked == []
    # 口令通路本身仍然可用（别把通路一起切断）
    assert d.get_admin_auth()["hash"] and d.verify_admin_password("246810") is True


def test_list_zones_kind_filter_and_get_zone_with_map(d):
    _seed_zones("my_map", [
        _zone("z1", "101", [[0, 0], [2, 0], [2, 2], [0, 2]], kind="ward"),
        _zone("z2", "走廊", [[0, 0], [9, 0], [9, 9], [0, 9]], kind="other"),
    ])
    _seed_zones("my_map2", [_zone("z1", "102", [[10, 10], [12, 10], [12, 12], [10, 12]])])

    assert [z["uid"] for z in d.list_zones(map_name="my_map", kind="ward")] == ["z1"]
    assert len(d.list_zones(map_name="my_map")) == 2
    assert d.list_zones(map_name="不存在的地图") == []          # 无缓存 → []（降级）

    # get_zone 必须能按地图限定：三张图各有一个 z1，只按 uid 查会串（既有隐患已修）
    assert d.get_zone("z1", "my_map")["name"] == "101"
    assert d.get_zone("z1", "my_map2")["name"] == "102"
    assert d.get_zone("z1", "nope") is None
    assert d.get_zone("z9", "my_map") is None
    assert d.get_zone("z1")["name"] in ("101", "102")           # 不传地图名：兼容旧行为


def test_zone_rows_expose_parsed_polygon(d):
    _seed_zones("my_map", [_zone("z1", "101", [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]])])
    z = d.list_zones(map_name="my_map")[0]
    assert z["polygon"] == [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]
    assert "polygon_json" not in z


def test_float_setting_roundtrip(d):
    """float 设置项读回来必须是 float 不是 "0.45"。

    这里用**一期末尾就已存在**的 `map_boundary_margin_m`（默认 0.3，float）来验类型转换分支——
    本任务不依赖任务 3 新增的键（`ward_zone_default_r` 的 float 往返由任务 3 自己的测试覆盖）。
    """
    d.set_settings({"map_boundary_margin_m": 0.45})
    assert d.get_settings()["map_boundary_margin_m"] == 0.45


def test_admin_password_hash_roundtrip(d):
    assert d.get_admin_auth() == {"required": True, "hash": "", "salt": ""}
    d.set_admin_password("246810")
    a = d.get_admin_auth()
    assert a["hash"] and a["salt"] and "246810" not in a["hash"]  # 绝不落明文
    assert d.verify_admin_password("246810") is True
    assert d.verify_admin_password("000000") is False
    d.set_admin_auth_required(False)
    assert d.get_admin_auth()["required"] is False
    d.set_admin_auth_required(True)
    assert d.get_admin_auth()["required"] is True


def test_verify_admin_password_without_hash_is_false(d):
    assert d.verify_admin_password("任意") is False


def test_verify_password_with_corrupt_salt_is_false(d):
    """盐被写坏（非十六进制）也必须只回"拒绝"，不许抛异常把登录端点打成 500。"""
    d._set_setting_raw("admin_password_hash", "deadbeef")
    d._set_setting_raw("admin_password_salt", "不是十六进制")
    assert d.verify_admin_password("任意") is False
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_ward_db.py -q`
预期：FAIL（`AttributeError: module 'LLM.db' has no attribute 'get_profile_kind'`）

- [ ] **步骤 3：实现**

**(3a)** 在 `init_db()` 的 `_ensure_columns(conn, "profiles", {...})` 字典里追加四列：

```python
            _ensure_columns(conn, "profiles", {
                "gender": "gender TEXT DEFAULT ''",
                "birthday": "birthday TEXT DEFAULT ''",
                # 分层用户体系（规格 §9）：kind=elder|ward；ward_id=老人所属病房 uid；
                # ward_map/ward_zone=病房关联的「地图名 + 区域 uid」（几何真相在 <图名>.tags.json）
                "kind": "kind TEXT DEFAULT 'elder'",
                "ward_id": "ward_id TEXT DEFAULT ''",
                "ward_map": "ward_map TEXT DEFAULT ''",
                "ward_zone": "ward_zone TEXT DEFAULT ''",
            })
```

> `kind TEXT DEFAULT 'elder'` 让**旧行自动满足**"默认老人"，所以**不需要**写任何迁移代码。

**(3b)** `list_profiles` 加 `kind` 过滤（默认空 = 不过滤，向后兼容），并在其后追加病房相关函数：

```python
def list_profiles(kind: str = "") -> list[dict]:
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
    """角色判定的**唯一权威依据**（R1）：admin 不写进 profiles → 返回 ""。

    注意：调用方（session.derive_role）必须把 "" 当"未知"并按集体层兜底（R2），
    绝不按 uid 前缀猜。
    """
    if not uid:
        return ""
    conn = _conn()
    try:
        row = conn.execute("SELECT kind FROM profiles WHERE uid=?", (uid,)).fetchone()
        return (row["kind"] or "elder") if row else ""
    finally:
        conn.close()


def list_wards() -> list[dict]:
    """全部病房用户（kind='ward'）。"""
    return list_profiles(kind="ward")


def upsert_ward(uid: str, name: str = "", ward_map: str = "", ward_zone: str = "") -> dict:
    """新建/更新一条病房用户。

    **只由本函数与 set_ward_zone 写 kind/ward_map/ward_zone**：upsert_profile 不碰这三列，
    否则管理台编辑老人档案时会静默清掉病房关联（见 git f6f6e54）。
    **三个字段一律"传空串 = 保持原值"，名字也一样**——`upsert_profile` 的 ON CONFLICT 是
    `name=excluded.name`，直接把空串喂进去会把已有名字抹成空（静默数据丢失），
    所以名字只在"行不存在"或"传了非空名字"时才写。
    """
    exists = bool(get_profile(uid))
    if not exists:
        upsert_profile(uid, name=name)      # 建行（行不存在时名字允许为空）
    elif name:
        with _lock:
            conn = _conn()
            try:
                conn.execute("UPDATE profiles SET name=?, updated_at=? WHERE uid=?",
                             (name, now_iso(), uid))
                conn.commit()
            finally:
                conn.close()
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "UPDATE profiles SET kind='ward', "
                "ward_map=CASE WHEN ?<>'' THEN ? ELSE ward_map END, "
                "ward_zone=CASE WHEN ?<>'' THEN ? ELSE ward_zone END, "
                "updated_at=? WHERE uid=?",
                (ward_map, ward_map, ward_zone, ward_zone, now_iso(), uid))
            conn.commit()
        finally:
            conn.close()
    return get_profile(uid) or {}


def set_ward_zone(uid: str, map_name: str, zone_uid: str) -> None:
    """把病房关联到「某张图上的某个区域 uid」（几何真相在 <图名>.tags.json，这里只存引用）。"""
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE profiles SET ward_map=?, ward_zone=?, updated_at=? WHERE uid=?",
                         (map_name or "", zone_uid or "", now_iso(), uid))
            conn.commit()
        finally:
            conn.close()


def set_profile_ward(uid: str, ward_id: str) -> None:
    """老人归入病房（写 profiles.ward_id；空串 = 移出病房）。"""
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE profiles SET ward_id=?, updated_at=? WHERE uid=?",
                         (ward_id or "", now_iso(), uid))
            conn.commit()
        finally:
            conn.close()
```

**(3c)** `list_zones` 加 `kind` 过滤、`get_zone` 加可选 `map_name`（都是向后兼容扩参，不新建表）：

```python
def list_zones(map_name: str = "", uid: str = "", kind: str = "") -> list[dict]:
    sql = "SELECT * FROM zones"
    where, args = [], []
    if map_name:
        where.append("map_name=?")
        args.append(map_name)
    if uid:
        where.append("uid=?")
        args.append(uid)
    if kind:
        where.append("kind=?")
        args.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name"
    conn = _conn()
    try:
        out = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            try:
                d["polygon"] = json.loads(d.pop("polygon_json") or "[]")
            except ValueError:
                d["polygon"] = []
            out.append(d)
        return out
    finally:
        conn.close()


def get_zone(uid: str, map_name: str = "") -> dict | None:
    """按 uid（+ 可选地图名）取区域行。

    **强烈建议带上 map_name**：uid（`z1`）只在单张图内稳定，三张图各有自己的 `z1`，
    只按 uid 查会跨图串（复合主键是 `(map_name, uid)`）。
    """
    rows = list_zones(map_name=map_name, uid=uid)
    return rows[0] if rows else None
```

**(3d)** 在 `settings` 区块（`get_settings`/`set_settings` 附近、`get_map_tags_manifest` 之前）追加口令读写：

```python
# ---------------------------------------------------------------- 管理员口令（规格 D12/D13）
# 为什么不用 set_settings：它只接受 DEFAULT_SETTINGS|TOOL_DEFAULTS 白名单里的 key，
# 而口令必须有自己的 key（且绝不落明文、绝不进前端设置页）。
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
    """口令门状态 + 盐/哈希（哈希为空 = 还没设过口令）。"""
    return {
        "required": _get_setting_raw("admin_auth_required", "1").lower() in ("1", "true", "yes"),
        "hash": _get_setting_raw("admin_password_hash"),
        "salt": _get_setting_raw("admin_password_salt"),
    }


def set_admin_password(pw: str) -> None:
    """设/改口令：随机盐 + PBKDF2-SHA256 20 万轮，**不落明文**。"""
    import os as _os
    salt = _os.urandom(16).hex()
    _set_setting_raw("admin_password_salt", salt)
    _set_setting_raw("admin_password_hash", _hash_pw(pw, salt))


def verify_admin_password(pw: str) -> bool:
    import hmac
    a = get_admin_auth()
    if not a["hash"] or not a["salt"]:
        return False
    try:
        return hmac.compare_digest(_hash_pw(pw, a["salt"]), a["hash"])
    except ValueError:
        # 盐被写坏（非十六进制）→ 一律拒绝。绝不让它变成 500（登录端点必须只回"口令错误"）。
        return False


def set_admin_auth_required(required: bool) -> None:
    """开关口令门（D13）。关掉之后任何人点「管理层」都能进，UI 必须显示警示。"""
    _set_setting_raw("admin_auth_required", "1" if required else "0")
```

**(3e)** `get_settings()` 的类型转换分支里补 `float`（否则 `ward_zone_default_r` 读回来是字符串 `"3.5"`）：在现有 `isinstance(out.get(r["key"]), int)` 分支**之后**加：

```python
            elif isinstance(out.get(r["key"]), float):
                try:
                    v = float(v)
                except ValueError:
                    v = out[r["key"]]
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_ward_db.py -q`
预期：`16 passed`

- [ ] **步骤 5：跑既有回归 + Commit**

```bash
.venv\Scripts\python.exe -m pytest LLM/tests tests -q
# 预期仍是 4 failed, 191+N passed（那 4 个红态与本任务无关，不许去修）
git add LLM/db.py LLM/tests/test_ward_db.py
git commit -m "feat(llm): profiles 扩 kind/ward_id/ward_map/ward_zone + 病房 CRUD + 管理员口令数据层"
```

---

### 任务 3：新增配置项

**文件：**
- 修改：`LLM/conf.py`（`DEFAULT_SETTINGS`，第 18-46 行）
- 测试：`LLM/tests/test_settings_roles.py`（新建）

**不许加的两个 key**：`rosbridge_url`（它不是设置项，是 `conf.ROSBRIDGE_URL` 模块级常量；`set_settings` 白名单也不含它）与 `current_map`（第 42 行**已存在**，重复添加会让"当前地图"出现两个来源）。

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_settings_roles.py
# -*- coding: utf-8 -*-
"""分层用户体系的配置项默认值（改参数先来这里看）。"""
from LLM.conf import DEFAULT_SETTINGS


def test_role_settings_have_defaults():
    assert DEFAULT_SETTINGS["admin_auth_required"] is True
    assert DEFAULT_SETTINGS["admin_session_ttl_s"] == 300
    assert DEFAULT_SETTINGS["ward_context_window"] == 10
    assert DEFAULT_SETTINGS["ward_autoswitch_enabled"] is True
    assert DEFAULT_SETTINGS["ward_switch_debounce"] == 3
    assert DEFAULT_SETTINGS["ward_zone_default_r"] == 3.0
    assert DEFAULT_SETTINGS["manual_override_sec"] == 600
    assert DEFAULT_SETTINGS["ward_map_source"] == "auto"


def test_new_float_setting_roundtrips_as_float():
    """新增的 float 键要能真的存进去并读回 float（任务 2 已给 get_settings 补 float 分支）。

    本用例自带临时库，不用 `d` fixture（`LLM/tests/` 没有 conftest.py，fixture 都是各文件自己定义的）。
    """
    from LLM import db
    import os, tempfile
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    try:
        db.set_settings({"ward_zone_default_r": 3.5})
        assert db.get_settings()["ward_zone_default_r"] == 3.5
    finally:
        db.DB_PATH = old


def test_does_not_shadow_existing_map_settings():
    """`current_map` 一期已有（真值是"下次启导航用哪张图"），本设计不重新定义它。"""
    assert DEFAULT_SETTINGS["current_map"] == "my_map"
    assert "rosbridge_url" not in DEFAULT_SETTINGS      # rosbridge 地址不在设置表里
    assert isinstance(DEFAULT_SETTINGS["map_topic_fingerprint_enabled"], bool)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_settings_roles.py -q`
预期：FAIL（`KeyError: 'admin_auth_required'`）

- [ ] **步骤 3：实现**

在 `DEFAULT_SETTINGS` 末尾（`mapeditor_auto_switch_map` 之后）追加：

```python
    # ---- 分层用户体系（2026-09-14，规格 docs/superpowers/specs/2026-09-14-layered-user-roles-design.md）----
    "admin_auth_required": True,     # 管理员口令门开关（D13：可在 UI 直接关掉，界面须警示）
    "admin_session_ttl_s": 300,      # 管理员提权后无操作自动降权秒数（D8）
    "ward_context_window": 10,       # 集体层上下文注入条数（老人可读本病房最近 N 条，R5）
    "ward_autoswitch_enabled": True, # 病房位置自动切换总开关（关掉=退回手动；rosbridge 地址在 conf.ROSBRIDGE_URL）
    "ward_switch_debounce": 3,       # 自动切病房防抖：连续 N 次 tick 同病房才认（D18）
    "ward_zone_default_r": 3.0,      # 便捷录入病房区域的半径（米），以当前位姿为圆心采样 16 边形
    "manual_override_sec": 600,      # 手动切病房后，位置判定不覆盖的秒数（D18）
    "ward_map_source": "auto",       # 判定"车在跑哪张图"：auto=/map 指纹反查（默认）；setting=用 current_map
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_settings_roles.py LLM/tests -q`
预期：新文件 3 passed；全量仍是那 4 个既有红态

- [ ] **步骤 5：Commit**

```bash
git add LLM/conf.py LLM/tests/test_settings_roles.py
git commit -m "feat(llm): 补分层用户体系配置项（口令门/TTL/病房上下文/自动切换/地图来源）"
```

---

### 任务 4：`policy.py` —— 角色策略包

**文件：**
- 创建：`LLM/policy.py`
- 测试：`LLM/tests/test_policy_roles.py`（新建）

**只做 P0 需要的**：`POLICY_DEFAULTS` + `role_policy()`。规格 §6.3 的 `check_action()` 属于 P1（依赖 MCP），**不要写空壳函数**——空壳会被后来者误当成"已经实现"。

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_policy_roles.py
# -*- coding: utf-8 -*-
from LLM import policy


def test_policy_keys_cover_three_roles():
    assert set(policy.POLICY_DEFAULTS) == {"admin", "ward", "elder"}


def test_ward_has_only_safety_tools_and_no_personal_scope():
    p = policy.POLICY_DEFAULTS["ward"]
    assert p["data_scope"] == "none"        # 不注入任何老人档案/私人记忆
    assert p["ward_context"] is True        # 但读得到本病房的集体上下文
    # 集体层只接"安全 + 只读"：急停（R3 永远允许）+ 状态只读播报（规格 §3.3 矩阵）。
    # 未识别的说话人会回落到这一层，所以这里**不能**是空列表。
    assert p["allowed_tools"] == ["robot_status", "robot_stop"]


def test_role_policy_returns_copy_not_the_shared_table():
    """策略表是模块级共享常量：调用方原地 append 会污染全局白名单（往低权限角色里长工具）。"""
    a, b = policy.role_policy("ward"), policy.role_policy("ward")
    a["allowed_tools"].append("__注入__")
    assert b["allowed_tools"] == ["robot_status", "robot_stop"]
    assert policy.POLICY_DEFAULTS["ward"]["allowed_tools"] == ["robot_status", "robot_stop"]


def test_elder_reads_self_plus_ward_context():
    p = policy.POLICY_DEFAULTS["elder"]
    assert p["data_scope"] == "self"
    assert p["ward_context"] is True
    assert "robot_stop" in p["allowed_tools"]     # R3：安全动作永远在列


def test_admin_reads_all_without_ward_context():
    p = policy.POLICY_DEFAULTS["admin"]
    assert p["data_scope"] == "all"
    assert p["allowed_tools"] is None             # None = 全部
    assert p["ward_context"] is False


def test_prompt_files_point_into_llm_prompt_dir():
    for role in ("ward", "elder", "admin"):
        f = policy.POLICY_DEFAULTS[role]["prompt_file"]
        assert f.name == f"{role}.md" and f.parent.name == "prompt"


def test_unknown_role_falls_back_to_ward():
    assert policy.role_policy("nope") == policy.POLICY_DEFAULTS["ward"]
    assert policy.role_policy(None) == policy.POLICY_DEFAULTS["ward"]
    assert policy.role_policy("") == policy.POLICY_DEFAULTS["ward"]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_policy_roles.py -q`
预期：FAIL（`ModuleNotFoundError: No module named 'LLM.policy'`）

- [ ] **步骤 3：实现**

```python
# LLM/policy.py
# -*- coding: utf-8 -*-
r"""
角色策略包（分层用户体系）：每个层级 = 一套策略 = 提示词片段 + 工具白名单 + 数据可见范围。

规格：docs/superpowers/specs/2026-09-14-layered-user-roles-design.md §3.2/§3.3
红线：R1 前端 role 不可信（role 只由 session.derive_role 推导）
      R2 fail-closed（未知角色一律按集体层最小能力）
      R5 层级上下文单向（集体层对同病房老人可读；反向与跨病房不可读）

设计取舍：本模块**只放纯数据 + 纯函数**，不做任何 IO（会话状态在 session.py）。
P1 的 check_action()（动作风险分级/二次确认）依赖 car MCP，**不在这里留空壳**。
"""
from pathlib import Path

from .conf import BASE_DIR

PROMPT_DIR = BASE_DIR / "LLM" / "prompt"

POLICY_DEFAULTS: dict[str, dict] = {
    # ---- 集体层：一屋子人。读得到本病房公开对话，读不到任何个人档案 ----
    "ward": {
        "prompt_file": PROMPT_DIR / "ward.md",
        # 只接"安全 + 只读"两类：状态播报与急停。
        # 依据规格 §3.3 能力矩阵：`车·状态查询（位姿/电量）✅ 只读播报`、`车·急停/呼救 ✅ 永远允许（R3）`。
        # 未识别的说话人也会回落到这一层 —— 急停必须可用（空列表 = 连急停都做不了，违反 R3）。
        "allowed_tools": ["robot_status", "robot_stop"],
        "data_scope": "none",
        "ward_context": True,
    },
    # ---- 老人层：本人档案 + 本病房集体上下文（只读、单向）----
    "elder": {
        "prompt_file": PROMPT_DIR / "elder.md",
        # R3：急停/呼救类工具永远在列（当前仅有 robot_stop 是安全动作）
        "allowed_tools": ["robot_status", "robot_stop"],
        "data_scope": "self",
        "ward_context": True,
    },
    # ---- 管理层：全部能力 ----
    "admin": {
        "prompt_file": PROMPT_DIR / "admin.md",
        "allowed_tools": None,          # None = 不按角色裁剪（仍受全局 per-tool 开关约束）
        "data_scope": "all",
        "ward_context": False,
    },
}


def role_policy(role: str | None) -> dict:
    """取角色策略；未知/None/空 → **集体层**（R2 fail-closed）。

    返回**浅拷贝**（`allowed_tools` 也是新 list）：策略表是模块级共享常量，调用方一旦原地
    `append` 就会污染全局白名单 —— 往低权限角色的白名单里长出一条危险工具，比少一条危险得多。
    """
    p = POLICY_DEFAULTS.get(role or "", POLICY_DEFAULTS["ward"])
    return {**p, "allowed_tools": None if p["allowed_tools"] is None else list(p["allowed_tools"])}
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_policy_roles.py -q`
预期：`7 passed`

- [ ] **步骤 5：Commit**

```bash
git add LLM/policy.py LLM/tests/test_policy_roles.py
git commit -m "feat(llm): 新增 policy.py 角色策略包（三层策略 + fail-closed 兜底）"
```

---

### 任务 5：`session.py` —— 双槽主体、角色推导、管理员 TTL

**文件：**
- 创建：`LLM/session.py`（**注意**：与早已存在的 `LLM/voice/session.py`（语音状态机）同名不同物，详见文件结构表下的提醒）
- 测试：`LLM/tests/test_session_roles.py`（新建；**不要**覆盖已有的 `LLM/tests/test_session.py`）

**模型（规格 §3.1/§4.1）：`uid`/`locked` 全端共享，`role` 按槽位隔离。** 这是"管理台登录不会把车前屏提权"（双槽存在的唯一理由）的实现方式：

```
_shared = {"uid", "locked", "ward_uid", "manual_until"}   # 全局：当前主体 + 当前病房
_state[slot] = {"role", "source", "until"}                # 按槽：kiosk | admin
```

**非管理员槽位的角色永远跟着全局主体走**（`get_principal` 里用 `derive_role(_shared["uid"])` 现算），否则会出现"管理台登出后车前屏 role=ward 但 uid 是某位老人"的不一致。

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
    assert session.derive_role("ghost_9") == "ward"      # R2 fail-closed
    assert session.derive_role("") == "ward"
    assert session.derive_role(None) == "ward"


def test_derive_role_not_fooled_by_uid_prefix(d):
    d.upsert_profile("ward_fake", name="其实是老人")      # kind 默认 elder
    assert session.derive_role("ward_fake") == "elder"    # 权威判定查 profiles.kind


def test_two_slots_are_isolated(d):
    session.set_subject("ward_101", locked=False, slot="kiosk")
    session.login_admin("111111", slot="admin")
    assert session.get_principal("kiosk")["role"] == "ward"
    assert session.get_principal("admin")["role"] == "admin"
    assert session.get_principal("kiosk")["uid"] == "ward_101"     # 车前屏不被提权
    assert session.get_principal("admin")["uid"] == "admin"


def test_admin_never_downgraded_by_voiceprint(d):
    session.login_admin("111111", slot="kiosk")
    session.set_subject("elder_101_1", locked=False, slot="kiosk", source="voiceprint")
    p = session.get_principal("kiosk")
    assert p["role"] == "admin" and p["uid"] == "admin"            # D8 提权只升不降


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
    assert p["role"] == "elder" and p["ward_uid"] == "ward_102"     # D18：当前病房跟随老人


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
    assert p["role"] == "ward" and p["uid"] == "ward_101"          # 回落集体层
    assert p["source"] == "expired"


def test_ttl_remain_counts_down(d):
    session.login_admin("111111", slot="kiosk", ttl_s=60)
    assert 0 < session.ttl_remain("kiosk") <= 60
    session.logout("kiosk")
    assert session.ttl_remain("kiosk") is None                      # 非管理员 → None


def test_logout_returns_to_ward_layer(d):
    session.set_subject("ward_101", slot="kiosk")
    session.login_admin("111111", slot="kiosk")
    session.logout("kiosk")
    p = session.get_principal("kiosk")
    assert p["role"] == "ward" and p["uid"] == "ward_101" and p["locked"] is False


def test_manual_switch_to_admin_uid_is_denied(d):
    """**R1 红线回归**：提权只能走 login_admin（口令）。

    主体切换若能把角色变成 admin，`POST /api/session/user {"uid":"admin"}` 就是免口令后门
    （该端点只拒绝 role 字段、CORS 全开）。
    """
    session.set_subject("admin", slot="kiosk", source="manual")
    p = session.get_principal("kiosk")
    assert p["role"] == "ward" and p["uid"] != "admin"


def test_expired_admin_slot_does_not_swallow_voiceprint(d, monkeypatch):
    """过期的 admin 槽不许吞掉一次声纹认人（D8 守卫只对"有效期内"的管理员会话生效）。"""
    session.set_subject("ward_101", slot="kiosk")
    session.login_admin("111111", slot="kiosk", ttl_s=1)
    base = session._now_ts()
    monkeypatch.setattr(session.time, "monotonic", lambda: base + 5)
    session.set_subject("elder_101_1", slot="kiosk", source="voiceprint")
    p = session.get_principal("kiosk")
    assert p["role"] == "elder" and p["uid"] == "elder_101_1"


def test_unknown_slot_is_rejected(d):
    """槽位名非法必须报错，绝不静默回落到 kiosk（否则会把车前屏提权、审计也失真）。"""
    with pytest.raises(ValueError):
        session.get_principal("TABLET")
    with pytest.raises(ValueError):
        session.login_admin("111111", slot="TABLET")


def test_admin_ignored_voiceprint_is_audited(d, monkeypatch):
    calls = []
    monkeypatch.setattr(session.audit, "log", lambda ev, **kw: calls.append((ev, kw)))
    session.login_admin("111111", slot="kiosk")
    session.set_subject("elder_101_1", slot="kiosk", source="voiceprint")
    assert calls and calls[-1][0] == "voice_spk"
    assert calls[-1][1]["action"] == "ignored_in_admin"
    assert calls[-1][1]["slot"] == "kiosk"


def test_auth_disabled_login_needs_no_password(d):
    """口令门关着时：无需口令直接进 admin，且**不再自动降权**（D13）。"""
    d.set_admin_auth_required(False)
    r = session.login_admin(password=None, slot="kiosk")
    assert r["ok"] is True and r["source"] == "auth_disabled" and r["until"] is None
    assert session.get_principal("kiosk")["role"] == "admin"


def test_unknown_uid_does_not_hijack_current_ward(d):
    """fail-closed 判成 ward，但不许把"当前病房"顶成那个不存在的 uid。"""
    session.set_subject("ward_101", slot="kiosk")
    session.set_subject("ghost_9", slot="kiosk", source="voiceprint")
    assert session.get_principal("kiosk")["ward_uid"] == "ward_101"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_session_roles.py -q`
预期：FAIL（`ModuleNotFoundError: No module named 'LLM.session'`）

- [ ] **步骤 3：实现**

```python
# LLM/session.py
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
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_session_roles.py -q`
预期：`16 passed`

- [ ] **步骤 5：Commit**

```bash
git add LLM/session.py LLM/tests/test_session_roles.py
git commit -m "feat(llm): session.py 双槽会话主体 + derive_role + 管理员 TTL"
```

---

### 任务 6：管理员口令的改 / 关 / 开 / 首启生成

**文件：**
- 修改：`LLM/session.py`（追加函数）
- 测试：`LLM/tests/test_session_roles.py`（追加用例）

- [ ] **步骤 1：编写失败的测试**（追加到 `test_session_roles.py` 末尾）

```python
def test_change_password_requires_old(d):
    d.set_admin_password("111111")
    assert session.change_admin_password("wrong", "222222")["ok"] is False
    assert session.change_admin_password("111111", "222222")["ok"] is True
    assert d.verify_admin_password("222222") is True
    assert d.verify_admin_password("111111") is False


def test_change_password_rejects_too_short(d):
    assert session.change_admin_password("111111", "12")["ok"] is False
    assert d.verify_admin_password("111111") is True          # 失败不改动原口令


def test_toggle_auth_required_and_login_without_password(d):
    assert session.set_admin_auth(False)["required"] is False
    r = session.login_admin(password=None, slot="kiosk")
    assert r["ok"] is True and r["source"] == "auth_disabled"
    assert r["ttl_remain"] is None                            # 无口令保护时不再自动降权
    assert session.set_admin_auth(True)["required"] is True


def test_re_enabling_lock_kills_existing_admin_sessions(d):
    """重新开启口令门 = 现有 admin 会话立即作废（强制下次要口令）。"""
    session.login_admin("111111", slot="admin", ttl_s=600)
    session.set_admin_auth(False)
    session.login_admin(password=None, slot="admin")          # 无保护状态下进的管理员
    session.set_admin_auth(True)
    assert session.get_principal("admin")["role"] == "ward"    # 已被踢回


def test_first_boot_generates_random_password(d):
    d._set_setting_raw("admin_password_hash", "")
    d._set_setting_raw("admin_password_salt", "")
    pw = session.ensure_admin_password()
    assert isinstance(pw, str) and len(pw) == 6 and pw.isdigit()
    assert d.verify_admin_password(pw) is True
    assert session.ensure_admin_password() is None             # 已有口令 → 不再生成
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_session_roles.py -q`
预期：FAIL（`AttributeError: module 'LLM.session' has no attribute 'change_admin_password'`）

- [ ] **步骤 3：实现**（追加到 `session.py` 末尾）

```python
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
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_session_roles.py -q`
预期：`21 passed`

- [ ] **步骤 5：Commit**

```bash
git add LLM/session.py LLM/tests/test_session_roles.py
git commit -m "feat(llm): 管理员口令可改/可开关 + 重开门即作废旧 admin 会话 + 首启随机口令"
```

---

### 任务 7：当前病房与位置自动切换（复用一期 locator）

**文件：**
- 修改：`LLM/session.py`（追加；并改 `reset_for_test`）
- 测试：`LLM/tests/test_ward_autoswitch.py`（新建）

**三条语义（规格 D17/D18）：**
1. **防抖**：连续 N 次 tick（≈N 秒）落在同一病房才认；离开病房区（走廊）**不切**。
2. **不抢私聊**：仅当 kiosk 槽 `role=="ward"` 且未锁定时才真正改主体；否则只更新"当前病房"。
3. **拿不到就停用**：位姿不可用 / 认不出当前图 / 该病房没关联区域 / 该区域不在当前图上 → 一律不判命中。

**两个性能约束（v2 新增，照旧版写会每秒打网络）：**
- tick **只读本地 `brain.db.zones` 缓存**（`db.list_zones`），**不**调 `maptags.get_zones()`（那条路径含 `_ensure_fresh → sync_map`，`MAPS_IO=ssh` 下可能秒级阻塞）；
- `locator.current_map()` 会列图 + 逐图读元数据，结果做 **10s TTL 缓存**。

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_ward_autoswitch.py
# -*- coding: utf-8 -*-
"""病房位置自动切换测试：全部注入假位姿，**不需要 ROS / rosbridge / 板卡**。"""
import json
import os
import tempfile

import pytest

from LLM import db
from LLM import locator
from LLM import session

_MAP = "my_map"

_SETTINGS = {"ward_autoswitch_enabled": True, "ward_switch_debounce": 3,
             "manual_override_sec": 600, "ward_map_source": "setting",
             "current_map": _MAP, "admin_session_ttl_s": 300}


def _seed_zones(map_name: str, zones: list[dict]) -> None:
    """整图重建缓存（`replace_map_tags` 是整图级的：一次把该图所有区域一起传）。"""
    db.replace_map_tags(
        map_name,
        {"file_mtime": 0, "file_size": 0, "sha1": "", "resolution": 0.05,
         "origin": {"x": 0.0, "y": 0.0}, "warnings": []},
        [],
        zones,
    )


def _zone(uid: str, name: str, poly: list) -> dict:
    return {"uid": uid, "name": name, "kind": "ward", "shape": "polygon",
            "polygon_json": json.dumps(poly), "parent": "", "note": "",
            "created_at": "", "updated_at": ""}


@pytest.fixture()
def d(monkeypatch):
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    session.reset_for_test()
    locator.clear_injection()
    _seed_zones(_MAP, [
        _zone("z1", "101", [[-3.0, -3.0], [3.0, -3.0], [3.0, 3.0], [-3.0, 3.0]]),
        _zone("z2", "102", [[17.0, -3.0], [23.0, -3.0], [23.0, 3.0], [17.0, 3.0]]),
    ])
    db.upsert_ward("ward_101", name="101 病房", ward_map=_MAP, ward_zone="z1")
    db.upsert_ward("ward_102", name="102 病房", ward_map=_MAP, ward_zone="z2")
    db.upsert_profile("elder_101_1", name="李爷爷")
    db.set_profile_ward("elder_101_1", "ward_101")
    monkeypatch.setattr(session, "_settings", lambda: dict(_SETTINGS))
    session.set_subject("ward_101", slot="kiosk")
    locator.set_pose_for_test(0.0, 0.0, 0.0)          # 起点在 101 里
    yield db
    db.DB_PATH = old
    locator.clear_injection()


def test_debounce_requires_repeated_ticks(d):
    locator.set_pose_for_test(20.0, 0.0, 0.0)          # 进入 102
    session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"    # 第 1 次不切
    session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"    # 第 2 次不切
    session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_102"    # 第 3 次才切
    assert session.current_ward() == "ward_102"


def test_debounce_resets_when_leaving_zone(d):
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    session.tick(); session.tick()
    locator.set_pose_for_test(99.0, 99.0, 0.0)         # 走廊
    session.tick()
    locator.set_pose_for_test(20.0, 0.0, 0.0)          # 又回 102
    session.tick(); session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"    # 计数被清零，重新数满 3 次
    session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_102"


def test_leaving_zone_does_not_switch(d):
    locator.set_pose_for_test(99.0, 99.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"


def test_ward_without_zone_never_switches(d):
    """病房没关联区域（清空 ward_map/ward_zone）→ 位置判定不命中（fail-safe）。"""
    db.set_ward_zone("ward_102", "", "")          # 显式清空关联（空串在 upsert_ward 里=保持原值）
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"


def test_ward_zone_on_other_map_never_switches(d):
    """病房区域绑在**别的地图**上 → 不判命中（防止换图后静默切错病房）。"""
    db.upsert_ward("ward_102", name="102 病房", ward_map="other_map", ward_zone="z2")
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"


def test_does_not_steal_elder_private_chat(d):
    session.set_subject("elder_101_1", slot="kiosk", source="voiceprint")
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    p = session.get_principal("kiosk")
    assert p["role"] == "elder" and p["uid"] == "elder_101_1"     # 私聊不被打断
    assert session.current_ward() == "ward_102"                   # 但"当前病房"已更新
    # 退出私聊回集体层时，用的是新病房
    session.set_subject("ward_102", slot="kiosk")
    assert session.get_principal("kiosk")["uid"] == "ward_102"


def test_locked_session_holds_even_in_ward_layer(d):
    session.set_subject("ward_101", locked=True, slot="kiosk")
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"
    assert session.current_ward() == "ward_102"
    assert session.autoswitch_state()["reason"] == "holding_session"


def test_no_pose_disables_autoswitch(d):
    locator.set_pose_for_test(None)
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"
    assert session.autoswitch_state() == {"enabled": False, "reason": "no_pose"}


def test_disabled_by_setting(d, monkeypatch):
    monkeypatch.setattr(session, "_settings",
                        lambda: {**_SETTINGS, "ward_autoswitch_enabled": False})
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.current_ward() == "ward_101"
    assert session.autoswitch_state()["reason"] == "disabled"


def test_manual_override_blocks_location(d):
    session.manual_set_ward("ward_101")
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.current_ward() == "ward_101"
    assert session.autoswitch_state()["reason"] == "manual_override"


def test_auto_map_source_uses_fingerprint(d, monkeypatch):
    """默认 ward_map_source='auto'：地图名来自 locator.current_map()（/map 指纹反查）。"""
    monkeypatch.setattr(session, "_settings",
                        lambda: {**_SETTINGS, "ward_map_source": "auto"})
    monkeypatch.setattr(session.locator, "current_map",
                        lambda *a, **k: {"ok": True, "source": "map_topic", "name": _MAP})
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(3):
        session.tick()
    assert session.current_ward() == "ward_102"


def test_unknown_map_disables_autoswitch(d, monkeypatch):
    """指纹认不出当前图 → 不切（宁可不切也不误切）。"""
    monkeypatch.setattr(session, "_settings",
                        lambda: {**_SETTINGS, "ward_map_source": "auto"})
    monkeypatch.setattr(session.locator, "current_map",
                        lambda *a, **k: {"ok": True, "source": "unknown", "name": None})
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.current_ward() == "ward_101"
    assert session.autoswitch_state()["reason"] == "map_unknown"


def test_divergent_slot_role_does_not_steal_private_chat(d, monkeypatch):
    """**回归（关键）**：位置判定必须用**有效角色**判断能不能改主体。

    `_expire_if_needed()`（admin TTL 到期）会把槽位原始 role 直接写成 `"ward"` 而**不重算**，
    而 `tick()` 的第一步恰好就是 `_expire_if_needed()`。拿原始 role 判定，就会把正在私聊的老人
    静默换成病房主体（D18 明令不抢私聊）。
    """
    session.set_subject("elder_101_1", slot="kiosk", source="voiceprint")
    session.login_admin("111111", slot="kiosk", ttl_s=1)      # kiosk 槽变 admin（主体仍是李爷爷）
    base = session._now_ts()
    monkeypatch.setattr(session.time, "monotonic", lambda: base + 5)
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "elder_101_1"   # 主体不许被换掉
    assert session.current_ward() == "ward_102"                     # 背景病房照常更新


def test_manual_override_expiry_needs_full_recount(d, monkeypatch):
    """**回归**：手动覆盖期内不攒计数 —— 覆盖一到期，不许"一个采样就切"（防抖不许跨 epoch 续数）。"""
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    session.tick()
    session.tick()                                            # 已攒 2 次（debounce=3）
    session.manual_set_ward("ward_101")                       # 手动覆盖 → 计数清零
    monkeypatch.setattr(session, "_now_ts", lambda: session.time.monotonic() + 10_000)
    session.tick()
    assert session.current_ward() == "ward_101"               # 第 1 次不许切
    session.tick()
    assert session.current_ward() == "ward_101"               # 第 2 次不许切
    session.tick()
    assert session.current_ward() == "ward_102"               # 数满 3 次才切


def test_current_map_is_cached_within_ttl(d, monkeypatch):
    """**性能红线回归**：`locator.current_map()`（含列图 + 逐图远端 stat + 全图像素统计）
    在 TTL 窗口内只准调一次 —— 它每秒跑在热路径上。"""
    monkeypatch.setattr(session, "_settings",
                        lambda: {**_SETTINGS, "ward_map_source": "auto"})
    calls = []
    monkeypatch.setattr(session.locator, "current_map",
                        lambda *a, **k: calls.append(1) or
                        {"ok": True, "source": "map_topic", "name": _MAP})
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(30):
        session.tick()
    assert len(calls) == 1
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_ward_autoswitch.py -q`
预期：FAIL（`AttributeError: module 'LLM.session' has no attribute 'tick'`）

- [ ] **步骤 3：实现**

**(3a)** `session.py` 顶部导入区补上：

```python
from . import bus
from . import locator
from . import zonegeo
```

**(3b)** 把 `reset_for_test` 改为也清缓存（否则上一个用例的防抖计数会漏到下一个用例）：

```python
def reset_for_test() -> None:
    """单测用：清空全部会话状态与病房判定缓存。"""
    _shared.update({"uid": "", "locked": False, "ward_uid": "", "manual_until": 0.0})
    _state.clear()
    _candidate.clear()
    _map_cache.update({"name": "", "at": float("-inf"), "reason": ""})
```

**(3c)** 追加到 `session.py` 末尾：

```python
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
    """给前端/诊断用：位置自动切换**当前为什么没生效**（判定顺序与 `_ward_tick` 保持一致）。"""
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
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_ward_autoswitch.py LLM/tests/test_session_roles.py -q`
预期：`15 passed` + `25 passed`

- [ ] **步骤 5：跑回归 + Commit**

```bash
.venv\Scripts\python.exe -m pytest LLM/tests tests -q
# 预期仍是那 4 个既有红态
git add LLM/session.py LLM/tests/test_ward_autoswitch.py
git commit -m "feat(llm): 病房位置自动切换（防抖/手动覆盖/不抢私聊/跟随老人/拿不到即停用）"
```

---

### 任务 8：提示词分层与集体层上下文注入

**文件：**
- 创建：`LLM/prompt/ward.md`、`LLM/prompt/elder.md`、`LLM/prompt/admin.md`
- 修改：`LLM/chat.py`（`build_system` 第 195-214 行；`build_messages` 第 217-224 行；`chat_stream` 第 306/322 行）
- 测试：`LLM/tests/test_prompt_layers.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_prompt_layers.py
# -*- coding: utf-8 -*-
"""三层提示词分层 + 集体上下文单向注入（R5）测试。"""
import os
import tempfile
from pathlib import Path

import pytest

from LLM import chat
from LLM import db


@pytest.fixture()
def d(monkeypatch):
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    db.upsert_ward("ward_101", name="101 病房")
    db.upsert_ward("ward_102", name="102 病房")
    db.upsert_profile("elder_101_1", name="张奶奶")
    db.set_profile_ward("elder_101_1", "ward_101")
    db.upsert_profile("elder_102_1", name="王奶奶")
    db.set_profile_ward("elder_102_1", "ward_102")
    for i in range(3):
        db.append_history("ward_101", "user", f"病房消息{i}")
    # 离线：不让 RAG 真去调 embedding（本测试只验装配，不验召回）
    monkeypatch.setattr(chat.rag, "recall_v3", lambda uid, q: {"context": ""})
    yield db
    db.DB_PATH = old


def _p(role, uid, ward=""):
    return {"role": role, "uid": uid, "slot": "kiosk", "source": "manual",
            "locked": False, "ward_uid": ward}


def test_ward_role_fragment_is_loaded(d):
    sys_p = chat.build_system("ward_101", {}, "", principal=_p("ward", "ward_101", "ward_101"))
    assert "病房里" in sys_p                       # ward.md 正文出现
    assert "小护" in sys_p                         # 共用 base 仍在


def test_ward_prompt_has_no_personal_profile(d):
    d.upsert_profile("elder_101_1", name="张奶奶", notes="糖尿病")
    sys_p = chat.build_system("ward_101", {}, "", principal=_p("ward", "ward_101", "ward_101"))
    assert "张奶奶" not in sys_p and "糖尿病" not in sys_p


def test_elder_sees_own_ward_context(d):
    sys_p = chat.build_system("elder_101_1", {}, "",
                              principal=_p("elder", "elder_101_1", "ward_101"))
    assert "病房消息2" in sys_p


def test_ward_does_not_see_elder_private_chat(d):
    db.append_history("elder_101_1", "user", "我昨晚没睡好")
    sys_p = chat.build_system("ward_101", {}, "", principal=_p("ward", "ward_101", "ward_101"))
    assert "我昨晚没睡好" not in sys_p


def test_cross_ward_isolation(d):
    """102 病房的老人看不到 101 病房的集体上下文（R5：跨病房不可读）。"""
    db.append_history("ward_102", "user", "102 病房的消息")
    sys_p = chat.build_system("elder_102_1", {}, "",
                              principal=_p("elder", "elder_102_1", "ward_102"))
    assert "病房消息2" not in sys_p and "102 病房的消息" in sys_p


def test_admin_gets_no_ward_context(d):
    sys_p = chat.build_system("admin", {}, "", principal=_p("admin", "admin"))
    assert "病房消息2" not in sys_p


def test_missing_role_file_degrades(d, monkeypatch):
    monkeypatch.setattr(chat, "_ROLE_PROMPT_DIR_OVERRIDE", Path("/nonexistent-dir"))
    sys_p = chat.build_system("ward_101", {}, "", principal=_p("ward", "ward_101", "ward_101"))
    assert "小护" in sys_p                          # base 仍在，没抛异常


def test_unknown_role_falls_back_to_ward_fragment(d):
    sys_p = chat.build_system("ward_101", {}, "", principal=_p("??", "ward_101", "ward_101"))
    assert "病房里" in sys_p
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_prompt_layers.py -q`
预期：FAIL（`TypeError: build_system() got an unexpected keyword argument 'principal'`）

- [ ] **步骤 3：实现**

**(3a)** 新建 `LLM/prompt/ward.md`：

```markdown
你现在是在**一个病房里**跟"大家"说话，不是跟某一位老人私聊。屋里可能有好几个人，也可能有人刚进来、有人正躺着。

1. 说给大家听：打招呼、报时、提醒吃药时间、公布消息（"今天下午三点体检"），有事说事，一句话讲完。
2. 别问谁是谁，也别替谁说他的私事。有人问"我的药""我是谁"这类个人问题 → 回他"这个我得单独跟本人说"。
3. 认出了具体是哪位老人，你就会被切到他的私聊里——那时候再聊他的事，现在不用打听。
4. 你不认识屋里的人，所以别用"张奶奶"这种称呼点名，用"您""大家"。
5. 安全红线照旧：有人说不舒服、胸口疼、摔倒 → 先安抚，再说"我这就去通知护士"。
```

**(3b)** 新建 `LLM/prompt/elder.md`：

```markdown
你现在是跟一位具体的老人单独说话（不是在病房里跟大家广播）。

1. 称呼他，聊他的事：吃什么、睡得好不好、想谁了、身上哪儿不舒服。
2. 〔病房里刚说过的事〕那一段，是他也在场听过的，可以顺着接着聊，不用从头再说一遍。
3. 他私下跟你说的话**只留在你俩之间**——不要拿到病房里当众说，也不要主动提"您刚才说的大家都知道了"。
4. 说话短、说人话、别背书，红线照旧：医疗只读、危险信号先安抚再叫护士。
```

**(3c)** 新建 `LLM/prompt/admin.md`：

```markdown
你现在是跟**管理员**说话，不是跟老人。

1. 直接给结论和数字：状态、参数、结果，一句话报完，不用寒暄、不用哄、不用卖萌。
2. 可以用术语和缩写（位姿、AMCL、/cmd_vel、TTL 等），管理员看得懂。
3. 管理员问"现在系统是什么状态"时，按事实回答：哪个层级在用、当前病房、口令门是否开着、位置源是否可用。
4. 危险动作执行完要回报结果（成功/失败/为什么失败），失败要给出下一步建议。
5. 老人隐私红线不因身份而取消：医疗信息仍只读。
```

**(3d)** `chat.py`：顶部新增 `from .policy import role_policy`，并在 `_load_prompt_base` 之后追加两个函数：

```python
_prompt_role_warned: set[str] = set()
_ROLE_PROMPT_DIR_OVERRIDE = None      # 测试用：指向不存在的目录以验证降级


def _load_role_prompt(role: str) -> str:
    """读 `LLM/prompt/<role>.md` 角色片段；缺失 → 空串 + 告警一次（**不阻断对话**）。"""
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
    """本病房集体层最近 N 条（R5 单向：**只从这里往外读**，绝不把私聊灌进来）。"""
    if not ward_uid:
        return ""
    rows = db.load_history(ward_uid, limit=limit)
    lines = [f"{r['role']}: {r['content']}" for r in rows if (r.get("content") or "").strip()]
    if not lines:
        return ""
    return ("【病房里刚说过的事（这位老人在场听过；只读参考，别当私事追问）】\n"
            + "\n".join(lines))
```

**(3e)** `build_system` 改为（**保持 `uid` 老签名兼容**，新增 `principal`）：

```python
def build_system(uid: str, settings: dict, query: str = "", principal: dict | None = None) -> str:
    """组装 System Prompt：base（人设+安全红线）+ 角色片段 + 记忆/上下文 + 当前时间。

    principal 缺省时取 kiosk 槽（兼容旧调用点）；**权限相关的取舍只看 principal（R1）**。
    """
    from . import session as session_mod
    p = principal or session_mod.get_principal("kiosk")
    pol = role_policy(p.get("role"))
    scope = pol["data_scope"]

    parts = [_load_prompt_base()]
    role_txt = _load_role_prompt(p.get("role"))
    if role_txt:
        parts.append("\n" + role_txt)

    if scope == "self":                      # 老人层：本人档案/记忆/画像
        recall_ctx = _recall_cached(uid, query) if query else rag.recall_v3(uid, "")["context"]
        parts.append("\n【我了解到的关于这位老人的信息（来自档案/记忆，可能不全或过时，仅供参考）】\n"
                     + recall_ctx)

    if pol["ward_context"]:                  # 老人层：本病房集体上下文（只读、单向）
        ward_txt = _ward_context(p.get("ward_uid", ""),
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

> **集体层的 `uid` 就是它自己的 uid**（`ward_101`）：历史与记忆本来就按 uid 存，所以集体层不需要新存储；而 `ward` 的 `data_scope="none"` 保证**不会**注入任何老人档案。

**(3f)** `build_messages` / `chat_stream` 透传 `principal`：

```python
def build_messages(uid: str, user_text: str, thinking_on: bool, settings: dict,
                   principal: dict | None = None) -> list[dict]:
    """上下文管理：滚动窗口取最近 N 条 + System Prompt + 本次用户消息。"""
    history = db.load_history(uid, limit=HISTORY_WINDOW)
    system = build_system(uid, settings, query=_build_query(user_text, history),
                          principal=principal)
    if thinking_on:
        system += "\n" + ROUTER_HIT
    return [{"role": "system", "content": system}, *history,
            {"role": "user", "content": user_text}]


def chat_stream(client, model: str, uid: str, user_text: str, thinking: str, settings: dict,
                principal: dict | None = None):
    ...
    messages = build_messages(uid, user_text, thinking_on, settings, principal=principal)
    tools = tool_mod.effective_tools(settings, principal)
```

（`chat_stream` 里其它位置不动；第 322-323 行的这两行是唯一改动点，任务的第 9 步会再来看一眼 `tools` 那一行。）

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_prompt_layers.py -q`
预期：`8 passed`

- [ ] **步骤 5：Commit**

```bash
git add LLM/prompt LLM/chat.py LLM/tests/test_prompt_layers.py
git commit -m "feat(llm): 三层提示词分层 + 集体层上下文单向注入（R5）"
```

---

### 任务 9：工具角色白名单（闸门 2）

**文件：**
- 修改：`LLM/tools.py`（`tool()` 第 31-44 行；`effective_tools` 第 83-92 行；`run_tool` 第 118-128 行）
- 修改：`LLM/chat.py`（`effective_tools` 调用点、`run_tool` 调用点）
- 测试：`LLM/tests/test_policy_tools.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_policy_tools.py
# -*- coding: utf-8 -*-
"""工具角色白名单（闸门 2）测试。

⚠️ `LLM/tool/` 目前**没有任何 `@tool` 注册**（只有两个 `_` 开头的 demo），`_TOOL_REGISTRY` 是空的
—— 直接断言"哪些工具可见"会**空转变绿**。所以下面注册一个只在测试期存在的探针工具，测完摘干净。
"""
import pytest

from LLM import tools


def _p(role):
    return {"role": role, "uid": "u", "slot": "kiosk", "ward_uid": "", "locked": False}


def _names(settings, principal):
    return [t["function"]["name"] for t in tools.effective_tools(settings, principal)]


@pytest.fixture()
def probe():
    """注册一个测试期探针工具（默认 enabled=True、不限角色），测完摘干净。"""
    tools.tool("__probe__", "探针（仅测试）", {})(lambda **kw: {"ok": True, "message": "probe"})
    yield "__probe__"
    tools._TOOL_REGISTRY.pop("__probe__", None)
    tools.TOOL_DEFAULTS.pop("__probe___enabled", None)


def test_ward_gets_only_safety_tools(probe):
    """集体层（含未识别说话人的兜底）只拿安全工具：急停 + 状态只读（R3 + 规格 §3.3 矩阵）。"""
    names = set(_names({}, _p("ward")))
    assert "__probe__" not in names
    assert names <= {"robot_status", "robot_stop"}


def test_elder_only_gets_whitelisted_tools(probe):
    names = set(_names({}, _p("elder")))
    assert "__probe__" not in names
    assert names <= {"robot_status", "robot_stop"}


def test_admin_sees_registered_tool(probe):
    """admin 的 allowed_tools=None → 不被角色裁剪（仍受 per-tool 开关约束）。"""
    assert "__probe__" in _names({}, _p("admin"))


def test_global_switch_still_applies(probe):
    assert "__probe__" not in _names({"__probe___enabled": False}, _p("admin"))


def test_tool_roles_field_filters_even_for_admin():
    """`@tool(roles=...)` 是闸门 2 的第二道：admin 不被角色白名单裁剪，但仍受工具自己的 roles 约束。"""
    tools.tool("__probe_elder_only__", "探针", {}, roles={"elder"})(lambda **kw: {"ok": True})
    try:
        assert "__probe_elder_only__" not in _names({}, _p("admin"))
        assert "__probe_elder_only__" in _names({}, _p("elder"))
    finally:
        tools._TOOL_REGISTRY.pop("__probe_elder_only__", None)
        tools.TOOL_DEFAULTS.pop("__probe_elder_only___enabled", None)


def test_unknown_role_falls_back_to_ward(probe):
    assert _names({}, {"role": "??"}) == _names({}, _p("ward"))
    assert _names({}) == _names({}, _p("ward"))     # principal 缺省 → 集体层（R2）


def test_run_tool_denies_out_of_whitelist(probe):
    res = tools.run_tool("__probe__", {}, _p("ward"))
    assert res["ok"] is False and "不允许" in (res.get("error") or res.get("message") or "")
    assert tools.run_tool("__probe__", {}, _p("admin"))["ok"] is True


def test_mcp_tools_default_to_admin_only(monkeypatch):
    """MCP 工具未声明 roles → 只有 admin 看得到（不受控外部能力，默认从严）。"""
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
        "fetch_html": {"server": "fetch", "schema": {
            "type": "function",
            "function": {"name": "fetch_html", "description": "", "parameters": {}}}}})
    assert "fetch_html" in _names({"mcp_enabled": True}, _p("admin"))
    assert "fetch_html" not in _names({"mcp_enabled": True}, _p("ward"))
    assert "fetch_html" not in _names({"mcp_enabled": True}, _p("elder"))
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_policy_tools.py -q`
预期：FAIL（`TypeError: effective_tools() takes 1 positional argument but 2 were given`）

- [ ] **步骤 3：实现**

**(3a)** `@tool` 增加 `roles`（`None`=不限角色，现有工具不改即行为兼容），并存入 registry：

```python
def tool(name: str, description: str, parameters: dict, enabled: bool = True,
         roles: set[str] | None = None):
    """注册一个工具。`roles=None` = 不限角色（现有工具保持兼容）；空集 = 谁都不给。

    角色白名单只是**闸门 2**（与全局 per-tool 开关取交集）；动作风险分级属 P1 的
    `policy.check_action()`（闸门 3），本函数不做。
    """
    def deco(fn):
        _TOOL_REGISTRY[name] = {
            "schema": {
                "type": "function",
                "function": {"name": name, "description": description, "parameters": parameters},
            },
            "fn": fn,
            "enabled": enabled,
            "roles": None if roles is None else set(roles),
        }
        return fn
    return deco
```

**(3b)** `effective_tools` 改为按「全局开关 ∩ 角色白名单」过滤；MCP 工具按服务器 `roles` 过滤（未声明 = 仅 admin）：

```python
def _mcp_tools_for(settings: dict, role: str) -> list[dict]:
    """MCP 工具按 `conf.MCP_SERVERS[server]["roles"]` 过滤；未声明视为 {"admin"}（从严）。"""
    from .conf import MCP_SERVERS
    out = []
    for name, entry in mcp_client.tools().items():
        if name in _TOOL_REGISTRY:
            continue                        # 与本地重名时本地优先（沿用旧口径）
        roles = MCP_SERVERS.get(entry.get("server", ""), {}).get("roles") or {"admin"}
        if role in roles:
            out.append(entry["schema"])
    return out


def effective_tools(settings: dict, principal: dict | None = None) -> list[dict]:
    """闸门 2：per-tool 开关 ∩ 角色白名单。`principal` 缺省 → 集体层（R2 fail-closed）。"""
    from .policy import role_policy
    role = (principal or {}).get("role")
    allow = role_policy(role)["allowed_tools"]      # None = 不按角色裁剪；[] = 一个都不给
    out = []
    for name, reg in _TOOL_REGISTRY.items():
        if not settings.get(f"{name}_enabled", reg["enabled"]):
            continue
        if allow is not None and name not in allow:
            continue
        if reg.get("roles") is not None and (role or "") not in reg["roles"]:
            continue
        out.append(reg["schema"])
    if settings.get("mcp_enabled"):
        out += _mcp_tools_for(settings, role or "")
    return out
```

> `tools_with_state()`（给设置页用）**不动**：它是"有哪些工具、开关在哪"，不是"这个人能用什么"。

**(3c)** `run_tool` 入口做**二次校验**（防绕过闸门 2 的直调点），拒绝时给模型一句可复述的理由并落审计：

```python
def run_tool(name: str, args: dict, principal: dict | None = None) -> dict:
    """统一分发。**执行前再校验一次角色白名单**（闸门 2 第二道）。"""
    from .policy import role_policy
    from . import log as audit
    p = principal or {}
    allow = role_policy(p.get("role"))["allowed_tools"]
    reg = _TOOL_REGISTRY.get(name)
    if allow is not None and name not in allow:
        audit.log("policy_deny", tool=name, role=p.get("role"), uid=p.get("uid"),
                  slot=p.get("slot"), reason="out_of_role_whitelist")
        return {"ok": False, "error": f"当前身份不允许调用工具 {name}"}
    if reg is not None and reg.get("roles") is not None and (p.get("role") or "") not in reg["roles"]:
        audit.log("policy_deny", tool=name, role=p.get("role"), uid=p.get("uid"),
                  slot=p.get("slot"), reason="tool_roles_mismatch")
        return {"ok": False, "error": f"当前身份不允许调用工具 {name}"}
    if not reg:
        # 不在本地注册表 → 尝试 MCP 工具（未注册/未连接时 mcp_client 返回 ok=False）
        if name in mcp_client.tools():
            return mcp_client.call_tool(name, args or {})
        return {"ok": False, "message": f"未知工具 {name}"}
    try:
        return _run_fn(reg["fn"], args or {})
    except Exception as e:
        return {"ok": False, "message": f"工具执行失败: {e}"}
```

**(3d)** `chat.py` 两处调用改为带 `principal`：

```python
    tools = tool_mod.effective_tools(settings, principal)          # chat_stream 内
    ...
                    result = tool_mod.run_tool(name, args, principal)   # 工具循环内
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests -q`
预期：新增 `6 passed`；全量仍是那 4 个既有红态

- [ ] **步骤 5：Commit**

```bash
git add LLM/tools.py LLM/chat.py LLM/tests/test_policy_tools.py
git commit -m "feat(llm): 工具角色白名单（闸门 2，含 run_tool 二次校验与审计）"
```

---

### 任务 10：集体层不沉淀记忆 + 语音链路接入角色

**文件：**
- 修改：`LLM/memory.py`（`note_turn()` 第 173 行）
- 修改：`LLM/voice_api.py`（`_session_uid`/`_session_locked` 第 51-81 行、`_stream_fn` 第 103-108 行）
- 修改：`LLM/voice/worker.py`（`_handle_speech` 第 341-362 行）
- 修改：`LLM/server.py`（`_post_chat_jobs` 第 113-136 行，本任务只改签名，调用点在任务 11 接上）
- 测试：`LLM/tests/test_ward_memory.py`、`LLM/tests/test_worker_roles.py`（均新建）

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
    yield db
    db.DB_PATH = old


def test_ward_turn_is_never_buffered(d):
    memory._pending_turns.pop("ward_101", None)
    memory.note_turn("ward_101", "小车", "明天九点体检", None, "", {}, role="ward")
    assert memory._pending_turns.get("ward_101") is None      # 连缓冲都不进
    assert db.list_memories("ward_101") == []


def test_elder_turn_is_still_buffered(d):
    memory._pending_turns.pop("elder_101_1", None)
    memory.note_turn("elder_101_1", "我", "我不爱吃甜的", None, "",
                     {"memory_consolidation_enabled": False}, role="elder")
    assert memory._pending_turns.get("elder_101_1")           # 老人层照旧进缓冲


def test_default_role_keeps_old_behavior(d):
    """不传 role 的老调用点（如旧测试/其他调用方）行为不变。"""
    memory._pending_turns.pop("elder_002", None)
    memory.note_turn("elder_002", "我", "话", None, "",
                     {"memory_consolidation_enabled": False})
    assert memory._pending_turns.get("elder_002")
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
    db.upsert_ward("ward_101", name="101 病房")
    db.upsert_profile("elder_101_1", name="李爷爷")
    db.set_profile_ward("elder_101_1", "ward_101")
    yield db
    db.DB_PATH = old


def test_voice_api_session_roundtrips_through_session(d):
    res = voice_api.set_session_uid("ward_101", False)
    assert res["uid"] == "ward_101" and res["role"] == "ward"
    got = voice_api.get_session_uid()
    assert got["uid"] == "ward_101" and got["role"] == "ward"
    assert got["locked"] is False
    assert "autoswitch" in got and "ttl_remain" in got


def test_voice_api_is_the_same_session_as_module(d):
    voice_api.set_session_uid("elder_101_1", True)
    assert session.get_principal("kiosk")["role"] == "elder"
    assert session.get_principal("kiosk")["locked"] is True
    assert session.get_principal("kiosk")["ward_uid"] == "ward_101"   # 跟随老人


def test_empty_uid_reads_as_none_for_old_callers(d):
    """老前端拿 uid=null 表示"没选人"；集体层没有病房时保持这个形状。"""
    session.reset_for_test()
    assert voice_api.get_session_uid()["uid"] in (None, "")


def test_worker_admin_is_not_downgraded(d):
    """管理员在车前说话 → 声纹认到老人也不改主体（D8 提权只升不降）。"""
    from LLM.voice import worker as worker_mod
    d.set_admin_password("111111")
    session.login_admin("111111", slot="kiosk")
    w = worker_mod.VoiceWorker(stream_fn=lambda uid, text: iter(()))
    w._apply_role_subject("elder_101_1")
    assert session.get_principal("kiosk")["role"] == "admin"
    assert session.get_principal("kiosk")["uid"] == "admin"


def test_worker_sets_voiceprint_subject_when_not_admin(d):
    """非管理员：认出谁就切到谁（source=voiceprint）。"""
    from LLM.voice import worker as worker_mod
    calls = []
    orig = session.set_subject

    def spy(uid, locked=False, slot="kiosk", source="manual"):
        calls.append((uid, locked, slot, source))
        return orig(uid, locked, slot, source)

    session.set_subject = spy                       # 函数级替换，测完还原
    try:
        w = worker_mod.VoiceWorker(stream_fn=lambda uid, text: iter(()))
        w._apply_role_subject("elder_101_1")
    finally:
        session.set_subject = orig
    assert calls and calls[0][0] == "elder_101_1" and calls[0][3] == "voiceprint"
    assert session.get_principal("kiosk")["role"] == "elder"
    assert session.get_principal("kiosk")["ward_uid"] == "ward_101"   # 当前病房跟随该老人
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_ward_memory.py LLM/tests/test_worker_roles.py -q`
预期：FAIL（`TypeError: note_turn() got an unexpected keyword argument 'role'`）

- [ ] **步骤 3：实现**

**(3a)** `memory.py::note_turn` 签名加 `role: str = "elder"`，函数体**第一行**早退：

```python
def note_turn(uid: str, user_text: str, assistant_text: str, client, model: str,
              settings: dict, role: str = "elder"):
    """把本轮对话放进"待整理"缓冲。

    集体层（role="ward"）**直接返回**：病房里的公开对话只作集体上下文，绝不沉淀成
    任何一位老人的记忆（规格 §5.3，R5 的另一半）。
    """
    if role == "ward":
        return
    ...（原有实现不动）
```

**(3b)** `voice_api.py`：会话持有权交给 `session`，**保留同名函数**（调用点一个都不用改）：

```python
def set_session_uid(uid: str, locked: bool) -> dict:
    """手动切换当前会话主体（规格 D11）。

    会话主体与角色的持有权已移交 `session.py`；本函数保留同名接口做转发，
    现有调用点（server 路由 / worker）不变；同时同步 worker 的锁定用户。
    """
    from . import session as session_mod
    res = session_mod.set_subject(uid, bool(locked), slot="kiosk", source="manual")
    if _worker is not None:
        try:
            _worker.locked_uid = uid if locked else None
        except Exception:
            pass
    audit.log("session", action="set_uid", uid=uid, locked=bool(locked), by="nurse")
    return res


def get_session_uid() -> dict:
    """当前会话主体（含角色/槽位/当前病房/TTL/自动切换状态）。"""
    from . import session as session_mod
    p = session_mod.get_principal("kiosk")
    uid = p["uid"] or None
    if uid is None and _worker is not None:      # 兼容旧行为：没主体时看一眼声纹判定结果
        uid = getattr(_worker, "current_uid", None)
    return {"ok": True, "uid": uid, "locked": p["locked"], "source": p["source"],
            "role": p["role"], "slot": p["slot"], "ward_uid": p["ward_uid"],
            "ttl_remain": session_mod.ttl_remain("kiosk"),
            "auth_required": db.get_admin_auth()["required"],
            "autoswitch": session_mod.autoswitch_state()}
```

> `voice_api.py` 里 `_session_uid`/`_session_locked` 两个模块变量随之删除（它们已无人读）。
> 注意 `uid=null` 的老语义保留：**没有病房也没主体时** `uid` 仍是 `None`，避免老前端误显示。

**(3c)** `_stream_fn` 里把 principal 传给 `chat_stream`（`voice_api.py` 里没有同名冲突，但仍用 `role_session` 别名，避免与 `LLM.voice.session` 混淆）：

```python
    def _fn(uid, text):
        settings = db.get_settings()
        from . import session as role_session
        return chat.chat_stream(client, model, uid, text, "auto", settings,
                                principal=role_session.get_principal("kiosk"))
```

**(3d)** `voice/worker.py::_handle_speech`：把"谁在说话"的决定抽成一个可单测的小方法，并在里面按角色分支。

> ⚠️ **名字冲突提醒**：`LLM/voice/session.py` 是**语音状态机**（IDLE/LISTENING/SPEAKING，worker 里已 import 为 `session_mod`），跟本设计的顶层 `LLM/session.py`（角色会话层）**是两回事**。所有新代码一律用 `role_session` 别名引用后者，绝不使用 `session_mod`。

```python
    def _apply_role_subject(self, recognized_uid: str | None) -> None:
        """按当前角色决定这次说话算谁说的（规格 §4.3）。

        * kiosk 槽是 admin → 按 admin 走，声纹认到谁都不降权（D8）；
        * 否则：认到老人 → 切到该老人（elder）；没认出来 → 留在集体层（当前病房）。
        """
        from LLM import session as role_session          # 顶层角色会话层（≠ LLM.voice.session）
        principal = role_session.get_principal("kiosk")
        if principal["role"] == "admin":
            return
        uid = recognized_uid or principal["uid"] or role_session.current_ward()
        if uid:
            role_session.set_subject(uid, bool(self.locked_uid), slot="kiosk",
                                     source="voiceprint")
```

`_handle_speech`（第 341-364 行）整体替换为：

```python
    def _handle_speech(self, seg, text, settings):
        self.session.note_speech()
        audit.log("voice_asr", text=text[:200])

        vote = self.fusion.resolve(seg)
        recognized = id_mod.effective_uid(vote, self.current_uid, self.locked_uid)
        # 锁定时识别到锁定外用户：只记审计提示，不切换（规格 §8.2 行为矩阵）
        if self.locked_uid and vote.candidate_uid and vote.candidate_uid != self.locked_uid:
            audit.log("voice_spk", action="locked_ignored", locked=self.locked_uid,
                      detected=vote.candidate_uid, score=round(vote.confidence, 3))
        audit.log("voice_spk", identified=(vote.candidate_uid is not None),
                  uid=vote.candidate_uid, score=round(vote.confidence, 3))

        from LLM import session as role_session
        prev = role_session.get_principal("kiosk")
        self._apply_role_subject(recognized)                 # 谁说的：按角色决定（含 D8）
        principal = role_session.get_principal("kiosk")
        chat_uid = principal["uid"] or principal["ward_uid"] or "elder_001"
        self.current_uid = chat_uid
        # I-1：主体变了 → 广播 user_changed，与 server.py 手动切换的广播格式一致
        if principal["uid"] != prev["uid"]:
            self._publish("user_changed", uid=chat_uid, locked=principal["locked"],
                          source=principal["source"], role=principal["role"],
                          slot="kiosk", ward_uid=principal["ward_uid"])
        self._publish("voice_state", state="recognized", uid=chat_uid, text=text)
        # 流式问答：应答线程消费 chat_stream → 逐字上屏(chat_partial) + 句级 TTS 播放
        self._start_answer(chat_uid, text, dict(settings))
```

> 细节以现有代码为准：**等义改写**——只把"谁说的"这一段的决定权交给 `_apply_role_subject`，其余（VAD/TTS/字幕/审计/`_start_answer`）不动。任务 15 的 §11.10 会实测"管理员期间声纹认人不降权"。

**(3e)** `server.py::_post_chat_jobs` 加角色参数（调用点任务 11 接上）：

```python
def _post_chat_jobs(uid: str, user_text: str, assistant: str, role: str = "elder"):
    ...
    try:
        rag.note_turn(uid, user_text, assistant, client, MODEL, settings, role=role)
    ...
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests -q`
预期：新增用例全 passed；全量仍是那 4 个既有红态

- [ ] **步骤 5：Commit**

```bash
git add LLM/memory.py LLM/voice_api.py LLM/voice/worker.py LLM/server.py \
        LLM/tests/test_ward_memory.py LLM/tests/test_worker_roles.py
git commit -m "feat(llm): 集体层不沉淀记忆 + 语音链路接入角色（管理员不降权）"
```

---

### 任务 11：REST 接口（登录/口令/病房/策略）+ 业务接口取 principal

**文件：**
- 修改：`LLM/server.py`（模型区第 205-207 行；会话状态区块第 742-754 行；`/api/chat` 第 327-351 行；`/api/profiles` 之后加一条归属接口；`lifespan` 第 52-77 行）
- 测试：`LLM/tests/test_server_roles_routes.py`（新建）

- [ ] **步骤 1：编写失败的测试**

```python
# LLM/tests/test_server_roles_routes.py
# -*- coding: utf-8 -*-
"""会话/病房/策略路由测试（临时库隔离，直接改 db.DB_PATH，**不 reload**）。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

K = {"X-Surface": "kiosk"}
A = {"X-Surface": "admin"}


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
    db.upsert_profile("elder_101_1", name="李爷爷")
    db.set_admin_password("111111")
    import LLM.server as server
    client = TestClient(server.app)        # 不进 lifespan（不启动语音/轮询/rosbridge）
    yield client
    db.DB_PATH = old


def test_session_user_rejects_role_field(c):
    r = c.post("/api/session/user", json={"uid": "ward_101", "locked": False, "role": "admin"},
               headers=K)
    assert r.status_code == 400            # R1：前端不许指定角色


def test_login_and_logout_flow_two_surfaces(c):
    assert c.post("/api/session/login", json={"password": "bad"}, headers=A).json()["ok"] is False
    ok = c.post("/api/session/login", json={"password": "111111"}, headers=A).json()
    assert ok["ok"] is True and ok["role"] == "admin"
    assert c.get("/api/session/user", headers=A).json()["role"] == "admin"
    assert c.get("/api/session/user", headers=K).json()["role"] == "ward"   # 双槽隔离
    c.post("/api/session/logout", headers=A)
    assert c.get("/api/session/user", headers=A).json()["role"] == "ward"


def test_session_user_get_has_role_fields(c):
    body = c.get("/api/session/user", headers=K).json()
    for key in ("uid", "role", "locked", "source", "slot", "ward_uid",
                "ttl_remain", "auth_required", "autoswitch"):
        assert key in body


def test_login_lockout_after_three_failures(c):
    for _ in range(3):
        c.post("/api/session/login", json={"password": "bad"}, headers=K)
    r = c.post("/api/session/login", json={"password": "111111"}, headers=K).json()
    assert r["ok"] is False and "10 秒" in r["error"]      # 冷却期内正确口令也先挡住
    assert c.get("/api/session/user", headers=K).json()["role"] == "ward"


def test_wards_crud_and_admin_auth_toggle(c):
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    body = c.get("/api/wards", headers=A).json()
    assert body["wards"][0]["uid"] == "ward_101"
    r = c.post("/api/wards", json={"uid": "ward_102", "name": "102 病房"}, headers=A)
    assert r.json()["ok"] is True
    r = c.post("/api/session/admin-auth", json={"required": False}, headers=A)
    assert r.json()["required"] is False


def test_non_admin_cannot_manage_wards(c):
    assert c.post("/api/wards", json={"uid": "ward_999", "name": "越权"}, headers=K).status_code == 403
    assert c.post("/api/session/admin-auth", json={"required": False}, headers=K).status_code == 403
    assert c.post("/api/wards/ward_101/zone", headers=K).status_code == 403


def test_password_change_flow(c):
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    assert c.post("/api/session/password",
                  json={"old": "000000", "new": "654321"}, headers=A).json()["ok"] is False
    assert c.post("/api/session/password",
                  json={"old": "111111", "new": "654321"}, headers=A).json()["ok"] is True
    c.post("/api/session/logout", headers=A)
    assert c.post("/api/session/login", json={"password": "654321"}, headers=A).json()["ok"] is True


def test_policy_roles_visibility(c):
    assert set(c.get("/api/policy/roles", headers=A).json()) == {"admin", "ward", "elder"}
    kiosk = c.get("/api/policy/roles", headers=K).json()
    assert set(kiosk) == {"ward"}          # 非管理员只拿到自己那份摘要


def test_profiles_endpoint_lists_elders_only(c):
    """**回归（历史隐患②）**：病房用户不许混进 `GET /api/profiles` 的老人列表。"""
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    c.post("/api/wards", json={"uid": "ward_102", "name": "102 病房"}, headers=A)
    profiles = c.get("/api/profiles", headers=A).json()["profiles"]
    uids = [p["uid"] for p in profiles]
    assert "elder_101_1" in uids and "ward_101" not in uids and "ward_102" not in uids
    assert [p["uid"] for p in c.get("/api/profiles?kind=ward", headers=A).json()["profiles"]] \
        == ["ward_101", "ward_102"]


def test_profile_ward_assignment(c):
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    r = c.post("/api/profiles/elder_101_1/ward", json={"ward_id": "ward_101"}, headers=A)
    assert r.json()["ok"] is True
    wards = c.get("/api/wards", headers=A).json()["wards"]
    assert wards[0]["elders"] == ["elder_101_1"]
    assert c.post("/api/profiles/elder_101_1/ward", json={"ward_id": ""}, headers=K).status_code == 403
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_server_roles_routes.py -q`
预期：FAIL（`404` / `422`）

- [ ] **步骤 3：实现**

**(3a)** 导入与依赖（`server.py` 顶部）：

- `from fastapi import FastAPI, Query` → `from fastapi import FastAPI, Header, HTTPException, Query`
- 顶部**新增两行**（本文件目前既没有 `time` 也没有模块级 `audit`，现有代码是在各函数里局部 import 的；本任务的登录冷却与审计要在多处用，改为顶层导入）：
  ```python
  import time
  from . import session
  from . import log as audit
  ```
- `from . import mapserver, maptags, mapstore, locator`（第 848 行）**保持原样**：`locator`/`maptags` 在第 742 行附近的 handler 里用到也没关系——模块导入在任何请求之前就执行完了，函数体内的全局名在调用时解析。
- **顺手修掉历史隐患②**：`GET /api/profiles` 目前返回**所有** profiles，病房用户会混进前端"老人列表"（`MemoriesPage`/`ChatPage`/`RegisterPage` 的下拉、车前屏换人弹层都吃它）。改为默认只列老人：
  ```python
  @app.get("/api/profiles")
  async def profiles_list(kind: str = Query("elder")):
      """老人列表（默认只列 kind='elder'）；要看病房用 /api/wards，要看全部传 ?kind=all。"""
      return {"ok": True, "profiles": db.list_profiles(kind="" if kind == "all" else kind)}
  ```
  `_seed_demo()` 里那句 `if db.list_profiles():` **不要动**——它判的是"库里有没有任何档案"，仍应看全量。

**(3b)** 模型区（第 205-207 行）替换/新增：

```python
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
```

**(3c)** 会话状态区块（第 742-754 行）整体替换为：

```python
# ---------------------------------------------------------------- 会话 / 角色（分层用户体系）
_SURFACES = ("kiosk", "admin")
_login_fail: dict[str, dict] = {}      # slot -> {"n": 连续失败次数, "until": 冷却截止}


def _surface(x_surface: str = Header(default="kiosk")) -> str:
    """端槽位：kiosk（车前/语音）| admin（管理台）。缺省按 kiosk。"""
    return x_surface if x_surface in _SURFACES else "kiosk"


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


@app.get("/api/session/user")
async def session_user_get(x_surface: str = Header(default="kiosk")):
    """当前会话主体（uid/角色/锁定/当前病房/TTL），**角色按请求槽位返回**。"""
    slot = _surface(x_surface)
    p = session.get_principal(slot)
    return {**p, "ttl_remain": session.ttl_remain(slot),
            "auth_required": db.get_admin_auth()["required"],
            "autoswitch": session.autoswitch_state()}


@app.post("/api/session/user")
async def session_user_set(s: SessionUserIn, x_surface: str = Header(default="kiosk")):
    """切换会话主体：**只接受 uid/locked**；传 role 一律 400（R1）。"""
    if s.role:
        raise HTTPException(status_code=400, detail="role 不可由前端指定（R1）")
    slot = _surface(x_surface)
    res = session.set_subject(s.uid, s.locked, slot=slot, source="manual")
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
```

**(3d)** `lifespan` 里补三件事（口令首启、位置源提示、每秒 tick）：

```python
    # 分层用户体系：首启生成管理员口令（D12）+ 每秒 tick（TTL 降权 + 病房位置自动切换）
    pw = session.ensure_admin_password()
    if pw:
        print(f"[INFO] 已生成管理员初始口令：{pw}（登录后请立即在「身份与权限」里修改）")
        audit.log("admin_password_generated")
    ok, why = locator.available()
    if not ok:
        print(f"[WARN] 位置源不可用（{why}）→ 病房位置自动切换停用，车前屏可手动切病房")

    async def _role_tick():
        while True:
            await asyncio.sleep(1)
            try:
                await asyncio.to_thread(session.tick)   # tick 可能碰网络/SSH，别占事件循环
            except Exception:                           # noqa: BLE001  降级：tick 出错不影响服务
                pass

    tick_task = asyncio.create_task(_role_tick())
```
并在 `yield` 之后的收尾里加 `tick_task.cancel()`（与现有 `drain_task.cancel()` 并列）。

**(3e)** `/api/chat` 按槽位取 principal，并把角色带给后台沉淀任务：

```python
@app.post("/api/chat")
async def chat_route(req: ChatRequest, x_surface: str = Header(default="kiosk")):
    settings = db.get_settings()
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
                _bg.submit(_post_chat_jobs, req.uid, req.message, assistant, principal["role"])

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_server_roles_routes.py LLM/tests -q`
预期：新文件 `9 passed`；全量仍是那 4 个既有红态

- [ ] **步骤 5：Commit**

```bash
git add LLM/server.py LLM/tests/test_server_roles_routes.py
git commit -m "feat(llm): 会话/病房/策略 REST 接口 + 业务接口按槽位取 principal"
```

---

### 任务 12：前端 —— shared 会话层与事件

**文件：**
- 修改：`frontend/packages/shared/src/api/client.ts`（全文 18 行）
- 修改：`frontend/packages/shared/src/api/session.ts`（全文 16 行，**扩展现有文件，不新建 `src/session.ts`**）
- 修改：`frontend/packages/shared/src/events.ts`（事件枚举 + `parseBusPayload`）
- 测试：`frontend/packages/shared/tests/events.test.ts`（追加用例）

> 落点核实：`shared/src/` 只有 `index.ts` / `events.ts` / `api/{client,session,alarm}.ts`；前端源码里 `X-Surface` **零出现**。

- [ ] **步骤 1：编写失败的测试**（追加到 `frontend/packages/shared/tests/events.test.ts`）

```ts
import { describe, it, expect } from "vitest";
import { parseBusPayload } from "../src/events";

describe("分层用户体系新增事件", () => {
  it("session_expired", () => {
    const ev = parseBusPayload(JSON.stringify({ type: "session_expired", slot: "kiosk" }));
    expect(ev?.type).toBe("session_expired");
  });

  it("admin_auth_changed", () => {
    const ev = parseBusPayload(JSON.stringify({ type: "admin_auth_changed", required: false }));
    expect(ev?.type).toBe("admin_auth_changed");
  });

  it("ward_changed", () => {
    const ev = parseBusPayload(JSON.stringify({ type: "ward_changed", uid: "ward_101", action: "location" }));
    expect(ev?.type).toBe("ward_changed");
  });

  it("user_changed 载荷扩了 role/slot/ward_uid 也不再被丢弃", () => {
    const ev = parseBusPayload(JSON.stringify({
      type: "user_changed", uid: "elder_101_1", locked: false,
      source: "voiceprint", role: "elder", slot: "kiosk", ward_uid: "ward_101",
    }));
    expect(ev?.type).toBe("user_changed");
  });
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd frontend && pnpm --filter shared test`
预期：FAIL（新用例 `expected null to be 'session_expired'`——这三个类型还不在 `KNOWN_TYPES` 里）

- [ ] **步骤 3：实现**

**(3a)** `api/client.ts` 全量替换：

```ts
// 统一 REST client：所有 /api 调用走这里。
// 分层用户体系：带 X-Surface 头区分端槽位（kiosk=车前/语音，admin=管理台），
// 后端据此返回**该槽位的角色**——role 绝不由前端指定（后端红线 R1）。
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

**(3b)** `api/session.ts` 全量替换：

```ts
import { apiGet, apiPost, type Surface } from "./client";

export type Role = "admin" | "ward" | "elder";

export interface SessionUser {
  ok?: boolean;
  uid: string | null;
  role: Role;
  locked: boolean;
  slot: Surface;
  /** default | manual | voiceprint | password | auth_disabled | expired | location | logout */
  source: string;
  ward_uid: string;
  ttl_remain: number | null;
  auth_required: boolean;
  autoswitch: { enabled: boolean; reason: string };
}

export interface WardZone {
  uid: string;
  name?: string;
  kind?: string;
  shape?: string;
  polygon?: number[][];
}

export interface Ward {
  uid: string;
  name: string;
  /** 病房区域所在的地图名（几何真相在地图文件夹的 <图名>.tags.json） */
  ward_map: string;
  /** 关联的区域 uid（形如 z1）；空串 = 未关联 */
  ward_zone: string;
  zone: WardZone | null;
  elders: string[];
}

export function getSessionUser(surface: Surface): Promise<SessionUser> {
  return apiGet<SessionUser>("/api/session/user", surface);
}

export function setSessionUser(uid: string, locked: boolean, surface: Surface): Promise<SessionUser> {
  return apiPost<SessionUser>("/api/session/user", { uid, locked }, surface);   // 不传 role（R1）
}

export function login(password: string | null, surface: Surface) {
  return apiPost<{ ok: boolean; error?: string; role?: Role; ttl_remain?: number | null }>(
    "/api/session/login", { password }, surface);
}

export function logout(surface: Surface): Promise<SessionUser> {
  return apiPost<SessionUser>("/api/session/logout", {}, surface);
}

export function changePassword(oldPw: string, newPw: string, surface: Surface) {
  return apiPost<{ ok: boolean; error?: string }>(
    "/api/session/password", { old: oldPw, new: newPw }, surface);
}

export function getAdminAuth(surface: Surface) {
  return apiGet<{ required: boolean }>("/api/session/admin-auth", surface);
}

export function setAdminAuth(required: boolean, surface: Surface) {
  return apiPost<{ required: boolean }>("/api/session/admin-auth", { required }, surface);
}

export function listWards(surface: Surface) {
  return apiGet<{ ok: boolean; wards: Ward[] }>("/api/wards", surface);
}

export function upsertWard(uid: string, name: string, wardMap: string, wardZone: string,
                          surface: Surface) {
  return apiPost<{ ok: boolean; ward?: Ward; error?: string }>(
    "/api/wards", { uid, name, ward_map: wardMap, ward_zone: wardZone }, surface);
}

/** 便捷录入：以小车当前位姿为圆心、ward_zone_default_r 为半径，把当前房间记成病房区域。 */
export function recordWardZone(wardUid: string, surface: Surface) {
  return apiPost<{ ok: boolean; error?: string; map?: string; zone_uid?: string;
                   zone?: WardZone | null }>(`/api/wards/${encodeURIComponent(wardUid)}/zone`,
                                            {}, surface);
}

export function assignElderWard(elderUid: string, wardId: string, surface: Surface) {
  return apiPost<{ ok: boolean }>(`/api/profiles/${encodeURIComponent(elderUid)}/ward`,
                                  { ward_id: wardId }, surface);
}
```

**(3c)** `events.ts`：`UserChangedEvent` 扩字段、加三个事件、把新类型加进 `KNOWN_TYPES`、`parseBusPayload` 的返回类型跟着扩：

```ts
export interface UserChangedEvent {
  type: "user_changed"; uid: string; locked: boolean;
  source: string;                    // manual | voiceprint | password | auth_disabled | logout ...
  role?: "admin" | "ward" | "elder";
  slot?: "kiosk" | "admin";
  ward_uid?: string;
}
export interface SessionExpiredEvent { type: "session_expired"; slot: "kiosk" | "admin" }
export interface AdminAuthChangedEvent { type: "admin_auth_changed"; required: boolean }
export interface WardChangedEvent { type: "ward_changed"; uid: string; action: string }

export type BusEvent =
  | ReminderEvent | ReminderConfirmedEvent | AlarmEvent | ChatNewEvent | ChatPartialEvent
  | VoiceStateEvent | UserChangedEvent | VoiceStatusEvent
  | SessionExpiredEvent | AdminAuthChangedEvent | WardChangedEvent;

const KNOWN_TYPES = new Set([
  "reminder", "reminder_confirmed", "alarm", "chat_new", "chat_partial",
  "voice_state", "user_changed", "voice_status",
  "session_expired", "admin_auth_changed", "ward_changed",
]);
```

（`parseBusEvent` / `parseBusPayload` / `parseSseChunk` 三个函数体不用改：它们靠 `KNOWN_TYPES` 放行。）

- [ ] **步骤 4：运行测试验证通过**

运行：`cd frontend && pnpm --filter shared test`
预期：全部 passed（含原有 events/client 用例）

- [ ] **步骤 5：Commit**

```bash
git add frontend/packages/shared
git commit -m "feat(frontend): shared 会话层（X-Surface/登录/口令/病房）+ 3 个新事件"
```

---

### 任务 13：前端 —— kiosk 左侧层级栏

**文件：**
- 修改：`frontend/packages/kiosk/src/components/UserSwitcher.vue`（换成三组层级抽屉）
- 修改：`frontend/packages/kiosk/src/components/VoiceStatusBar.vue`（角色徽标 + 倒计时）
- 修改：`frontend/packages/kiosk/src/App.vue`（接线：`setSessionUser(uid, locked, "kiosk")`、监听 `ward_changed`/`session_expired`）

> 落点核实：换人入口按钮在 `VoiceStatusBar.vue:34`；弹层由 `App.vue:22/213` 的 `showSwitcher` 控制；`useBus.ts` 已经把 `/api/events` 的所有事件交给 `App.vue::onEvent`。

- [ ] **步骤 1：实现 `UserSwitcher.vue`（三组层级栏，用户明确要求"按下换人后左侧显示当前层级"）**

```vue
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  changePassword, getSessionUser, listWards, login, logout, recordWardZone,
  setAdminAuth, setSessionUser, getAdminAuth,
  type SessionUser, type Ward,
} from "shared";

const props = defineProps<{ session: SessionUser }>();
const emit = defineEmits<{ (e: "changed"): void; (e: "close"): void }>();

const profiles = ref<{ uid: string; name?: string; nickname?: string; bed?: string }[]>([]);
const wards = ref<Ward[]>([]);
const pw = ref("");
const errMsg = ref("");
const showPwChange = ref(false);
const oldPw = ref("");
const newPw = ref("");
const authRequired = ref(true);
const allWards = ref(false);

const role = computed(() => props.session.role);
const elders = computed(() => {
  const cur = wards.value.find(w => w.uid === props.session.ward_uid);
  const inWard = cur ? cur.elders : [];
  return props.profiles.filter(p => (allWards.value ? true : inWard.includes(p.uid)));
});

function fmt(sec: number | null) {
  if (sec == null) return "--:--";
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

async function refresh() {
  try {
    const [w, a, p] = await Promise.all([
      listWards("kiosk"),
      getAdminAuth("kiosk"),
      fetch("/api/profiles").then(r => r.json()),      // 沿用组件原本的取法（不经 shared）
    ]);
    wards.value = w.wards ?? [];
    authRequired.value = a.required;
    profiles.value = p.profiles ?? [];
  } catch { /* 后端不可达：层级栏仍可用已缓存数据 */ }
}

async function doLogin() {
  errMsg.value = "";
  const r = await login(pw.value || null, "kiosk");
  if (!r.ok) { errMsg.value = r.error ?? "登录失败"; return; }
  pw.value = ""; emit("changed");
}

async function doLogout() { await logout("kiosk"); emit("changed"); }

async function pick(uid: string, locked = true) {
  await setSessionUser(uid, locked, "kiosk");
  emit("changed");
}

async function unlock() {                     // 解锁 = 恢复声纹自动判定（不换人）
  await setSessionUser(props.session.uid ?? "", false, "kiosk");
  emit("changed");
}

async function doChangePw() {
  errMsg.value = "";
  const r = await changePassword(oldPw.value, newPw.value, "kiosk");
  if (!r.ok) { errMsg.value = r.error ?? "改口令失败"; return; }
  showPwChange.value = false; oldPw.value = ""; newPw.value = "";
}

async function toggleAuth() {
  const r = await setAdminAuth(!authRequired.value, "kiosk");
  authRequired.value = r.required;
  emit("changed");
}

async function markZone(w: Ward) {
  errMsg.value = "";
  const r = await recordWardZone(w.uid, "kiosk");
  if (!r.ok) errMsg.value = r.error ?? "记录失败";
  await refresh();
}

onMounted(refresh);
</script>

<template>
  <div class="layer-drawer">
    <section>
      <h4>🛡 管理层</h4>
      <template v-if="role !== 'admin'">
        <div class="row">
          <input v-model="pw" type="password" placeholder="管理员口令" @keyup.enter="doLogin" />
          <button @click="doLogin">{{ authRequired ? "进入" : "直接进入（无口令保护⚠️）" }}</button>
        </div>
      </template>
      <template v-else>
        <p class="cur">当前（剩 {{ fmt(session.ttl_remain) }}）</p>
        <div class="row">
          <button @click="doLogout">退出管理层</button>
          <button @click="showPwChange = !showPwChange">口令设置</button>
          <button @click="toggleAuth">{{ authRequired ? "关闭口令门" : "开启口令门" }}</button>
        </div>
        <div v-if="showPwChange" class="row">
          <input v-model="oldPw" type="password" placeholder="旧口令" />
          <input v-model="newPw" type="password" placeholder="新口令" />
          <button @click="doChangePw">保存</button>
        </div>
      </template>
      <p v-if="!authRequired" class="warn">⚠️ 当前无口令保护，任何人都能进管理层</p>
    </section>

    <section>
      <h4>🏠 集体层（病房）</h4>
      <p v-if="!session.autoswitch.enabled" class="hint">
        位置未知 · 手动切病房（{{ session.autoswitch.reason }}）
      </p>
      <ul>
        <li v-for="w in wards" :key="w.uid" :class="{ active: w.uid === session.ward_uid }">
          <span @click="pick(w.uid)">{{ w.name || w.uid }}</span>
          <button class="mini" title="把当前房间记为这个病房的区域" @click="markZone(w)">记录位置</button>
        </li>
        <li v-if="!wards.length" class="hint">还没有病房用户（在管理台「病房管理」里新建）</li>
      </ul>
    </section>

    <section>
      <h4>
        👴 老人层
        <button class="mini" @click="allWards = !allWards">
          {{ allWards ? "只看本病房" : "全部病房" }}
        </button>
      </h4>
      <ul>
        <li v-for="p in elders" :key="p.uid" :class="{ active: p.uid === session.uid }"
            @click="pick(p.uid)">
          {{ p.nickname || p.name || p.uid }}
        </li>
        <li v-if="!elders.length" class="hint">本病房还没有登记老人</li>
      </ul>
      <button v-if="session.locked" class="mini" @click="unlock">解除锁定（恢复声纹自动判定）</button>
    </section>

    <p v-if="errMsg" class="warn">{{ errMsg }}</p>
    <button class="close" @click="emit('close')">关闭</button>
  </div>
</template>

<style scoped>
.layer-drawer { position: absolute; left: 0; top: 0; bottom: 0; width: 320px; overflow-y: auto;
  background: #1b2430; color: #eef3f8; padding: 16px; z-index: 40; }
section { border-bottom: 1px solid #33445a; padding-bottom: 12px; margin-bottom: 12px; }
h4 { margin: 0 0 8px; font-size: 15px; }
ul { list-style: none; margin: 0; padding: 0; }
li { display: flex; align-items: center; justify-content: space-between; padding: 8px;
  border-radius: 8px; cursor: pointer; }
li.active { background: #2b6cb0; }
.row { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
input { flex: 1; min-width: 90px; padding: 6px; border-radius: 6px; border: 1px solid #46586f;
  background: #0f1620; color: inherit; }
button { padding: 6px 10px; border-radius: 6px; border: 1px solid #46586f; background: #243244;
  color: inherit; cursor: pointer; }
button.mini { font-size: 12px; padding: 2px 6px; }
.hint { color: #9fb2c8; font-size: 12px; }
.warn { color: #ffd166; font-size: 12px; }
.cur { margin: 4px 0; }
</style>
```

> 说明：`kiosk` 的层级栏**不暴露**任何老人档案细节（只显示昵称/床位），符合"集体层不注入个人档案"的口径；"记录位置"是管理员动作，非管理员点击会被后端 403 挡下并显示错误。

- [ ] **步骤 2：改 `App.vue`（接线）**

`<UserSwitcher>` 的用法改为（**组件自己拉 `/api/profiles`**，所以只传 session，不再传 current/pick/unlock）：

```vue
    <UserSwitcher v-if="showSwitcher" :session="session" @changed="loadSession"
                  @close="showSwitcher = false" />
```

`App.vue` 里：

```ts
import { getSessionUser, setSessionUser, type SessionUser } from "shared";

const session = ref<SessionUser | null>(null);

async function loadSession() {
  try { session.value = await getSessionUser("kiosk"); } catch { /* 后端不可达：保持旧值 */ }
}

async function onSwitchUser(nextUid: string) {          // 手动切换 = 锁定（规格 D11）
  session.value = await setSessionUser(nextUid, true, "kiosk");
  uid.value = session.value.uid ?? nextUid;
  locked.value = session.value.locked;
  showSwitcher.value = false;
}

async function onUnlock() {
  session.value = await setSessionUser(uid.value ?? "", false, "kiosk");
  locked.value = session.value.locked;
  showSwitcher.value = false;
}
```

`onEvent` 里补三个分支：

```ts
    } else if (ev.type === "user_changed") {
      uid.value = ev.uid; locked.value = ev.locked; loadSession();
    } else if (ev.type === "ward_changed") {
      loadSession();                                   // 当前病房变了（位置/手动）
    } else if (ev.type === "session_expired") {
      loadSession();
      if (ev.slot === "kiosk") liveText.value = "管理员会话已超时，已回到集体层";
    }
```

并把 `onMounted` 里的 `loadSession()` 保留（原本已有）。

- [ ] **步骤 3：改 `VoiceStatusBar.vue`（按角色换徽标）**

```vue
<script setup lang="ts">
import type { SessionUser } from "shared";
const props = defineProps<{ state: string; session: SessionUser | null }>();
const emit = defineEmits<{ (e: "open-switcher"): void }>();

function fmt(sec: number | null) {
  if (sec == null) return "";
  return ` ${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}
const badge = computed(() => {
  const s = props.session;
  if (!s) return "👤 未选择";
  if (s.role === "admin") return `🛡 管理员${fmt(s.ttl_remain)}`;
  if (s.role === "elder") return `👴 ${s.uid}${s.locked ? " 🔒" : ""}`;
  return `🏠 ${s.ward_uid || "未选病房"}`;
});
</script>
```

模板里第 34 行那颗按钮改为：

```vue
    <button class="user" @click="emit('open-switcher')">{{ badge }}</button>
```

（`App.vue` 传 `:session="session"`；老人昵称可从 `/api/profiles` 查，v1 先用 uid，避免引入新状态。）

- [ ] **步骤 4：构建验证**

运行：`cd frontend && pnpm --filter kiosk build`
预期：构建成功、无 TS 报错

- [ ] **步骤 5：Commit**

```bash
git add frontend/packages/kiosk
git commit -m "feat(kiosk): 左侧层级栏（管理层/集体层/老人层）+ 状态条角色徽标"
```

---

### 任务 14：前端 —— admin 登录门与两个新页签

**文件：**
- 修改：`frontend/packages/admin/src/App.vue`（登录门 + `tabs` 数组加两项 + Header 身份）
- 创建：`frontend/packages/admin/src/pages/RolesPage.vue`（身份与权限）
- 创建：`frontend/packages/admin/src/pages/WardsPage.vue`（病房管理）
- 修改：`frontend/packages/admin/src/pages/RegisterPage.vue`（注册向导加「病房」下拉）

> 落点核实：admin 页签注册唯一位置 = `App.vue:14-23` 的 `tabs` 数组 + 模板 `v-else-if`；admin **目前没有任何登录/鉴权代码**；`RegisterPage.vue` 用本地 `json()` 裸 fetch，不走 shared。

- [ ] **步骤 1：`App.vue` 登录门 + 新页签**

```ts
import { getSessionUser, login, logout, type SessionUser } from "shared";
import RolesPage from "./pages/RolesPage.vue";
import WardsPage from "./pages/WardsPage.vue";

const session = ref<SessionUser | null>(null);
const pw = ref("");
const loginErr = ref("");
const loading = ref(true);

const tabs = [
  { id: "overview", label: "监控总览" },
  { id: "register", label: "老人注册" },
  { id: "chat", label: "对话" },
  { id: "memories", label: "记忆" },
  { id: "reminders", label: "提醒" },
  { id: "wards", label: "病房管理" },        // 新增
  { id: "tools", label: "工具日志" },
  { id: "voice", label: "语音状态" },
  { id: "roles", label: "身份与权限" },      // 新增
  { id: "settings", label: "设置" },
];

async function loadSession() {
  try { session.value = await getSessionUser("admin"); } finally { loading.value = false; }
}

async function doLogin() {
  loginErr.value = "";
  const r = await login(pw.value || null, "admin");
  if (!r.ok) { loginErr.value = r.error ?? "登录失败"; return; }
  await loadSession();
}

async function doLogout() { await logout("admin"); await loadSession(); }

onMounted(loadSession);
```

模板骨架（登录门 + 常驻警示 + Header）：

```vue
<template>
  <div v-if="loading" class="gate">载入中…</div>

  <div v-else-if="session && session.role !== 'admin'" class="gate">
    <h3>🛡 管理员登录</h3>
    <p v-if="!session.auth_required" class="warn">当前口令门已关闭，直接点「进入」即可（建议尽快开启）</p>
    <input v-if="session.auth_required" v-model="pw" type="password" placeholder="管理员口令"
           @keyup.enter="doLogin" />
    <button @click="doLogin">进入</button>
    <p v-if="loginErr" class="warn">{{ loginErr }}</p>
  </div>

  <div v-else>
    <header class="topbar">
      <strong>陪护机器人 · 管理台</strong>
      <span class="who">🛡 管理员<span v-if="session?.ttl_remain != null">（剩 {{ Math.floor(session.ttl_remain / 60) }} 分）</span></span>
      <button @click="doLogout">退出</button>
    </header>
    <div v-if="session && !session.auth_required" class="redbar">⚠️ 当前无口令保护，任何人都能进管理台</div>

    <nav class="tabs">
      <button v-for="t in tabs" :key="t.id" :class="{ active: active === t.id }"
              @click="active = t.id">{{ t.label }}</button>
    </nav>

    <OverviewPage v-if="active === 'overview'" />
    <RegisterPage v-else-if="active === 'register'" />
    <ChatPage v-else-if="active === 'chat'" />
    <MemoriesPage v-else-if="active === 'memories'" />
    <RemindersPage v-else-if="active === 'reminders'" />
    <WardsPage v-else-if="active === 'wards'" />
    <ToolLogPage v-else-if="active === 'tools'" />
    <VoiceStatusPage v-else-if="active === 'voice'" />
    <RolesPage v-else-if="active === 'roles'" />
    <SettingsPage v-else-if="active === 'settings'" />
  </div>
</template>
```

（`<style>` 里补 `.gate/.topbar/.who/.redbar/.tabs` 的基础样式即可；`.tabs` 沿用现有页签样式。）

- [ ] **步骤 2：`pages/RolesPage.vue`（策略矩阵只读 + 口令设置）**

```vue
<script setup lang="ts">
import { onMounted, ref } from "vue";
import { changePassword, getAdminAuth, setAdminAuth } from "shared";

const roles = ref<Record<string, any>>({});
const authRequired = ref(true);
const oldPw = ref(""); const newPw = ref(""); const msg = ref("");
const ttl = ref(300); const wardWindow = ref(10);

async function load() {
  const [p, a, s] = await Promise.all([
    fetch("/api/policy/roles", { headers: { "X-Surface": "admin" } }).then(r => r.json()),
    getAdminAuth("admin"),
    fetch("/api/settings", { headers: { "X-Surface": "admin" } }).then(r => r.json()),
  ]);
  roles.value = p ?? {}; authRequired.value = a.required;
  ttl.value = s.admin_session_ttl_s ?? 300; wardWindow.value = s.ward_context_window ?? 10;
}

async function savePw() {
  msg.value = "";
  const r = await changePassword(oldPw.value, newPw.value, "admin");
  msg.value = r.ok ? "✅ 口令已更新" : `❌ ${r.error}`;
  if (r.ok) { oldPw.value = ""; newPw.value = ""; }
}

async function toggleAuth() {
  const r = await setAdminAuth(!authRequired.value, "admin");
  authRequired.value = r.required;
  msg.value = r.required ? "口令门已开启" : "口令门已关闭（当前无保护）";
}

async function saveLimits() {
  await fetch("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json", "X-Surface": "admin" },
    body: JSON.stringify({ admin_session_ttl_s: Number(ttl.value),
                           ward_context_window: Number(wardWindow.value) }),
  });
  msg.value = "✅ 已保存";
}

onMounted(load);
</script>

<template>
  <section class="page">
    <h3>身份与权限</h3>
    <table>
      <thead><tr><th>角色</th><th>提示词片段</th><th>工具白名单</th><th>数据范围</th><th>读病房上下文</th></tr></thead>
      <tbody>
        <tr v-for="(pol, role) in roles" :key="role">
          <td>{{ role }}</td>
          <td class="mono">{{ pol.prompt_file }}</td>
          <td class="mono">{{ pol.allowed_tools === null ? "全部" : (pol.allowed_tools.join(", ") || "无") }}</td>
          <td>{{ pol.data_scope }}</td>
          <td>{{ pol.ward_context ? "是" : "否" }}</td>
        </tr>
      </tbody>
    </table>

    <h4>口令设置</h4>
    <p v-if="!authRequired" class="warn">⚠️ 口令门当前是关的：任何人点「管理层」都能进</p>
    <div class="row">
      <input v-model="oldPw" type="password" placeholder="旧口令" />
      <input v-model="newPw" type="password" placeholder="新口令（≥4 位）" />
      <button @click="savePw">改口令</button>
      <button @click="toggleAuth">{{ authRequired ? "关闭口令门" : "开启口令门" }}</button>
    </div>

    <h4>可调项</h4>
    <div class="row">
      <label>管理员会话时效(秒) <input v-model.number="ttl" type="number" /></label>
      <label>病房上下文条数 <input v-model.number="wardWindow" type="number" /></label>
      <button @click="saveLimits">保存</button>
    </div>
    <p v-if="msg">{{ msg }}</p>
  </section>
</template>
```

- [ ] **步骤 3：`pages/WardsPage.vue`（病房管理）**

```vue
<script setup lang="ts">
import { onMounted, ref } from "vue";
import { assignElderWard, listWards, recordWardZone, upsertWard, type Ward } from "shared";

const wards = ref<Ward[]>([]);
const elders = ref<{ uid: string; name?: string; nickname?: string; ward_id?: string }[]>([]);
const newUid = ref(""); const newName = ref("");
const bindMap = ref(""); const bindZone = ref("");
const msg = ref(""); const err = ref("");

async function load() {
  const [w, p] = await Promise.all([
    listWards("admin"),
    fetch("/api/profiles", { headers: { "X-Surface": "admin" } }).then(r => r.json()),
  ]);
  wards.value = w.wards ?? [];
  elders.value = (p.profiles ?? []).filter((x: any) => (x.kind ?? "elder") === "elder");
}

async function create() {
  err.value = "";
  if (!newUid.value.trim()) { err.value = "请填病房 uid（如 ward_101）"; return; }
  const r = await upsertWard(newUid.value.trim(), newName.value, "", "", "admin");
  if (!r.ok) { err.value = r.error ?? "创建失败"; return; }
  newUid.value = ""; newName.value = ""; await load();
}

/** 精确形状（多边形/矩形）在 /mapeditor 里画；这里只做「关联」到已存在的区域 uid。 */
async function bind(w: Ward) {
  err.value = "";
  if (!bindMap.value.trim() || !bindZone.value.trim()) {
    err.value = "地图名与区域 uid 都要填（区域 uid 到地图编辑器的区域列表里抄）";
    return;
  }
  const r = await upsertWard(w.uid, w.name, bindMap.value.trim(), bindZone.value.trim(), "admin");
  msg.value = r.ok ? "✅ 已关联区域" : `❌ ${r.error}`;
  await load();
}

/** 便捷入口：以小车当前位姿为圆心 + ward_zone_default_r 半径，采样 16 边形写进该图 tags.json。 */
async function markHere(w: Ward) {
  err.value = "";
  const r = await recordWardZone(w.uid, "admin");
  if (!r.ok) { err.value = r.error ?? "记录失败"; return; }
  msg.value = `✅ 已记下 ${r.map} / ${r.zone_uid}`;
  await load();
}

async function assign(elderUid: string, wardId: string) {
  await assignElderWard(elderUid, wardId, "admin");
  await load();
}

onMounted(load);
</script>

<template>
  <section class="page">
    <h3>病房管理（集体层）</h3>
    <p class="hint">一个病房 = 一个"病房用户"（uid 形如 ward_101）。区域几何的真相在地图文件夹的
      &lt;图名&gt;.tags.json：便捷入口按当前位姿生成 16 边形近似圆，**精确形状请到
      <a href="/mapeditor/" target="_blank">地图编辑器</a> 画多边形/矩形**（类型选「ward 病区」），
      本页只负责把病房关联到那个区域。</p>

    <div class="row">
      <input v-model="newUid" placeholder="uid（ward_101）" />
      <input v-model="newName" placeholder="名称（101 病房）" />
      <button @click="create">新建病房</button>
    </div>

    <table>
      <thead><tr><th>uid</th><th>名称</th><th>关联区域</th><th>归属老人</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="w in wards" :key="w.uid">
          <td class="mono">{{ w.uid }}</td>
          <td>{{ w.name }}</td>
          <td class="mono">
            <template v-if="w.zone">{{ w.ward_map }} / {{ w.ward_zone }}（{{ w.zone.name }}）</template>
            <template v-else>未关联</template>
          </td>
          <td class="mono">{{ w.elders.join(", ") || "—" }}</td>
          <td>
            <button @click="markHere(w)">记录当前房间为病房区域</button>
            <button @click="bind(w)">关联已画区域</button>
          </td>
        </tr>
      </tbody>
    </table>

    <h4>关联已画好的区域</h4>
    <p class="hint">先在上一列填好「地图名 + 区域 uid」（区域在地图编辑器里画好后从它的列表里抄 uid），
      再点目标病房那一行的「关联已画区域」。</p>
    <div class="row">
      <input v-model="bindMap" placeholder="地图名（my_map）" />
      <input v-model="bindZone" placeholder="区域 uid（z1）" />
    </div>

    <h4>老人归属</h4>
    <table>
      <thead><tr><th>老人</th><th>当前病房</th><th>改为</th></tr></thead>
      <tbody>
        <tr v-for="e in elders" :key="e.uid">
          <td>{{ e.nickname || e.name || e.uid }}</td>
          <td class="mono">{{ wards.find(w => w.elders.includes(e.uid))?.uid || "—" }}</td>
          <td>
            <select @change="assign(e.uid, ($event.target as HTMLSelectElement).value)">
              <option value="">（移出病房）</option>
              <option v-for="w in wards" :key="w.uid" :value="w.uid">{{ w.name || w.uid }}</option>
            </select>
          </td>
        </tr>
      </tbody>
    </table>

    <p v-if="msg">{{ msg }}</p>
    <p v-if="err" class="warn">{{ err }}</p>
  </section>
</template>
```

> 后端的 `upsert_ward` 约定"空串 = 保持原值"，所以改名不会把已有区域关联清掉；
> 要**清空**关联走 `db.set_ward_zone(uid, "", "")`（验收 §11.13 用到）。

- [ ] **步骤 4：`RegisterPage.vue` 加病房归属**

在 step1（基本信息）表单里加一个下拉，并在 `saveProfile()` 成功后调用归属接口（**记得把 `watch` 加进 `vue` 的 import**，第 1 行原本只有 `onMounted, ref`）：

```ts
const wards = ref<{ uid: string; name: string }[]>([]);
const wardId = ref("");

// onMounted 里补：
fetch("/api/wards", { headers: { "X-Surface": "admin" } })
  .then(r => r.json()).then(b => { wards.value = b.wards ?? []; });

// 【按床位号自动建议病房】bed="101-2" → 建议 ward_101（可改）
watch(bed, (v) => {
  const m = /^(\d+)/.exec((v || "").trim());
  if (m) wardId.value = `ward_${m[1]}`;
});

// saveProfile() 末尾补：
if (wardId.value) {
  await json(`/api/profiles/${encodeURIComponent(uid.value)}/ward`,
             { method: "POST", body: { ward_id: wardId.value } });
}
```

模板（step1）加：

```vue
    <label>病房
      <select v-model="wardId">
        <option value="">（暂不指定）</option>
        <option v-for="w in wards" :key="w.uid" :value="w.uid">{{ w.name || w.uid }}</option>
      </select>
    </label>
```

- [ ] **步骤 5：构建验证 + Commit**

运行：`cd frontend && pnpm --filter admin build && pnpm --filter kiosk build`
预期：两个包均构建成功、无 TS 报错

```bash
git add frontend/packages/admin
git commit -m "feat(admin): 登录门 + 身份与权限页签 + 病房管理页签 + 注册向导加病房归属"
```

---

### 任务 15：文档、降级自检与端到端验收

**文件：**
- 修改：`AGENTS.md`（「LLM/ 后端」模块表加 `session.py`/`policy.py`/`zonegeo.py`/`prompt/`；「关键约定」补角色闸门与红线 R1–R5）
- 修改：`docs/log.md`（按日期追加本次实现）
- 修改：`docs/superpowers/specs/2026-09-14-layered-user-roles-design.md`（状态改「已实现 P0」）

- [ ] **步骤 1：跑规格 §11 的 16 条验收（全部本机，**不用动车、不用板卡**）**

起后端（项目根目录）：
```bash
.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000
```
借助 `X-Surface` 模拟两端；借助 `POST /api/mapeditor/pose/inject` 注入假位姿与假地图（无 ROS 也能验）：

| 验收项 | 怎么验（本机） | 判据 |
|---|---|---|
| §11.1 三层可切 | 先 `POST /api/wards {"uid":"ward_101","name":"101 病房"}`（admin 槽），再 `GET /api/session/user`（kiosk 槽） | 返回 `role` 随主体在 `ward`/`elder` 间切；前端层级栏三组可见 |
| §11.2 口令门 | 连发 3 次错口令再发正确口令 | 第 4 次返回 `ok:false`（冷却），审计有 `session_login_fail`；等 10s 后正确口令可进 |
| §11.3 口令可改可关 | 改口令 → 旧口令失效；关口令门 → 无口令直接进且 `source=auth_disabled`；开回来 → 需口令 | 审计 `admin_password_changed` / `admin_auth_changed` |
| §11.4 R1 | `POST /api/session/user` 带 `"role":"admin"` | HTTP 400 |
| §11.5 集体层无个人数据 | 给老人填 `notes="糖尿病"` → `GET /api/context?uid=ward_101` | 返回的 System Prompt 里**不含**"糖尿病"，但含本病房最近对话 |
| §11.6 R5 单向 | 集体层说"明天九点体检" → 切到该老人 | 老人 System Prompt 含这条；反向：老人私聊内容**不出现**在 `ward_101` 的后续上下文 |
| §11.7 跨病房隔离 | 102 老人上下文 | 不含 `ward_101` 的任何消息 |
| §11.8 TTL | 把 `admin_session_ttl_s` 调成 20 秒，登录后等过期 | 审计 `session_expired`，再发管理请求得 403 |
| §11.9 双槽隔离 | admin 槽登录 → 查 kiosk 槽 | kiosk 槽 `role` 仍是 `ward`/`elder` |
| §11.10 管理员语音 | kiosk 槽登录管理员后调 `_apply_role_subject("elder_001")` | 主体不变（`role=admin`），审计 `voice_spk action=ignored_in_admin` |
| §11.11 未识别默认态 | kiosk 槽主体为空时发 `/api/chat` | System Prompt 里无任何老人档案，角色是集体层 |
| §11.12 不破坏既有 | `POST /api/alarm`（任何角色）；`python -c "import LLM.server"` | 报警 `ok:true`；import 成功 |
| §11.13 按位置自动切病房 | `maptags.upsert_zone("my_map", {"name":"102","kind":"ward","polygon":[[17,-3],[23,-3],[23,3],[17,3]]})` → 取回 `uid` 写进 `ward_102`；`POST /api/mapeditor/pose/inject {"x":20,"y":0,"yaw":0,"width":100,"height":100,"resolution":0.05,"origin":[0,0,0]}`；设置 `ward_map_source="setting"` + `current_map="my_map"` | 3 秒后 `GET /api/session/user` 的 `ward_uid=ward_102`，审计 `ward_change source=location`；把位姿挪到 `(99,99)` 后**不变** |
| §11.14 不打断私聊 | 上一步之前先把 kiosk 切到某位 101 的老人 | 主体不动（仍 `elder`），但 `ward_uid` 已变 `ward_102`；退出私聊回集体层即 `ward_102` |
| §11.15 位置源不可用即降级 | 设置里把 `ward_autoswitch_enabled` 关掉（或让指纹认不出） | `/api/session/user` 的 `autoswitch.enabled=false`、reason 明确；对话照常无报错 |
| §11.16 手动覆盖 | 手动把当前病房设为 `ward_101` 后再注入 `ward_102` 位姿 | 10 分钟内不抢（仍 `ward_101`），`autoswitch.reason="manual_override"` |

把结果逐条记进 `docs/log.md`。

- [ ] **步骤 2：降级自检**

```bash
.venv\Scripts\python.exe -c "import LLM.server; print('import ok')"
.venv\Scripts\python.exe -c "from LLM import zonegeo, policy, session; print('modules ok')"
```
预期：两行都打印 ok（**新代码不得引入顶层硬 import 第三方库**；`zonegeo`/`policy`/`session` 只依赖 stdlib 与仓内模块）。

- [ ] **步骤 3：改 AGENTS.md 与规格状态**

`AGENTS.md` 的「LLM/ 后端」模块表补四行（`session.py` / `policy.py` / `zonegeo.py` / `prompt/`），「关键约定」补一段：

```markdown
7. **角色闸门与红线（2026-09-14 分层用户体系）**：前端传上来的 role 一律不可信（R1）——uid→role
   只由 `session.derive_role()` 按 `profiles.kind` 推导；未知 ≈ 集体层最小能力（R2）；急停/呼救
   永远放行（R3）；医疗写入红线与角色无关（R4）；层级上下文单向（R5）。业务接口按请求头
   `X-Surface: kiosk|admin` 取 principal。**病房区域几何的唯一真相是地图文件夹的
   `<图名>.tags.json`，`brain.db.zones` 只是只读缓存；`profiles` 只记（地图名 + 区域 uid）。**
```

`docs/superpowers/specs/2026-09-14-layered-user-roles-design.md` 头部状态改「**已实现 P0（2026-09-14）**」，并把本文档路径标为"实现计划（v2，已执行）"。

- [ ] **步骤 4：跑全量测试 + 最终 Commit**

```bash
.venv\Scripts\python.exe -m pytest LLM/tests tests -q
# 预期：通过数 = 191 + 本次新增（约 60 项）；红态仍是那 4 个既有的
cd frontend && pnpm test && pnpm -r build
git add AGENTS.md docs/log.md docs/superpowers/specs/2026-09-14-layered-user-roles-design.md
git commit -m "docs: 分层用户体系 P0 实现日志 + AGENTS 红线 R1-R5 与角色闸门"
```

---

## 交付说明

- 本计划覆盖规格 **P0（用户系统）**。规格 §6.3 动作分级、§7 地点白名单 / `robot_goto` / 二次确认属 **P1 暂缓**（用户 2026-09-14：「先不管 mcp，先完成用户系统」）。
- **全程不需要动车、不需要板卡**：病房自动切换用注入假位姿验证；真机联调（rosbridge 实位姿 + 真地图指纹）留到 MCP/导航线重启后一并做。
- **每完成一个任务就 commit**（计划里每任务末尾都给了 commit 命令），不要攒大提交。
- 动手前的三条提醒（来自规格 §14）：① 先跑一次基线命令认下那 4 个既有红态；② 任何"改标记"的接口都必须是「改 `<图名>.tags.json` → `maptags.sync_map()` 刷缓存 → 审计」三步，**禁止只改 SQLite**；③ 病房自动切换的一切路径都要能"拿不到就不切"。
- 验收通过后，把 P1 的动作约束按规格 §7 另立计划。

---

# 实现台账与偏差（2026-09-14 落地）

> **本节性质：** 上文（v2 计划正文）是**设计意图**，本节是**实际落地事实**。凡与正文冲突，以本节为准。
> **事实来源：** 本节条目逐条核对过 `LLM/{zonegeo,policy,session,chat,tools,memory,voice_api,server,db,conf}.py`、
> `LLM/prompt/{ward,elder,admin}.md`、`frontend/packages/{shared,kiosk,admin}/**` 与
> `pytest --collect-only -q` 的**实际代码/实测值**，不是照抄计划。
> **「计划怎么写」一列以基线 `6295df3` 的计划文本为准** —— 计划文件在实现期被**部分回写**
> （`255f2e4`/`b83cb9f`/`610bee1` 是计划修订提交；并行会话的 `29fa314` 又混入 330 行计划回写，
> 其中包含任务 4 的白名单与浅拷贝），所以**直接看当前工作区的计划文件已经看不到这些差异**，
> 必须 `git show 6295df3:<本文件>` 才能对照。

## L1. 各任务的测试条数（最终值）

`LLM/tests` 按文件 `--collect-only -q` 实测（2026-09-14）：

| 任务 | 测试文件 | 条数 |
|---|---|---|
| 1 | `LLM/tests/test_zonegeo.py` | 6 |
| 2 | `LLM/tests/test_ward_db.py` | 16 |
| 3 | `LLM/tests/test_settings_roles.py` | 3 |
| 4 | `LLM/tests/test_policy_roles.py` | 7 |
| 5 / 6（同文件） | `LLM/tests/test_session_roles.py` | 26（会话/角色/双槽/TTL + 口令族） |
| 7 | `LLM/tests/test_ward_autoswitch.py` | 15 |
| 8 | `LLM/tests/test_prompt_layers.py` | 11 |
| 9 | `LLM/tests/test_policy_tools.py` | 12 |
| 10 | `LLM/tests/test_ward_memory.py` + `LLM/tests/test_worker_roles.py` | 4 + 9 = 13 |
| 11 | `LLM/tests/test_server_roles_routes.py` | 14 |
| 12-14 | 前端 `vitest`（**本机跑不了**，见 L3 第 1 条） | 未测 |

- **新增合计 123 条、11 个新测试文件**。计划 §13/任务清单写的是"10 个新测试文件"：实际是 **11 个**
  （口令族用例进了任务 2 的 `test_ward_db.py`，任务 5/6 共用 `test_session_roles.py`，
  另多了 `test_worker_roles.py`）—— 条数与本表为准。
- 另有 **1 个既有文件改断言**：`LLM/tests/test_chat_text_tts.py` 随 `_post_chat_jobs(..., role)` 新签名
  改断言（用例条数不变，不是新增）。
- 全量实测：`pytest LLM/tests -q` → **222 passed**；`pytest tests -q` → **4 failed / 186 passed / 1 skipped**
  （4 个红态是既有基线漂移，红态名与 §12 记的一致，未修）。

## L2. 事实性偏差（计划怎么写 → 实际怎么落 → 为什么）

### 任务 4：`policy.py`

| # | 计划怎么写 | 实际怎么落 | 为什么 |
|---|---|---|---|
| L2-1 | `ward.allowed_tools = []`（"集体层无任何工具"，测试断言 `p["allowed_tools"] == []`） | `["robot_status", "robot_stop"]` | **R3 急停/呼救永远放行** —— 空列表等于连急停都做不了；规格 §3.3 里 `robot_stop` 是安全动作（`aaef2cc`） |
| L2-2 | `role_policy()` 直接 `return POLICY_DEFAULTS.get(...)`（返回共享 dict） | 返回浅拷贝（`allowed_tools` 也是新 list） | 策略表是模块级共享常量，调用方一次原地 `append` 就会**往低权限角色的白名单里长出工具**（比少一条危险得多）；已补回归用例 `test_role_policy_returns_copy_not_the_shared_table` |

### 任务 8：提示词分层与集体层上下文

| # | 计划怎么写 | 实际怎么落 | 为什么 |
|---|---|---|---|
| L2-3 | `_load_role_prompt`：`path = base_dir / f"{role or 'ward'}.md"` | 文件名取 `role_policy(role)["prompt_file"].name` | 未知角色在 `role_policy` 里已 fail-closed 落集体层，这里必须**跟着落 `ward.md`**；否则一个没见过的 `role` 会把整层角色提示词都丢掉、只剩共用 base |
| L2-4 | `ward.md` 第 4 条：「别用**"张奶奶"这种称呼**点名」 | 「别用**具体姓名**点名」 | 计划同文件的测试断言 `assert "张奶奶" not in sys_p` —— 片段里照写"张奶奶"会**与断言互斥**（把测试要排除的字符串亲手写进提示词） |
| L2-5 | `build_system` 里 `rag.recall_v3(uid, ...)` / `db.get_summary(uid)`（直接用入参 uid） | 以 `principal["uid"]` 为准（`data_uid`；`principal` 缺省时才回落到入参 uid） | 客户端传来的 uid 可能过期/伪造，拿它取档案会把**另一位老人的画像**注入当前会话（R1 与 R5 同族的互泄） |
| L2-6 | `_ward_context` 只判 `if not ward_uid: return ""` | 再加**"必须是真病房"**守卫（`db.get_profile_kind(ward_uid) != "ward"` → 空串 + 审计 `ward_context_denied`） | `profiles.ward_id` 一旦被误设成某位老人的 uid，就会把那位的**私聊**当"病房里刚说过的事"注入给别人（R5 的反方向） |
| L2-7 | `int(settings.get("ward_context_window", 10))`（无保护） | `try/except (TypeError, ValueError)` → 10 | 设置被写坏也不许炸掉整条对话（降级原则） |

### 任务 9：工具角色白名单（闸门 2）

| # | 计划怎么写 | 实际怎么落 | 为什么 |
|---|---|---|---|
| L2-8 | 测试直接断言 `_names({}, _p("ward")) == []`、`set(_names(...)) == set(tools._TOOL_REGISTRY)` | 改为**注册测试期探针工具**（`__probe__`/`__probe_ward_only__` fixture，用完即摘）再断言可见性 | 空列表断言本身与 R3 冲突（见 L2-1）；拿真注册表当期望值会让"工具集合变化"变成假红。探针 + 对照组才有判别力 |
| L2-9 | 只写"admin 不被角色裁剪" | 明确为**只收窄**（交集语义）：admin 也受工具自身 `roles` 约束 | 否则 `@tool(roles={"ward"})` 这类声明对 admin 形同虚设，"闸门 2"漏成两套口径 |
| L2-10 | `_mcp_tools_for()` 单独按服务器 `roles` 过滤 MCP 工具（未声明 = 仅 admin） | 纳入**同一份角色白名单交集**（白名单是天花板），并给 `run_tool()` 补**服务器级 roles 二次校验** | 只按服务器 roles 过滤时，白名单外的高权限 MCP 工具仍能被低权限角色**看见并调用**；回归用例 `test_mcp_tools_intersect_role_whitelist` / `test_run_tool_denies_mcp_outside_server_roles` |
| L2-11 | `policy_deny` 审计未列字段 | 补 `decision`（规格 §9 要求每条策略判定含 `role/uid/slot/tool/decision/reason`） | 没有 `decision` 的审计无法区分"拒绝"与"记账"，事后追溯不可用；断言 `test_policy_deny_audit_has_decision_field` |

### 任务 10：集体层不沉淀 + 语音链路接入角色

| # | 计划怎么写 | 实际怎么落 | 为什么 |
|---|---|---|---|
| L2-12 | worker：`uid = recognized_uid or principal["uid"] or role_session.current_ward()`，非空就 `set_subject(uid, ...)` | **未识别时不改主体**（不再把"当前病房/兜底 uid"回灌成主体） | 回灌会把"没认出是谁"变成一次**主体切换**，还可能把兜底值当老人的话沉淀成记忆 |
| L2-13 | 前端 `unlock()` 传 `props.session.uid ?? ""`（把当前 uid 原样送回） | 后端**忽略前端 uid**：解锁 = 回**当前病房**的集体层（规格 §4.5） | 否则"解锁"等于用前端 uid 再设一次主体，可借解锁把主体设成任意 uid（绕开"解锁不改人"的语义） |
| L2-14 | `_ensure_schema` 只做记忆化幂等建表 | 加**廉价探活 + 失效重试**：命中缓存也读一次 `profiles`；库文件被删/被换则清记忆化重试一次，仍失败就抛出 | 记忆化的洞是"库文件被删/被换（路径没变）"会让车前屏轮询端点**一直 500**；同时不吞真实错误（如目录不可写） |
| L2-15 | `/api/chat` 显式把角色传给 `_post_chat_jobs` | 再加「`role=None` 时**从会话层现取**」，取不到按 fail-closed 记 `ward` 且异常不外抛 | 语音等老调用点只传 3 个参数；漏这一步会把**病房公开对话当老人的话沉淀**（规格 §5.3），异常外抛则整条 post-chat 管线（沉淀/摘要）跟着丢 |

### 任务 11：REST 接口 + lifespan 接线

| # | 计划怎么写 | 实际怎么落 | 为什么 |
|---|---|---|---|
| L2-16 | `_surface`：`return x_surface if x_surface in _SURFACES else "kiosk"`（**静默回落**） | 非法值/空串一律 **400**（只有非 str 才按缺省处理） | 静默回落会让 `X-Surface: TABLET` 这类笔误把**管理台的口令登录写进车前屏槽**（顺带提权），且返回体/审计里的 slot 还是那个错名（追溯性一并破坏） |
| L2-17 | `GET /api/session/user` 同步返回 `{**p, ...}` | `await asyncio.to_thread(_session_user_payload, slot)`，并补老契约形状（`ok` 必在、无主体时 `uid` 为 `None`） | `autoswitch_state()` 会碰位姿（最多 drain 0.8s）并反查"车在跑哪张图"（`MAPS_IO=ssh` 下含远端 stat/整图拉取），占住事件循环会拖慢整个后端 |
| L2-18 | lifespan：首启生成口令 + **1 条 WARN**（位置源不可用）+ 每秒 tick | **多补 1 条 WARN**：`ensure_admin_password()` 返回 `None` ⇒ `[WARN] 管理员口令未初始化：管理层将无法登录！` | 首启"半写坏库"（盐在哈希没了）时口令自愈失败**不会进任何日志**，管理员只会看到"进不去"而无从排查。（tick 的 `asyncio.to_thread` 与计划一致，非偏差） |

### 任务 12-14：前端落点

| # | 计划怎么写 | 实际怎么落 | 为什么 |
|---|---|---|---|
| L2-19 | 计划 v1：「**新建** `shared/src/session.ts`」 | **扩展现有** `shared/src/api/session.ts`（+ `api/client.ts` 加 `X-Surface`、`events.ts` +3 事件与 `user_changed` 载荷扩展） | v1 没核实前端现状（session API 早已在 `src/api/` 下）。v2 修订表第 5 条已改，实现按 v2 落地，**无二次偏差** |
| L2-20 | 计划正文把换人按钮写在 `components/UserSwitcher.vue` | 入口按钮在 `components/VoiceStatusBar.vue`（`@open-switcher`），`UserSwitcher.vue` 是**抽屉本体**，开关状态在 `App.vue` | 与 v2 修订表核实的落点（`VoiceStatusBar.vue`）一致；改这一带时别只翻 `UserSwitcher.vue` |
| L2-21 | 计划正文按页签名描述 admin 新页签 | 唯一注册点是 `App.vue` 的 `tabs` 数组（新增 `{id:"wards"}` 病房管理、`{id:"roles"}` 身份与权限），页面组件 `pages/WardsPage.vue` / `pages/RolesPage.vue` | 与 v2 修订表第 5 条一致；页签渲染分支也在 `App.vue`（`WardsPage`/`RolesPage` 各一行） |

## L3. 未做 / 待验（与计划的差距）

1. **前端未验**：本机（沙箱）`cd frontend && pnpm --filter shared test` 直接失败于
   `Error: spawn EPERM`（`child_process.spawn`，栈底 `esbuild@0.21.3/lib/main.js ensureServiceIsRunning`）——
   属环境限制，非代码问题。故三个包的 `vitest` 与 `pnpm -r build` **均未在本机复验**，需用户侧执行
   `cd frontend && pnpm test && pnpm -r build` 确认。
2. **真机联调未做**：rosbridge 真位姿 + 真地图指纹下的病房自动切换（本机只用注入假位姿验过），
   以及"麦克风声纹 → 角色 → 提示词分层"这条链的实测。
3. **P1 未做（D16 暂缓）**：car MCP `robot_goto`（§7.2）、`destinations` 与地点白名单解析、风险分级、
   二次确认状态机、admin「地点白名单」页签（§6.3/§7）。用户 2026-09-14：「先不管 mcp，先完成用户系统」。
4. **验收覆盖**：规格 §11 的 16 条中，13 条以一次性脚本（`.superpowers/sdd/task-15-acceptance.py`，
   **未提交**）在本机跑通 PASS；其余 4 条（§11.8 TTL、§11.10 管理员语音、§11.11 未识别默认态、
   §11.14 不打断私聊）由上表单测覆盖，未在脚本里单列。详见 `docs/log.md` 2026-09-14（续三）条目。
