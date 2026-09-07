# -*- coding: utf-8 -*-
"""火山豆包语音 TTS 2.0 HTTP 单向流式探针：验证端点/鉴权/NDJSON 音频分片。
用法：python docs/2.pre/tts2_probe.py   （读根 .env 的 VOLC_ASR_API_KEY / VOLC_TTS_SPEAKER）
通过 → 打印前 3 个音频分片字节数 + done 标记；失败 → 打印 HTTP 状态与响应体前 300 字。"""
import json, os, sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent.parent          # 项目根
sys.path.insert(0, str(BASE))
for ln in (BASE / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in ln and not ln.lstrip().startswith("#"):
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

key = os.getenv("VOLC_ASR_API_KEY") or ""
speaker = os.getenv("VOLC_TTS_SPEAKER") or ""
assert key, "缺少 VOLC_ASR_API_KEY"
assert speaker, "缺少 VOLC_TTS_SPEAKER（音色 ID 未配置）"
import requests
url = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
body = {"user": {"uid": "tts-probe"},
        "req_params": {"text": "你好，这是火山豆包语音合成测试。",
                       "speaker": speaker,
                       "audio_params": {"format": "pcm", "sample_rate": 16000}}}
hdrs = {"Content-Type": "application/json", "X-Api-Key": key, "X-Api-Resource-Id": "seed-tts-2.0"}
try:
    with requests.post(url, json=body, headers=hdrs, stream=True, timeout=30) as r:
        print("HTTP", r.status_code)
        if r.status_code != 200:
            print(r.text[:300]); sys.exit(1)
        n, done = 0, 0
        for line in r.iter_lines():
            if not line:
                continue
            j = json.loads(line)
            if j.get("code") not in (0, 20000000):
                print("code:", j.get("code"), j.get("message")); sys.exit(1)
            if j.get("data"):
                import base64
                raw = base64.b64decode(j["data"])
                n += len(raw)
                if n <= 48000:
                    print("audio chunk bytes:", len(raw))
            if j.get("done"):
                done = 1
                break
        print("total audio bytes:", n, "| done:", bool(done))
        # 实测(2026-09-07)：服务端以流关闭收尾，不发显式 done 帧(done=False 属正常)；
        # 验收 = 音频字节 > 0 即通过（tts_cloud.py 依赖 iter_lines 耗尽收尾，与 done 无关）
        sys.exit(0 if n > 0 else 2)
except Exception as e:
    print("FAIL:", repr(e)[:300]); sys.exit(2)
