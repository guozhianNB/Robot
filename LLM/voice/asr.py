# -*- coding: utf-8 -*-
r"""中文识别（sherpa-onnx streaming-zipformer-zh-14M）。v1 对完整语音段转写。"""
import numpy as np
import sherpa_onnx

from . import config


class StreamASR:
    def __init__(self, asr_dir=config.ASR_DIR):
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(asr_dir / "tokens.txt"),
            encoder=str(asr_dir / "encoder-epoch-99-avg-1.onnx"),
            decoder=str(asr_dir / "decoder-epoch-99-avg-1.onnx"),
            joiner=str(asr_dir / "joiner-epoch-99-avg-1.onnx"),
            num_threads=2, sample_rate=config.SAMPLE_RATE, feature_dim=80,
            decoding_method="greedy_search", provider="cpu",
        )

    def transcribe(self, samples: np.ndarray) -> str:
        s = self._recognizer.create_stream()
        s.accept_waveform(config.SAMPLE_RATE, samples)
        tail = int(0.66 * config.SAMPLE_RATE)   # 尾静音，促使 decoder 收尾
        s.accept_waveform(config.SAMPLE_RATE, np.zeros(tail, dtype=np.float32))
        s.input_finished()
        while self._recognizer.is_ready(s):
            self._recognizer.decode_stream(s)
        return self._recognizer.get_result(s).strip()
