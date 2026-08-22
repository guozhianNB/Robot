# -*- coding: utf-8 -*-
r"""语音链路专属配置：模型路径、音频/VAD/KWS 参数。纯常量。"""
from ..conf import BASE_DIR, DATA_DIR

# 目录
MODEL_DIR = BASE_DIR / "LLM" / "models" / "voice"
SPEAKER_DIR = DATA_DIR / "speakers"
SPEAKER_DIR.mkdir(parents=True, exist_ok=True)

# 音频
SAMPLE_RATE = 16000
BLOCK_MS = 100
BLOCK_SAMPLES = SAMPLE_RATE * BLOCK_MS // 1000   # 1600 样本/块

# sherpa-onnx 模型路径（目录名 = 解压产物名，勿改）
VAD_MODEL = MODEL_DIR / "silero_vad.onnx"
KWS_DIR = MODEL_DIR / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
ASR_DIR = MODEL_DIR / "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
TTS_DIR = MODEL_DIR / "sherpa-onnx-vits-zh-ll"

# 声纹（3D-Speaker ERes2NetV2，经 modelscope）
SPK_MODEL_ID = "iic/speech_eres2netv2_sv_zh-cn_16k-common"
SPK_EMBED_DIM = 192
SPK_THRESHOLD = 0.55          # 官方基线 0.360，精准优先上调；运行时可被 settings 覆盖

# VAD（silero）
VAD_THRESHOLD = 0.5
VAD_MIN_SILENCE_S = 0.5
VAD_MIN_SPEECH_S = 0.25
VAD_MAX_SPEECH_S = 20.0

# 唤醒（kws）
KWS_THRESHOLD = 0.25

# 打断：播报开始后忽略 VAD 的宽限时长（防开场误判）
BARGE_IN_GRACE_S = 0.3
# 音频设备掉线：连续失败多少次判定为 degraded（仍持续重试，不退出线程）
MAX_RECONNECT = 5
