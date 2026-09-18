# -*- coding: utf-8 -*-
r"""车控连接层测试：仅替换 websocket 边界，不访问板卡。"""
import importlib
import importlib.util
import asyncio
import json
import math
import queue
import threading
import time
import runpy
from pathlib import Path
from unittest.mock import mock_open
from concurrent.futures import Future

import pytest


class DeferredExecutor:
    """Tests can inspect an accepted task before running its worker."""
    def __init__(self):
        self.jobs = []
        self.closed = False

    def submit(self, fn, *args, **kwargs):
        future = Future()
        self.jobs.append((fn, args, kwargs, future))
        return future

    def run_next(self):
        fn, args, kwargs, future = self.jobs.pop(0)
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)

    def shutdown(self, wait=True):
        self.closed = True


class ServerLink:
    def __init__(self, *, ready=True, nav_available=True):
        self.ready = ready
        self.nav_available = nav_available
        self.tasks = []
        self.calls = []
        self.finished = []
        self.stopped = 0
        self.closed = False
        self.fail_action = None

    def readiness(self):
        return {"ok": True, "ready": self.ready, "exec_state": "idle",
                "nav_available": self.nav_available, "message": "ready"}

    def begin_task(self, action, target, expected_state):
        if self.tasks:
            return {"ok": False, "status": "rejected", "error": "已有执行中的任务"}
        task = {"ok": True, "task_id": len(self.finished) + 1, "action": action,
                "target": target, "expected_state": expected_state}
        self.tasks.append(task)
        return task

    def call_action(self, action, request, *, task_id):
        self.calls.append((action, request, task_id))
        if self.fail_action:
            raise self.fail_action
        return {"ok": True, "message": "accepted"}

    def finish_task(self, task_id, ok, detail):
        self.tasks.clear()
        self.finished.append((task_id, ok, detail))
        return {"ok": True}

    def stop(self):
        self.stopped += 1
        return {"ok": True, "message": "stopped"}

    def snapshot(self):
        return {"ok": True, "exec_state": "idle", "last": self.finished[-1] if self.finished else None}

    def close(self):
        self.closed = True


class ServerNav:
    def __init__(self):
        self.calls = []

    def _out(self, kind, *args):
        self.calls.append((kind, args))
        return {"ok": True, "target": {"x": 1.0, "y": 2.0, "yaw_deg": 30.0, "goal_source": kind},
                "warnings": ["地图标记较旧"], "summary_target": kind}

    def resolve_point(self, x, y, yaw_deg=0): return self._out("point", x, y, yaw_deg)
    def resolve_zone(self, zone): return self._out("zone", zone)
    def resolve_place(self, place): return self._out("place", place)


@pytest.fixture
def car_server():
    return importlib.import_module("LLM.car_mcp.car_server")


@pytest.fixture
def server_rig(car_server):
    link, nav, executor = ServerLink(), ServerNav(), DeferredExecutor()
    return car_server, link, nav, executor


def load_json(text):
    assert isinstance(text, str)
    return json.loads(text)


def test_server_registers_seven_json_text_tools(server_rig):
    module, link, nav, executor = server_rig
    server = module.build_server(link=link, nav=nav, executor=executor)
    assert {tool.name for tool in asyncio.run(server.list_tools())} == {
        "robot_move", "robot_turn", "robot_goto_point", "robot_goto_zone",
        "robot_goto_place", "robot_stop", "robot_status"}
    assert load_json(module._status(link))["ok"] is True
    assert load_json(module._stop(link))["ok"] is True


@pytest.mark.parametrize("direction,distance", [
    (True, 1), ("up", 1), ("forward", True), ("forward", "1"),
    ("forward", float("nan")), ("forward", float("inf")), ("forward", 0), ("forward", 5.1),
])
def test_move_rejects_unsafe_parameters_without_registering_task(server_rig, direction, distance):
    module, link, nav, executor = server_rig
    out = load_json(module._move(link, executor, direction, distance))
    assert out["ok"] is False
    assert not link.tasks and not executor.jobs


@pytest.mark.parametrize("angle", [True, "90", float("nan"), float("inf"), 0, 361, -361])
def test_turn_rejects_unsafe_parameters_without_registering_task(server_rig, angle):
    module, link, nav, executor = server_rig
    out = load_json(module._turn(link, executor, angle))
    assert out["ok"] is False
    assert not link.tasks and not executor.jobs


def test_readiness_failure_does_not_register_task(server_rig):
    module, link, nav, executor = server_rig
    link.ready = False
    out = load_json(module._move(link, executor, "forward", 1))
    assert out["ok"] is False and "就绪" in out["error"]
    assert not link.tasks and not executor.jobs


