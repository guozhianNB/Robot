# -*- coding: utf-8 -*-
r"""MCP 生命周期性能回归测试。"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from LLM.agent import mcp_client


def _reset_state():
    mcp_client.stop()
    mcp_client._sessions.clear()
    mcp_client._tools.clear()
    mcp_client._errors.clear()


def test_start_does_not_wait_for_server_connections(monkeypatch):
    started = asyncio.Event()

    async def slow_connect(_enabled):
        started.set()
        await asyncio.sleep(60)

    _reset_state()
    monkeypatch.setattr(mcp_client, "_MCP_AVAILABLE", True)
    monkeypatch.setattr(mcp_client, "MCP_SERVERS", {
        "slow": {"command": "unused", "enabled": True},
    })
    monkeypatch.setattr(mcp_client, "MCP_CONNECT_TIMEOUT", 0.1)
    monkeypatch.setattr(mcp_client, "_connect_all", slow_connect)

    before = time.perf_counter()
    try:
        assert mcp_client.start({"mcp_enabled": True}) == []
        elapsed = time.perf_counter() - before
        assert elapsed < 0.05
        assert mcp_client._connect_future is not None
    finally:
        _reset_state()


def test_start_is_idempotent_while_connections_are_pending(monkeypatch):
    calls = 0

    async def slow_connect(_enabled):
        nonlocal calls
        calls += 1
        await asyncio.sleep(60)

    _reset_state()
    monkeypatch.setattr(mcp_client, "_MCP_AVAILABLE", True)
    monkeypatch.setattr(mcp_client, "MCP_SERVERS", {
        "slow": {"command": "unused", "enabled": True},
    })
    monkeypatch.setattr(mcp_client, "MCP_CONNECT_TIMEOUT", 0.1)
    monkeypatch.setattr(mcp_client, "_connect_all", slow_connect)

    try:
        mcp_client.start({"mcp_enabled": True})
        mcp_client.start({"mcp_enabled": True})
        time.sleep(0.02)
        assert calls == 1
    finally:
        _reset_state()


def test_partial_connection_is_closed_when_initialize_fails(monkeypatch):
    closed = []

    class FakeContext:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, *_args):
            closed.append("context")

    class FakeSession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed.append("session")

        async def initialize(self):
            raise RuntimeError("handshake failed")

    monkeypatch.setattr(mcp_client, "stdio_client", lambda _params: FakeContext())
    monkeypatch.setattr(mcp_client, "ClientSession", FakeSession)

    with pytest.raises(RuntimeError, match="handshake failed"):
        asyncio.run(mcp_client._connect_one("broken", {"command": "unused"}))

    assert closed == ["session", "context"]


def test_connect_one_resolves_env_sentinels_before_starting_server(monkeypatch):
    captured = []

    class FakeParameters:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    class FakeContext:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, *_args):
            return False

    class FakeSession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    monkeypatch.setattr(mcp_client, "StdioServerParameters", FakeParameters)
    monkeypatch.setattr(mcp_client, "stdio_client", lambda _params: FakeContext())
    monkeypatch.setattr(mcp_client, "ClientSession", FakeSession)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "runtime-key")
    monkeypatch.setenv("VISION_SEE_MODEL", "runtime-qwen")

    try:
        asyncio.run(mcp_client._connect_one("vision-env", {
            "command": "unused",
            "args": ["see_server.py"],
            "env": {
                "DASHSCOPE_API_KEY": "",
                "VISION_HOST": "10.0.0.9",
                "VISION_PORT": "19540",
                "VISION_SEE_MODEL": "",
            },
        }))

        assert captured == [{
            "command": "unused",
            "args": ["see_server.py"],
            "env": {
                "DASHSCOPE_API_KEY": "runtime-key",
                "VISION_HOST": "10.0.0.9",
                "VISION_PORT": "19540",
                "VISION_SEE_MODEL": "runtime-qwen",
            },
        }]
    finally:
        mcp_client._sessions.pop("vision-env", None)


def test_status_reports_enabled_server_as_connecting_while_startup_is_pending(monkeypatch):
    class PendingFuture:
        @staticmethod
        def done():
            return False

    monkeypatch.setattr(mcp_client, "MCP_SERVERS", {
        "slow": {"command": "unused", "enabled": True},
    })
    monkeypatch.setattr(mcp_client, "_connect_future", PendingFuture())
    monkeypatch.setattr(mcp_client, "_sessions", {})
    monkeypatch.setattr(mcp_client, "_errors", {})

    assert mcp_client.status()["servers"] == {"slow": "connecting"}
