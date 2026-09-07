# -*- coding: utf-8 -*-
r"""云端中文流式识别：火山引擎「豆包语音」流式语音识别大模型2.0（Seed-ASR，SAUC WebSocket）。

协议要点（官方流式识别 API 2.0 文档映射，见 GizClaw/doubao-speech-go streaming_asr.md）：
  - 端点  wss://openspeech.bytedance.com/api/v3/sauc/bigmodel（双向流式，随包出字）
  - 鉴权  新版「豆包语音」控制台 API Key（UUID），放握手 header：X-Api-Key + X-Api-Resource-Id
          （流式识别2.0 资源 = volc.seedasr.sauc.duration；旧版流式1.0 = volc.bigasr.sauc.duration）
  - 帧    4 字节头 + [sequence] + 4 字节 payload 长度 + payload（可选 gzip，本实现不发 gzip）
          首帧 full-client 请求 = JSON(audio 元信息 + request 参数)；随后 audio-only 帧 = 原始 PCM16LE；
          结束帧 flags=final → 服务器回最终结果。
  - 语义  本实现与 asr.py 的本地引擎共用同一流式接口（start_session/accept/finish/abort/close），
          每句话新建一条 WebSocket（官方推荐，识别结束即断开）。

稳健性：websocket-client 缺失时本模块仍可导入（_WS_OK=False），实例化时抛 RuntimeError 说明，
由 worker 启动降级处理 —— 不污染后端导入链。"""
import gzip
import json
import struct
import time
import uuid

import numpy as np

from . import config

try:
    import websocket  # websocket-client（可选依赖，见 requirement.txt「语音链路」）
    _WS_OK = True
except Exception:      # 缺依赖：仅云端 ASR 不可用，实例化时给出明确提示
    websocket = None
    _WS_OK = False

# 服务端成功码（错误响应 JSON 里 code 非此值 → 记错误）
_OK_CODES = (0, 20000000)

# 收尾等待：发完 final 帧后最多等这么久拿最终结果
_FINAL_WAIT_S = 3.0


