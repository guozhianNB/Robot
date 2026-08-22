# -*- coding: utf-8 -*-
r"""语音服务的挂载逻辑：启动 worker、状态查询、声纹建档。路由按 AGENTS.md 惯例放 server.py。"""
import time
from pathlib import Path

import numpy as np

from . import db, bus, chat, log as audit
from .voice import worker as worker_mod, speaker as spk_mod, vad as vad_mod
from .voice import audio as audio_mod, config as voice_config

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
    if not db.get_settings().get("voice_enabled", True):
        return None
    _worker = worker_mod.VoiceWorker(_chat_fn(client, model), post_turn_fn, publish_fn=bus.publish)
    _worker.start()
    return _worker


def stop_voice():
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None


def get_status():
    settings = db.get_settings()
    if _worker is None:
        return {"ok": True, "voice_enabled": settings.get("voice_enabled", True),
                "status": "stopped", "modules": {}, "speakers": list_speakers()}
    return {"ok": True, "voice_enabled": settings.get("voice_enabled", True),
            "status": _worker.status, "modules": dict(_worker.sub_status),
            "speakers": list_speakers()}


def list_speakers():
    return sorted(p.stem for p in voice_config.SPEAKER_DIR.glob("*.npz"))


def enroll_speaker(uid: str, seconds: int = 15) -> dict:
    """录 seconds 秒 → VAD 切段 → 提特征平均入档。"""
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
    finally:
        if _worker is not None:
            try:
                _worker.src.start()
            except Exception:
                pass
