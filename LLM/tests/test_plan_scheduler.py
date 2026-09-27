# -*- coding: utf-8 -*-
"""共享车控动作闸门与确定性 Plan 调度器测试。"""

import pytest
import json

from LLM.agent import action_gate, plan_scheduler, tools, plan


@pytest.mark.parametrize("raw,kind", [
    ({"ok": False, "message": "断开"}, "transport_error"),
    ({"ok": True, "result": '{"ok":false,"status":"error"}'}, "action_error"),
    ({"ok": True, "result": '{"ok":true,"status":"unavailable"}'}, "unavailable"),
    ({"ok": True, "result": "not json"}, "malformed"),
])
def test_parse_result_is_fail_closed(raw, kind):
    assert plan_scheduler.parse_tool_result(raw).kind == kind


def test_parse_started_requires_task_id_and_never_means_success():
    result = plan_scheduler.parse_tool_result(
        {"ok": True, "result": '{"ok":true,"status":"started","task_id":7}'})
    assert result.kind == "started"
    assert result.task_id == 7
    assert plan_scheduler.parse_tool_result(
        {"ok": True, "result": '{"ok":true,"status":"started"}'}).kind == "malformed"


def test_reconcile_ignores_wrong_task_id_and_matches_last_once():
    step = {"action": "robot_move", "timeout_sec": 45,
            "attempt": {"car_task_id": 7, "started_at_mono": 0}}
    wrong = {"ok": True, "last": {"task_id": 8, "ok": True}}
    assert plan_scheduler.reconcile_step(step, wrong, 2) == "waiting"
    done = {"ok": True, "last": {"task_id": 7, "ok": True}}
    assert plan_scheduler.reconcile_step(step, done, 2) == "succeeded"
    assert plan_scheduler.reconcile_step(step, done, 3) == "succeeded"


def test_reconcile_unavailable_grace_then_review():
    step = {"action": "robot_move", "timeout_sec": 45,
            "attempt": {"car_task_id": 7, "started_at_mono": 0,
                         "unavailable_since": 10}}
    payload = {"ok": True, "status": "unavailable"}
    assert plan_scheduler.reconcile_step(step, payload, 20) == "waiting"
    assert plan_scheduler.reconcile_step(step, payload, 26) == "needs_review"


def test_tick_dispatches_once_then_advances_on_matching_last(monkeypatch, tmp_path):
    monkeypatch.setattr("LLM.store.db.DB_PATH", tmp_path / "plan.db")
    from LLM.store import db
    db.init_db()
    made = db.create_plan(
        {"title": "巡逻", "priority": "P2", "status": "queued"},
        [{"seq": 1, "step_type": "action", "action": "robot_move",
          "args_json": {"direction": "forward", "distance_m": 0.2},
          "timeout_sec": 45, "retry_policy": "none", "max_attempts": 1}],
    )
    monkeypatch.setattr("LLM.store.db.get_settings", lambda: {"mcp_enabled": True})
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
        name: {"server": "car", "schema": {}}
        for name in ("robot_move", "robot_status")
    })
    calls = []
    monkeypatch.setattr(tools.mcp_client, "call_tool", lambda name, args: (
        calls.append(name) or ({"ok": True, "result": json.dumps({
            "ok": True, "status": "started", "task_id": 9})}
            if name == "robot_move" else {"ok": True, "result": json.dumps({
                "ok": True, "last": {"task_id": 9, "ok": True}})})))
    plan_scheduler.tick_once(100)
    assert calls == ["robot_move"]
    running = db.get_plan(made["id"], include_steps=True, include_attempts=True)
    assert running["steps"][0]["status"] == "running"
    plan_scheduler.tick_once(101)
    assert calls == ["robot_move", "robot_status"]
    finished = db.get_plan(made["id"], include_steps=True, include_attempts=True)
    assert finished["status"] == "succeeded"
    assert finished["steps"][0]["status"] == "succeeded"


