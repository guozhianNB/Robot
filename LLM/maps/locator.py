# -*- coding: utf-8 -*-
r"""
位姿与「车此刻在跑哪张图」（规格 §5.3 / §5.4）—— 只读，且**默认降级可用**。

分工：
  * :mod:`LLM.maps.roslink` 负责"连接 rosbridge + 降级 + 假数据注入"；
  * 本模块负责"位姿从哪来、当前地图是哪张"这两件业务。

降级口径（AGENTS「系统稳健性」）：
  * rosbridge 未配置/连不上/超时 → ``status: "unavailable"``，查询类保持 ``ok: True``；
  * 位姿超 TTL 未更新 → 视为不可用（不拿旧值糊弄编辑器）；
  * ``/amcl_pose`` 恒为原点且未对齐时 → 给 ``suspect: True``（§7.5 的"位姿可疑"提示）；
  * 支持 ``set_pose_for_test()`` 注入假位姿 —— 无 ROS 环境（Windows 开发机）也能跑通全部逻辑。

「当前地图识别」的**第一权威是导航自己加载的那张图**：问 ``map_server`` 的 ``yaml_filename``
参数（导航拉起哪张，这里就是哪张）。只有拿不到它（导航没起 / 不是 nav2 map_server / rosbridge 断）
才退回旧口径 —— 拿 ``/map`` 的 ``width/height/resolution/origin`` 四项与本地每张 yaml 比对、
**唯一命中**才认（规格 §5.3）。

为什么改（2026-09-19 实锤）：像素编辑器「另存」出的副本与原图的四项元数据**天然完全一致**
（`my_map3` vs `my_map3_edited`：yaml 只差 `image:` 一行，pgm 同 88×107/0.05/[-1.2,-1.38,0]），
指纹反查必然"多命中" → 按不唯一口径判 unknown → 车控三个 goto 工具全部
`rejected「当前地图未知」`。问 map_server 要路径则无歧义。
"""
from __future__ import annotations

import threading
import time
import copy
from pathlib import PurePosixPath

from .. import conf
from . import mapserver, roslink
from .mapstore import MapStoreError, check_name, get_store

_lock = threading.RLock()
_injected_pose: dict | None = None
_injected_map: dict | None = None
_last: dict | None = None
_current_cache: tuple[tuple, float, dict] | None = None


# ---------------------------------------------------------------------------
# 假数据注入（测试 / 无 ROS 开发）
# ---------------------------------------------------------------------------
def set_pose_for_test(x: float | None, y: float | None = None, yaw: float | None = None,
                      source: str = "injected") -> None:
    """注入假位姿（``x=None`` 清除）。规格 §5.4「可注入假位姿」。"""
    global _injected_pose
    with _lock:
        if x is None:
            _injected_pose = None
        else:
            _injected_pose = {"x": float(x), "y": float(y or 0.0),
                              "yaw": None if yaw is None else float(yaw),
                              "source": source, "at": time.time()}


def set_map_for_test(width: int | None, height: int | None = None, resolution: float | None = None,
                     origin=None) -> None:
    """注入假地图元数据（``width=None`` 清除）。"""
    global _injected_map
    with _lock:
        if width is None:
            _injected_map = None
        else:
            _injected_map = {"width": int(width), "height": int(height or 0),
                             "resolution": float(resolution or 0.05),
                             "origin": list(origin or [0.0, 0.0, 0.0]), "at": time.time()}
    clear_current_map_cache()


def clear_injection() -> None:
    set_pose_for_test(None)
    set_map_for_test(None)


def _mock_from_env() -> dict | None:
    """``ROSBRIDGE_MOCK_POSE="x,y,yaw"`` 环境变量注入（无 ROS 的演示环境）。"""
    raw = (conf.ROSBRIDGE_MOCK_POSE or "").strip()
    if not raw:
        return None
    try:
        parts = [float(v) for v in raw.split(",")]
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    return {"x": parts[0], "y": parts[1], "yaw": parts[2] if len(parts) > 2 else None,
            "source": "mock_env", "at": time.time()}


# ---------------------------------------------------------------------------
# 位姿
# ---------------------------------------------------------------------------
def available(settings: dict | None = None) -> tuple[bool, str]:
    """位姿源是否可用（不问"能不能拿到值"，只问"通道在不在"）。"""
    with _lock:
        if _injected_pose is not None or _injected_map is not None:
            return True, "injected"
    if _mock_from_env() is not None:
        return True, "mock_env"
    return roslink.available()


