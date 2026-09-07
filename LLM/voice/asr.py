# -*- coding: utf-8 -*-
r"""本地中文识别（sherpa-onnx streaming-zipformer-zh-14M），流式接口。

worker 编排约定（本地与云端引擎共用，见 asr_cloud.py）：
  start_session()         一句话开始（重置内部流）
  accept(samples) -> str  喂一块音频，返回当前 partial 文本（可能为空串，未变化时与上次相同）
  finish() -> str         一句话输入结束 → 整句最终文本
  abort()                 放弃当前句（重置，不产出）
  close()                 释放资源（云端断开连接）

旧式整段一次性转写保留为 transcribe(samples)，供"无流式会话的 VAD 整段"兜底路径使用。
"""
import numpy as np
import sherpa_onnx

from . import config

_TAIL_S = 0.66   # finish/transcribe 时补尾静音，促使 decoder 收尾


class StreamASR:
    """本地流式识别引擎（sherpa online recognizer 增量解码即 partial）。"""

    provider = "local"

    def __init__(self, asr_dir=config.ASR_DIR):
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(asr_dir / "tokens.txt"),
            encoder=str(asr_dir / "encoder-epoch-99-avg-1.onnx"),
            decoder=str(asr_dir / "decoder-epoch-99-avg-1.onnx"),
            joiner=str(asr_dir / "joiner-epoch-99-avg-1.onnx"),
            num_threads=2, sample_rate=config.SAMPLE_RATE, feature_dim=80,
            decoding_method="greedy_search", provider="cpu",
        )
        self._stream = None

    # ---- 流式接口 ----
    def start_session(self):
        self._stream = self._recognizer.create_stream()

    def accept(self, samples: np.ndarray) -> str:
        if self._stream is None:
            self._stream = self._recognizer.create_stream()
        self._stream.accept_waveform(config.SAMPLE_RATE, samples)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        return self._recognizer.get_result(self._stream).strip()

    def finish(self) -> str:
        if self._stream is None:
            return ""
        tail = int(_TAIL_S * config.SAMPLE_RATE)
        self._stream.accept_waveform(config.SAMPLE_RATE, np.zeros(tail, dtype=np.float32))
        self._stream.input_finished()
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        text = self._recognizer.get_result(self._stream).strip()
        self._stream = None
        return text

    def abort(self):
        self._stream = None

    def close(self):
        self._stream = None

    # ---- 兼容：整段一次性转写 ----
    def transcribe(self, samples: np.ndarray) -> str:
        s = self._recognizer.create_stream()
        s.accept_waveform(config.SAMPLE_RATE, samples)
        tail = int(_TAIL_S * config.SAMPLE_RATE)
        s.accept_waveform(config.SAMPLE_RATE, np.zeros(tail, dtype=np.float32))
        s.input_finished()
        while self._recognizer.is_ready(s):
            self._recognizer.decode_stream(s)
        return self._recognizer.get_result(s).strip()