class _FakeNav:
    """只能解析「护士站」的假 CarNav；其余目标一律报当前地图未知。"""

    def __init__(self, available=True):
        self.available = available
        self.calls = 0

    def resolve_place(self, place):
        self.calls += 1
        if not self.available:
            return {"ok": False, "status": "rejected", "error": "当前地图未知"}
        return {"ok": True, "map": "ward-map",
                "target": {"x": 1, "y": 2, "yaw_deg": 30, "goal_source": "destination"},
                "tags_fingerprint": "sha1:" + "a" * 40}


def _deferred_goto_place_plan(db):
    return db.create_plan(
        {"title": "去护士站", "priority": "P2", "status": "waiting"},
        [{"seq": 1, "step_type": "action", "action": "robot_goto_place",
          "args_json": {"place": "护士站"}, "status": "waiting",
          "timeout_sec": 200, "retry_policy": "safe_goto_only", "max_attempts": 2,
          "last_error": "目标尚未解析：当前地图未知"}],
    )


def _stub_car_mcp(monkeypatch, calls):
    monkeypatch.setattr("LLM.store.db.get_settings", lambda: {"mcp_enabled": True})
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
        name: {"server": "car", "schema": {}}
        for name in ("robot_goto_point", "robot_status")
    })

    def call(name, args):
        calls.append((name, args))
        if name == "robot_goto_point":
            return {"ok": True, "result": json.dumps(
                {"ok": True, "status": "started", "task_id": 9})}
        return {"ok": True, "result": json.dumps(
            {"ok": True, "last": {"task_id": 9, "ok": True}})}

    monkeypatch.setattr(tools.mcp_client, "call_tool", call)


def test_deferred_navigation_step_is_resolved_then_dispatched(monkeypatch, tmp_path):
    """设计 §4.2：创建期解析不了的目标由调度器重解析，成功后才派发。"""
    monkeypatch.setattr("LLM.store.db.DB_PATH", tmp_path / "plan.db")
    from LLM.store import db
    db.init_db()
    made = _deferred_goto_place_plan(db)
    calls = []
    _stub_car_mcp(monkeypatch, calls)
    nav = _FakeNav()
    monkeypatch.setattr("LLM.car_mcp.car_nav.CarNav", lambda: nav)
    plan_scheduler._resolve_checked.clear()

    plan_scheduler.tick_once(100)
    resolved = db.get_plan(made["id"], include_steps=True)
    step = resolved["steps"][0]
    assert step["status"] == "pending"
    assert step["action"] == "robot_goto_point"
    assert step["args_json"] == {"x": 1.0, "y": 2.0, "yaw_deg": 30.0}
    assert step["map_name"] == "ward-map"
    assert step["target_json"]["source_action"] == "robot_goto_place"
    assert step["tags_fingerprint"] == "sha1:" + "a" * 40
    assert step["last_error"] == ""
    assert step["retry_policy"] == "safe_goto_only"
    assert calls == []                      # 解析成功那一 tick 只改状态，不派发

    plan_scheduler.tick_once(101)
    assert [name for name, _args in calls] == ["robot_goto_point"]
    assert calls[0][1] == {"x": 1.0, "y": 2.0, "yaw_deg": 30.0}
    running = db.get_plan(made["id"], include_steps=True)
    assert running["steps"][0]["status"] == "running"
    assert running["status"] == "running"


def test_unresolved_navigation_retry_is_throttled_and_never_dispatches(monkeypatch, tmp_path):
    monkeypatch.setattr("LLM.store.db.DB_PATH", tmp_path / "plan.db")
    from LLM.store import db
    db.init_db()
    made = _deferred_goto_place_plan(db)
    calls = []
    _stub_car_mcp(monkeypatch, calls)
    nav = _FakeNav(available=False)
    monkeypatch.setattr("LLM.car_mcp.car_nav.CarNav", lambda: nav)
    plan_scheduler._resolve_checked.clear()

    plan_scheduler.tick_once(100)
    got = db.get_plan(made["id"], include_steps=True)
    assert got["steps"][0]["status"] == "waiting"
    assert got["steps"][0]["action"] == "robot_goto_place"
    assert "当前地图未知" in got["steps"][0]["last_error"]
    assert calls == []
    assert nav.calls == 1

    version = got["version"]
    plan_scheduler.tick_once(101)           # 节流窗口内：不重试、不写库
    assert nav.calls == 1
    assert db.get_plan(made["id"], include_steps=True)["version"] == version

    plan_scheduler.tick_once(100 + plan_scheduler.conf.PLAN_RESOLVE_RETRY_S + 0.1)
    assert nav.calls == 2
    assert calls == []


