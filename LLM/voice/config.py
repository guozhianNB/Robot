# -*- coding: utf-8 -*-
r"""语音链路专属配置：模型路径、音频/VAD/KWS 参数。纯常量。

云端流式 ASR（火山引擎「豆包语音」控制台）：
  凭据放项目根 .env（server.py load_dotenv 加载），因加载时机在模块 import 之后，
  这里用读取函数（延迟到调用时 os.getenv），不要做成 import 期常量。"""
import os

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
SPK_THRESHOLD = 0.40          # 声纹余弦阈值（实测校准：真人 0.46-0.47 / 异人 0.14，取 0.40 留余量；运行时可被 settings 覆盖）

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

# ---- 流式 ASR（worker 编排用）----
# VAD 判定"开始说话"需要一小段窗口，起会话时回补这段前沿音频，避免丢句首字
ASR_ONSET_TAIL_S = 0.5


# ---- 云端 ASR 环境变量（延迟读取，见模块 docstring）----
def cloud_asr_ws():
    """SAUC 双向流式端点；如开通/使用的是流式1.0，可改配 volc.bigasr 资源。"""
    return os.getenv("VOLC_ASR_WS_URL") or "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"


def cloud_asr_api_key():
    return os.getenv("VOLC_ASR_API_KEY") or ""


def cloud_asr_resource_id():
    """流式识别2.0(Seed-ASR)=volc.seedasr.sauc.duration；1.0=volc.bigasr.sauc.duration"""
    return os.getenv("VOLC_ASR_RESOURCE_ID") or "volc.seedasr.sauc.duration"
