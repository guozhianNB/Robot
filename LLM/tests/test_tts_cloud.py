# -*- coding: utf-8 -*-
"""CloudStreamTTS 纯逻辑测试：请求构造 / NDJSON 行解析（不触网）。"""
import base64

from LLM.voice import tts_cloud as tc


def test_build_request_body():
    body = tc.build_request_body("你好。", "zh_xx_speaker_1", 16000)
    rp = body["req_params"]
    assert rp["text"] == "你好。"
    assert rp["speaker"] == "zh_xx_speaker_1"
    assert rp["audio_params"] == {"format": "pcm", "sample_rate": 16000}
    assert body["user"]["uid"]


def test_parse_stream_line_audio():
    pcm = b"\x00\x01\xff\xfe"
    line = '{"code":0,"data":"' + base64.b64encode(pcm).decode() + '","done":false}'
    out = tc.parse_stream_line(line)
    assert out["audio"] == pcm
    assert out["done"] is False
    assert out["error"] is None


def test_parse_stream_line_done_and_error():
    ok = tc.parse_stream_line('{"code":0,"done":true}')
    assert ok["audio"] is None and ok["done"] is True
    err = tc.parse_stream_line('{"code":12345,"message":"boom"}')
    assert err["error"] == "boom"
    none = tc.parse_stream_line("not json")
    assert none is None


def test_parse_stream_line_ok_code_alias():
    # 服务端成功码别名 20000000（对齐 SAUC 的 _OK_CODES 惯例）
    out = tc.parse_stream_line('{"code":20000000,"data":"","done":true}')
    assert out["done"] is True and out["error"] is None
