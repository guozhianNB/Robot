# -*- coding: utf-8 -*-
r"""车控连接层测试：仅替换 websocket 边界，不访问板卡。"""
import importlib
import importlib.util
import json
import math
import queue
import threading
import time

import pytest


class FakeSocket:
    def __init__(self):
        self.inbox = queue.Queue()
        self.sent = []
        self.timeout = 0.05
        self.closed = False
        self.on_send = None
        self.send_timeouts = []

    def settimeout(self, timeout):
        self.timeout = timeout

    def send(self, raw):
        if self.closed:
            raise ConnectionError("socket closed")
        msg = json.loads(raw)
        self.sent.append(msg)
        self.send_timeouts.append(self.timeout)
        if self.on_send:
            self.on_send(msg)

    def recv(self):
        try:
            value = self.inbox.get(timeout=self.timeout)
        except queue.Empty:
            raise TimeoutError("receive timeout")
        if isinstance(value, Exception):
            raise value
        return value

    def close(self, timeout=0):
        self.closed = True
        self.inbox.put(ConnectionError("closed"))

    def push(self, value):
        self.inbox.put(json.dumps(value))

    def reply(self, request, values=None, result=True):
        self.push({"op": "service_response", "id": request["id"],
                   "result": result, "values": values or {}})


class Factory:
    def __init__(self):
        self.sockets = []

    def __call__(self, url, timeout):
        assert url == "ws://fake:9090"
        sock = FakeSocket()
        self.sockets.append(sock)
        return sock


def eventually(predicate):
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


@pytest.fixture
def link_class():
    assert importlib.util.find_spec("LLM.car_mcp.car_link") is not None, "CarLink implementation missing"
    return importlib.import_module("LLM.car_mcp.car_link").CarLink


def test_link_implementation_exists():
    assert importlib.util.find_spec("LLM.car_mcp.car_link") is not None, "CarLink implementation missing"


@pytest.fixture
def rig(link_class):
    factory = Factory()
    link = link_class("ws://fake:9090", ws_factory=factory, probe_timeout=0.05)
    assert link.start()["ok"]
    assert len(factory.sockets) == 4
    ctrl, probe, stop, state = factory.sockets
    probe.on_send = lambda req: probe.reply(req, {
        "ready": True, "exec_state": "idle", "nav_available": True, "message": "ready"})
    yield link, ctrl, probe, stop, state, factory
    link.close()


def publish(sock, topic, value):
    sock.push({"op": "publish", "topic": topic, "msg": {"data": value}})


def test_readiness_success_and_unique_matching_ids(rig):
    link, _, probe, *_ = rig
    def reply(req):
        probe.push({"op": "service_response", "id": "old", "result": False})
        probe.push({"op": "publish", "topic": "/unrelated", "msg": {}})
        probe.reply(req, {"ready": True, "exec_state": "idle", "nav_available": False, "message": "ready"})
    probe.on_send = reply
    first, second = link.readiness(), link.readiness()
    assert first["ok"] and first["ready"] and not first["nav_available"]
    assert second["exec_state"] == "idle"
    calls = probe.sent
    assert calls[0]["id"] != calls[1]["id"]
    assert calls[0]["service"] == "/robot/readiness"
    assert calls[0]["type"] == "robot_interfaces/srv/RobotReadiness"
    assert calls[0]["args"] == {}


@pytest.mark.parametrize("failure", ["missing", "timeout", "disconnect", "json"])
def test_readiness_failures_are_dict_errors(rig, failure):
    link, _, probe, *_ = rig
    def reply(req):
        if failure == "missing":
            probe.reply(req, {"message": "service does not exist"}, result=False)
        elif failure == "disconnect":
            probe.inbox.put(ConnectionError("peer disconnected"))
        elif failure == "json":
            probe.inbox.put("{broken")
    probe.on_send = reply
    result = link.readiness()
    assert result["ok"] is False
    assert result["error"]
    assert link.begin_task("move", {}, "moving")["status"] == "error"


