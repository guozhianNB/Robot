# -*- coding: utf-8 -*-
import os
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
print("window_size:", cfg.silero_vad.window_size)

sil = np.zeros(int(0.8 * sr), dtype=np.float32)
tone = (0.5 * np.sin(2 * np.pi * 440 * np.arange(int(1.2 * sr)) / sr)).astype(np.float32)
audio = np.concatenate([sil, tone, sil])
print("audio len:", len(audio), "dur:", len(audio) / sr)

# mode A: single call
vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
vad.accept_waveform(audio)
vad.flush()
print("modeA speech_detected:", vad.is_speech_detected(), "empty:", vad.empty())
while not vad.empty():
    seg = vad.front
    print("modeA seg start(s):", seg.start / sr, "dur(s):", len(seg.samples) / sr)
    vad.pop()

# mode B: chunked feed like official example, then flush
vad2 = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
ws = cfg.silero_vad.window_size
n_chunks = 0
for i in range(0, len(audio), ws):
    vad2.accept_waveform(audio[i:i + ws])
    n_chunks += 1
vad2.flush()
print("modeB chunks:", n_chunks, "speech_detected:", vad2.is_speech_detected(), "empty:", vad2.empty())
while not vad2.empty():
    seg = vad2.front
    print("modeB seg start(s):", seg.start / sr, "dur(s):", len(seg.samples) / sr)
    vad2.pop()