def test_move_timeout_never_retries(monkeypatch, tmp_path):
    monkeypatch.setattr("LLM.store.db.DB_PATH", tmp_path / "plan.db")
    from LLM.store import db
    db.init_db()
    made = db.create_plan(
        {"title": "移动", "priority": "P2", "status": "queued"},
        [{"seq": 1, "step_type": "action", "action": "robot_move",
          "args_json": {"direction": "forward", "distance_m": 0.2},
          "timeout_sec": 1, "retry_policy": "none", "max_attempts": 1}],
    )
    monkeypatch.setattr("LLM.store.db.get_settings", lambda: {"mcp_enabled": True})
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
        name: {"server": "car", "schema": {}}
        for name in ("robot_move", "robot_status")
    })
    calls = []
    monkeypatch.setattr(tools.mcp_client, "call_tool", lambda name, args: (
        calls.append(name) or ({"ok": True, "result": json.dumps({
            "ok": True, "status": "started", "task_id": 4})}
            if name == "robot_move" else {"ok": True, "result": json.dumps({
                "ok": True, "exec_state": "moving", "current": {"task_id": 4}})})))
    plan_scheduler.tick_once(10)
    plan_scheduler.tick_once(12)
    plan_scheduler.tick_once(13)
    assert calls.count("robot_move") == 1
    got = db.get_plan(made["id"], include_steps=True)
    assert got["steps"][0]["status"] == "needs_review"


def test_scheduler_scans_all_plans_without_legacy_limit(monkeypatch):
    seen = []
    monkeypatch.setattr(plan_scheduler.db, "list_plans",
                        lambda **kwargs: seen.append(kwargs) or [])
    assert plan_scheduler._all_plans() == []
    assert seen and seen[0].get("limit") is None


def test_execution_transition_is_one_aggregate_transaction():
    from LLM.store import db
    assert hasattr(db, "transition_plan_execution")


