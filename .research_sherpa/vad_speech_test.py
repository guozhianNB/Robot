# -*- coding: utf-8 -*-
import os
import wave
import numpy as np
import sherpa_onnx

base = os.path.dirname(os.path.abspath(__file__))
sr = 16000
cfg = sherpa_onnx.VadModelConfig()
cfg.silero_vad.model = os.path.join(base, "silero_vad.onnx")
cfg.sample_rate = sr
cfg.silero_vad.threshold = 0.5
cfg.silero_vad.min_silence_duration = 0.25
cfg.silero_vad.min_speech_duration = 0.25

with wave.open(os.path.join(base, "speech_0.wav"), "rb") as f:
    assert f.getframerate() == sr
    data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768
print("wav dur(s):", len(data) / sr)

vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
ws = cfg.silero_vad.window_size
for i in range(0, len(data), ws):
    vad.accept_waveform(data[i:i + ws])
vad.flush()
print("speech_detected:", vad.is_speech_detected(), "empty:", vad.empty())
n = 0
while not vad.empty():
    seg = vad.front
    print(f"seg{n}: start={seg.start/sr:.3f}s end={(seg.start+len(seg.samples))/sr:.3f}s "
          f"dur={len(seg.samples)/sr:.3f}s dtype={seg.samples.dtype} shape={seg.samples.shape}")
    n += 1
    vad.pop()
print("total segments:", n)
