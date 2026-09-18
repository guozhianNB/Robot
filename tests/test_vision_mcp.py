# -*- coding: utf-8 -*-
"""vision MCP business interface tests."""

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import importlib
import os

import pytest

from LLM.vision_mcp import vision_client
from LLM.vision_mcp import see_server
from LLM import conf


def _fake_client(response=None, error=None, calls=None):
    def create(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if error is not None:
            raise error
        return response

    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create),
        )
    )


def test_ask_qwen_sends_prompt_image_data_url_and_limits(monkeypatch):
    calls = []
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))]
    )
    monkeypatch.setattr(vision_client, "_client", _fake_client(response, calls=calls))
    monkeypatch.setattr(vision_client, "MODEL", "test-vision-model")
    monkeypatch.setattr(vision_client, "TIMEOUT", 13.5)
    monkeypatch.setattr(vision_client, "MAX_TOKENS", 77)

    prompt = "  keep this prompt exactly\n  "
    assert vision_client.ask_qwen(prompt, b"png-bytes", "image/png") == "answer"

    request = calls[0]
    assert request["model"] == "test-vision-model"
    assert request["timeout"] == 13.5
    assert request["max_tokens"] == 77
    content = request["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": prompt}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,cG5nLWJ5dGVz"},
    }


def test_ask_qwen_converts_cloud_sdk_exception_to_see_error(monkeypatch):
    monkeypatch.setattr(
        vision_client,
        "_client",
        _fake_client(error=RuntimeError("provider unavailable")),
    )

    with pytest.raises(vision_client.SeeError, match="云端视觉模型调用失败"):
        vision_client.ask_qwen("describe", b"jpeg", "image/jpeg")


def test_cloud_error_text_is_not_returned_or_audited(monkeypatch):
    secret = "sk-fake-key prompt-secret data:image/jpeg;base64,c2VjcmV0"
    audit = {}
    monkeypatch.setattr(vision_client, "OpenAI", object())
    monkeypatch.setattr(
        vision_client,
        "_client",
        _fake_client(error=RuntimeError(secret)),
    )
    monkeypatch.setattr(vision_client, "grab_camera_jpeg", lambda channel: (b"jpeg", 8, 8))
    monkeypatch.setattr(vision_client, "_audit", lambda **fields: audit.update(fields))

    result = vision_client.see_what("camera", "private prompt", channel=3)

    assert result["ok"] is False
    assert secret not in result["error"]
    assert secret not in str(audit)
    assert "sk-fake-key" not in str(audit)
    assert "prompt-secret" not in str(audit)
    assert "c2VjcmV0" not in str(audit)


def test_cloud_client_constructor_error_is_not_returned_or_audited(monkeypatch):
    secret = "sk-fake-dashscope prompt-secret data:image/jpeg;base64,c2VjcmV0"
    audit = {}

    def fail_openai(**_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(vision_client, "OpenAI", fail_openai)
    monkeypatch.setattr(vision_client, "_client", None)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "configured-test-key")
    monkeypatch.setattr(vision_client, "grab_camera_jpeg", lambda channel: (b"jpeg", 8, 8))
    monkeypatch.setattr(vision_client, "_audit", lambda **fields: audit.update(fields))

    with pytest.raises(vision_client.SeeError) as exc_info:
        vision_client._ensure_client()
    assert secret not in str(exc_info.value)

    result = vision_client.see_what("camera", "private prompt", channel=3)

    assert result["ok"] is False
    assert secret not in result["error"]
    assert secret not in str(audit)
    assert "sk-fake-dashscope" not in str(audit)
    assert "prompt-secret" not in str(audit)
    assert "c2VjcmV0" not in str(audit)


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(choices=[]),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=""))]
        ),
    ],
)
def test_ask_qwen_rejects_missing_or_empty_response(response, monkeypatch):
    monkeypatch.setattr(vision_client, "_client", _fake_client(response))

    with pytest.raises(vision_client.SeeError):
        vision_client.ask_qwen("describe", b"jpeg")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("plain answer", "plain answer"),
        (
            [SimpleNamespace(text="part 1"), SimpleNamespace(text=" part 2 ")],
            "part 1 part 2",
        ),
        ([{"type": "text", "text": "part 1"}, {"text": " part 2 "}], "part 1 part 2"),
    ],
)
def test_ask_qwen_extracts_string_and_content_blocks(content, expected, monkeypatch):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    monkeypatch.setattr(vision_client, "_client", _fake_client(response))

    assert vision_client.ask_qwen("describe", b"jpeg") == expected


