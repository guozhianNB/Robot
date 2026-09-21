# -*- coding: utf-8 -*-
"""系统退出相关停止钩子测试（reminder / bus）。"""
import threading
import asyncio
import os

from LLM.agent import reminder
from LLM.core import bus


def test_reminder_start_clears_then_stop_sets_event():
    # 重置为全新事件，避免测试间状态残留
    reminder._stop_evt = threading.Event()
    reminder.start()
    try:
        assert not reminder._stop_evt.is_set()   # start 必须清标志
        reminder.stop()
        assert reminder._stop_evt.is_set()        # stop 必须置位
    finally:
        reminder.stop()                           # 保证线程退出，不泄漏


def test_bus_stop_sets_flag():
    bus._stop = False
    bus.stop()
    assert bus._stop is True


def test_lifespan_stops_plan_scheduler_before_mcp(monkeypatch):
    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
    import LLM.server as server

    events = []

    class _Drain:
        def cancel(self):
            events.append("drain.stop")

    monkeypatch.setattr(server.db, "init_db", lambda: None)
    monkeypatch.setattr(server, "_audit_config_warnings", lambda: None)
    monkeypatch.setattr(server, "_seed_demo", lambda: None)
    monkeypatch.setattr(server.notify, "prune", lambda: None)
    monkeypatch.setattr(server.db, "get_settings", lambda: {})
    monkeypatch.setattr(server.reminder, "start", lambda: events.append("reminder.start"))
    monkeypatch.setattr(server.bus, "start_drain", lambda: _Drain())
    monkeypatch.setattr(server.voice_api, "start_voice", lambda *a: None)
    monkeypatch.setattr(server.voice_api, "stop_voice", lambda: None)
    monkeypatch.setattr(server.session, "ensure_admin_password", lambda: None)
    monkeypatch.setattr(server.db, "get_admin_auth", lambda: {"hash": "x"})
    monkeypatch.setattr(server.locator, "available", lambda: (True, ""))
    monkeypatch.setattr(server.mapctl, "stop", lambda: events.append("map.stop"))
    monkeypatch.setattr(server.mcp_client, "start", lambda settings: events.append("mcp.start"))
    monkeypatch.setattr(server.mcp_client, "stop", lambda: events.append("mcp.stop"))
    monkeypatch.setattr(server.plan_scheduler, "start", lambda: events.append("plan.start"))
    monkeypatch.setattr(server.plan_scheduler, "stop", lambda: events.append("plan.stop"))

    async def run():
        async with server.lifespan(server.app):
            assert events.index("mcp.start") < events.index("plan.start")
        assert events.index("plan.stop") < events.index("mcp.stop")

    asyncio.run(run())


def test_lifespan_scheduler_start_failure_is_degraded(monkeypatch):
    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
    import LLM.server as server

    audits = []
    monkeypatch.setattr(server.db, "init_db", lambda: None)
    monkeypatch.setattr(server, "_audit_config_warnings", lambda: None)
    monkeypatch.setattr(server, "_seed_demo", lambda: None)
    monkeypatch.setattr(server.notify, "prune", lambda: None)
    monkeypatch.setattr(server.db, "get_settings", lambda: {})
    monkeypatch.setattr(server.reminder, "start", lambda: None)
    monkeypatch.setattr(server.bus, "start_drain", lambda: type("D", (), {"cancel": lambda s: None})())
    monkeypatch.setattr(server.voice_api, "start_voice", lambda *a: None)
    monkeypatch.setattr(server.mcp_client, "start", lambda settings: None)
    monkeypatch.setattr(server.plan_scheduler, "start",
                        lambda: (_ for _ in ()).throw(RuntimeError("recover failed")))
    monkeypatch.setattr(server.audit, "log", lambda event, **fields: audits.append((event, fields)))
    monkeypatch.setattr(server.session, "ensure_admin_password", lambda: None)
    monkeypatch.setattr(server.db, "get_admin_auth", lambda: {"hash": "x"})
    monkeypatch.setattr(server.locator, "available", lambda: (True, ""))
    monkeypatch.setattr(server, "_stop_runtime", lambda **kw: [])

    async def run():
        async with server.lifespan(server.app):
            pass

    asyncio.run(run())
    assert any(fields.get("action") == "scheduler_start_failed" for _, fields in audits)


def test_lifespan_cleanup_continues_and_aggregates_stop_errors(monkeypatch):
    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
    import LLM.server as server

    calls = []
    monkeypatch.setattr(server.plan_scheduler, "stop",
                        lambda: (_ for _ in ()).throw(RuntimeError("plan stop")))
    monkeypatch.setattr(server.mapctl, "stop",
                        lambda: (_ for _ in ()).throw(RuntimeError("map stop")))
    monkeypatch.setattr(server.mcp_client, "stop", lambda: calls.append("mcp.stop"))
    monkeypatch.setattr(server.voice_api, "stop_voice", lambda: calls.append("voice.stop"))
    errors = server._stop_runtime(hard_map=False)
    assert calls == ["mcp.stop", "voice.stop"]
    assert [item[0] for item in errors] == ["plan_scheduler", "mapctl"]


def test_system_shutdown_uses_plan_before_mcp_even_when_plan_stop_fails(monkeypatch):
    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
    import LLM.server as server

    calls = []
    server._shutting_down = False
    monkeypatch.setattr(server.plan_scheduler, "stop", lambda: (
        calls.append("plan.stop"), (_ for _ in ()).throw(RuntimeError("boom")))[1])
    monkeypatch.setattr(server.mapctl, "stop", lambda **kw: calls.append("map.stop"))
    monkeypatch.setattr(server.mcp_client, "stop", lambda: calls.append("mcp.stop"))
    monkeypatch.setattr(server.reminder, "stop", lambda: calls.append("reminder.stop"))
    monkeypatch.setattr(server.voice_api, "stop_voice", lambda: calls.append("voice.stop"))
    monkeypatch.setattr(server.bus, "stop", lambda: calls.append("bus.stop"))
    monkeypatch.setattr(server._bg, "shutdown", lambda **kw: calls.append("bg.stop"))
    monkeypatch.setattr(server.audit, "log", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_schedule_delayed_exit", lambda: calls.append("exit.scheduled"))

    asyncio.run(server.system_shutdown())
    assert calls.index("plan.stop") < calls.index("mcp.stop")
    assert "mcp.stop" in calls and calls[-1] == "exit.scheduled"
