# -*- coding: utf-8 -*-
r"""云端中文 TTS：火山豆包语音 2.0 HTTP 单向流式（音频分片 NDJSON 返回）。

协议（蓝本 = GizClaw/doubao-speech-go tts_v2.go，2026-09-07 已核对；端点/鉴权以
docs/2.pre/tts2_probe.py 实测为准）：
  - POST {endpoint}/api/v3/tts/unidirectional，JSON body：
      {"user":{"uid":...}, "req_params": {"text":..., "speaker":..., "audio_params":
      {"format":"pcm","sample_rate":16000}}}
  - header：X-Api-Key（豆包语音控制台 UUID Key，同云端 ASR）+ X-Api-Resource-Id=seed-tts-2.0
  - 响应 = NDJSON：每行 {"code":0,"message":...,"done":bool,"data":"<base64 PCM16 分片>"}
    成功码 0 / 20000000；done=true 收尾；非零 code 报错。
  - 本实现按句请求（每句一个短请求，首音最快；不用双向 WS 文本增量流，YAGNI）。

稳健性：requests 缺失时仍可导入（_REQ_OK=False），实例化时抛 RuntimeError 说明，
由 worker 启动降级处理 —— 不污染后端导入链。输出恒为 16k 单声道 float32，与本地/声卡一致。"""
import json

import numpy as np

from . import config

try:
    import requests
    _REQ_OK = True
except Exception:      # 缺依赖：仅云端 TTS 不可用，实例化时给出明确提示
    requests = None
    _REQ_OK = False

_OK_CODES = (0, 20000000)
_FALLBACK_SAMPLE_RATE = 16000


def build_request_body(text: str, speaker: str, sample_rate: int = 16000,
                       uid: str = "robot-voice-tts") -> dict:
    """构造 POST body（纯函数，便于单测）。"""
    return {
        "user": {"uid": uid},
        "req_params": {
            "text": text,
            "speaker": speaker,
            "audio_params": {"format": "pcm", "sample_rate": sample_rate},
        },
    }


def parse_stream_line(raw) -> dict:
    """解析一行 NDJSON → dict(audio=bytes|None, done=bool, error=str|None)；坏行返回 None。

    data 为 base64(PCM16) 分片；成功码 0/20000000，其余按错误返回 message。"""
    if not isinstance(raw, (str, bytes, bytearray)):
        return None
    line = raw.decode("utf-8", "ignore").strip() if isinstance(raw, (bytes, bytearray)) else raw.strip()
    if not line:
        return None
    try:
        j = json.loads(line)
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    code = j.get("code", 0)
    if code not in _OK_CODES:
        return {"audio": None, "done": False,
                "error": j.get("message") or j.get("msg") or ("错误码 " + str(code))}
    audio = None
    data = j.get("data")
    if data:
        try:
            audio = __import__("base64").b64decode(data)
        except Exception:
            audio = None
    return {"audio": audio, "done": bool(j.get("done")), "error": None}


class CloudStreamTTS:
    """火山豆包 TTS 2.0 引擎：句级请求，NDJSON 分片流式合成。"""

    provider = "cloud"
    sample_rate = _FALLBACK_SAMPLE_RATE

    def __init__(self):
        if not _REQ_OK:
            raise RuntimeError("云端 TTS 不可用：缺少 requests 依赖")
        if not config.cloud_tts_api_key():
            raise RuntimeError("云端 TTS 不可用：未配置 API Key（VOLC_TTS_API_KEY，"
                               "缺省复用 VOLC_ASR_API_KEY，豆包语音控制台 → API 调用 → 创建 API Key）")
        if not config.cloud_tts_speaker():
            raise RuntimeError("云端 TTS 不可用：未配置音色 VOLC_TTS_SPEAKER"
                               "（豆包语音控制台 → 音色库，形如 zh_female_xxx_bigtts）")
        self._endpoint = config.cloud_tts_endpoint()
        self._key = config.cloud_tts_api_key()
        self._speaker = config.cloud_tts_speaker()
        self._resource = config.cloud_tts_resource_id()

    def synthesize_chunks(self, text):
        """句级请求；逐 NDJSON 分片 yield float32 16k 样本。空文本 yield 空。"""
        clean = text.strip()
        if not clean:
            return
        import base64
        body = build_request_body(clean, self._speaker, self.sample_rate)
        hdrs = {"Content-Type": "application/json",
                "X-Api-Key": self._key,
                "X-Api-Resource-Id": self._resource}
        try:
            resp = requests.post(self._endpoint, json=body, headers=hdrs,
                                 stream=True, timeout=(10, 60))
        except Exception as e:
            raise RuntimeError("云端 TTS 请求失败: {}".format(str(e)[:200]))
        try:
            if resp.status_code != 200:
                raise RuntimeError("云端 TTS HTTP {}: {}".format(
                    resp.status_code, resp.text[:200]))
            for line in resp.iter_lines():
                out = parse_stream_line(line)
                if out is None:
                    continue
                if out["error"]:
                    raise RuntimeError("云端 TTS 错误: " + out["error"])
                if out["audio"]:
                    pcm16 = np.frombuffer(out["audio"], dtype="<i2")
                    yield pcm16.astype(np.float32) / 32768.0
                if out["done"]:
                    break
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def synthesize(self, text):
        """整段合成（测试/兜底用）：拼装全部分片。"""
        chunks = list(self.synthesize_chunks(text))
        if not chunks:
            return np.zeros(0, dtype=np.float32), self.sample_rate
        return np.concatenate(chunks), self.sample_rate
