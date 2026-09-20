# -*- coding: utf-8 -*-
r"""Plan candidate validation, target compilation and tool boundary tests."""
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from LLM import conf
from LLM.agent import plan, tools
from LLM.store import db


@pytest.fixture()
def plan_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "plan.db"))
    db.init_db()
    return db


@pytest.fixture()
def fake_resolver():
    class Resolver:
        def resolve_place(self, place):
            assert place == "护士站"
            return {
                "ok": True,
                "map": "ward-map",
                "target": {"x": 1, "y": 2, "yaw_deg": 30, "goal_source": "destination"},
                "tags_fingerprint": "sha1:" + "a" * 40,
            }

        def resolve_zone(self, zone):
            assert zone == "活动区"
            return {
                "ok": True,
                "map": "ward-map",
                "target": {"x": 3, "y": 4, "yaw_deg": 90, "goal_source": "explicit"},
                "tags_fingerprint": "sha1:" + "b" * 40,
            }

    return Resolver()


def _creator(role="elder"):
    return {"uid": "elder_1", "role": role, "slot": "kiosk"}


def _manual_step(**changes):
    step = {"type": "manual", "label": "等待护士确认"}
    step.update(changes)
    return step


def test_place_compiles_to_point(plan_db, fake_resolver):
    out = plan.create_candidate({
        "title": "去护士站", "priority": "P2",
        "steps": [{"type": "action", "action": "robot_goto_place",
                   "args": {"place": "护士站"}, "label": "去护士站"}],
    }, creator=_creator(), resolver=fake_resolver)

    step = out["plan"]["steps"][0]
    assert step["action"] == "robot_goto_point"
    assert step["args_json"] == {"x": 1.0, "y": 2.0, "yaw_deg": 30.0}
    assert step["map_name"] == "ward-map"
    assert step["tags_fingerprint"] == "sha1:" + "a" * 40
    assert step["target_json"] == {
        "source_action": "robot_goto_place", "name": "护士站",
        "x": 1.0, "y": 2.0, "yaw_deg": 30.0, "goal_source": "destination",
    }


def test_zone_compiles_to_point(plan_db, fake_resolver):
    out = plan.create_candidate({
        "title": "去活动区",
        "steps": [{"type": "action", "action": "robot_goto_zone",
                   "args": {"zone": "活动区"}, "label": "去活动区"}],
    }, creator=_creator(), resolver=fake_resolver)

    step = out["plan"]["steps"][0]
    assert step["action"] == "robot_goto_point"
    assert step["args_json"] == {"x": 3.0, "y": 4.0, "yaw_deg": 90.0}
    assert step["target_json"]["source_action"] == "robot_goto_zone"
    assert step["target_json"]["name"] == "活动区"
    assert step["tags_fingerprint"] == "sha1:" + "b" * 40


def test_time_wait_requires_wake_at():
    with pytest.raises(ValueError, match="wake_at"):
        plan.compile_steps([{"type": "wait", "wait_kind": "time", "label": "等十分钟"}], None)


def test_manual_step_never_accepts_action(plan_db):
    with pytest.raises(ValueError, match="manual.*action|action.*manual"):
        plan.create_candidate({
            "title": "等护士", "steps": [_manual_step(action="robot_stop")],
        }, creator=_creator())


@pytest.mark.parametrize("priority", ["P0", "urgent"])
def test_llm_cannot_create_forbidden_priority(priority):
    with pytest.raises(ValueError, match="priority|P0"):
        plan.create_candidate({
            "title": "越权计划", "priority": priority, "steps": [_manual_step()],
        }, creator=_creator())


def test_action_outside_plan_whitelist_is_rejected():
    with pytest.raises(ValueError, match="白名单"):
        plan.compile_steps([
            {"type": "action", "action": "see_what", "args": {}, "label": "观察"},
        ], None)


@pytest.mark.parametrize("field,value", [
    ("wait_kind", "time"), ("wake_at", "2026-09-22 09:00:00"),
])
def test_action_rejects_fields_from_other_step_types(field, value):
    with pytest.raises(ValueError, match=field):
        plan.compile_steps([{
            "type": "action", "action": "robot_move",
            "args": {"direction": "forward", "distance_m": 1}, field: value,
        }], None)


