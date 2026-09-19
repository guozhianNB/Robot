# -*- coding: utf-8 -*-
r"""事件总线测试（LLM/bus.py）。

核心回归（2026-09-15 修复）：
老实现用 `run_in_executor(None, _q.get)`（**无超时**的阻塞取）把事件循环的默认
线程池 worker 永久占住；`asyncio.run()` / `with TestClient(app)` 收尾时
`shutdown_default_executor()` 要 join 那个 worker，于是**永久挂住**——
实测现象：`tests/test_modules_status.py` 跑不完（120s+ 不结束）、uvicorn 重启退不掉，
只能强杀。改成带超时（`bus.POLL_SECS`）后 worker 最多 0.5s 返回一次，`_stop`
才真正有机会生效；即使调用方忘了 `stop()` 也不会死锁。

用例组织：
  * 语义：publish 的载荷原样保留；SSE 生成器输出带 `data: ` 前缀与空行结尾
  * 端到端：publish → drain → 订阅者真能收到
  * **回归**：stop() 后 drain 在超时窗口内退出；**不调用 stop() 时事件循环也能关掉**
  * 不变量：POLL_SECS 必须是有限正数（有人改成 None/0 就等于退回老 bug）
"""
from __future__ import annotations

import asyncio
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from LLM import bus  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_bus(monkeypatch):
    """bus 是模块级单例：每个用例给它一套干净状态，测完自动还原。"""
    monkeypatch.setattr(bus, "_q", queue.Queue(), raising=False)
    monkeypatch.setattr(bus, "_subscribers", set(), raising=False)
    monkeypatch.setattr(bus, "_stop", False, raising=False)
    yield


def test_poll_secs_is_finite_positive():
    """不变量：取队列的等待上限必须是有限正数，否则等于退回死锁老实现。"""
    assert isinstance(bus.POLL_SECS, (int, float))
    assert 0 < bus.POLL_SECS <= 5


def test_publish_keeps_type_and_payload():
    bus.publish("alarm", level="critical", uid="u1", message="跌倒")
    assert bus._q.get_nowait() == {"type": "alarm", "level": "critical",
                                   "uid": "u1", "message": "跌倒"}


def test_publish_is_usable_from_other_threads():
    """publish 的契约是"任意线程可调、立即返回"（提醒调度线程在用）。"""
    def worker():
        for i in range(5):
            bus.publish("reminder", rid=i)
    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)
    assert [bus._q.get_nowait()["rid"] for _ in range(5)] == [0, 1, 2, 3, 4]


def test_publish_reaches_sse_subscriber():
    """端到端：publish → drain → 订阅连接真的收到（SSE 格式）。"""
    async def main():
        task = bus.start_drain()
        gen = bus.stream_events()
        await asyncio.sleep(0.05)                 # 让订阅登记进 _subscribers
        bus.publish("user_changed", uid="u9", role="elder")
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=3)
        await gen.aclose()
        bus.stop()
        await asyncio.wait_for(task, timeout=3)
        return chunk

    chunk = asyncio.run(main())
    assert chunk.startswith("data: ") and chunk.endswith("\n\n")
    assert '"type": "user_changed"' in chunk and '"uid": "u9"' in chunk


def test_stop_lets_drain_exit_within_poll_window():
    """stop() 置位后，drain 必须在一个超时窗口内退出（而不是等下一次消息）。"""
    async def main():
        task = bus.start_drain()
        await asyncio.sleep(0.1)                  # 让它进入"阻塞取"
        t0 = time.monotonic()
        bus.stop()
        await asyncio.wait_for(task, timeout=3)   # 老实现：永不返回 → 超时
        return time.monotonic() - t0

    assert asyncio.run(main()) < 1.5


def test_loop_shutdown_not_blocked_even_without_stop():
    """**核心回归**：调用方忘了 stop()，事件循环收尾也不能被卡住。

    放子线程跑并自带超时，避免这个用例自己把整个 pytest 拖死。
    老实现下 `asyncio.run()` 会在 `shutdown_default_executor()` 永久阻塞，
    `done` 永远不会置位。
    """
    done = threading.Event()

    def worker():
        async def main():
            bus.start_drain()
            await asyncio.sleep(0.2)              # 确保 drain 已进入阻塞取队列
        asyncio.run(main())                       # 退出即关循环 + 关默认线程池
        done.set()

    threading.Thread(target=worker, daemon=True, name="bus-shutdown-probe").start()
    assert done.wait(5), "事件循环收尾被阻塞（回归：取队列无超时）"


def test_drain_survives_subscriber_queue_full():
    """订阅者队列满（前端卡住）只丢该订阅者，不打断扇出。"""
    async def main():
        task = bus.start_drain()
        small = asyncio.Queue(maxsize=1)
        bus._subscribers.add(small)
        for i in range(3):                        # 第一条占满，后两条应触发 QueueFull
            bus.publish("reminder", rid=i)
            await asyncio.sleep(0.05)
        ok = small in bus._subscribers
        bus.stop()
        await asyncio.wait_for(task, timeout=3)
        return ok

    assert asyncio.run(main()) is False           # 满队列被摘除，避免内存堆积
