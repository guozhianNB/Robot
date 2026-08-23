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


def read_warnings(limit: int = 50) -> list[dict]:
    """读 audit.jsonl 末尾 limit 条，过滤警告/错误类事件（*_error / alarm / voice_degraded / *warn* / level 含 warn / 含 error 字段）。

    命中任一条规则即视为警告/错误：action 或 event 含 "error"；event == "alarm"；
    level 含 "warn"；event == "voice_degraded"；存在 error 字段。
    文件不存在 → 空列表；单条 JSON 解析失败 → 跳过。
    """
    out = []
    try:
        with open(AUDIT_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue   # 坏行跳过
                if _is_warning(rec):
                    out.append(rec)
    except FileNotFoundError:
        return []
    return out[-limit:] if limit > 0 else out


def _is_warning(rec: dict) -> bool:
    action = str(rec.get("action") or "")
    event = str(rec.get("event") or "")
    level = str(rec.get("level") or "")
    if "error" in action or "error" in event:
        return True
    if event == "alarm" or "warn" in level:
        return True
    if event == "voice_degraded":
        return True
    if "error" in rec:
        return True
    return False
