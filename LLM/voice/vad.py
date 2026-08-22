# -*- coding: utf-8 -*-
r"""VAD 封装（sherpa-onnx silero-vad）：喂 PCM，弹完整语音段。"""
import numpy as np
import sherpa_onnx

from . import config


class VAD:
    def __init__(self, model_path=config.VAD_MODEL,
                 threshold=config.VAD_THRESHOLD,
                 min_silence=config.VAD_MIN_SILENCE_S,
                 min_speech=config.VAD_MIN_SPEECH_S,
                 max_speech=config.VAD_MAX_SPEECH_S):
        cfg = sherpa_onnx.VadModelConfig()
        cfg.sample_rate = config.SAMPLE_RATE
        cfg.silero_vad.model = str(model_path)
        cfg.silero_vad.threshold = threshold
        cfg.silero_vad.min_silence_duration = min_silence
        cfg.silero_vad.min_speech_duration = min_speech
        cfg.silero_vad.max_speech_duration = max_speech
        self._vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
        self._ws = cfg.silero_vad.window_size

    def accept(self, samples: np.ndarray):
        for i in range(0, len(samples), self._ws):
            self._vad.accept_waveform(samples[i:i + self._ws])

    def pop_speech(self) -> np.ndarray | None:
        """有完整语音段则返回其 PCM（float32 1-D），否则 None。"""
        if self._vad.empty():
            return None
        seg = self._vad.front
        pcm = np.asarray(seg.samples, dtype=np.float32)
        self._vad.pop()
        return pcm

    def is_speech_now(self) -> bool:
        return self._vad.is_speech_detected()

    def flush(self):
        self._vad.flush()

    def reset(self):
        self._vad.reset()
