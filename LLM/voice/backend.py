# -*- coding: utf-8 -*-
r"""推理后端抽象：BPU 优先、CPU 兜底、自动检测（RDK X5 征程6E）。

Windows 开发机恒为 cpu；板卡上探测 hobot_dnn 可用则 bpu。
检测优先级：env VOICE_BACKEND(bpu|cpu|auto) > 探测 hobot_dnn > 默认 cpu。
板卡阶段：实现 BPU runner 遵守 InferenceBackend/ModelRunner 契约，见
docs/superpowers/specs/2026-08-22-voice-bpu-acceleration-goal.md。"""
import os
import threading

# 当前认定可上 BPU 的模型键（板卡实测后可增删）
BPU_SUPPORTED = {"speaker_eres2netv2"}

_available = None
_lock = threading.Lock()


def reset_backend():
    """清缓存（测试用；也用于运行时重新探测）。"""
    global _available
    _available = None


def detect_backend() -> str:
    """返回 'bpu' 或 'cpu'，结果缓存。"""
    global _available
    if _available is not None:
        return _available
    with _lock:
        if _available is not None:
            return _available
        forced = os.environ.get("VOICE_BACKEND", "").strip().lower()
        if forced in ("bpu", "cpu"):
            _available = forced
        else:
            _available = "bpu" if _probe_bpu() else "cpu"
        return _available


def _probe_bpu() -> bool:
    try:
        import hobot_dnn  # noqa: F401  板卡工具链；本机无则抛 ImportError
        return True
    except Exception:
        return False


def resolve_backend(model_key: str, requested: str = "auto") -> str:
    """按模型解析实际后端：'bpu' 仅当模型在支持表、探测到 BPU、且未被强制 cpu。"""
    if requested == "cpu":
        return "cpu"
    if model_key in BPU_SUPPORTED and detect_backend() == "bpu":
        return "bpu"
    return "cpu"


# ---- 板卡阶段需实现的后端契约（本期仅为文档化占位，不实例化）----
class InferenceBackend:
    name = "cpu"

    def load(self, model_key, model_spec):
        raise NotImplementedError


class ModelRunner:
    def run(self, **inputs):
        raise NotImplementedError
