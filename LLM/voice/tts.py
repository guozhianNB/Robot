# -*- coding: utf-8 -*-
r"""中文 TTS（sherpa-onnx vits-zh-ll，16k 输出）。"""
import numpy as np
import sherpa_onnx

from . import config


class TTS:
    def __init__(self, tts_dir=config.TTS_DIR, sid=2):
        tts_config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(tts_dir / "model.onnx"),
                    lexicon=str(tts_dir / "lexicon.txt"),
                    tokens=str(tts_dir / "tokens.txt"),
                ),
                provider="cpu", num_threads=2,
            ),
            rule_fsts=",".join(
                str(tts_dir / f) for f in ("phone.fst", "date.fst", "number.fst")),
            max_num_sentences=1,
        )
        assert tts_config.validate()
        self._tts = sherpa_onnx.OfflineTts(tts_config)
        self._sid = sid

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """返回 (float32 1-D samples, sample_rate)。"""
        gen = sherpa_onnx.GenerationConfig()
        gen.sid = self._sid
        gen.speed = 1.0
        audio = self._tts.generate(text, gen)
        return np.asarray(audio.samples, dtype=np.float32), int(audio.sample_rate)
