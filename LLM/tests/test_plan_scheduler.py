# -*- coding: utf-8 -*-
"""共享车控动作闸门与 Plan MCP 入口契约测试。"""

from LLM.agent import action_gate, tools


def test_dialog_action_is_busy_while_plan_owns_slot(monkeypatch):
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