def test_goto_requires_navigation_before_task_registration(server_rig):
    module, link, nav, executor = server_rig
    link.nav_available = False
    out = load_json(module._goto_point(link, nav, executor, 1, 2))
    assert out["ok"] is False and "导航" in out["error"]
    assert not link.tasks and not executor.jobs


def test_started_returns_immediately_and_worker_sends_exact_ros_request(server_rig):
    module, link, nav, executor = server_rig
    out = load_json(module._goto_point(link, nav, executor, 4, 5, 90))
    assert out == {"ok": True, "status": "started", "action": "goto_point",
                   "summary": "前往 point", "exec_state": "navigating",
                   "target": {"x": 1.0, "y": 2.0, "yaw_deg": 30.0, "goal_source": "point"},
                   "warnings": ["地图标记较旧"]}
    assert not link.calls
    executor.run_next()
    assert link.calls == [("navigate_to", {"place": "", "x": 1.0, "y": 2.0, "theta": 30.0}, 1)]
    assert link.finished[-1][1] is True


def test_worker_failure_is_recorded_after_started_response(server_rig):
    module, link, nav, executor = server_rig
    link.fail_action = RuntimeError("service offline")
    assert load_json(module._move(link, executor, "forward", 1))["status"] == "started"
    executor.run_next()
    assert link.finished[-1][1] is False
    assert "service offline" in str(link.finished[-1][2])


def test_busy_rejection_stop_bypass_status_degradation_and_lifecycle(server_rig):
    module, link, nav, executor = server_rig
    link.tasks.append({"task_id": 99})
    assert load_json(module._move(link, executor, "forward", 1))["ok"] is False
    assert load_json(module._stop(link))["ok"] is True and link.stopped == 1
    link.snapshot = lambda: (_ for _ in ()).throw(RuntimeError("connection unavailable"))
    status = load_json(module._status(link))
    assert status == {"ok": True, "status": "unavailable", "reason": "connection unavailable"}
    module._close_resources(link, executor)
    assert link.closed and executor.closed


def test_server_import_has_no_controller_or_rclpy_and_optional_mcp_degrades(monkeypatch, server_rig):
    module, link, nav, executor = server_rig
    assert "car_controller" not in module.__dict__.get("__doc__", "")
    monkeypatch.setattr(module, "MCPServer", None)
    assert module.build_server(link=link, nav=nav, executor=executor) is None


@pytest.mark.parametrize("name,args", [
    ("robot_move", {"direction": "forward", "distance_m": True}),
    ("robot_move", {"direction": "forward", "distance_m": "1"}),
    ("robot_turn", {"angle_deg": True}),
    ("robot_turn", {"angle_deg": "90"}),
])
def test_mcp_numeric_inputs_rejected_before_sdk_coercion(server_rig, name, args):
    module, link, nav, executor = server_rig
    server = module.build_server(link=link, nav=nav, executor=executor)
    response = asyncio.run(server.call_tool(name, args))
    assert load_json(response.content[0].text)["ok"] is False
    assert not link.tasks and not executor.jobs


@pytest.mark.parametrize("snapshot", [
    {"ok": True, "available": False, "last_error": "offline"},
    {"ok": True, "available": True, "state_fresh": False, "exec_state": "unknown"},
])
def test_status_unavailable_keeps_snapshot_details(server_rig, snapshot):
    module, link, nav, executor = server_rig
    link.snapshot = lambda: snapshot
    result = load_json(module._status(link))
    assert result["ok"] is True and result["status"] == "unavailable"
    assert result["reason"]
    assert result["available"] == snapshot["available"]


def test_main_keeps_tools_when_link_unavailable_and_closes(monkeypatch, server_rig):
    module, link, nav, executor = server_rig
    def no_startup_probe():
        pytest.fail("main must not probe network at startup")
    link.start = no_startup_probe
    monkeypatch.setattr(module, "CarLink", lambda: link)
    monkeypatch.setattr(module, "CarNav", lambda: nav)
    monkeypatch.setattr(module, "ThreadPoolExecutor", lambda **kw: executor)
    monkeypatch.setattr(module, "_log", lambda msg: None)
    calls = []
    class Server:
        def run(self, **kw): calls.append(kw)
    monkeypatch.setattr(module, "build_server", lambda **kw: Server())
    module.main()
    assert calls == [{"transport": "stdio"}]
    assert link.closed and executor.closed


def test_script_entry_imports_from_any_working_directory(monkeypatch, car_server):
    monkeypatch.chdir(Path(car_server.__file__).resolve().parents[2] / "docs")
    namespace = runpy.run_path(car_server.__file__, run_name="car_entry_test")
    assert namespace["CarLink"].__module__ == "LLM.car_mcp.car_link"


