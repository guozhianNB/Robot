# -*- coding: utf-8 -*-
r"""中文 TTS（sherpa-onnx vits-zh-ll，16k 输出）。

播报文本清洗：LLM 回复常带 markdown（**加粗**、`、#）、URL、emoji、半角括号引号等，
sherpa zh 词表（lexicon）没有这些符号，会逐字刷 "Ignore OOV" 日志且朗读错乱；
清洗后再合成（2026-09-07）。"""
import re

import numpy as np
import sherpa_onnx

from . import config

# 去掉 markdown/装饰符号与半角格式符号（不读）；保留中文句读与基础半角标点
_BAD_CHARS = re.compile(r"[*_#~\[\]{}()<>|\\/^$+=@%&`\"']")
# URL / HTML 标签
_URL = re.compile(r"(?:https?|ftp)://\S+|www\.\S+|<[^>]+>")
# 常见 emoji/装饰符区段（宽字符）
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF\U00002B00-\U00002BFF]")
# 控制字符（保留 \n）
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 全角引号→中文引号规整（词表通常只含中文引号）
_QUOTES = str.maketrans({"“": "\u201c", "”": "\u201d", "‘": "\u2018", "’": "\u2019"})


def sanitize_tts_text(text: str) -> str:
    """播报前清洗：去 URL/HTML/markdown 装饰/emoji/控制符，规整空白与换行。"""
    if not text:
        return ""
    t = text.translate(_QUOTES)
    t = _URL.sub(" ", t)
    t = _BAD_CHARS.sub("", t)
    t = _EMOJI.sub("", t)
    t = _CTRL.sub("", t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


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
        """返回 (float32 1-D samples, sample_rate)。清洗后无内容则返回空样本（长度 0）。"""
        clean = sanitize_tts_text(text)
        if not clean:
            return np.zeros(0, dtype=np.float32), config.SAMPLE_RATE
        gen = sherpa_onnx.GenerationConfig()
        gen.sid = self._sid
        gen.speed = 1.0
        audio = self._tts.generate(clean, gen)
        return np.asarray(audio.samples, dtype=np.float32), int(audio.sample_rate)

    @property
    def sample_rate(self):
        return config.SAMPLE_RATE

    def synthesize_chunks(self, text):
        """流式合成接口：本地离线模型一次生成，yield 整段（句级即粒度）。"""
        samples, _ = self.synthesize(text)
        if len(samples) > 0:
            yield samples
