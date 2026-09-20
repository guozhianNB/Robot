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
