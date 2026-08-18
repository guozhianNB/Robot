# -*- coding: utf-8 -*-
r"""
审计日志：对话 / 记忆改动 / 提醒事件 / 工具调用 / 设置变更，全部 JSON Lines 落盘。
安全红线"日志与审计（可追溯）"的执行出口。
"""
import json
import threading
from datetime import datetime

from .conf import AUDIT_LOG

_lock = threading.Lock()


def log(event: str, **fields):
    """event: chat / memory_change / reminder / tool / settings / alarm 等。"""
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        **fields,
    }
    with _lock:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