@pytest.mark.parametrize("step", [
    {"type": "wait", "wait_kind": "device", "args": {"completion": "arrived"}},
    {"type": "wait", "wait_kind": "external", "args": {"on_failure": "continue"}},
    {"type": "manual", "args": {"retry_policy": "always"}},
])
def test_wait_and_manual_reject_undefined_args(step):
    with pytest.raises(ValueError, match="args"):
        plan.compile_steps([step], None)


@pytest.mark.parametrize("field", ["completion", "on_failure", "retry_policy", "status"])
def test_llm_cannot_inject_step_execution_fields(field):
    with pytest.raises(ValueError, match=field):
        plan.compile_steps([_manual_step(**{field: "model-controlled"})], None)


def test_any_invalid_step_prevents_database_write(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "create_plan", lambda *args: calls.append(args))

    with pytest.raises(ValueError, match="白名单"):
        plan.create_candidate({
            "title": "不能半落库",
            "steps": [
                _manual_step(),
                {"type": "action", "action": "unknown_action", "args": {}},
            ],
        }, creator=_creator())

    assert calls == []


def test_real_tool_entry_rejects_unknown_top_level_fields(plan_db):
    out = tools.run_tool(
        "create_plan",
        {"title": "禁止注入", "steps": [_manual_step()], "status": "succeeded"},
        _creator(),
    )

    assert out["ok"] is False
    assert "status" in (out.get("error") or out.get("message") or "")
    assert db.list_plans() == []


def test_navigation_snapshot_requires_map_and_valid_sha1(plan_db):
    class MissingSnapshot:
        def resolve_place(self, _place):
            return {"ok": True, "map": "", "target": {"x": 1, "y": 2, "yaw_deg": 0}}

    class BadFingerprint:
        def resolve_place(self, _place):
            return {"ok": True, "map": "ward-map",
                    "target": {"x": 1, "y": 2, "yaw_deg": 0},
                    "tags_fingerprint": "sha1:not-a-digest"}

    payload = {"title": "去护士站", "steps": [{
        "type": "action", "action": "robot_goto_place", "args": {"place": "护士站"}}]}
    with pytest.raises(ValueError, match="地图"):
        plan.create_candidate(payload, _creator(), resolver=MissingSnapshot())
    with pytest.raises(ValueError, match="指纹"):
        plan.create_candidate(payload, _creator(), resolver=BadFingerprint())


def test_report_is_persisted_as_structured_plan_data(plan_db):
    out = plan.create_candidate({
        "title": "完成后回报", "steps": [_manual_step()],
        "report": {"notify": True, "speak_if_present": False},
    }, _creator())

    stored = db.get_plan(out["plan"]["id"])
    assert stored["report_json"] == {"notify": True, "speak_if_present": False}


def test_plan_timeout_defaults_and_environment_fallback():
    assert (conf.PLAN_MOVE_TIMEOUT_S, conf.PLAN_TURN_TIMEOUT_S,
            conf.PLAN_GOTO_TIMEOUT_S) == (45, 45, 200)
    env = dict(os.environ)
    env.update({"PLAN_MOVE_TIMEOUT_S": "12", "PLAN_TURN_TIMEOUT_S": "bad",
                "PLAN_GOTO_TIMEOUT_S": "-1"})
    proc = subprocess.run(
        [sys.executable, "-c",
         "from LLM import conf; print(conf.PLAN_MOVE_TIMEOUT_S, conf.PLAN_TURN_TIMEOUT_S, conf.PLAN_GOTO_TIMEOUT_S)"],
        cwd=conf.BASE_DIR, env=env, capture_output=True, text=True, check=True,
    )
    assert proc.stdout.strip().splitlines()[-1] == "12 45 200"