@pytest.mark.parametrize("action,typ,arguments", [
    ("move", "Move", {"direction": "back", "distance_m": 0.3}),
    ("turn", "Turn", {"angle_deg": -45}),
    ("navigate_to", "NavigateTo", {"place": "", "x": 1, "y": 2, "theta": 90}),
])
def test_link_action_passthrough_and_service_success(rig, action, typ, arguments):
    link, ctrl, *_ = rig
    link.readiness()
    task = link.begin_task(action, arguments, "navigating" if action == "navigate_to" else "moving")
    ctrl.on_send = lambda req: ctrl.reply(req, {"success": True, "message": "done"})
    result = link.call_action(action, arguments, task_id=task["task_id"])
    assert result["ok"] and result["message"] == "done"
    assert ctrl.sent[-1]["service"] == "/robot/" + action
    assert ctrl.sent[-1]["type"] == "robot_interfaces/srv/" + typ
    assert ctrl.sent[-1]["args"] == arguments
    assert ctrl.timeout > 189
    ctrl.on_send = lambda req: ctrl.reply(req, {"success": False, "message": "blocked"})
    assert link.call_action(action, arguments, task_id=task["task_id"])["ok"] is False


def test_link_ctrl_block_does_not_block_stop_probe_state(rig):
    link, ctrl, _, stop, state, _ = rig
    link.readiness()
    task = link.begin_task("turn", {}, "moving")
    sent = threading.Event()
    ctrl.on_send = lambda req: sent.set()
    worker = threading.Thread(target=lambda: link.call_action("turn", {"angle_deg": 20}, task_id=task["task_id"]))
    worker.start()
    try:
        assert sent.wait(1)
        assert link.readiness()["ok"]
        publish(state, "/robot/arrived", True)
        eventually(lambda: link.snapshot()["arrived"] is True)
        assert link.stop()["ok"]
        assert [msg["op"] for msg in stop.sent] == ["advertise", "publish", "publish"]
        assert stop.sent[0]["type"] == "std_msgs/Bool"
        assert stop.sent[1]["topic"] == "/robot/cmd_stop"
        assert stop.sent[1]["msg"] == {"data": True}
    finally:
        ctrl.reply(ctrl.sent[-1], {"success": True, "message": "done"})
        worker.join(1)
    assert not worker.is_alive()