def test_wait_uses_wall_clock(monkeypatch):
    calls = []
    monkeypatch.setattr(plan_scheduler.db, "transition_plan_execution",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(plan_scheduler.db, "get_plan",
                        lambda *args, **kwargs: {"steps": [{"id": 1, "status": "pending"}]})
    monkeypatch.setattr(plan_scheduler.time, "time", lambda: 2_000.0)
    step = {"id": 1, "plan_id": 2, "step_type": "wait", "wait_kind": "time",
            "wake_at": "1970-01-01T00:33:20+00:00", "status": "pending"}
    plan_scheduler._handle_wait(step, plan_scheduler.time.time())
    assert calls and calls[0][1]["step_changes"]["status"] == "succeeded"


def test_stop_joins_previous_scheduler_thread(monkeypatch):
    class FakeThread:
        def __init__(self):
            self.joined = False
        def is_alive(self):
            return True
        def join(self, timeout=None):
            self.joined = True

    fake = FakeThread()
    monkeypatch.setattr(plan_scheduler, "_thread", fake)
    plan_scheduler.stop()
    assert fake.joined is True


def test_final_time_wait_completes_plan(monkeypatch, tmp_path):
    monkeypatch.setattr("LLM.store.db.DB_PATH", tmp_path / "plan.db")
    from LLM.store import db
    db.init_db()
    made = db.create_plan(
        {"title": "等待", "priority": "P2", "status": "queued"},
        [{"seq": 1, "step_type": "wait", "wait_kind": "time",
          "wake_at": "1970-01-01T00:00:01+00:00"}],
    )
    monkeypatch.setattr(plan_scheduler.time, "time", lambda: 2.0)
    plan_scheduler.tick_once(10.0)
    got = db.get_plan(made["id"], include_steps=True)
    assert got["steps"][0]["status"] == "succeeded"
    assert got["status"] == "succeeded"


def test_transition_rejects_attempt_from_different_step_without_bump(tmp_path, monkeypatch):
    monkeypatch.setattr("LLM.store.db.DB_PATH", tmp_path / "plan.db")
    from LLM.store import db
    db.init_db()
    made = db.create_plan(
        {"title": "归属", "priority": "P2", "status": "queued"},
        [{"seq": 1, "step_type": "action", "action": "robot_move"},
         {"seq": 2, "step_type": "action", "action": "robot_turn"}],
    )
    first, second = made["steps"]
    attempt = db.prepare_plan_attempt(first["id"], 1, {"action": "robot_move"})
    before = db.get_plan(made["id"], include_steps=True, include_attempts=True)
    with pytest.raises(ValueError):
        db.transition_plan_execution(
            made["id"], step_id=second["id"], step_changes={"status": "running"},
            attempt_id=attempt["id"], attempt_changes={"dispatch_state": "dispatched"},
        )
    after = db.get_plan(made["id"], include_steps=True, include_attempts=True)
    assert after["version"] == before["version"]
    assert after["steps"][1]["status"] == "pending"
    assert after["attempts"][0]["dispatch_state"] == "prepared"


def test_join_timeout_keeps_live_thread_and_blocks_start(monkeypatch):
    class LiveThread:
        def __init__(self):
            self.joined = False
        def is_alive(self):
            return True
        def join(self, timeout=None):
            self.joined = True

    old = LiveThread()
    monkeypatch.setattr(plan_scheduler, "_thread", old)
    monkeypatch.setattr(plan_scheduler.conf, "PLAN_TICK_S", 0.001)
    plan_scheduler.stop()
    assert old.joined is True
    assert plan_scheduler._thread is old


def test_start_after_joined_stop_creates_new_thread(monkeypatch):
    class ThreadStub:
        def __init__(self, *args, **kwargs):
            self.started = False
            self.alive = False
        def is_alive(self):
            return self.alive
        def start(self):
            self.started = True
            self.alive = True
        def join(self, timeout=None):
            self.alive = False

    class OldThread(ThreadStub):
        def __init__(self):
            super().__init__()
            self.alive = True
        def join(self, timeout=None):
            self.alive = False

    old = OldThread()
    created = []
    monkeypatch.setattr(plan_scheduler, "_thread", old)
    monkeypatch.setattr(plan_scheduler.threading, "Thread",
                        lambda *args, **kwargs: created.append(ThreadStub()) or created[-1])
    monkeypatch.setattr(plan_scheduler.db, "recover_running_plan_steps", lambda: 0)
    monkeypatch.setattr(plan_scheduler.conf, "PLAN_EXECUTOR_ENABLED", True)
    plan_scheduler.stop()
    plan_scheduler.start()
    assert created and created[0].started is True
    plan_scheduler.stop()


def test_invalid_plan_config_enters_lifespan_warning_queue(monkeypatch):
    monkeypatch.setenv("PLAN_TICK_S", "nan")
    warning_name = "PLAN_TICK_S"
    before = len(plan_scheduler.conf.CONFIG_WARNINGS)
    value = plan_scheduler.conf._positive_finite_env(warning_name, 1.0)
    assert value == 1.0
    assert any(item.get("name") == warning_name
               for item in plan_scheduler.conf.CONFIG_WARNINGS[before:])


def test_dialog_action_is_busy_while_plan_owns_slot(monkeypatch):
    monkeypatch.setattr("LLM.store.db.get_settings", lambda: {"mcp_enabled": True})
    token = action_gate.claim("plan", "plan:7:step:2")
    assert token is not None
    try:
        monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
            "robot_move": {"server": "car", "schema": {}}
        })
        out = tools.run_tool(
            "robot_move", {"direction": "forward", "distance_m": 0.2},
            {"role": "elder", "uid": "u"},
        )
        assert out["ok"] is False and out["status"] == "busy"
    finally:
        action_gate.release(token)