def test_config_fallback_warnings_are_flushed_to_audit(monkeypatch):
    from LLM import server

    warnings = [{"name": "PLAN_MOVE_TIMEOUT_S", "value": "bad", "fallback": 45}]
    records = []
    monkeypatch.setattr(conf, "CONFIG_WARNINGS", warnings)
    monkeypatch.setattr(server.audit, "log", lambda event, **fields: records.append(
        {"event": event, **fields}))

    server._audit_config_warnings()

    assert records == [{
        "event": "config_warning", "action": "fallback", "level": "warning",
        "name": "PLAN_MOVE_TIMEOUT_S", "value": "bad", "fallback": 45,
    }]
    assert warnings == []


def test_compiled_actions_use_configured_timeouts(monkeypatch, fake_resolver):
    monkeypatch.setattr(conf, "PLAN_MOVE_TIMEOUT_S", 11)
    monkeypatch.setattr(conf, "PLAN_TURN_TIMEOUT_S", 22)
    monkeypatch.setattr(conf, "PLAN_GOTO_TIMEOUT_S", 33)

    move, turn, goto = plan.compile_steps([
        {"type": "action", "action": "robot_move",
         "args": {"direction": "forward", "distance_m": 1}},
        {"type": "action", "action": "robot_turn", "args": {"angle_deg": 90}},
        {"type": "action", "action": "robot_goto_place", "args": {"place": "护士站"}},
    ], fake_resolver)

    assert move["timeout_sec"] == 11
    assert turn["timeout_sec"] == 22
    assert goto["timeout_sec"] == 33


def test_post_commit_read_and_audit_failures_do_not_turn_success_into_failure(
        plan_db, monkeypatch):
    monkeypatch.setattr(db, "get_plan", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("unexpected post-commit read")))
    monkeypatch.setattr(plan.audit, "log", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("audit disk unavailable")))

    out = tools.run_tool(
        "create_plan", {"title": "只创建一次", "steps": [_manual_step()]}, _creator())

    assert out["ok"] is True
    assert len(db.list_plans()) == 1


def test_create_plan_tool_records_current_principal_and_resets_context(plan_db):
    principal = {"uid": "elder_9", "role": "elder", "slot": "kiosk"}

    out = tools.run_tool(
        "create_plan",
        {"title": "等护士", "steps": [_manual_step()]},
        principal,
    )

    made = db.get_plan(out["plan_id"], include_steps=True)
    assert out["ok"] is True
    assert made["creator_uid"] == "elder_9"
    assert made["creator_role"] == "elder"
    assert made["creator_surface"] == "kiosk"
    assert tools.current_principal() == {}


def test_omitted_owner_does_not_silently_become_creator(plan_db):
    out = tools.run_tool(
        "create_plan",
        {"title": "无指定服务对象", "steps": [_manual_step()]},
        {"uid": "elder_9", "role": "elder", "slot": "kiosk"},
    )

    made = db.get_plan(out["plan_id"])
    assert made["owner_uid"] is None


def test_local_tool_exception_still_resets_principal():
    name = "__principal_failure_probe__"

    @tools.tool(name, "测试 principal 清理", {}, roles={"admin"})
    def fail():
        raise RuntimeError("boom")

    try:
        out = tools.run_tool(name, {}, {"uid": "admin", "role": "admin", "slot": "admin"})
        assert out["ok"] is False
        assert tools.current_principal() == {}
    finally:
        tools._TOOL_REGISTRY.pop(name, None)
        tools.TOOL_DEFAULTS.pop(f"{name}_enabled", None)


def test_nested_local_tool_call_restores_outer_principal():
    inner_name, outer_name = "__principal_inner__", "__principal_outer__"
    seen = []

    @tools.tool(inner_name, "读取内层 principal", {}, roles={"admin"})
    def inner():
        seen.append(("inner", tools.current_principal()["uid"]))
        return {"ok": True}

    @tools.tool(outer_name, "读取外层 principal", {}, roles={"admin"})
    def outer():
        seen.append(("outer-before", tools.current_principal()["uid"]))
        tools.run_tool(inner_name, {}, {"uid": "inner", "role": "admin", "slot": "admin"})
        seen.append(("outer-after", tools.current_principal()["uid"]))
        return {"ok": True}

    try:
        tools.run_tool(outer_name, {}, {"uid": "outer", "role": "admin", "slot": "admin"})
        assert seen == [("outer-before", "outer"), ("inner", "inner"),
                        ("outer-after", "outer")]
        assert tools.current_principal() == {}
    finally:
        for name in (inner_name, outer_name):
            tools._TOOL_REGISTRY.pop(name, None)
            tools.TOOL_DEFAULTS.pop(f"{name}_enabled", None)