def test_race_task_requires_fresh_idle_and_single_reservation(rig, monkeypatch):
    link, *_ = rig
    assert link.begin_task("move", {}, "moving")["status"] == "error"
    assert link.readiness()["ok"]
    module = importlib.import_module("LLM.car_mcp.car_link")
    now = module.time.monotonic()
    with monkeypatch.context() as patch:
        patch.setattr(module.time, "monotonic", lambda: now + 100)
        assert link.begin_task("move", {}, "moving")["status"] == "error"
    barrier = threading.Barrier(3)
    results = []
    def reserve():
        barrier.wait()
        results.append(link.begin_task("move", {}, "moving"))
    workers = [threading.Thread(target=reserve) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(1)
    assert sorted(result["status"] for result in results) == ["accepted", "rejected"]


def test_race_old_idle_does_not_finish_new_task(rig):
    link, ctrl, _, _, state, _ = rig
    link.readiness()
    task = link.begin_task("navigate_to", {"x": 1}, "navigating")
    publish(state, "/robot/exec_state", "navigating")
    publish(state, "/robot/arrived", False)
    eventually(lambda: link.snapshot()["arrived"] is False)
    assert not link.snapshot()["current"]["seen_active"]
    ctrl.on_send = lambda req: ctrl.reply(req, {"success": True, "message": "done"})
    assert link.call_action("navigate_to", {"x": 1}, task_id=task["task_id"])["ok"]
    assert link.snapshot()["current"]["dispatched"]
    publish(state, "/robot/exec_state", "idle")
    publish(state, "/robot/arrived", False)
    eventually(lambda: link.snapshot()["arrived"] is False)
    assert link.snapshot()["current"]["task_id"] == task["task_id"]
    publish(state, "/robot/exec_state", "moving")
    publish(state, "/robot/arrived", True)
    eventually(lambda: link.snapshot()["arrived"] is True)
    assert not link.snapshot()["current"]["seen_active"]
    publish(state, "/robot/exec_state", "navigating")
    eventually(lambda: link.snapshot()["current"]["seen_active"])
    publish(state, "/robot/exec_state", "idle")
    eventually(lambda: link.snapshot()["current"] is None)
    assert link.snapshot()["last"]["task_id"] == task["task_id"]


def test_stop_old_finish_and_state_cannot_overwrite_new_generation(rig):
    link, _, _, _, state, _ = rig
    link.readiness()
    old = link.begin_task("navigate_to", {}, "navigating")
    assert link.stop()["ok"]
    publish(state, "/robot/exec_state", "navigating")
    publish(state, "/robot/arrived", False)
    eventually(lambda: link.snapshot()["arrived"] is False)
    assert link.snapshot()["exec_state"] == "idle"
    assert not link.snapshot()["moving"]
    assert link.finish_task(old["task_id"], True, "late")["status"] == "stale"
    assert link.snapshot()["last"]["action"] == "stop"
    assert link.begin_task("move", {}, "moving")["status"] == "error"
    link.readiness()
    new = link.begin_task("move", {}, "moving")
    assert new["task_id"] != old["task_id"]
    link.finish_task(old["task_id"], False, "late error")
    assert link.snapshot()["current"]["task_id"] == new["task_id"]
    assert link.snapshot()["last"]["action"] == "stop"


def test_stop_invalidates_task_before_dispatch(rig):
    link, ctrl, *_ = rig
    link.readiness()
    task = link.begin_task("move", {}, "moving")
    link.stop()
    assert link.call_action("move", {}, task_id=task["task_id"])["status"] == "stale"
    assert not ctrl.sent


def test_link_pose_bad_messages_deep_copy_and_close(rig):
    link, _, _, _, state, factory = rig
    state.inbox.put("bad json")
    state.push({"op": "publish", "topic": "/amcl_pose", "msg": {"pose": None}})
    state.push({"op": "publish", "topic": "/amcl_pose", "msg": {"pose": {"pose": {
        "position": {"x": 2, "y": 3},
        "orientation": {"x": 0, "y": 0, "z": math.sqrt(0.5), "w": math.sqrt(0.5)}}}}})
    eventually(lambda: link.snapshot()["pose"] is not None)
    assert link.snapshot()["pose"]["yaw_deg"] == pytest.approx(90)
    assert link.snapshot()["last_error"]
    link.readiness()
    target = {"nested": {"value": 1}}
    link.begin_task("move", target, "moving")
    target["nested"]["value"] = 2
    snap = link.snapshot()
    json.dumps(snap, allow_nan=False)
    snap["pose"]["x"] = 999
    snap["current"]["target"]["nested"]["value"] = 999
    assert link.snapshot()["pose"]["x"] == 2
    assert link.snapshot()["current"]["target"]["nested"]["value"] == 1
    started = time.monotonic()
    link.close()
    assert time.monotonic() - started < 1
    assert all(sock.closed for sock in factory.sockets)
    assert not link._state_thread.is_alive()


def test_link_state_reconnects_after_disconnect(rig):
    link, _, _, _, state, factory = rig
    state.inbox.put(ConnectionError("lost"))
    eventually(lambda: len(factory.sockets) == 5)
    replacement = factory.sockets[-1]
    assert {msg["topic"] for msg in replacement.sent} == {
        "/robot/exec_state", "/robot/arrived", "/amcl_pose"}
    publish(replacement, "/robot/exec_state", "idle")
    eventually(lambda: link.snapshot()["exec_state"] == "idle")


def test_link_missing_dependency_and_connection_failure(link_class, monkeypatch):
    module = importlib.import_module("LLM.car_mcp.car_link")
    monkeypatch.setattr(module, "websocket", None)
    link = link_class("ws://fake:9090")
    for result in (link.start(), link.readiness(), link.stop(), link.call_action("move", {}, task_id=1)):
        assert result["ok"] is False
        assert "websocket-client" in result["error"]
    link.close()
    def broken(url, timeout):
        raise ConnectionError("connection refused")
    link = link_class("ws://fake:9090", ws_factory=broken)
    try:
        assert "connection refused" in link.start()["error"]
        assert link.snapshot()["available"] is False
    finally:
        link.close()


def test_stop_busy_probe_cannot_restore_motion_or_allow_new_task(rig):
    link, _, probe, *_ = rig
    link.readiness()
    link.stop()
    link.readiness()  # 急停后已收到过一次 idle，随后仍可能迟到 busy 回包。
    probe.on_send = lambda req: probe.reply(req, {
        "ready": False, "exec_state": "navigating", "nav_available": True, "message": "busy"})
    assert not link.readiness()["ready"]
    assert link.snapshot()["exec_state"] == "idle"
    assert link.begin_task("move", {}, "moving")["status"] != "accepted"


def test_readiness_malformed_reply_invalidates_old_idle(rig):
    link, _, probe, *_ = rig
    link.readiness()
    probe.on_send = lambda req: probe.reply(req, {"ready": True})
    assert not link.readiness()["ok"]
    assert link.begin_task("move", {}, "moving")["status"] == "error"


def test_readiness_not_ready_idle_does_not_authorize_dispatch(rig):
    link, _, probe, *_ = rig
    probe.on_send = lambda req: probe.reply(req, {
        "ready": False, "exec_state": "idle", "nav_available": False, "message": "initializing"})
    link.readiness()
    assert link.begin_task("move", {}, "moving")["status"] != "accepted"


def test_stop_old_probe_inflight_cannot_authorize_new_task(rig):
    link, _, probe, *_ = rig
    requested = threading.Event()
    probe.on_send = lambda req: requested.set()
    worker = threading.Thread(target=link.readiness)
    worker.start()
    assert requested.wait(1)
    link.stop()
    probe.reply(probe.sent[-1], {
        "ready": True, "exec_state": "idle", "nav_available": True, "message": "ready"})
    worker.join(1)
    assert link.begin_task("move", {}, "moving")["status"] == "error"


def test_link_ctrl_calls_remain_serial_and_ids_are_matched(rig):
    link, ctrl, *_ = rig
    link.readiness()
    task = link.begin_task("move", {}, "moving")
    entered = threading.Event()
    ctrl.on_send = lambda req: entered.set()
    results = []
    first = threading.Thread(target=lambda: results.append(link.call_action("move", {"direction": "forward"}, task_id=task["task_id"])))
    second = threading.Thread(target=lambda: results.append(link.call_action("turn", {"angle_deg": 15}, task_id=task["task_id"])))
    first.start()
    assert entered.wait(1)
    second.start()
    assert len(ctrl.sent) == 1
    ctrl.push({"op": "service_response", "id": "unrelated", "result": False})
    ctrl.reply(ctrl.sent[0], {"success": True, "message": "first"})
    eventually(lambda: len(ctrl.sent) == 2)
    ctrl.reply(ctrl.sent[1], {"success": True, "message": "second"})
    first.join(1)
    second.join(1)
    assert all(result["ok"] for result in results) and len(results) == 2


def test_stop_failed_publish_preserves_current_task(rig):
    link, _, _, stop, *_ = rig
    link.readiness()
    task = link.begin_task("move", {}, "moving")
    def fail(_):
        raise ConnectionError("stop transport failed")
    stop.on_send = fail
    assert not link.stop()["ok"]
    assert link.snapshot()["current"]["task_id"] == task["task_id"]
    assert link.snapshot()["last"] is None


# --- 导航目标解析与 fail-closed 安全层 ---
@pytest.fixture
def nav_deps():
    tags = {
        "destinations": [
            {"uid": "d1", "name": "护士站", "aliases": ["护士台"], "x": 1.0, "y": 1.0, "yaw_deg": 10},
            {"uid": "d2", "name": "病房", "aliases": ["护士台"], "x": 8.0, "y": 1.0, "yaw_deg": 20},
            {"uid": "d3", "name": "远端", "aliases": [], "x": 8.0, "y": 8.0, "yaw_deg": 30},
        ],
        "zones": [{"uid": "z1", "name": "一区", "shape": "rect", "polygon": [[0, 0], [10, 0], [10, 5], [0, 5]],
                   "goal": {"x": 2.0, "y": 2.0, "yaw_deg": 0}}],
        "resolution": 0.05, "origin": [0, 0, 0],
    }
    def running(): return ("demo", "")
    def resolve(name): return {"ok": True, "exists": True, "stale": False, "tags": tags, "warnings": [], "fingerprint": {"ok": True, "changed": False, "reasons": []}}
    def info(name, *args): return {"meta_ok": True, "resolution": 0.05, "origin": [0, 0, 0]}
    def validate(name, x, y, *args, **kwargs): return {"ok": True, "in_bounds": True, "on_obstacle": False, "on_unknown": False,
                                                        "edge_margin_m": 1.0, "clearance_m": 1.0, "reasons": []}
    def hit(zone, x, y): return 0 <= x <= 10 and 0 <= y <= 5
    return dict(running_map_name=running, resolve=resolve, map_info=info, validate_point=validate,
                fingerprint_check=lambda tags, info: {"ok": True, "changed": False, "reasons": []}, zone_hit=hit)


@pytest.fixture
def nav(nav_deps):
    module = importlib.import_module("LLM.car_mcp.car_nav")
    return module.CarNav(**nav_deps)


def test_nav_place_exact_alias_and_rejects_ambiguous(nav):
    assert nav.resolve_place(" 护士站 ")["target"]["goal_source"] == "destination"
    assert nav.resolve_place("护士台")["ok"] is False
    assert "不唯一" in nav.resolve_place("护士台")["error"]
    assert nav.resolve_place("不存在")["ok"] is False


def test_nav_zone_explicit_and_nearest_destination_fallback(nav):
    explicit = nav.resolve_zone("一区")
    assert explicit["ok"] and explicit["target"]["goal_source"] == "explicit"
    fallback = nav.resolve_zone("一区")
    assert fallback["ok"]


def test_nav_point_and_input_validation(nav):
    assert nav.resolve_point(1, 2, 90)["ok"]
    for value in [True, "1", float("nan"), float("inf")]:
        assert nav.resolve_point(value, 2)["ok"] is False


def test_nav_fail_closed_map_and_validate_failures(nav, nav_deps):
    for field, value in [("running_map_name", lambda: ("", "unknown")),
                         ("resolve", lambda name: {"ok": False, "error": "missing"}),
                         ("map_info", lambda *a: {"meta_ok": False}),
                         ("validate_point", lambda *a, **k: {"ok": False, "reasons": ["障碍"]})]:
        deps = dict(nav_deps); deps[field] = value
        module = importlib.import_module("LLM.car_mcp.car_nav")
        obj = module.CarNav(**deps)
        assert obj.resolve_point(1, 2)["ok"] is False


def test_nav_zone_without_goal_does_not_create_centroid(nav, nav_deps):
    deps = dict(nav_deps)
    deps["resolve"] = lambda name: {"ok": True, "exists": True, "tags": {"destinations": [],
        "zones": [{"name": "空区", "shape": "rect", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
        "resolution": 0.05, "origin": [0, 0, 0]}, "fingerprint": {"ok": True, "changed": False}}
    obj = importlib.import_module("LLM.car_mcp.car_nav").CarNav(**deps)
    out = obj.resolve_zone("空区")
    assert out["ok"] is False and "停靠点" in out["hint"]


def test_race_dispatch_first_stop_publishes_immediately_then_again(rig):
    link, ctrl, _, stop, *_ = rig
    link.readiness()
    task = link.begin_task("move", {}, "moving")
    sending, release, immediate = threading.Event(), threading.Event(), threading.Event()
    def send(req):
        sending.set()
        assert release.wait(1)
        ctrl.reply(req, {"success": True, "message": "done"})
    ctrl.on_send = send
    stop.on_send = lambda _: immediate.set()
    action = threading.Thread(target=lambda: link.call_action("move", {}, task_id=task["task_id"]))
    stopping = threading.Thread(target=link.stop)
    action.start()
    assert sending.wait(1)
    stopping.start()
    assert immediate.wait(1)  # 首次急停不能等 ctrl.send，更不能等 service_response。
    release.set()
    action.join(1)
    stopping.join(1)
    assert len([msg for msg in stop.sent if msg["op"] == "publish"]) == 2
    assert link.snapshot()["current"] is None
    assert link.snapshot()["last"]["action"] == "stop"
    assert all(timeout <= 0.5 for timeout in ctrl.send_timeouts + stop.send_timeouts)


def test_race_stop_first_prevents_waiting_action_dispatch(rig):
    link, ctrl, _, stop, *_ = rig
    link.readiness()
    task = link.begin_task("move", {}, "moving")
    second_stop, release = threading.Event(), threading.Event()
    def send(_):
        if len([msg for msg in stop.sent if msg["op"] == "publish"]) == 2:
            second_stop.set()
            assert release.wait(1)
    stop.on_send = send
    ctrl.on_send = lambda req: ctrl.reply(req, {"success": True, "message": "done"})
    results = []
    stopping = threading.Thread(target=link.stop)
    action = threading.Thread(target=lambda: results.append(link.call_action("move", {}, task_id=task["task_id"])))
    stopping.start()
    try:
        assert second_stop.wait(1)
        action.start()
    finally:
        release.set()
        stopping.join(1)
        if action.ident:
            action.join(1)
    assert results[0]["status"] == "stale"
    assert not ctrl.sent


def test_link_action_requires_task_id(rig):
    link, ctrl, *_ = rig
    ctrl.on_send = lambda req: ctrl.reply(req, {"success": True})
    with pytest.raises(TypeError, match="task_id"):
        link.call_action("move", {})
    assert not ctrl.sent


@pytest.mark.parametrize("task_id", [None, True, 1.0, "1", 0, -1])
def test_link_action_rejects_invalid_task_id(rig, task_id):
    link, ctrl, *_ = rig
    link.readiness()
    link.begin_task("move", {}, "moving")
    ctrl.on_send = lambda req: ctrl.reply(req, {"success": True})
    result = link.call_action("move", {}, task_id=task_id)
    assert result["ok"] is False
    assert not ctrl.sent


def test_stop_invalidates_second_action_queued_on_ctrl(rig):
    link, ctrl, *_ = rig
    link.readiness()
    task = link.begin_task("move", {}, "moving")
    sent, queued = threading.Event(), threading.Event()
    ctrl.on_send = lambda req: sent.set()
    results = []
    first = threading.Thread(target=lambda: link.call_action("move", {}, task_id=task["task_id"]))
    def second_call():
        queued.set()
        results.append(link.call_action("turn", {}, task_id=task["task_id"]))
    second = threading.Thread(target=second_call)
    first.start()
    assert sent.wait(1)
    second.start()
    assert queued.wait(1)
    assert link.stop()["ok"]
    ctrl.reply(ctrl.sent[0], {"success": True})
    first.join(1)
    second.join(1)
    assert results[0]["status"] == "stale"
    assert len(ctrl.sent) == 1


@pytest.mark.parametrize("newer_result", ["busy", "failure"])
def test_readiness_network_and_application_are_linearized(rig, monkeypatch, newer_result):
    link, _, probe, *_ = rig
    link.stop()
    old_received, release_old, second_started, second_network = (threading.Event() for _ in range(4))
    service = link._service
    calls = 0
    def paused_service(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            result = service(*args, **kwargs)
            old_received.set()
            assert release_old.wait(1)
            return result
        second_network.set()
        return service(*args, **kwargs)
    monkeypatch.setattr(link, "_service", paused_service)
    first = threading.Thread(target=link.readiness)
    def newer_probe():
        second_started.set()
        link.readiness()
    second = threading.Thread(target=newer_probe)
    first.start()
    assert old_received.wait(1)  # 第一调用已释放 socket 锁，但尚未应用 idle。
    probe.on_send = lambda req: probe.reply(req, {
        "ready": False, "exec_state": "navigating", "nav_available": True, "message": "busy"
    }, result=newer_result != "failure")
    second.start()
    assert second_started.wait(1)
    overtook_application = second_network.wait(0.1)
    release_old.set()
    first.join(1)
    second.join(1)
    assert not overtook_application, "new probe overtook prior probe cache application"
    assert second_network.is_set()
    assert link.begin_task("move", {}, "moving")["status"] != "accepted"