@pytest.mark.parametrize("name,args,action,ros_request", [
    ("robot_move", {"direction": "back", "distance_m": 5}, "move_back", {"direction": "back", "distance_m": 5.0}),
    ("robot_turn", {"angle_deg": -360}, "turn", {"angle_deg": -360.0}),
    ("robot_goto_zone", {"zone": "ward"}, "goto_zone", {"place": "", "x": 1.0, "y": 2.0, "theta": 30.0}),
    ("robot_goto_place", {"place": "desk"}, "goto_place", {"place": "", "x": 1.0, "y": 2.0, "theta": 30.0}),
])
def test_mcp_actions_return_json_and_preserve_ros_fields(server_rig, name, args, action, ros_request):
    module, link, nav, executor = server_rig
    server = module.build_server(link=link, nav=nav, executor=executor)
    response = asyncio.run(server.call_tool(name, args))
    accepted = load_json(response.content[0].text)
    assert accepted["status"] == "started" and accepted["action"] == action
    executor.run_next()
    transport_action = "navigate_to" if action.startswith("goto_") else name.removeprefix("robot_")
    assert link.calls == [(transport_action, ros_request, 1)]


def test_executor_rejection_releases_task_and_returns_error(server_rig):
    module, link, nav, executor = server_rig
    executor.submit = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("shutdown"))
    result = load_json(module._move(link, executor, "forward", 1))
    assert result["ok"] is False and result["status"] == "error"
    assert not link.tasks and link.finished[-1][1] is False


def test_python_mcp_missing_main_exits_two_without_stdout(monkeypatch, capsys, car_server):
    monkeypatch.setattr(car_server, "MCPServer", None)
    opened = mock_open()
    monkeypatch.setattr("builtins.open", opened)
    with pytest.raises(SystemExit) as exc:
        car_server.main()
    assert exc.value.code == 2
    streams = capsys.readouterr()
    assert not streams.out and "python-mcp" in streams.err
    opened().write.assert_called_once()


def test_mcp_stop_remains_responsive_while_readiness_waits(server_rig):
    module, link, nav, executor = server_rig
    original = link.readiness
    waiting, release = threading.Event(), threading.Event()
    def blocked():
        waiting.set()
        release.wait(1)
        return original()
    link.readiness = blocked
    server = module.build_server(link=link, nav=nav, executor=executor)
    async def calls():
        action = asyncio.create_task(server.call_tool("robot_move", {"direction": "forward", "distance_m": 1}))
        try:
            assert await asyncio.to_thread(waiting.wait, 1)
            stop = await server.call_tool("robot_stop", {})
            assert load_json(stop.content[0].text)["ok"]
            assert not action.done(), "MCP readiness blocked the event loop and delayed stop"
        finally:
            release.set()
            await action
    asyncio.run(calls())


def test_state_connections_start_once_on_first_use(server_rig):
    module, link, nav, executor = server_rig
    starts = []
    link.start = lambda: starts.append(True) or {"ok": True}
    assert load_json(module._stop(link))["ok"]
    assert not starts
    assert load_json(module._status(link))["ok"]
    assert load_json(module._move(link, executor, "forward", 1))["status"] == "started"
    assert load_json(module._status(link))["ok"]
    assert starts == [True]


@pytest.mark.parametrize("success", [True, False])
def test_real_link_worker_results_reach_status(rig, car_server, success):
    link, ctrl, *_ = rig
    executor = DeferredExecutor()
    ctrl.on_send = lambda req: ctrl.reply(req, {"success": success, "message": "done" if success else "blocked"})
    assert load_json(car_server._turn(link, executor, 30))["status"] == "started"
    executor.run_next()
    result = load_json(car_server._status(link))
    assert result["last"]["ok"] is success and result["current"] is None
    if not success:
        assert result["last_error"] == "blocked"


def test_missing_websocket_still_registers_and_returns_json(monkeypatch, car_server):
    link_module = importlib.import_module("LLM.car_mcp.car_link")
    monkeypatch.setattr(link_module, "websocket", None)
    link = link_module.CarLink("ws://unavailable:9090")
    executor = DeferredExecutor()
    server = car_server.build_server(link=link, nav=ServerNav(), executor=executor)
    try:
        assert len(asyncio.run(server.list_tools())) == 7
        result = asyncio.run(server.call_tool("robot_move", {"direction": "forward", "distance_m": 1}))
        assert "websocket-client" in load_json(result.content[0].text)["error"]
        assert load_json(car_server._status(link))["status"] == "unavailable"
        assert not executor.jobs
    finally:
        car_server._close_resources(link, executor)


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

