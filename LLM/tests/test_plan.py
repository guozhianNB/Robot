# -*- coding: utf-8 -*-
r"""Plan candidate validation, target compilation and tool boundary tests."""
import pytest

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
                "tags_fingerprint": "sha1:place",
            }

        def resolve_zone(self, zone):
            assert zone == "活动区"
            return {
                "ok": True,
                "map": "ward-map",
                "target": {"x": 3, "y": 4, "yaw_deg": 90, "goal_source": "explicit"},
                "tags_fingerprint": "sha1:zone",
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
    assert step["tags_fingerprint"] == "sha1:place"
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
    assert step["tags_fingerprint"] == "sha1:zone"


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
