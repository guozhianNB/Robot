# -*- coding: utf-8 -*-
r"""关键词唤醒（sherpa-onnx kws-zipformer 中文）。"""
from pathlib import Path

import numpy as np
import sherpa_onnx

from . import config


class WakeWordDetector:
    def __init__(self, kws_dir=config.KWS_DIR, keywords_file=None):
        if keywords_file is None:
            keywords_file = Path(__file__).with_name("kws_keywords.txt")
        self._kws = sherpa_onnx.KeywordSpotter(
            tokens=str(kws_dir / "tokens.txt"),
            encoder=str(kws_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            decoder=str(kws_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            joiner=str(kws_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"),
            keywords_file=str(keywords_file),
            num_threads=2, provider="cpu",
            keywords_threshold=config.KWS_THRESHOLD,
        )
        self._stream = self._kws.create_stream()

    def accept(self, samples: np.ndarray) -> str | None:
        """喂 PCM；命中返回关键词字符串，未命中返回 None。"""
        self._stream.accept_waveform(config.SAMPLE_RATE, samples)
        hit = None
        while self._kws.is_ready(self._stream):
            self._kws.decode_stream(self._stream)
            r = self._kws.get_result(self._stream)
            if r != "":
                hit = r
                self._kws.reset_stream(self._stream)
        return hit
