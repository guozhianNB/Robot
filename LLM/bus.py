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
_stop = False                   # 停止标志：stop() 置 True 后 drain 循环退出

# 从 _q 取消息的**等待上限**（秒）。必须是有限值，理由见 _drain() 的注释。
POLL_SECS = 0.5


def publish(event_type: str, **payload):
    """任意线程调用，立即返回。"""
    _q.put({"type": event_type, **payload})


async def _drain():
    """把队列里的广播扇出到每个订阅连接。

    **取队列必须带超时，不能无超时阻塞**（2026-09-15 实测修复）：`run_in_executor
    (None, ...)` 用的是事件循环的**默认线程池**，而关闭事件循环时 Python 会
    `shutdown_default_executor()` 去 join 那个 worker。若 worker 永远卡在
    `_q.get()` 上，收尾就再也不返回 —— 表现是 `with TestClient(app)`（关 lifespan）
    与 uvicorn 重启/Ctrl+C 永久挂住，只能强杀。

    还要清楚：**光靠 `stop()` 置标志不够**——拨开关不会打断一个正在阻塞的调用。
    带超时后 worker 最多 `POLL_SECS` 就返回一次，`while not _stop` 才有机会生效；
    即使调用方忘了 stop()，也不会再死锁。
    """
    loop = asyncio.get_running_loop()
    while not _stop:
        try:
            # (block=True, timeout=POLL_SECS)：有消息立即返回，没消息最多等这么久
            item = await loop.run_in_executor(None, _q.get, True, POLL_SECS)
        except queue.Empty:
            continue                    # 空手而归：立刻回 while 检查 _stop，不 sleep
        except Exception:
            await asyncio.sleep(0.5)    # 真异常（非"没消息"）：退避后重试，别忙等
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
    """在应用启动时创建后台任务。

    顺手把 `_stop` 复位：`stop()` 会把它**永久**置位，而同一个进程里 lifespan 可能
    跑不止一次（连开两个 `with TestClient(app)`、uvicorn 的进程内重启）。不复位的话
    第二次的 drain 会立刻自杀 —— 表现是提醒/报警/切换用户全都推不出去（静默失效，
    比报错更难查）。启动与停止必须成对。
    """
    global _stop
    _stop = False
    task = asyncio.create_task(_drain())
    return task


def stop():
    """停止扇出任务（置位标志，drain 循环退出）。"""
    global _stop
    _stop = True
