# -*- coding: utf-8 -*-
"""系统退出相关停止钩子测试（reminder / bus）。"""
import threading

from LLM import reminder, bus


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