def test_nested_exception_restores_outer_principal():
    inner_name, outer_name = "__principal_inner_fail__", "__principal_outer_catch__"
    seen = []

    @tools.tool(inner_name, "内层异常", {}, roles={"admin"})
    def inner():
        raise RuntimeError("boom")

    @tools.tool(outer_name, "捕获内层结果", {}, roles={"admin"})
    def outer():
        tools.run_tool(inner_name, {}, {"uid": "inner", "role": "admin", "slot": "admin"})
        seen.append(tools.current_principal()["uid"])
        return {"ok": True}

    try:
        tools.run_tool(outer_name, {}, {"uid": "outer", "role": "admin", "slot": "admin"})
        assert seen == ["outer"]
        assert tools.current_principal() == {}
    finally:
        for name in (inner_name, outer_name):
            tools._TOOL_REGISTRY.pop(name, None)
            tools.TOOL_DEFAULTS.pop(f"{name}_enabled", None)


def _stored_plan(plan_db, *, status="queued", step_status="pending", step_type="manual",
                 wait_kind="manual", action=None, retry_policy="none"):
    return plan_db.create_plan(
        {"title": "人工操作", "priority": "P2", "status": status},
        [{"seq": 1, "step_type": step_type, "action": action,
          "wait_kind": wait_kind, "status": step_status,
          "retry_policy": retry_policy, "max_attempts": 1}],
    )


def test_manual_confirm_uses_versioned_aggregate_transaction(plan_db):
    made = _stored_plan(plan_db)
    out = plan.confirm_step(made["id"], made["version"], made["steps"][0]["id"],
                            "complete", "护士确认", {"uid": "nurse"})
    assert out["ok"] is True
    assert out["plan"]["status"] == "succeeded"
    assert out["plan"]["steps"][0]["status"] == "succeeded"


def test_time_wait_cannot_be_manually_confirmed(plan_db):
    made = _stored_plan(plan_db, step_type="wait", wait_kind="time")
    out = plan.confirm_step(made["id"], made["version"], made["steps"][0]["id"],
                            "complete", "强制完成", {"uid": "nurse"})
    assert out["ok"] is False and out["error"] == "time_wait_cannot_be_confirmed"


def test_running_cancel_is_idempotent_and_stops_once(plan_db, monkeypatch):
    made = _stored_plan(plan_db, status="running", step_status="running",
                        step_type="action", action="robot_goto_point",
                        retry_policy="safe_goto_only")
    calls = []
    monkeypatch.setattr(tools, "run_plan_tool",
                        lambda name, args: calls.append(name) or {
                            "ok": True, "result": '{"ok": true}'})
    first = plan.cancel_plan(made["id"], made["version"], "护士取消", {"uid": "nurse"})
    second = plan.cancel_plan(made["id"], first["plan"]["version"], "重复点击", {"uid": "nurse"})
    assert first["plan"]["status"] == "cancelling"
    assert second["plan"]["status"] == "cancelling"
    assert calls == ["robot_stop"]


def test_running_cancel_inner_stop_failure_marks_attempt_uncertain(plan_db, monkeypatch):
    made = _stored_plan(plan_db, status="running", step_status="running",
                        step_type="action", action="robot_goto_point",
                        retry_policy="safe_goto_only")
    step = made["steps"][0]
    plan_db.update_step(step["id"], status="pending")
    attempt = plan_db.prepare_plan_attempt(step["id"], 1, {"action": "robot_goto_point"})
    plan_db.transition_plan_execution(
        made["id"], step_id=step["id"], attempt_id=attempt["id"],
        step_changes={"status": "running"},
        attempt_changes={"dispatch_state": "dispatched", "car_task_id": 9})
    current = plan_db.get_plan(made["id"], include_steps=True, include_attempts=True)
    monkeypatch.setattr(tools, "run_plan_tool", lambda name, args: {
        "ok": True, "result": '{"ok": false, "error": "底盘拒绝急停"}'})
    out = plan.cancel_plan(made["id"], current["version"], "护士取消", {"uid": "n", "role": "ward"})
    assert out["ok"] is False and out["plan"]["status"] == "needs_review"
    assert out["plan"]["attempts"][0]["dispatch_state"] == "uncertain"
    retry = plan.retry_step(out["plan"]["id"], out["plan"]["version"], step["id"],
                            "重新执行", {"uid": "n", "role": "ward"})
    assert retry["ok"] is True