def test_nav_zone_fallback_validates_yaw_and_zone_exceptions(nav_deps):
    deps = dict(nav_deps)
    deps["resolve"] = lambda name: {"ok": True, "exists": True, "tags": {
        "destinations": [{"name": "坏yaw", "x": 1, "y": 1, "yaw_deg": float("nan")}],
        "zones": [{"name": "无goal", "polygon": [[0,0],[2,0],[2,2],[0,2]]}]},
        "fingerprint": {"ok": True, "changed": False}}
    obj = importlib.import_module("LLM.car_mcp.car_nav").CarNav(**deps)
    assert obj.resolve_zone("无goal")["ok"] is False
    deps["zone_hit"] = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    assert importlib.import_module("LLM.car_mcp.car_nav").CarNav(**deps).resolve_zone("无goal")["ok"] is False

@pytest.mark.parametrize("field,value", [
    ("in_bounds", False), ("on_obstacle", True), ("on_unknown", True),
    ("edge_margin_m", 0.2), ("clearance_m", 0.2)])
def test_nav_point_each_validate_failure(nav, nav_deps, field, value):
    deps = dict(nav_deps)
    deps["validate_point"] = lambda *a, **k: {"ok": True, "in_bounds": True, "on_obstacle": False,
        "on_unknown": False, "edge_margin_m": 1, "clearance_m": 1, "reasons": [field], field: value}
    assert importlib.import_module("LLM.car_mcp.car_nav").CarNav(**deps).resolve_point(1, 2)["ok"] is False

def test_nav_old_tags_fingerprint_warning_and_changed_rejection(nav_deps):
    deps = dict(nav_deps)
    deps["resolve"] = lambda name: {"ok": True, "exists": True, "tags": {"destinations": [], "zones": []}, "fingerprint": {"ok": True, "changed": False}}
    deps["fingerprint_check"] = lambda tags, info: {"ok": True, "changed": False, "reasons": ["该 tags.json 无指纹"]}
    obj = importlib.import_module("LLM.car_mcp.car_nav").CarNav(**deps)
    out = obj.resolve_point(1, 2)
    assert out["ok"] and any("无指纹" in w for w in out["warnings"])
    deps["fingerprint_check"] = lambda tags, info: {"ok": False, "changed": False, "reasons": ["yaml 元数据不可用"]}
    assert importlib.import_module("LLM.car_mcp.car_nav").CarNav(**deps).resolve_point(1, 2)["ok"] is False


def test_nav_zone_fallback_selects_nearest_destination_and_reports_it(nav_deps):
    deps = dict(nav_deps)
    deps["resolve"] = lambda name: {"ok": True, "exists": True, "tags": {
        "destinations": [{"name": "远", "x": 0.5, "y": 0.5, "yaw_deg": 0},
                         {"name": "近", "x": 2.7, "y": 2.7, "yaw_deg": 90}],
        "zones": [{"name": "无goal", "shape": "rect", "polygon": [[0, 0], [5, 0], [5, 5], [0, 5]]}],
        "resolution": .05, "origin": [0, 0, 0]}}
    out = importlib.import_module("LLM.car_mcp.car_nav").CarNav(**deps).resolve_zone("无goal")
    assert out["ok"] and out["target"] == {"x": 2.7, "y": 2.7, "yaw_deg": 90.0, "goal_source": "destination"}
    assert any("近" in warning for warning in out["warnings"])


def test_nav_zone_rejects_explicit_or_fallback_target_outside_zone(nav_deps):
    deps = dict(nav_deps)
    deps["resolve"] = lambda name: {"ok": True, "exists": True, "tags": {
        "destinations": [], "zones": [{"name": "显式外", "polygon": [[0,0],[1,0],[1,1],[0,1]],
            "goal": {"x": 2, "y": 2, "yaw_deg": 0}}], "resolution": .05, "origin": [0,0,0]}}
    obj = importlib.import_module("LLM.car_mcp.car_nav").CarNav(**deps)
    obj.zone_hit = lambda *_: False
    assert obj.resolve_zone("显式外")["ok"] is False
    deps["resolve"] = lambda name: {"ok": True, "exists": True, "tags": {
        "destinations": [{"name": "区外", "x": 2, "y": 2, "yaw_deg": 0}],
        "zones": [{"name": "回退外", "polygon": [[0,0],[1,0],[1,1],[0,1]]}], "resolution": .05, "origin": [0,0,0]}}
    obj = importlib.import_module("LLM.car_mcp.car_nav").CarNav(**deps)
    obj.zone_hit = lambda *_: False
    assert obj.resolve_zone("回退外")["ok"] is False


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