# ---------------------------------------------------------------------------
# SAUC 帧编解码（纯函数，便于单测）
# ---------------------------------------------------------------------------
def build_client_frame(payload: dict) -> bytes:
    """full-client 请求帧：JSON 序列化，不压缩。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _frame(0b0001, 0b0000, 0b0001, data)


def build_audio_frame(pcm: bytes, final: bool = False) -> bytes:
    """audio-only 帧：裸 PCM；final=True 标记输入结束。"""
    return _frame(0b0010, 0b0010 if final else 0b0000, 0b0000, pcm)


def _frame(msg_type: int, flags: int, serial: int, payload: bytes) -> bytes:
    """4 字节头 + payload 长度(BE) + payload。serial: 0=raw 1=JSON；无压缩。"""
    header = bytes([0x11, (msg_type << 4) | flags, (serial << 4) | 0x00, 0x00])
    return header + struct.pack(">I", len(payload)) + payload


def decode_server_message(raw: bytes):
    """解析服务端二进制帧 → (kind, obj)。
    kind: 'result'(obj=dict) / 'error'(obj=错误文本) / None(坏帧，忽略)。
    result dict 统一含 text 键（无法识别的合法 JSON 原样返回）。"""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 4:
        return None
    b1, b2 = raw[1], raw[2]
    msg_type = (b1 >> 4) & 0xF
    compression = b2 & 0xF
    off = 4
    # full/audio 服务端响应带 4 字节 sequence；错误帧是 code+message_size 结构
    if msg_type in (0b1001, 0b1011):
        if len(raw) < off + 4:
            return None
        off += 4                       # sequence(int32，可忽略)
        if len(raw) < off + 4:
            return None
        n = struct.unpack(">I", raw[off:off + 4])[0]
        off += 4
        data = bytes(raw[off:off + n])
        if compression == 0b0001:
            try:
                data = gzip.decompress(data)
            except Exception:
                return ("error", "解压失败")
        try:
            text = data.decode("utf-8", "ignore")
        except Exception:
            return None
        try:
            j = json.loads(text)
        except Exception:
            j = None
        if isinstance(j, dict):
            code = j.get("code")
            if code is not None and code not in _OK_CODES:
                return ("error", j.get("message") or j.get("msg") or ("错误码 " + str(code)))
            if "result" in j or "text" in j or "utterances" in j or not j:
                return ("result", j)
            return ("result", j)       # 其它合法 JSON 原样返回，上层宽容取值
        return ("result", {"text": text.strip()})
    if msg_type == 0b1111:             # 错误帧：header + error_code + message_size + message
        if len(raw) < 12:
            return ("error", "未知错误帧")
        code = struct.unpack(">I", raw[4:8])[0]
        n = struct.unpack(">I", raw[8:12])[0]
        msg = raw[12:12 + n].decode("utf-8", "ignore")
        return ("error", msg or ("错误码 " + str(code)))
    return None


def _result_text(j: dict) -> str:
    """从 result dict 里取整句文本（result.text，或按 utterances 拼接）。"""
    r = j.get("result") if isinstance(j, dict) else None
    if isinstance(r, dict):
        t = r.get("text")
        if isinstance(t, str) and t.strip():
            return t.strip()
        uts = r.get("utterances")
        if isinstance(uts, list):
            parts = [u.get("text", "") for u in uts if isinstance(u, dict)]
            if any(p for p in parts):
                return "".join(parts).strip()
    t = j.get("text")
    return t.strip() if isinstance(t, str) else ""


def _result_definite(j: dict) -> bool:
    """是否收到 definite 终稿标记（utterance.definite / result.definite）。"""
    r = j.get("result") if isinstance(j, dict) else None
    if isinstance(r, dict):
        if r.get("definite"):
            return True
        uts = r.get("utterances")
        if isinstance(uts, list):
            return any(isinstance(u, dict) and u.get("definite") for u in uts)
    return False


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
class CloudStreamASR:
    """火山流式识别引擎：一句话一个 WebSocket 会话。与本地引擎同接口。"""

    provider = "cloud"

    def __init__(self):
        if not _WS_OK:
            raise RuntimeError("云端 ASR 不可用：缺少 websocket-client（语音依赖组里安装）")
        if not config.cloud_asr_api_key():
            raise RuntimeError("云端 ASR 不可用：未配置 VOLC_ASR_API_KEY（"
                               "火山引擎「豆包语音」控制台 → 语音识别 → API 调用 → 创建 API Key）")
        self._ws = None
        self._text = ""
        self._err = None

    # ---- 流式接口 ----
    def start_session(self):
        self.close()
        hdrs = [
            "X-Api-Key: " + config.cloud_asr_api_key(),
            "X-Api-Resource-Id: " + config.cloud_asr_resource_id(),
            "X-Api-Request-Id: " + uuid.uuid4().hex,
            "X-Api-Sequence: -1",
            "X-Api-Connect-Id: " + uuid.uuid4().hex,
        ]
        try:
            self._ws = websocket.create_connection(
                config.cloud_asr_ws(), header=hdrs, timeout=10, enable_multithread=True)
        except Exception as e:       # 握手失败（含鉴权/未开通资源）→ 带原文报错
            raise RuntimeError("云端 ASR 连接失败: {}".format(str(e)[:300]))
        try:
            self._ws.send_binary(build_client_frame(self._start_payload()))
        except Exception as e:
            self.close()
            raise RuntimeError("云端 ASR 建连后发送失败: {}".format(str(e)[:300]))
        self._text = ""
        self._err = None

    def accept(self, samples: np.ndarray) -> str:
        """喂一块音频（float32 16k 单声道）→ 返回服务器当前已识别文本。"""
        if self._ws is None:
            return self._text
        pcm = np.clip(samples, -1.0, 1.0)
        pcm16 = (pcm * 32767).astype("<i2").tobytes()
        try:
            self._ws.send_binary(build_audio_frame(pcm16))
            self._collect(quiet_s=0.02)     # 非阻塞式读空当前缓冲，更新 partial
        except Exception:
            self._drop()
        return self._text

    def finish(self) -> str:
        """输入结束：发 final 帧，收服务器最终结果（有限等待 + 出错回落 partial）。"""
        if self._ws is None:
            return self._text.strip()
        try:
            self._ws.send_binary(build_audio_frame(b"", final=True))
        except Exception:
            self._drop()
            return self._text.strip()
        deadline = time.time() + _FINAL_WAIT_S
        last = self._text
        while self._ws is not None and time.time() < deadline:
            try:
                kind, obj = self._recv(timeout=0.5)
            except Exception:
                self._drop()
                break
            if kind is None:                 # 超时无新消息
                continue
            if kind == "error":
                self._err = obj
                break
            if isinstance(obj, dict):
                t = _result_text(obj)
                if t:
                    self._text = t
                    last = t
                if _result_definite(obj):
                    break                    # definite 终稿已到
        text = last.strip()
        self.close()
        return text

    def abort(self):
        self.close()

    def transcribe(self, samples: np.ndarray) -> str:
        """整段一次性识别（流式会话失败的兜底路径）：按 100ms 分包发给同一连接。"""
        try:
            self.start_session()
        except Exception:
            return ""
        n = int(config.BLOCK_SAMPLES)
        for i in range(0, len(samples), n):
            if self._ws is None:
                break
            self.accept(samples[i:i + n])
        return self.finish()

    def close(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    # ---- 内部 ----
    def _start_payload(self) -> dict:
        return {
            "user": {"uid": "robot-voice-worker"},
            "audio": {
                "format": "pcm", "rate": config.SAMPLE_RATE,
                "bits": 16, "channel": 1, "language": "zh-CN",
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,      # 数字/日期等归一
                "enable_punc": True,
                "enable_ddc": False,
                "show_utterances": True,
                "result_type": "single",
            },
        }

    def _collect(self, quiet_s: float):
        """把当前所有已到达的消息读完（超时即认为读空），更新文本/错误。"""
        while self._ws is not None:
            try:
                kind, obj = self._recv(timeout=quiet_s)
            except Exception:
                self._drop()
                break
            if kind is None:
                break
            if kind == "error":
                self._err = obj
                break
            if isinstance(obj, dict):
                t = _result_text(obj)
                if t:
                    self._text = t

    def _recv(self, timeout: float):
        """读一条消息 → (kind, obj)；超时返回 (None, None)；连接异常抛给上层。

        注意：websocket-client 的 WebSocket.recv() 不接受 timeout 关键字，
        超时必须用 settimeout(timeout) + recv()（超时抛 WebSocketTimeoutException）。"""
        try:
            self._ws.settimeout(timeout)
            raw = self._ws.recv()
        except websocket.WebSocketTimeoutException:
            return None, None
        return decode_server_message(raw)

    def _drop(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if not self._err:
            self._err = "连接中断"
