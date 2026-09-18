# -*- coding: utf-8 -*-
r"""
rosbridge 连接层（规格 §5.4 / §B 通道决策）——「连接 + 降级 + 假数据注入」，不含业务。

为什么走 rosbridge（websocket :9090）而不走 MCP/rclpy：
  LLM 后端默认跑在 **Windows PC** 上，没有 rclpy；rosbridge 是板卡上 `~/tools/nav_screen.sh lat`
  起的那个会话，跨机可用。依赖 ``websocket-client`` 属**已有依赖**（asr_cloud.py 在用），
  但仍按 AGENTS「系统稳健性」顶层 try/except —— 缺失只降级，绝不让 `LLM.server` import 失败。

约定：
  * 所有对外函数**不抛异常**，失败返回 ``None`` 或 ``{"ok": False, ...}``；
  * 位姿/地图元数据都带 TTL（超时视为不可用），不拿旧值糊弄上层；
  * **订阅是连接级的**：rosbridge 的 ``subscribe`` 不跨 socket 保留，换连接必须补发
    （见 ``_connect()`` 里的说明，以及 ``subscribe()/drain()`` 的 ``_sub_ws`` 判定）。
"""
from __future__ import annotations

import json
import threading
import time

from .. import conf

# ---- 可选依赖：websocket-client ----
try:                                       # pragma: no cover - 取决于运行环境
    import websocket                       # type: ignore
    _WS_AVAILABLE = True
    _WS_WHY = ""
except Exception as _e:                    # noqa: BLE001
    websocket = None                       # type: ignore
    _WS_AVAILABLE = False
    _WS_WHY = f"未安装 websocket-client（{type(_e).__name__}）"

_lock = threading.RLock()
_ws = None
_ws_at = 0.0
_ws_error = ""
_retry_at = 0.0
_sub_ws = None                     # 订阅发在**哪条连接**上（rosbridge 订阅不跨连接保留）
_sub_topics: "tuple[str, str] | None" = None   # 上次订阅的话题，重连后据此补发


def available() -> tuple[bool, str]:
    """(是否可用, 原因)。配置缺失/依赖缺失都只降级。"""
    if not _WS_AVAILABLE:
        return False, _WS_WHY
    if not conf.ROSBRIDGE_URL:
        return False, "未配置 ROSBRIDGE_URL"
    return True, ""


def status() -> dict:
    ok, why = available()
    with _lock:
        return {"ok": True, "available": ok, "reason": why,
                "url": conf.ROSBRIDGE_URL, "connected": _ws is not None,
                "subscribed": _sub_ws is not None and _sub_ws is _ws,
                "last_error": _ws_error,
                "connected_at": _ws_at or None,
                "mock": bool(conf.ROSBRIDGE_MOCK_POSE)}


def _drop_ws() -> None:
    """把当前连接标记为已死（下次 ``_connect()`` 重建），订阅态一并解除。"""
    global _ws, _sub_ws
    with _lock:
        _ws = None
        _sub_ws = None


def _connect() -> "websocket.WebSocket | None":
    """惰性建连（带 30s 复用窗口）。返回 ``None`` 表示不可用。"""
    global _ws, _ws_at, _ws_error, _retry_at, _sub_ws
    if not _WS_AVAILABLE:
        return None
    with _lock:
        if _ws is not None and (time.time() - _ws_at) < 30:
            return _ws
        if _ws is None and time.monotonic() < _retry_at:
            return None
        try:
            if _ws is not None:
                try:
                    _ws.close()
                except Exception:      # noqa: BLE001
                    pass
            sock = websocket.create_connection(conf.ROSBRIDGE_URL,
                                              timeout=conf.ROSBRIDGE_TIMEOUT)
            _ws = sock
            _ws_at = time.time()
            _ws_error = ""
            _retry_at = 0.0
            # ★ 订阅态必须跟着连接走：rosbridge 的 subscribe 是**连接级**的，换一条
            #   socket 就全没了。此前只用一个 `_subscribed` 布尔，30s 复用窗口一到就
            #   重连、却永远在 subscribe() 里早退不再补订阅 —— 现象是"rosbridge 已连
            #   但一条数据都收不到"（2026-09-19 实测：强制重连后 drain 恒为 0）。
            _sub_ws = None
            return sock
        except Exception as e:      # noqa: BLE001
            _ws = None
            _sub_ws = None
            _ws_error = f"{type(e).__name__}: {e}"
            _retry_at = time.monotonic() + conf.ROSBRIDGE_RETRY_S
            return None


def _call(payload: dict, expect: str = "", timeout: float | None = None) -> dict | None:
    """发一条 rosbridge op 并等它的应答（``op: service_response`` 等）。失败返回 None。"""
    sock = _connect()
    if sock is None:
        return None
    to = timeout if timeout is not None else conf.ROSBRIDGE_TIMEOUT
    try:
        sock.send(json.dumps(payload))
        sock.settimeout(to)
        deadline = time.time() + to
        while time.time() < deadline:
            raw = sock.recv()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if expect and msg.get("op") != expect:
                continue
            return msg
        return None
    except Exception as e:      # noqa: BLE001
        global _ws_error
        _ws_error = f"{type(e).__name__}: {e}"
        _drop_ws()
        return None


