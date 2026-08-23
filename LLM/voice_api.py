# -*- coding: utf-8 -*-
r"""语音服务的挂载逻辑：启动 worker、状态查询、声纹建档。路由按 AGENTS.md 惯例放 server.py。

稳健性（系统要求，见 AGENTS.md「系统稳健性」）：
  语音链路依赖 numpy / sherpa_onnx / sounddevice / modelscope 等外部包，
  目标环境（requirement.txt 未装齐 / 无模型）缺失时，本模块必须降级：
  置 _VOICE_AVAILABLE=False 并记录 _MISSING_DEPS，所有语音能力返回"不可用"，
  绝不让导入链崩掉主程序 —— 后端必须在无语音依赖时也能正常启动。
"""
import io
import time
import uuid
import wave
from pathlib import Path

from . import db, bus, chat, log as audit
from .conf import VOICE_PENDING_TTL_S

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

_pending = {}   # recording_id -> {"emb", "segments", "wav", "ts"}（录制暂存，TTL 后清理）


def _wav_bytes(samples: np.ndarray) -> bytes:
    """float32 16k 单声道 → wav bytes（16bit PCM），供试听。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(voice_config.SAMPLE_RATE)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def _cleanup_pending():
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v["ts"] > VOICE_PENDING_TTL_S]:
        _pending.pop(k, None)


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


def record_speaker(seconds: int = 15) -> dict:
    """录 seconds 秒 → VAD 切段 → 提特征，暂存内存（特征+音频），返回 recording_id。
    不落档；由 commit_speaker 提交入档，discard_recording 丢弃。"""
    if not _VOICE_AVAILABLE:
        return {"ok": False, "error": _degraded_msg()}
    global _recognizer
    if _worker is not None:
        _worker.src.stop()
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
        embs = [_recognizer.embed(s) for s in segs if len(s) >= voice_config.SAMPLE_RATE]
        if not embs:
            return {"ok": False, "error": "没有检测到有效语音段，请靠近麦克风再说一遍"}
        emb = np.mean(embs, axis=0).astype(np.float32)
        rid = uuid.uuid4().hex
        _cleanup_pending()
        _pending[rid] = {"emb": emb, "segments": len(segs),
                         "wav": _wav_bytes(samples), "ts": time.time()}
        audit.log("voice_spk", action="record", rid=rid, segments=len(segs))
        return {"ok": True, "recording_id": rid, "segments": len(segs)}
    except Exception as e:
        audit.log("voice_error", action="record", error=str(e))
        return {"ok": False, "error": str(e)}
    finally:
        if _worker is not None:
            try:
                _worker.src.start()
            except Exception:
                pass


def commit_speaker(recording_id: str, uid: str, append: bool = True) -> dict:
    """把暂存特征入档（append=True 与已有档案合并平均），入档后清除暂存。"""
    if not _VOICE_AVAILABLE:
        return {"ok": False, "uid": uid, "error": _degraded_msg()}
    item = _pending.pop(recording_id, None)
    if item is None:
        return {"ok": False, "uid": uid, "error": "录音已过期或不存在，请重新录制"}
    try:
        global _recognizer
        if _recognizer is None:
            _recognizer = spk_mod.SpeakerRecognizer()
        _recognizer.enroll_embedding(uid, item["emb"], append=append)
        audit.log("voice_spk", action="commit", uid=uid, append=append,
                  segments=item["segments"])
        return {"ok": True, "uid": uid, "samples": _recognizer.sample_count(uid)}
    except Exception as e:
        audit.log("voice_error", action="commit", uid=uid, error=str(e))
        return {"ok": False, "uid": uid, "error": str(e)}


def discard_recording(recording_id: str) -> dict:
    """丢弃暂存（幂等）。"""
    _pending.pop(recording_id, None)
    return {"ok": True}


def get_recording_audio(recording_id: str):
    """返回 (wav_bytes, "audio/wav")；不存在返回 None（试听用）。"""
    item = _pending.get(recording_id)
    if item is None:
        return None
    return item["wav"], "audio/wav"


def delete_speaker(uid: str) -> dict:
    """清除该 uid 的声纹档案（不影响老人基本信息档案）。"""
    if not _VOICE_AVAILABLE:
        return {"ok": False, "uid": uid, "error": _degraded_msg()}
    try:
        global _recognizer
        if _recognizer is None:
            _recognizer = spk_mod.SpeakerRecognizer()
        _recognizer.delete(uid)
        audit.log("voice_spk", action="delete", uid=uid, by="nurse")
        return {"ok": True, "uid": uid}
    except Exception as e:
        audit.log("voice_error", action="delete", uid=uid, error=str(e))
        return {"ok": False, "uid": uid, "error": str(e)}


def list_speaker_details() -> dict:
    """{uid: {"samples": n}}，前端渲染样本数用。语音不可用时返回空。"""
    if not _VOICE_AVAILABLE:
        return {}
    global _recognizer
    if _recognizer is None:
        try:
            _recognizer = spk_mod.SpeakerRecognizer()
        except Exception:
            return {}
    return {uid: {"samples": _recognizer.sample_count(uid)}
            for uid in _recognizer.list_profiles()}


def enroll_speaker(uid: str, seconds: int = 15) -> dict:
    """旧行为兼容：录 seconds 秒并覆盖建档（等效 record + commit append=False）。"""
    res = record_speaker(seconds)
    if not res.get("ok"):
        return {"ok": False, "uid": uid, "error": res.get("error", "录制失败")}
    return commit_speaker(res["recording_id"], uid, append=False)
