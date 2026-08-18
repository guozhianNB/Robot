# -*- coding: utf-8 -*-
r"""
事件总线（SSE 广播通道）：
  - 定时提醒触发 / 未确认升级等事件，由提醒调度线程（或任何线程）推入 bus，
    一个 asyncio 任务负责把它扇出到所有前端 SSE 订阅连接（GET /api/events）。
  - 提醒调度独立于对话进程：对话再卡，提醒照样触发、照样广播。
"""
import asyncio
import json
import queue
import threading

_q = queue.Queue()          # 线程安全：任意线程 publish()
_subscribers = set()        # 事件循环侧：asyncio.Queue 集合


def publish(event_type: str, **payload):
    """任意线程调用，立即返回。"""
    _q.put({"type": event_type, **payload})


async def _drain():
    """把队列里的广播扇出到每个订阅连接。"""
    loop = asyncio.get_running_loop()
    while True:
        try:
            item = await loop.run_in_executor(None, _q.get)  # 阻塞取（线程安全）
        except Exception:
            await asyncio.sleep(0.5)
            continue
        dead = []
        for q in list(_subscribers):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            _subscribers.discard(q)


async def stream_events():
    """SSE 生成器：给每个前端连接一个独立队列。"""
    loop = asyncio.get_running_loop()
    q = asyncio.Queue(maxsize=64)
    _subscribers.add(q)
    try:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=15)
                yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"   # 注释行，防代理掐断
    finally:
        _subscribers.discard(q)


def start_drain():
    """在应用启动时创建后台任务。"""
    task = asyncio.create_task(_drain())
    return task
