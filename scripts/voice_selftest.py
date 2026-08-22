# -*- coding: utf-8 -*-
r"""离线自检：不依赖麦克风，验证 5 类能力可加载 + TTS→ASR 往返 + 声纹 embedding 形状。
用法：.venv\Scripts\python.exe scripts\voice_selftest.py
（Linux 下：.venv/bin/python scripts/voice_selftest.py）
"""
import sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from LLM.voice import config
from LLM.voice import tts as tts_mod, asr as asr_mod, vad as vad_mod, kws as kws_mod, speaker as spk_mod


def main():
    print("1/5 VAD 加载 ...")
    v = vad_mod.VAD()
    v.accept(np.zeros(config.SAMPLE_RATE, dtype=np.float32))
    print("   VAD ok")

    print("2/5 KWS 加载 ...")
    k = kws_mod.WakeWordDetector()
    assert k.accept(np.zeros(config.BLOCK_SAMPLES, dtype=np.float32)) is None
    print("   KWS ok")

    print("3/5 TTS 合成 ...")
    t = tts_mod.TTS()
    samples, sr = t.synthesize("你好，世界")
    assert samples.size > 0 and sr == config.SAMPLE_RATE
    print(f"   TTS ok: {samples.size} samples @ {sr}Hz")

    print("4/5 ASR 转写（TTS 往返）...")
    a = asr_mod.StreamASR()
    print(f"   ASR ok: {a.transcribe(samples)!r}")

    print("5/5 声纹 embedding ...")
    s = spk_mod.SpeakerRecognizer()
    emb = s.embed(samples)
    assert emb.shape == (config.SPK_EMBED_DIM,)
    print(f"   Speaker ok: embedding shape {emb.shape}")

    print("ALL OK")


if __name__ == "__main__":
    main()