def test_ask_qwen_rejects_malformed_content_blocks_without_type_error(monkeypatch):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=[SimpleNamespace(text=object()), {"text": None}]
                )
            )
        ]
    )
    monkeypatch.setattr(vision_client, "_client", _fake_client(response))

    with pytest.raises(vision_client.SeeError, match="空回答"):
        vision_client.ask_qwen("describe", b"jpeg")


def test_see_what_passes_original_prompt_after_trimmed_validation(monkeypatch):
    seen = {}
    original_prompt = "  preserve surrounding whitespace  "

    monkeypatch.setattr(vision_client, "OpenAI", object())
    monkeypatch.setattr(vision_client, "grab_camera_jpeg", lambda channel: (b"jpeg", 8, 8))

    def fake_ask(prompt, data, mime):
        seen["prompt"] = prompt
        return "answer"

    monkeypatch.setattr(vision_client, "ask_qwen", fake_ask)

    result = vision_client.see_what("camera", original_prompt, channel=1)

    assert result["ok"] is True
    assert seen["prompt"] == original_prompt


def test_see_what_rejects_non_string_prompt_without_external_calls(monkeypatch):
    calls = []

    def fail_external(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("external image or cloud call should not happen")

    monkeypatch.setattr(vision_client, "grab_camera_jpeg", fail_external)
    monkeypatch.setattr(vision_client, "load_image_file", fail_external)
    monkeypatch.setattr(vision_client, "ask_qwen", fail_external)

    result = vision_client.see_what("camera", 123, channel=1)

    assert result["ok"] is False
    assert calls == []


def test_default_qwen_model_is_formal_vision_model(monkeypatch):
    original = os.environ.get("VISION_SEE_MODEL")
    monkeypatch.delenv("VISION_SEE_MODEL", raising=False)
    importlib.reload(vision_client)

    try:
        assert vision_client.MODEL == "qwen-vl-plus"
    finally:
        if original is None:
            os.environ.pop("VISION_SEE_MODEL", None)
        else:
            os.environ["VISION_SEE_MODEL"] = original
        importlib.reload(vision_client)


def test_qwen_model_can_be_overridden_by_environment(monkeypatch):
    original = os.environ.get("VISION_SEE_MODEL")
    monkeypatch.setenv("VISION_SEE_MODEL", "custom-qwen-vision")
    importlib.reload(vision_client)

    try:
        assert vision_client.MODEL == "custom-qwen-vision"
    finally:
        if original is None:
            os.environ.pop("VISION_SEE_MODEL", None)
        else:
            os.environ["VISION_SEE_MODEL"] = original
        importlib.reload(vision_client)


def test_vision_client_blank_model_and_base_url_use_defaults(monkeypatch):
    original_model = os.environ.get("VISION_SEE_MODEL")
    original_base_url = os.environ.get("VISION_SEE_BASE_URL")
    monkeypatch.setenv("VISION_SEE_MODEL", "   ")
    monkeypatch.setenv("VISION_SEE_BASE_URL", "  ")
    importlib.reload(vision_client)

    try:
        assert vision_client.MODEL == "qwen-vl-plus"
        assert vision_client.BASE_URL == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    finally:
        if original_model is None:
            os.environ.pop("VISION_SEE_MODEL", None)
        else:
            os.environ["VISION_SEE_MODEL"] = original_model
        if original_base_url is None:
            os.environ.pop("VISION_SEE_BASE_URL", None)
        else:
            os.environ["VISION_SEE_BASE_URL"] = original_base_url
        importlib.reload(vision_client)


@pytest.mark.parametrize("raw_port", ["", "  ", "not-a-port", "0", "65536", "-1"])
def test_conf_invalid_vision_port_and_blank_host_fall_back_without_import_error(raw_port):
    original_host = os.environ.get("VISION_HOST")
    original_port = os.environ.get("VISION_PORT")
    os.environ["VISION_HOST"] = "   "
    os.environ["VISION_PORT"] = raw_port
    importlib.reload(conf)

    try:
        assert conf.VISION_HOST == "127.0.0.1"
        assert conf.VISION_PORT == 9540
    finally:
        if original_host is None:
            os.environ.pop("VISION_HOST", None)
        else:
            os.environ["VISION_HOST"] = original_host
        if original_port is None:
            os.environ.pop("VISION_PORT", None)
        else:
            os.environ["VISION_PORT"] = original_port
        importlib.reload(conf)


def test_conf_vision_host_and_port_strip_valid_values():
    original_host = os.environ.get("VISION_HOST")
    original_port = os.environ.get("VISION_PORT")
    os.environ["VISION_HOST"] = " 10.0.0.9 "
    os.environ["VISION_PORT"] = " 19540 "
    importlib.reload(conf)

    try:
        assert conf.VISION_HOST == "10.0.0.9"
        assert conf.VISION_PORT == 19540
    finally:
        if original_host is None:
            os.environ.pop("VISION_HOST", None)
        else:
            os.environ["VISION_HOST"] = original_host
        if original_port is None:
            os.environ.pop("VISION_PORT", None)
        else:
            os.environ["VISION_PORT"] = original_port
        importlib.reload(conf)


def test_vision_cloud_timeout_is_inside_mcp_tool_budget():
    assert vision_client.TIMEOUT < conf.MCP_TOOL_TIMEOUT


def test_vision_mcp_env_omits_unset_optional_values_and_passes_valid_target(monkeypatch):
    optional_names = tuple(conf._VISION_SEE_ENV_NAMES)
    for name in optional_names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    env = conf._vision_mcp_env()

    assert env["DASHSCOPE_API_KEY"] == ""
    assert env["VISION_HOST"] == str(conf.VISION_HOST)
    assert env["VISION_PORT"] == str(conf.VISION_PORT)
    assert env["VISION_PORT"] != ""
    assert set(env) == {"DASHSCOPE_API_KEY", "VISION_HOST", "VISION_PORT"}


def test_vision_mcp_env_passes_custom_supported_values(monkeypatch):
    optional_names = tuple(conf._VISION_SEE_ENV_NAMES)
    for name in optional_names:
        monkeypatch.delenv(name, raising=False)
    custom = {
        "VISION_SEE_MODEL": "qwen-custom",
        "VISION_SEE_BASE_URL": "https://vision.example/v1",
        "VISION_SEE_TIMEOUT": "7",
        "VISION_SEE_MAX_TOKENS": "99",
        "VISION_SEE_MAX_WIDTH": "640",
        "VISION_SEE_QUALITY": "70",
        "VISION_SEE_MAX_BYTES": "12345",
        "VISION_SEE_IMAGE_DIRS": "C:\\vision-inbox",
        "VISION_SEE_GRAB_TIMEOUT": "1.5",
        "VISION_SEE_CONNECT_TIMEOUT": "0.5",
        "VISION_SEE_CHANNEL": "2",
    }
    for name, value in custom.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(conf, "VISION_HOST", "10.0.0.9")
    monkeypatch.setattr(conf, "VISION_PORT", 19540)

    env = conf._vision_mcp_env()

    assert env == {
        **custom,
        "DASHSCOPE_API_KEY": "test-key",
        "VISION_HOST": "10.0.0.9",
        "VISION_PORT": "19540",
    }


def test_openai_client_disables_internal_retries(monkeypatch):
    calls = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(vision_client, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(vision_client, "_client", None)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    assert vision_client._ensure_client() is not None
    assert calls == [{
        "api_key": "test-key",
        "base_url": vision_client.BASE_URL,
        "max_retries": 0,
    }]


def test_see_what_camera_passes_channel_and_echoes_actual_channel(monkeypatch):
    seen = {}

    def fake_grab(channel):
        seen["channel"] = channel
        return b"jpeg", 640, 480

    monkeypatch.setattr(vision_client, "OpenAI", object())
    monkeypatch.setattr(vision_client, "grab_camera_jpeg", fake_grab)
    monkeypatch.setattr(vision_client, "ask_qwen", lambda prompt, data, mime: "answer")

    result = vision_client.see_what(" CAMERA ", "  what is here?  ", channel=7)

    assert result["ok"] is True
    assert seen["channel"] == 7
    assert result["image"] == {
        "source": "camera",
        "channel": 7,
        "mime": "image/jpeg",
        "width": 640,
        "height": 480,
        "bytes": 4,
    }


def test_grab_camera_jpeg_rejects_encoded_image_over_limit(monkeypatch):
    class FakeCamera:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get_frame(self, channel):
            assert channel == 1
            return SimpleNamespace(data=b"nv12", width=16, height=16)

    monkeypatch.setattr(vision_client, "CameraClient", object())
    monkeypatch.setattr(vision_client, "_new_camera_client", lambda: FakeCamera())
    monkeypatch.setattr(vision_client, "nv12_to_jpeg", lambda *args, **kwargs: b"x" * 11)
    monkeypatch.setattr(vision_client, "MAX_IMAGE_BYTES", 10)

    with pytest.raises(vision_client.SeeError, match="太大"):
        vision_client.grab_camera_jpeg(1)


def test_see_what_audit_does_not_record_prompt(monkeypatch):
    audit = {}

    monkeypatch.setattr(vision_client, "OpenAI", object())
    monkeypatch.setattr(vision_client, "grab_camera_jpeg", lambda channel: (b"jpeg", 8, 8))
    monkeypatch.setattr(vision_client, "ask_qwen", lambda prompt, data, mime: "answer")
    monkeypatch.setattr(vision_client, "_audit", lambda **fields: audit.update(fields))

    result = vision_client.see_what("camera", "private prompt", channel=3)

    assert result["ok"] is True
    assert "prompt" not in audit
    assert set(audit) <= {"action", "model", "ms", "source", "bytes", "error"}


def test_see_what_file_uses_load_image_file(monkeypatch, tmp_path):
    seen = {}
    image_path = tmp_path / "scene.png"

    def fake_load(path):
        seen["path"] = path
        return b"png", "image/png", Path(path)

    monkeypatch.setattr(vision_client, "OpenAI", object())
    monkeypatch.setattr(vision_client, "load_image_file", fake_load, raising=False)
    monkeypatch.setattr(vision_client, "ask_qwen", lambda prompt, data, mime: "answer")

    result = vision_client.see_what(str(image_path), "describe this", channel=9)

    assert result["ok"] is True
    assert seen["path"] == str(image_path)
    assert result["image"]["source"] == "file"
    assert result["image"]["mime"] == "image/png"
    assert result["image"]["bytes"] == 3
    assert "channel" not in result["image"]


def test_see_what_file_ignores_invalid_channel(monkeypatch, tmp_path):
    image_path = tmp_path / "scene.png"

    monkeypatch.setattr(vision_client, "OpenAI", object())
    monkeypatch.setattr(
        vision_client,
        "load_image_file",
        lambda path: (b"png", "image/png", Path(path)),
        raising=False,
    )
    monkeypatch.setattr(vision_client, "ask_qwen", lambda prompt, data, mime: "answer")

    result = vision_client.see_what(str(image_path), "describe this", channel=0)

    assert result["ok"] is True
    assert result["image"]["source"] == "file"


@pytest.mark.parametrize(
    ("image", "prompt", "channel"),
    [
        ("", "describe", 1),
        ("   ", "describe", 1),
        ("camera", "", 1),
        ("camera", "   ", 1),
        ("camera", "describe", 0),
        ("camera", "describe", 256),
        ("camera", "describe", -1),
        ("camera", "describe", "1"),
    ],
)
def test_see_what_rejects_missing_inputs_and_invalid_channels(image, prompt, channel):
    result = vision_client.see_what(image, prompt, channel=channel)

    assert result["ok"] is False
    assert result["error"]


@pytest.mark.parametrize(
    ("suffix", "payload", "mime"),
    [
        (".jpg", b"\xff\xd8\xffjpeg", "image/jpeg"),
        (".png", b"\x89PNG\r\n\x1a\nimage", "image/png"),
        (".webp", b"RIFFxxxxWEBPimage", "image/webp"),
    ],
)
def test_load_image_file_accepts_supported_magic_bytes(
    monkeypatch, tmp_path, suffix, payload, mime
):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    image_path = allowed / ("scene" + suffix)
    image_path.write_bytes(payload)
    monkeypatch.setattr(vision_client, "_ALLOWED_DIRS", [allowed])

    data, actual_mime, actual_path = vision_client.load_image_file(str(image_path))

    assert data == payload
    assert actual_mime == mime
    assert actual_path == image_path.resolve()


def test_load_image_file_rejects_path_outside_allowed_dirs(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"\xff\xd8\xffjpeg")
    monkeypatch.setattr(vision_client, "_ALLOWED_DIRS", [allowed])

    with pytest.raises(vision_client.SeeError, match="不在允许目录"):
        vision_client.load_image_file(str(outside))


def test_load_image_file_rejects_empty_file(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    image_path = allowed / "empty.jpg"
    image_path.write_bytes(b"")
    monkeypatch.setattr(vision_client, "_ALLOWED_DIRS", [allowed])

    with pytest.raises(vision_client.SeeError, match="空文件"):
        vision_client.load_image_file(str(image_path))


def test_load_image_file_rejects_file_over_limit(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    image_path = allowed / "large.jpg"
    image_path.write_bytes(b"\xff\xd8\xffjpeg")
    monkeypatch.setattr(vision_client, "_ALLOWED_DIRS", [allowed])
    monkeypatch.setattr(vision_client, "MAX_IMAGE_BYTES", 3)

    with pytest.raises(vision_client.SeeError, match="太大"):
        vision_client.load_image_file(str(image_path))


def test_load_image_file_rechecks_limit_after_read(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    image_path = allowed / "grown.jpg"
    image_path.write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr(vision_client, "_ALLOWED_DIRS", [allowed])
    monkeypatch.setattr(vision_client, "MAX_IMAGE_BYTES", 3)
    monkeypatch.setattr(Path, "read_bytes", lambda _path: b"\xff\xd8\xffextra")

    with pytest.raises(vision_client.SeeError, match="太大"):
        vision_client.load_image_file(str(image_path))


def test_load_image_file_rejects_sibling_path_with_similar_prefix(monkeypatch, tmp_path):
    allowed = tmp_path / "inbox"
    sibling = tmp_path / "inbox2"
    allowed.mkdir()
    sibling.mkdir()
    image_path = sibling / "scene.jpg"
    image_path.write_bytes(b"\xff\xd8\xffjpeg")
    monkeypatch.setattr(vision_client, "_ALLOWED_DIRS", [allowed])

    with pytest.raises(vision_client.SeeError, match="不在允许目录"):
        vision_client.load_image_file(str(image_path))


@pytest.mark.parametrize(
    ("image", "prompt", "channel"),
    [
        ("", "describe", 1),
        ("camera", "", 1),
        ("camera", "describe", 0),
    ],
)
def test_see_what_rejects_invalid_input_without_external_calls(
    monkeypatch, image, prompt, channel
):
    calls = []

    def fail_external(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("external image or cloud call should not happen")

    monkeypatch.setattr(vision_client, "grab_camera_jpeg", fail_external)
    monkeypatch.setattr(vision_client, "load_image_file", fail_external)
    monkeypatch.setattr(vision_client, "ask_qwen", fail_external)

    result = vision_client.see_what(image, prompt, channel=channel)

    assert result["ok"] is False
    assert calls == []


def test_load_image_file_rejects_unknown_magic(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    image_path = allowed / "unknown.jpg"
    image_path.write_bytes(b"not an image")
    monkeypatch.setattr(vision_client, "_ALLOWED_DIRS", [allowed])

    with pytest.raises(vision_client.SeeError, match="JPEG/PNG/WEBP"):
        vision_client.load_image_file(str(image_path))


def test_load_image_file_is_the_public_file_loader():
    assert hasattr(vision_client, "load_image_file")
    assert not hasattr(vision_client, "load_image_jpeg")


def test_mcp_handler_forwards_image_prompt_and_channel_as_json(monkeypatch):
    calls = []

    def fake_see_what(image, prompt, channel=1):
        calls.append((image, prompt, channel))
        return {"ok": True, "answer": "a person"}

    monkeypatch.setattr(see_server.vision_client, "see_what", fake_see_what)

    result = see_server._see_what("camera", "is anyone here?", channel=7)

    assert json.loads(result) == {"ok": True, "answer": "a person"}
    assert calls == [("camera", "is anyone here?", 7)]


def test_see_server_uses_package_vision_client_instance():
    assert see_server.vision_client is vision_client


def test_mcp_handler_exception_json_does_not_leak_exception_text(monkeypatch):
    secret = "sk-fake-key prompt-secret data:image/jpeg;base64,c2VjcmV0"

    def fail(*_args):
        raise RuntimeError(secret)

    monkeypatch.setattr(see_server.vision_client, "see_what", fail)

    result = json.loads(see_server._see_what("camera", "describe", channel=1))

    assert result["ok"] is False
    assert result["error"] == "see_what 异常（RuntimeError）"
    assert secret not in json.dumps(result, ensure_ascii=False)


def test_main_exits_without_stdout_when_mcp_dependency_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(see_server, "MCPServer", None)
    monkeypatch.setattr(see_server, "_log", lambda *_args: None)

    with pytest.raises(SystemExit) as exc_info:
        see_server.main()

    assert exc_info.value.code == 2
    assert capsys.readouterr().out == ""


def test_main_exits_without_stdout_when_cloud_is_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(see_server, "MCPServer", object())
    monkeypatch.setattr(see_server.vision_client, "cloud_available", lambda: False)
    monkeypatch.setattr(see_server.vision_client, "missing", lambda: [])
    monkeypatch.setattr(see_server, "_log", lambda *_args: None)

    with pytest.raises(SystemExit) as exc_info:
        see_server.main()

    assert exc_info.value.code == 2
    assert capsys.readouterr().out == ""


def test_mcp_server_registers_see_what_contract_and_safety_description():
    server = see_server.build_server()
    assert server is not None

    tools = asyncio.run(server.list_tools())
    tool = next(item for item in tools if item.name == "see_what")

    schema = tool.input_schema
    assert schema["required"] == ["image", "prompt"]
    assert set(schema["properties"]) == {"image", "prompt", "channel"}
    assert schema["properties"]["image"]["type"] == "string"
    assert schema["properties"]["prompt"]["type"] == "string"
    assert schema["properties"]["channel"]["default"] == 1
    registered = server._tool_manager.get_tool("see_what")
    assert str(inspect.signature(registered.fn)) == "(image: str, prompt: str, channel: int = 1) -> str"

    description = tool.description or ""
    for phrase in ("camera", "VISION_SEE_IMAGE_DIRS", "阿里云", "可能有误", "跌倒", "告警", "医疗"):
        assert phrase in description