def call_service(service: str, args: dict | None = None, service_type: str = "",
                 timeout: float | None = None) -> dict | None:
    """调一个 ROS 服务（第二期「说去哪」复用这个连接调 ``/robot/navigate_to``）。"""
    payload = {"op": "call_service", "service": service, "args": args or {}}
    if service_type:
        payload["type"] = service_type
    msg = _call(payload, expect="service_response", timeout=timeout)
    if msg is None:
        return None
    return {"ok": bool(msg.get("result")), "values": msg.get("values") or {},
            "raw": msg}


_pose: dict | None = None
_pose_at = 0.0
_map_meta: dict | None = None
_map_at = 0.0


def _on_message(raw: str) -> None:
    """把订阅到的消息分派到最新位姿 / 地图元数据缓存。"""
    global _pose, _pose_at, _map_meta, _map_at
    try:
        msg = json.loads(raw)
    except ValueError:
        return
    topic = msg.get("topic")
    m = msg.get("msg") or {}
    now = time.time()
    if topic == "/amcl_pose" and isinstance(m, dict):
        p = m.get("pose") or {}
        pos, ori = p.get("position") or {}, p.get("orientation") or {}
        if "x" in pos and "y" in pos:
            _pose = {"x": float(pos["x"]), "y": float(pos["y"]),
                     "yaw": _yaw_from_quat(ori), "source": "amcl_pose",
                     "at": now, "cov": (p.get("covariance") or [None])[0]}
            _pose_at = now
    elif topic == "/map" and isinstance(m, dict):
        info = m.get("info") or {}
        if info.get("width") and info.get("height"):
            org = info.get("origin") or {}
            pos = org.get("position") or {}
            _map_meta = {"width": int(info["width"]), "height": int(info["height"]),
                         "resolution": float(info.get("resolution") or 0),
                         "origin": [float(pos.get("x") or 0.0),
                                    float(pos.get("y") or 0.0),
                                    float(_yaw_from_quat(org.get("orientation") or {}) or 0.0)],
                         "at": now}
            _map_at = now


def _yaw_from_quat(ori: dict) -> float | None:
    import math
    try:
        x, y, z, w = float(ori.get("x", 0)), float(ori.get("y", 0)), \
            float(ori.get("z", 0)), float(ori.get("w", 1))
    except (TypeError, ValueError):
        return None
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


_POSE_TOPIC_TYPE = "geometry_msgs/PoseWithCovarianceStamped"
_MAP_TOPIC_TYPE = "nav_msgs/OccupancyGrid"


def _send_subscriptions(sock, pose_topic: str, map_topic: str) -> bool:
    """在 ``sock`` 上发出两条订阅。失败返回 False 并把这条连接判死。"""
    for topic, typ in ((pose_topic, _POSE_TOPIC_TYPE), (map_topic, _MAP_TOPIC_TYPE)):
        try:
            sock.send(json.dumps({"op": "subscribe", "topic": topic, "type": typ,
                                  "throttle_rate": 1000}))
        except Exception:      # noqa: BLE001  发送失败 = 这条连接不能用了
            _drop_ws()
            return False
    return True


def subscribe(pose_topic: str = "/amcl_pose", map_topic: str = "/map") -> bool:
    """订阅位姿与地图元数据（幂等）。返回是否成功发出订阅请求。

    "幂等"的判据是 **同一连接**：换了 socket（30s 复用窗口到期重连、或上次发送失败
    判死）就重新发一遍，否则会误以为还订阅着而永远收不到数据。
    """
    global _sub_ws, _sub_topics
    sock = _connect()
    if sock is None:
        return False
    if _sub_ws is sock:
        return True
    ok = _send_subscriptions(sock, pose_topic, map_topic)
    _sub_ws = sock if ok else None
    _sub_topics = (pose_topic, map_topic) if ok else None
    return ok


def drain(seconds: float = 1.0) -> int:
    """收一小段时间的消息（编辑器 1Hz 轮询就够用）。返回收到的消息条数。

    若期间发生了重连（订阅不跨连接），先按上次的话题补发订阅再收。
    """
    sock = _connect()
    if sock is None:
        return 0
    if _sub_topics is not None and _sub_ws is not sock:
        subscribe(*_sub_topics)
    n = 0
    deadline = time.time() + max(0.0, seconds)
    try:
        sock.settimeout(0.2)
        while time.time() < deadline:
            try:
                raw = sock.recv()
            except Exception:      # noqa: BLE001  超时正常
                continue
            if raw:
                _on_message(raw)
                n += 1
    except Exception:      # noqa: BLE001
        return n
    return n


def latest_pose() -> dict | None:
    """最新一位姿（含 TTL 判定）；过期返回 None。"""
    with _lock:
        if _pose is None:
            return None
        if (time.time() - _pose_at) > conf.ROSBRIDGE_POSE_TTL_S:
            return None
        return dict(_pose)


def latest_map() -> dict | None:
    with _lock:
        if _map_meta is None:
            return None
        if (time.time() - _map_at) > max(conf.ROSBRIDGE_POSE_TTL_S, 30.0):
            return None
        return dict(_map_meta)


def close() -> None:
    global _ws, _sub_ws, _sub_topics
    with _lock:
        if _ws is not None:
            try:
                _ws.close()
            except Exception:      # noqa: BLE001
                pass
        _ws = None
        _sub_ws = None
        _sub_topics = None


def reset_for_test() -> None:
    """测试用：清掉连接与缓存。"""
    global _pose, _pose_at, _map_meta, _map_at, _ws_error, _retry_at, _sub_topics
    close()
    with _lock:
        _pose, _pose_at, _map_meta, _map_at = None, 0.0, None, 0.0
        _ws_error = ""
        _retry_at = 0.0
        _sub_topics = None