def get_pose(force: bool = False) -> dict | None:
    """拿当前位姿；不可用返回 ``None``（调用方据此显示"位置未知"）。

    返回 ``{x, y, yaw, source, at, suspect, details}``。
    """
    global _last
    with _lock:
        inj = dict(_injected_pose) if _injected_pose is not None else None
    if inj is not None:
        _last = {**inj, "suspect": False}
        return dict(_last)
    mock = _mock_from_env()
    if mock is not None:
        _last = {**mock, "suspect": False}
        return dict(_last)
    ok, why = roslink.available()
    if not ok:
        return None
    # 先看缓存里有没有（订阅流已推过来）；没有再短收一会儿
    pose = roslink.latest_pose()
    if pose is None:
        roslink.subscribe()
        roslink.drain(0.8)
        pose = roslink.latest_pose()
    if pose is None:
        return None
    suspect = bool(pose.get("cov") == 0.0 and abs(pose.get("x", 0)) < 1e-6
                   and abs(pose.get("y", 0)) < 1e-6 and abs(pose.get("yaw") or 0) < 1e-9)
    out = {**pose, "suspect": suspect}
    _last = dict(out)
    return out


def pose_payload(settings: dict | None = None) -> dict:
    """``GET /api/robot/pose`` 的载荷。降级时保持 ``ok: True``（AGENTS 口径）。"""
    ok, why = available(settings)
    if not ok:
        return {"ok": True, "status": "unavailable", "reason": why,
                "source": None, "x": None, "y": None, "yaw": None}
    pose = get_pose()
    if pose is None:
        return {"ok": True, "status": "unavailable",
                "reason": f"位姿不可用（rosbridge {conf.ROSBRIDGE_URL} 未连上或导航未运行）",
                "source": None, "x": None, "y": None, "yaw": None}
    return {"ok": True, "status": "ok", "source": pose.get("source"),
            "x": pose.get("x"), "y": pose.get("y"), "yaw": pose.get("yaw"),
            "suspect": bool(pose.get("suspect")),
            "note": "位姿长时间恒在原点，AMCL 初始位姿可能尚未对齐" if pose.get("suspect") else ""}


# ---------------------------------------------------------------------------
# 当前地图识别（指纹反查，规格 §5.3）
# ---------------------------------------------------------------------------
def _fingerprint(width, height, resolution, origin) -> tuple:
    """四项指纹：``width/height/resolution/origin(x,y)``（规格 §5.3）。

    只有四项全一致才算命中 —— 三张图的坐标系不通用，宽度相同但原点不同也是两张图。
    """
    org = list(origin or [])
    return (int(width), int(height),
            round(float(resolution), 6) if resolution else None,
            round(float(org[0]), 6) if len(org) > 0 else None,
            round(float(org[1]), 6) if len(org) > 1 else None)


def clear_current_map_cache() -> None:
    """地图文件或 /map 测试输入变化后，强制下一次重新识别。"""
    global _current_cache
    with _lock:
        _current_cache = None


def _map_name_from_param() -> tuple[str, str]:
    """导航**实际加载**的地图名 → ``(name, 认不出的原因)``；认不出返回 ``("", reason)``。

    来源 = ``conf.MAPSERVER_NODE`` 的 ``conf.MAPSERVER_MAP_PARAM``（默认
    ``/map_server`` 的 ``yaml_filename``）。导航用的是哪张图，这个参数就是哪张 ——
    ``nav_screen.sh nav <地图>`` 的 ``map:=`` 经 RewrittenYaml 落到它上面。

    名字只取 basename 去后缀，并过 ``mapstore.check_name()`` 白名单（地图名来自 ROS 参数，
    同样不能信任：``../`` 之类一律当作认不出，退回指纹路）。
    """
    node = getattr(conf, "MAPSERVER_NODE", "/map_server")
    param = getattr(conf, "MAPSERVER_MAP_PARAM", "yaml_filename")
    raw = roslink.node_string_param(node, param)
    if not raw:
        return "", f"未取到 {node}/{param}（导航没起 / 不是 nav2 map_server / rosbridge 不通）"
    stem = PurePosixPath(str(raw).replace("\\", "/")).stem
    try:
        return check_name(stem), ""
    except MapStoreError as e:
        return "", f"{node}/{param} 的值不可用：{e}"


def _topic_map_meta() -> dict | None:
    with _lock:
        inj = dict(_injected_map) if _injected_map is not None else None
    if inj is not None:
        return inj
    ok, _why = roslink.available()
    if not ok:
        return None
    got = roslink.latest_map()
    if got is None:
        roslink.subscribe()
        roslink.drain(1.0)
        got = roslink.latest_map()
    return got