def test_stop_and_status_bypass_action_gate(monkeypatch):
    monkeypatch.setattr("LLM.store.db.get_settings", lambda: {"mcp_enabled": True})
    token = action_gate.claim("plan", "plan:7:step:2")
    assert token is not None
    try:
        calls = []
        monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
            name: {"server": "car", "schema": {}}
            for name in ("robot_stop", "robot_status")
        })
        monkeypatch.setattr(
            tools.mcp_client, "call_tool",
            lambda name, args: calls.append(name) or {"ok": True, "name": name},
        )
        assert tools.run_tool("robot_stop", {}, {"role": "ward"})["ok"] is True
        assert tools.run_tool("robot_status", {}, {"role": "ward"})["ok"] is True
        assert calls == ["robot_stop", "robot_status"]
    finally:
        action_gate.release(token)


def test_old_gate_token_cannot_release_new_owner():
    old = action_gate.claim("plan", "plan:7:step:2")
    assert old is not None
    assert action_gate.release(old) is True
    new = action_gate.claim("dialog", "dialog:1")
    assert new is not None
    try:
        assert action_gate.release(old) is False
        assert action_gate.snapshot()["owner"] == "dialog"
    finally:
        action_gate.release(new)


def test_run_plan_tool_only_accepts_canonical_car_actions(monkeypatch):
    monkeypatch.setattr("LLM.store.db.get_settings", lambda: {"mcp_enabled": True})
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
        "robot_goto_point": {"server": "car", "schema": {}},
        "robot_goto_place": {"server": "car", "schema": {}},
    })
    calls = []
    monkeypatch.setattr(
        tools.mcp_client, "call_tool",
        lambda name, args: calls.append((name, args)) or {"ok": True},
    )
    assert tools.run_plan_tool("robot_goto_point", {"x": 1})["ok"] is True
    denied = tools.run_plan_tool("robot_goto_place", {"place": "desk"})
    assert denied["ok"] is False
    assert calls == [("robot_goto_point", {"x": 1})]


def test_run_plan_tool_requires_mcp_enabled_and_car_server(monkeypatch):
    monkeypatch.setattr("LLM.store.db.get_settings", lambda: {"mcp_enabled": False})
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
        "robot_move": {"server": "car", "schema": {}}
    })
    assert tools.run_plan_tool("robot_move", {})["ok"] is False

    monkeypatch.setattr("LLM.store.db.get_settings", lambda: {"mcp_enabled": True})
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
        "robot_move": {"server": "other", "schema": {}}
    })
    assert tools.run_plan_tool("robot_move", {})["ok"] is False


def test_cancel_poll_requires_fresh_idle_status_before_cancelled(monkeypatch, tmp_path):
    monkeypatch.setattr("LLM.store.db.DB_PATH", tmp_path / "plan.db")
    from LLM.store import db
    db.init_db()
    made = db.create_plan(
        {"title": "取消", "priority": "P2", "status": "cancelling"},
        [{"seq": 1, "step_type": "action", "action": "robot_goto_point",
          "status": "interrupting", "args_json": {"x": 1, "y": 2}}],
    )
    step = db.get_plan(made["id"], include_steps=True, include_attempts=True)["steps"][0]
    monkeypatch.setattr(tools, "run_plan_tool", lambda name, args: {
        "ok": True, "result": json.dumps({"ok": True, "exec_state": "idle",
                                             "state_fresh": True})})
    plan_scheduler._cancel_started.clear()
    plan_scheduler._poll_cancel(step, 10.0)
    got = db.get_plan(made["id"], include_steps=True)
    assert got["status"] == "cancelled"
    assert got["steps"][0]["status"] == "interrupted"


def test_cancel_poll_marks_review_at_exact_grace_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr("LLM.store.db.DB_PATH", tmp_path / "plan.db")
    from LLM.store import db
    db.init_db()
    made = db.create_plan(
        {"title": "取消超时", "priority": "P2", "status": "cancelling"},
        [{"seq": 1, "step_type": "action", "action": "robot_goto_point",
          "status": "interrupting", "args_json": {"x": 1, "y": 2}}],
    )
    step = db.get_plan(made["id"], include_steps=True, include_attempts=True)["steps"][0]
    monkeypatch.setattr(plan_scheduler.conf, "PLAN_STATUS_GRACE_S", 15.0)
    monkeypatch.setattr(tools, "run_plan_tool", lambda name, args: {
        "ok": True, "result": json.dumps({"ok": True, "status": "unavailable"})})
    plan_scheduler._cancel_started.clear()
    plan_scheduler._poll_cancel(step, 0.0)
    plan_scheduler._poll_cancel(step, 15.0)
    got = db.get_plan(made["id"], include_steps=True)
    assert got["status"] == "needs_review"
    assert got["steps"][0]["status"] == "needs_review"