def test_queued_cancel_preserves_succeeded_steps_and_notifies(plan_db, monkeypatch):
    made = plan_db.create_plan(
        {"title": "部分完成", "priority": "P2", "status": "queued"},
        [{"seq": 1, "step_type": "manual", "status": "succeeded"},
         {"seq": 2, "step_type": "manual", "status": "pending"}],
    )
    events = []
    monkeypatch.setattr(plan.notify, "ingest", lambda *args, **kwargs: events.append((args, kwargs)))
    out = plan.cancel_plan(made["id"], made["version"], "不再需要", {"uid": "n", "role": "ward", "slot": "nurse"})
    assert out["plan"]["steps"][0]["status"] == "succeeded"
    assert out["plan"]["steps"][1]["status"] == "cancelled"
    assert events and events[-1][0][:2] == ("plan", "plan_done")


def test_version_conflict_does_not_mutate_plan(plan_db):
    made = _stored_plan(plan_db)
    out = plan.change_priority(made["id"], made["version"] - 1, "P1", {"uid": "nurse"})
    assert out["ok"] is False and out["error"] == "version_conflict"
    assert plan_db.get_plan(made["id"])["priority"] == "P2"


def test_manual_audit_keeps_actor_context(plan_db, monkeypatch):
    made = _stored_plan(plan_db)
    records = []
    monkeypatch.setattr(plan.audit, "log",
                        lambda event, **fields: records.append({"event": event, **fields}))
    out = plan.change_priority(
        made["id"], made["version"], "P1",
        {"uid": "nurse-1", "role": "ward", "slot": "nurse"})
    assert out["ok"] is True
    record = records[-1]
    assert (record["actor_uid"], record["actor_role"], record["actor_surface"]) == (
        "nurse-1", "ward", "nurse")


def test_needs_review_retry_rejects_relative_action(plan_db):
    made = _stored_plan(plan_db, status="needs_review", step_status="needs_review",
                        step_type="action", action="robot_move", retry_policy="none")
    out = plan.retry_step(made["id"], made["version"], made["steps"][0]["id"],
                          "不确定，人工复核", {"uid": "nurse"})
    assert out["ok"] is False and out["error"] == "retry_not_allowed"


def test_needs_review_retry_creates_prepared_new_attempt(plan_db):
    made = _stored_plan(plan_db, status="needs_review", step_status="needs_review",
                        step_type="action", action="robot_goto_point",
                        retry_policy="safe_goto_only")
    out = plan.retry_step(made["id"], made["version"], made["steps"][0]["id"],
                          "重新校验后执行", {"uid": "nurse"})
    assert out["ok"] is True
    assert out["plan"]["steps"][0]["status"] == "pending"
    assert out["plan"]["attempts"][-1]["dispatch_state"] == "prepared"


def test_principal_context_is_isolated_between_threads():
    name = "__principal_thread_probe__"
    barrier = threading.Barrier(2)

    @tools.tool(name, "跨线程读取 principal", {}, roles={"admin"})
    def probe():
        barrier.wait(timeout=2)
        return {"ok": True, "uid": tools.current_principal()["uid"]}

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(tools.run_tool, name, {},
                            {"uid": uid, "role": "admin", "slot": "admin"})
                for uid in ("thread-a", "thread-b")
            ]
        assert {future.result()["uid"] for future in futures} == {"thread-a", "thread-b"}
        assert tools.current_principal() == {}
    finally:
        tools._TOOL_REGISTRY.pop(name, None)
        tools.TOOL_DEFAULTS.pop(f"{name}_enabled", None)
