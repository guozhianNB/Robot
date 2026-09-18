# -*- coding: utf-8 -*-
r"""车控 rosbridge 四连接；网络等待不持有任务状态锁，急停独立发送。

call_action 仅传输；上层负责 begin_task / finish_task。所有调用必须带 task_id，
在发送前检查任务是否仍有效，发送后登记 dispatched。状态话题没有 task_id，
因此只能用 dispatched + seen_active 门控，不能证明跨连接旧帧的来源。
"""
from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
import uuid

from .. import conf

try:
    import websocket
except Exception:  # 可选依赖不影响后端导入。
    websocket = None


class CarLink:
    SEND_TIMEOUT_S = 0.5
    _ACTIONS = {"move": "Move", "turn": "Turn", "navigate_to": "NavigateTo"}
    _TASK_ACTIONS = {"move_forward", "move_back", "move_left", "move_right", "turn",
                     "goto_point", "goto_zone", "goto_place"}
    _TOPICS = (("/robot/exec_state", "std_msgs/String"),
               ("/robot/arrived", "std_msgs/Bool"),
               ("/amcl_pose", "geometry_msgs/PoseWithCovarianceStamped"))

    def __init__(self, url=None, *, ws_factory=None, logger=None,
                 heartbeat_ttl=None, probe_timeout=None, action_timeout=None):
        self.url = conf.ROSBRIDGE_URL if url is None else url
        self._factory = ws_factory
        self._logger = logger
        self.heartbeat_ttl = self._env_seconds("CAR_HEARTBEAT_TTL_S", heartbeat_ttl, 3.0)
        self.probe_timeout = self._env_seconds("CAR_PROBE_TIMEOUT_S", probe_timeout, 3.0)
        self.action_timeout = self._env_seconds("CAR_ACTION_TIMEOUT_S", action_timeout, 190.0)
        self._lock = threading.RLock()
        self._dispatch_lock = threading.Lock()
        self._readiness_lock = threading.Lock()
        self._io_locks = {name: threading.Lock() for name in ("ctrl", "probe", "stop", "state")}
        self._sockets = dict.fromkeys(self._io_locks)
        self._closed = threading.Event()
        self._start_lock = threading.Lock()
        self._state_thread = None
        self._generation = 0
        self._current = self._last = None
        self._exec_state = "unknown"
        self._state_at = self._state_mono = None
        self._pose = self._arrived = None
        self._arrived_at = None
        self._last_error = ""
        self._idle_override = False
        self._needs_idle_probe = False
        self._nav_available = False

    @staticmethod
    def _env_seconds(name, explicit, default):
        if explicit is not None:
            return float(explicit)
        try:
            value = float(os.environ.get(name, default))
            return value if math.isfinite(value) and value > 0 else default
        except (TypeError, ValueError):
            return default

    def _error(self, error):
        detail = str(error) or type(error).__name__
        with self._lock:
            self._last_error = detail
        if self._logger is not None:
            try:
                self._logger(detail)
            except Exception:
                pass
        return {"ok": False, "status": "error", "error": detail, "message": detail}

    def _dependency_error(self):
        if self._factory is None and websocket is None:
            return "缺少可选依赖 websocket-client"
        if not self.url:
            return "未配置 ROSBRIDGE_URL"
        if self._closed.is_set():
            return "CarLink is closed"
        return ""

    @staticmethod
    def _close_socket(sock):
        if sock is not None:
            try:
                # websocket-client 的默认 close 会等待握手；shutdown 直接唤醒 recv。
                if hasattr(sock, "shutdown"):
                    sock.shutdown()
                else:
                    sock.close()
            except Exception:
                pass

    def _drop(self, channel, sock):
        with self._lock:
            if self._sockets[channel] is sock:
                self._sockets[channel] = None
            if channel == "state":
                self._state_mono = None
        self._close_socket(sock)

    def _connect(self, channel):
        """调用者持本通道锁；所有 connect/send/recv 均在状态锁外。"""
        reason = self._dependency_error()
        if reason:
            raise RuntimeError(reason)
        with self._lock:
            sock = self._sockets[channel]
        if sock is not None:
            return sock
        factory = self._factory or websocket.create_connection
        sock = factory(self.url, timeout=0.3 if channel == "state" else self.probe_timeout)
        try:
            if channel == "state":
                sock.settimeout(0.2)
                for topic, typ in self._TOPICS:
                    sock.send(json.dumps({"op": "subscribe", "topic": topic, "type": typ}))
            elif channel == "stop":
                sock.settimeout(self.SEND_TIMEOUT_S)
                sock.send(json.dumps({"op": "advertise", "topic": "/robot/cmd_stop",
                                      "type": "std_msgs/Bool"}))
            with self._lock:
                if self._closed.is_set():
                    raise RuntimeError("CarLink is closed")
                self._sockets[channel] = sock
            return sock
        except Exception:
            self._close_socket(sock)
            raise

    def start(self):
        with self._start_lock:
            reason = self._dependency_error()
            if reason:
                return self._error(reason)
            errors = []
            for channel in self._sockets:
                try:
                    with self._io_locks[channel]:
                        self._connect(channel)
                except Exception as exc:
                    errors.append(f"{channel}: {exc}")
            if self._state_thread is None:
                self._state_thread = threading.Thread(target=self._state_loop, name="car-state", daemon=True)
                self._state_thread.start()
            return self._error("; ".join(errors)) if errors else {"ok": True}

    def close(self):
        self._closed.set()
        with self._lock:
            sockets = list(self._sockets.values())
            self._sockets = dict.fromkeys(self._sockets)
            self._state_mono = None
        for sock in sockets:
            self._close_socket(sock)
        thread = self._state_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        return {"ok": True}

    def _service(self, channel, service, typ, arguments, timeout, task_id=None):
        sock = None
        dispatched = False
        try:
            with self._io_locks[channel]:
                sock = self._connect(channel)
                request_id = uuid.uuid4().hex
                payload = json.dumps({"op": "call_service", "service": service,
                                      "type": typ, "args": arguments, "id": request_id}, allow_nan=False)
                sock.settimeout(self.SEND_TIMEOUT_S)
                if channel == "ctrl":
                    # 与 stop 的第二次发布建立本地顺序；绝不包含 recv。
                    with self._dispatch_lock:
                        with self._lock:
                            if type(task_id) is not int or task_id <= 0 or not self._matches(task_id):
                                return {"ok": False, "status": "stale", "error": "任务已失效"}
                        sock.send(payload)
                        self.mark_dispatched(task_id)
                        dispatched = True
                else:
                    sock.send(payload)
                deadline = time.monotonic() + timeout
                while not self._closed.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"{service} timeout")
                    sock.settimeout(remaining)
                    raw = sock.recv()
                    if not raw:
                        raise ConnectionError(f"{service} disconnected")
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError("rosbridge response must be an object")
                    if message.get("id") != request_id or message.get("op") != "service_response":
                        continue
                    values = message.get("values")
                    if message.get("result") is not True:
                        detail = values.get("message") if isinstance(values, dict) else values
                        return self._error(detail or f"{service} service failed")
                    if not isinstance(values, dict):
                        raise ValueError("rosbridge values must be an object")
                    if channel == "ctrl" and values.get("success") is not True:
                        return self._error(values.get("message") or "action failed")
                    return {"ok": True, "values": values, "message": values.get("message", "")}
                raise RuntimeError("CarLink is closed")
        except Exception as exc:
            if sock is not None:
                self._drop(channel, sock)
            result = self._error(exc)
            if channel == "ctrl" and dispatched:
                result["uncertain"] = True
            return result

    def readiness(self, timeout=None):
        # 包住收包及缓存应用，防同 generation 的旧 idle 覆盖较新 busy/error。
        # 锁序：readiness → probe → state；没有反向获取 readiness 的路径。
        with self._readiness_lock:
            return self._readiness(timeout)

    def _readiness(self, timeout):
        with self._lock:
            generation = self._generation
        result = self._service("probe", "/robot/readiness", "robot_interfaces/srv/RobotReadiness", {},
                               self.probe_timeout if timeout is None else timeout)
        if not result["ok"]:
            with self._lock:
                if generation == self._generation:
                    self._state_mono = None
            return result
        values = result["values"]
        if (not isinstance(values.get("ready"), bool) or
                values.get("exec_state") not in ("idle", "moving", "navigating", "error") or
                not isinstance(values.get("nav_available"), bool)):
            with self._lock:
                if generation == self._generation:
                    self._state_mono = None
            return self._error("invalid readiness values")
        with self._lock:
            if generation == self._generation:
                self._nav_available = values["nav_available"]
                if self._idle_override:
                    if values["ready"] and values["exec_state"] == "idle":
                        self._needs_idle_probe = False
                        self._state_mono, self._state_at = time.monotonic(), time.time()
                    else:
                        self._needs_idle_probe = True
                        self._state_mono = None
                else:
                    self._exec_state = values["exec_state"]
                    self._state_mono, self._state_at = time.monotonic(), time.time()
                    if not values["ready"] and values["exec_state"] == "idle":
                        self._state_mono = None
        return {"ok": True, **values}

    def call_action(self, action, request, timeout=None, *, task_id: int):
        if type(task_id) is not int or task_id <= 0:
            return self._error("task_id must be a positive int")
        if action not in self._ACTIONS:
            return self._error(f"unknown action: {action}")
        return self._service("ctrl", f"/robot/{action}", f"robot_interfaces/srv/{self._ACTIONS[action]}",
                             request, self.action_timeout if timeout is None else timeout, task_id)

    def _matches(self, task_id):
        return self._current is not None and self._current["task_id"] == task_id

    def begin_task(self, action, target, expected_state):
        with self._lock:
            if self._current is not None:
                return {"ok": False, "status": "rejected", "error": "已有执行中的任务"}
            if (self._closed.is_set() or self._needs_idle_probe or self._state_mono is None or
                    time.monotonic() - self._state_mono > self.heartbeat_ttl or
                    self._exec_state == "unknown"):
                return {"ok": False, "status": "error", "error": "执行状态未知或过期，请重新探测"}
            if self._exec_state != "idle":
                return {"ok": False, "status": "rejected", "error": f"执行器忙：{self._exec_state}"}
            if action not in self._TASK_ACTIONS or expected_state not in ("moving", "navigating"):
                return {"ok": False, "status": "error", "error": "invalid action or expected_state"}
            self._generation += 1
            self._idle_override = False
            self._current = {"task_id": self._generation, "action": action, "target": copy.deepcopy(target),
                             "expected_state": expected_state, "seen_active": False, "dispatched": False,
                             "started_at": time.time()}
            return {"ok": True, "status": "accepted", **copy.deepcopy(self._current)}

    def mark_dispatched(self, task_id):
        with self._lock:
            if not self._matches(task_id):
                return {"ok": False, "status": "stale"}
            self._current["dispatched"] = True
            return {"ok": True}

    def finish_task(self, task_id, ok, detail):
        with self._lock:
            if not self._matches(task_id):
                return {"ok": False, "status": "stale", "task_id": task_id}
            at = time.time()
            self._last = {**self._current, "ok": bool(ok), "detail": copy.deepcopy(detail),
                          "at": at, "finished_at": at}
            self._current = None
            return {"ok": True, "status": "finished", "task_id": task_id}

    def mark_uncertain(self, task_id, detail):
        with self._lock:
            if not self._matches(task_id):
                return {"ok": False, "status": "stale", "task_id": task_id}
            if not self._current.get("dispatched"):
                return self.finish_task(task_id, False, detail)
            at = time.time()
            self._exec_state = "unknown"
            self._state_at = at
            self._state_mono = None
            self._last = {**self._current, "ok": False, "detail": copy.deepcopy(detail),
                          "at": at, "finished_at": at, "status": "uncertain"}
            return {"ok": True, "status": "uncertain", "task_id": task_id}

    def stop(self):
        sock = None
        try:
            with self._io_locks["stop"]:
                sock = self._connect("stop")
                payload = json.dumps({"op": "publish", "topic": "/robot/cmd_stop", "msg": {"data": True}})
                sock.settimeout(self.SEND_TIMEOUT_S)
                sock.send(payload)
                with self._dispatch_lock:
                    with self._lock:
                        self._generation += 1
                        self._current = None
                        at = time.time()
                        self._last = {"task_id": self._generation, "action": "stop", "ok": True,
                                      "at": at, "finished_at": at, "detail": "stop published"}
                        self._exec_state = "idle"
                        self._idle_override = self._needs_idle_probe = True
                        self._state_mono = None
                        self._state_at = time.time()
                    sock.send(payload)
            return {"ok": True, "message": "stop published"}
        except Exception as exc:
            if sock is not None:
                self._drop("stop", sock)
            return self._error(exc)

    def snapshot(self):
        with self._lock:
            fresh = self._state_mono is not None and time.monotonic() - self._state_mono <= self.heartbeat_ttl
            return copy.deepcopy({"ok": True, "available": all(self._sockets.values()),
                                  "exec_state": self._exec_state, "state_fresh": fresh,
                                  "moving": self._exec_state in ("moving", "navigating"),
                                  "pose": self._pose, "arrived": self._arrived, "arrived_at": self._arrived_at,
                                  "current": self._current, "last": self._last, "state_at": self._state_at,
                                  "nav_available": self._nav_available, "last_error": self._last_error})

    def _on_state(self, raw):
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise ValueError("state message must be an object")
        if message.get("op") != "publish":
            return
        topic, body = message.get("topic"), message.get("msg")
        now = time.time()
        if topic == "/amcl_pose":
            pose = body["pose"]["pose"]
            pos, ori = pose["position"], pose["orientation"]
            x, y = float(pos["x"]), float(pos["y"])
            qx, qy, qz, qw = (float(ori[key]) for key in ("x", "y", "z", "w"))
            if not all(math.isfinite(v) for v in (x, y, qx, qy, qz, qw)):
                raise ValueError("non-finite pose")
            yaw = math.degrees(math.atan2(2 * (qw*qz + qx*qy), 1 - 2 * (qy*qy + qz*qz)))
            with self._lock:
                self._pose = {"x": x, "y": y, "yaw_deg": yaw, "at": now}
        elif topic == "/robot/arrived":
            if not isinstance(body["data"], bool):
                raise ValueError("invalid arrived")
            with self._lock:
                self._arrived, self._arrived_at = body["data"], now
        elif topic == "/robot/exec_state":
            state = body["data"]
            if state not in ("idle", "moving", "navigating", "error"):
                raise ValueError("invalid exec_state")
            with self._lock:
                if self._idle_override:
                    return
                self._exec_state = state
                self._state_at, self._state_mono = now, time.monotonic()
                current = self._current
                if current and current["dispatched"]:
                    if state == current["expected_state"]:
                        current["seen_active"] = True
                    elif current["seen_active"] and state in ("idle", "error"):
                        self.finish_task(current["task_id"], state == "idle", f"exec_state: {state}")

    def _state_loop(self):
        while not self._closed.is_set():
            sock = None
            try:
                with self._io_locks["state"]:
                    sock = self._connect("state")
                    raw = sock.recv()
                if not raw:
                    raise ConnectionError("state disconnected")
                try:
                    self._on_state(raw)
                except Exception as exc:
                    self._error(f"invalid state message: {exc}")
            except Exception as exc:
                timeout_type = getattr(websocket, "WebSocketTimeoutException", TimeoutError)
                if isinstance(exc, (TimeoutError, timeout_type)):
                    continue
                if sock is not None:
                    self._drop("state", sock)
                if not self._closed.is_set():
                    self._error(exc)
                    self._closed.wait(0.1)
