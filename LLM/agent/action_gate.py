# -*- coding: utf-8 -*-
r"""对话与 Plan 共用的车控动作所有权闸门。"""

from __future__ import annotations

from dataclasses import dataclass
import threading


@dataclass(frozen=True)
class GateToken:
    """一次成功 claim 的不可变凭证。"""

    owner: str
    ref: str
    generation: int


_lock = threading.RLock()
_owner: str | None = None
_ref: str | None = None
_generation = 0


def claim(owner: str, ref: str) -> GateToken | None:
    """占用动作槽；已有 owner 时返回 ``None``。"""
    if not isinstance(owner, str) or not owner or not isinstance(ref, str) or not ref:
        return None
    global _owner, _ref, _generation
    with _lock:
        if _owner is not None:
            return None
        _generation += 1
        _owner, _ref = owner, ref
        return GateToken(owner, ref, _generation)


def release(token: GateToken | None) -> bool:
    """释放当前 token；generation/owner/ref 任一不匹配都拒绝。"""
    global _owner, _ref
    if not isinstance(token, GateToken):
        return False
    with _lock:
        if (_owner, _ref, _generation) != (token.owner, token.ref, token.generation):
            return False
        _owner = _ref = None
        return True


def snapshot() -> dict:
    """返回当前所有权快照，供调度器和测试读取。"""
    with _lock:
        return {"owner": _owner, "ref": _ref, "generation": _generation}