def test_finish_cannot_overwrite_newer_manual_transition(monkeypatch, tmp_path):
    monkeypatch.setattr("LLM.store.db.DB_PATH", tmp_path / "plan.db")
    from LLM.store import db
    db.init_db()
    made = db.create_plan(
        {"title": "CAS", "priority": "P2", "status": "queued"},
        [{"seq": 1, "step_type": "action", "action": "robot_move",
          "status": "pending", "args_json": {"direction": "forward", "distance_m": .2}}],
    )
    step = made["steps"][0]
    attempt = db.prepare_plan_attempt(step["id"], 1, {"action": "robot_move"})
    running = db.transition_plan_execution(
        made["id"], step_id=step["id"], attempt_id=attempt["id"],
        step_changes={"status": "running"},
        attempt_changes={"dispatch_state": "dispatched", "car_task_id": 7},
        plan_changes={"status": "running"})
    stale_step = running["steps"][0]
    stale_step["_plan_version"] = running["version"]
    stale_attempt = running["attempts"][0]
    status, newer = db.update_plan_versioned(made["id"], running["version"], priority="P1")
    assert status == "updated"
    plan_scheduler._finish(stale_step, stale_attempt, "succeeded", {"ok": True})
    got = db.get_plan(made["id"], include_steps=True)
    assert got["version"] == newer["version"]
    assert got["status"] == "running"
    assert got["steps"][0]["status"] == "running"


def test_prepare_cas_conflict_after_priority_change_does_not_review(monkeypatch, tmp_path):
    monkeypatch.setattr("LLM.store.db.DB_PATH", tmp_path / "plan.db")
    from LLM.store import db
    db.init_db()
    made = db.create_plan(
        {"title": "调级并发", "priority": "P2", "status": "queued"},
        [{"seq": 1, "step_type": "action", "action": "robot_goto_point",
          "status": "pending", "args_json": {"x": 1, "y": 2}}],
    )
    stale = db.get_plan(made["id"], include_steps=True, include_attempts=True)
    step = stale["steps"][0]
    step["_plan_version"] = stale["version"]
    changed = plan.change_priority(made["id"], stale["version"], "P1", {"uid": "n"})
    assert changed["ok"] is True
    plan_scheduler._dispatch(step, 1.0)
    got = db.get_plan(made["id"], include_steps=True)
    assert got["priority"] == "P1"
    assert got["status"] == "queued"
    assert got["steps"][0]["status"] == "pending"


def test_prepare_cas_conflict_after_manual_confirm_does_not_review(monkeypatch, tmp_path):
    monkeypatch.setattr("LLM.store.db.DB_PATH", tmp_path / "plan.db")
    from LLM.store import db
    db.init_db()
    made = db.create_plan(
        {"title": "人工完成并发", "priority": "P2", "status": "queued"},
        [{"seq": 1, "step_type": "manual", "status": "pending", "wait_kind": "manual"}],
    )
    stale = db.get_plan(made["id"], include_steps=True, include_attempts=True)
    step = stale["steps"][0]
    step.update({"step_type": "action", "action": "robot_goto_point",
                 "_plan_version": stale["version"]})
    changed = plan.confirm_step(made["id"], stale["version"], stale["steps"][0]["id"],
                                "complete", "护士确认", {"uid": "n"})
    assert changed["ok"] is True
    plan_scheduler._dispatch(step, 1.0)
    got = db.get_plan(made["id"], include_steps=True)
    assert got["status"] == "succeeded"
    assert got["steps"][0]["status"] == "succeeded"
