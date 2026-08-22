# -*- coding: utf-8 -*-
"""Runtime verification of sherpa-onnx 1.13.6 Python API (installed from PyPI)."""
import inspect
import numpy as np
import sherpa_onnx

print("sherpa-onnx version:", sherpa_onnx.version)
print("onnxruntime:", sherpa_onnx.onnxruntime_version)


def show(name, obj):
    try:
        sig = inspect.signature(obj)
        print(f"\n== {name} ==\n{sig}")
    except (ValueError, TypeError) as e:
        print(f"\n== {name} == (no signature: {e})")
        print("  attrs:", [a for a in dir(obj) if not a.startswith("__")][:40])


show("VadModelConfig", sherpa_onnx.VadModelConfig)
show("SileroVadModelConfig", sherpa_onnx.SileroVadModelConfig)
show("VoiceActivityDetector", sherpa_onnx.VoiceActivityDetector)
show("KeywordSpotter", sherpa_onnx.KeywordSpotter)
show("OnlineRecognizer.from_transducer", sherpa_onnx.OnlineRecognizer.from_transducer)
show("OnlineRecognizer.from_paraformer", sherpa_onnx.OnlineRecognizer.from_paraformer)
show("OnlineRecognizer.from_zipformer2_ctc", sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc)
show("OfflineTtsConfig", sherpa_onnx.OfflineTtsConfig)
show("OfflineTtsModelConfig", sherpa_onnx.OfflineTtsModelConfig)
show("OfflineTtsVitsModelConfig", sherpa_onnx.OfflineTtsVitsModelConfig)
show("OfflineTts", sherpa_onnx.OfflineTts)
show("GenerationConfig", sherpa_onnx.GenerationConfig)
show("SpeechSegment", sherpa_onnx.SpeechSegment)

# ---- construct configs (no model files needed for construction of configs) ----
import os
cfg = sherpa_onnx.VadModelConfig()
cfg.silero_vad.model = os.path.join(os.path.dirname(os.path.abspath(__file__)), "silero_vad.onnx")
cfg.sample_rate = 16000
print("\nVadModelConfig constructed; window_size =", cfg.silero_vad.window_size)

# ---- REAL VAD RUN ----
sr = 16000
ws_cfg = cfg.silero_vad.window_size
cfg.silero_vad.min_silence_duration = 0.25
cfg.silero_vad.min_speech_duration = 0.25
t_sil = np.zeros(int(0.8 * sr), dtype=np.float32)
t_one = int(1.2 * sr)
tone = (0.5 * np.sin(2 * np.pi * 440 * np.arange(t_one) / sr)).astype(np.float32)
audio = np.concatenate([t_sil, tone, t_sil])

vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
ws = cfg.silero_vad.window_size
for i in range(0, len(audio), ws):
    vad.accept_waveform(audio[i:i + ws])
vad.flush()

print("\n-- VAD segments (start_s, end_s, dur_s, samples_dtype, samples_shape) --")
n = 0
while not vad.empty():
    seg = vad.front
    start_s = seg.start / sr
    end_s = (seg.start + len(seg.samples)) / sr
    print(f"seg{n}: start={start_s:.3f}s end={end_s:.3f}s dur={len(seg.samples)/sr:.3f}s "
          f"dtype={seg.samples.dtype} shape={seg.samples.shape}")
    n += 1
    vad.pop()
print("total segments:", n)

# ---- check write_wave helper exists ----
print("\nwrite_wave:", callable(sherpa_onnx.write_wave))
