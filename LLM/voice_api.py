# -*- coding: utf-8 -*-
r"""语音服务的挂载逻辑：启动 worker、状态查询、声纹建档。路由按 AGENTS.md 惯例放 server.py。

稳健性（系统要求，见 AGENTS.md「系统稳健性」）：
  语音链路依赖 numpy / sherpa_onnx / sounddevice / modelscope 等外部包，
  目标环境（requirement.txt 未装齐 / 无模型）缺失时，本模块必须降级：
  置 _VOICE_AVAILABLE=False 并记录 _MISSING_DEPS，所有语音能力返回"不可用"，
  绝不让导入链崩掉主程序 —— 后端必须在无语音依赖时也能正常启动。
"""
import time
from pathlib import Path

from . import db, bus, chat, log as audit

# ---------------------------------------------------------------------------
# 可选能力降级：外部依赖逐个尝试引入，收集所有缺失项。
# _VOICE_AVAILABLE = False 时，以下模块级名字（np / *_mod / voice_config）不可用，
# 所有语音函数必须先行判断 _VOICE_AVAILABLE 再使用它们。
# ---------------------------------------------------------------------------
_VOICE_AVAILABLE = True
_MISSING_DEPS = []

try:
    import numpy as np
except ImportError as _exc:
    _VOICE_AVAILABLE = False
    _MISSING_DEPS.append(str(_exc))

try:
    from .voice import worker as worker_mod, speaker as spk_mod, vad as vad_mod
    from .voice import audio as audio_mod, config as voice_config
except ImportError as _exc:
    _VOICE_AVAILABLE = False
    _MISSING_DEPS.append(str(_exc))


def _degraded_msg():
    """降级原因文案（缺失依赖列表；无缺失时给通用文案）。"""
    if _MISSING_DEPS:
        return "语音链路不可用（缺少依赖：{}）".format("; ".join(_MISSING_DEPS))
    return "语音链路不可用（模块加载失败）"

_worker = None
_recognizer = None   # 声纹实例（建档端点复用，避免重复加载）


def _chat_fn(client, model):
    def _fn(uid, text):
        settings = db.get_settings()
        parts = []
        for ev in chat.chat_stream(client, model, uid, text, "auto", settings):
            if ev["type"] == "content":
                parts.append(ev["content"])
        return "".join(parts)
    return _fn


def start_voice(client, model, post_turn_fn):
    global _worker
    if not _VOICE_AVAILABLE:
        audit.log("voice_degraded", action="start_voice", error=_degraded_msg())
        print("[WARN] " + _degraded_msg() + "，后端继续以无语音模式运行")
        return None
    if not db.get_settings().get("voice_enabled", True):
        return None
    _worker = worker_mod.VoiceWorker(_chat_fn(client, model), post_turn_fn, publish_fn=bus.publish)
    _worker.start()
    return _worker


def stop_voice():
    global _worker
    if not _VOICE_AVAILABLE:
        return
    if _worker is not None:
        _worker.stop()
        _worker = None


def get_status():
    settings = db.get_settings()
    if not _VOICE_AVAILABLE:
        return {"ok": True, "voice_enabled": settings.get("voice_enabled", True),
                "status": "unavailable", "modules": {}, "speakers": [],
                "reason": _degraded_msg()}
    if _worker is None:
        return {"ok": True, "voice_enabled": settings.get("voice_enabled", True),
                "status": "stopped", "modules": {}, "speakers": list_speakers()}
    return {"ok": True, "voice_enabled": settings.get("voice_enabled", True),
            "status": _worker.status, "modules": dict(_worker.sub_status),
            "speakers": list_speakers()}


def list_speakers():
    if not _VOICE_AVAILABLE:
        return []
    return sorted(p.stem for p in voice_config.SPEAKER_DIR.glob("*.npz"))


def enroll_speaker(uid: str, seconds: int = 15) -> dict:
    """录 seconds 秒 → VAD 切段 → 提特征平均入档。"""
    if not _VOICE_AVAILABLE:
        return {"ok": False, "uid": uid, "error": _degraded_msg()}
    global _recognizer
    if _worker is not None:
        _worker.src.stop()   # 暂停常驻采集，避免设备争用
    try:
        src = audio_mod.AudioSource()
        src.start()
        buf = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            chunk = src.read()
            if chunk is not None:
                buf.append(chunk)
        src.stop()
        samples = np.concatenate(buf) if buf else np.zeros(0, dtype=np.float32)
        if _recognizer is None:
            _recognizer = spk_mod.SpeakerRecognizer()
        v = vad_mod.VAD()
        v.accept(samples)
        v.flush()
        segs = []
        while True:
            seg = v.pop_speech()
            if seg is None:
                break
            segs.append(seg)
        _recognizer.enroll(uid, segs)
        audit.log("voice_spk", action="enroll", uid=uid, segments=len(segs))
        return {"ok": True, "uid": uid, "segments": len(segs)}
    except Exception as e:
        audit.log("voice_error", action="enroll", uid=uid, error=str(e))
        return {"ok": False, "uid": uid, "error": str(e)}
    finally:
        if _worker is not None:
            try:
                _worker.src.start()
            except Exception:
                pass