def current_map(store=None) -> dict:
    """``GET /api/map/current`` 的载荷（规格 §5.3）。

    * **第一权威**：``map_server`` 的 ``yaml_filename`` → ``source: "map_server_param"``
      （导航拉起哪张就认哪张，不做推断、不存在多命中）；
    * 读不到才退回 ``/map`` 四项指纹反查：唯一命中 → ``source: "map_topic"``；
    * 多项命中或零命中 → ``source: "unknown"`` 并说明原因；
    * rosbridge 不可用 → ``source: "unknown"``，但**编辑功能照常可用**。

    第一权威不受 ``map_topic_fingerprint_enabled`` 约束：那个开关管的是「用 /map 元数据**推断**」，
    而这里是直接读导航的配置，不是推断（关掉推断的人照样应该能知道在跑哪张图）。
    它也**不进缓存**：一次服务调用很便宜，而换图后必须**尽快**跟着变，不能等 TTL。
    """
    global _current_cache
    param_name, param_why = _map_name_from_param()
    if param_name:
        return {"ok": True, "source": "map_server_param", "name": param_name,
                "detail": (f"{conf.MAPSERVER_NODE}/{conf.MAPSERVER_MAP_PARAM} = "
                           f"{param_name}.yaml（导航实际加载的图）")}
    settings = None
    try:
        from ..store import db as _db
        settings = _db.get_settings()
    except Exception:      # noqa: BLE001  拿不到设置就用默认（不影响只读识别）
        settings = None
    fp_enabled = (settings or conf.DEFAULT_SETTINGS).get("map_topic_fingerprint_enabled", True)
    if not fp_enabled:
        return {"ok": True, "source": "unknown", "name": None,
                "detail": f"「用 /map 元数据识别当前地图」已在设置里关闭，且{param_why}"}
    topic = _topic_map_meta()
    if topic is None:
        ok, why = roslink.available()
        return {"ok": True, "source": "unknown", "name": None,
                "detail": (f"导航未运行或 rosbridge 未启动（{why}）—— 看图与标点不受影响；{param_why}"
                           if not ok else
                           f"未收到 /map（rosbridge {conf.ROSBRIDGE_URL} 已连但无数据）；{param_why}")}
    key = _fingerprint(topic["width"], topic["height"], topic.get("resolution"),
                       topic.get("origin"))
    now = time.monotonic()
    with _lock:
        cached = _current_cache
        if cached is not None and cached[0] == key and cached[1] > now:
            return copy.deepcopy(cached[2])
    st = store or get_store()
    try:
        entries = st.list()
    except MapStoreError as e:
        return {"ok": True, "source": "unknown", "name": None,
                "detail": f"地图列表不可用：{e}"}
    target = key
    hits = []
    for e in entries:
        try:
            info = mapserver.map_info(e["name"], st)
        except MapStoreError:
            continue
        if not info.get("meta_ok") or not info.get("width"):
            continue
        fp = _fingerprint(info["width"], info["height"], info.get("resolution"),
                          info.get("origin"))
        if fp == target:
            hits.append(e["name"])
    if len(hits) == 1:
        result = {"ok": True, "source": "map_topic", "name": hits[0],
                "detail": f"/map {topic['width']}x{topic['height']} @{topic['resolution']} "
                          f"origin={topic['origin'][:2]}",
                "topic": {k: topic[k] for k in ("width", "height", "resolution", "origin")}}
    elif not hits:
        result = {"ok": True, "source": "unknown", "name": None,
                "detail": (f"/map 的元数据与本地任何一张图都不匹配"
                           f"（{topic['width']}x{topic['height']} @{topic['resolution']} "
                           f"origin={topic['origin'][:2]}）"),
                "topic": {k: topic[k] for k in ("width", "height", "resolution", "origin")}}
    else:
        result = {"ok": True, "source": "unknown", "name": None, "ambiguous": hits,
                  "detail": f"地图元数据不唯一，请核对：{'、'.join(hits)}",
                  "topic": {k: topic[k] for k in ("width", "height", "resolution", "origin")}}
    completed_at = time.monotonic()
    with _lock:
        _current_cache = (key, completed_at + conf.MAP_CURRENT_CACHE_TTL_S, copy.deepcopy(result))
    return result


def status(store=None) -> dict:
    """编辑器顶部状态条要的那一份汇总。"""
    ok, why = available()
    pose = pose_payload()
    return {"pose": pose, "rosbridge": roslink.status(),
            "available": ok, "reason": why, "current_map": current_map(store)}
