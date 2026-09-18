# -*- coding: utf-8 -*-
r"""护士传达 MCP（`notify_nurse`）测试：业务层 `notice_client` + MCP 层 `notice_server` + 接线配置 + 闸门。

【测试红线（.superpowers/sdd/progress.md）】不碰真后端：HTTP 桩是本进程起的
**127.0.0.1 随机端口** `ThreadingHTTPServer`，既不连 8000、也不起 uvicorn，无进程级外部副作用。
"""
import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from LLM import conf
from LLM.agent import tools
from LLM.notice_mcp import notice_client as nc
from LLM.notice_mcp import notice_server as ns


# ---------------------------------------------------------------------------
# HTTP 桩（127.0.0.1 随机端口）
# ---------------------------------------------------------------------------
class _Stub:
    def __init__(self, status=200, body=None, delay=0.0):
        self.status = status
        self.body = body if body is not None else {"ok": True, "id": 7,
                                                  "deduped": False, "level": "info"}
        self.delay = delay
        self.calls: list[dict] = []


def _serve(stub: _Stub) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):                                  # noqa: N802（BaseHTTPRequestHandler 约定）
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            stub.calls.append({
                "path": self.path,
                "ctype": self.headers.get("Content-Type") or "",
                "body": json.loads(raw.decode("utf-8")),
            })
            if stub.delay:
                time.sleep(stub.delay)
            if isinstance(stub.body, bytes):
                payload = stub.body
            else:
                payload = json.dumps(stub.body, ensure_ascii=False).encode("utf-8")
            self.send_response(stub.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):                       # 静音：不许往 stdout 打日志
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture()
def stub(monkeypatch):
    """返回一个工厂：`stub(status=…, body=…, delay=…)` → 桩对象（含 `calls`），并把 nc.BACKEND_URL 指过去。"""
    servers: list[ThreadingHTTPServer] = []

    def _make(status=200, body=None, delay=0.0):
        s = _Stub(status, body, delay)
        httpd = _serve(s)
        servers.append(httpd)
        monkeypatch.setattr(nc, "BACKEND_URL",
                            f"http://127.0.0.1:{httpd.server_address[1]}")
        return s

    yield _make
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _closed_port() -> int:
    """拿一个刚被释放的端口 → 连它必然被拒。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# 业务层：notice_client
# ---------------------------------------------------------------------------
def test_push_sends_notice_payload_and_reports_ok(stub):
    s = stub(body={"ok": True, "id": 12, "deduped": False, "level": "critical"})
    r = nc.push("张爷爷说胸口疼，想找护士", level="critical", uid="elder_001")

    assert r["ok"] is True and r["id"] == 12 and r["level"] == "critical"
    assert "已通知护士" in r["detail"]
    call = s.calls[0]
    assert call["path"] == "/api/notifications"          # 走的是免鉴权投递口（D4）
    assert call["body"] == {"source": "cart", "type": "message", "level": "critical",
                            "uid": "elder_001", "title": "", "message": "张爷爷说胸口疼，想找护士"}
    assert "application/json" in call["ctype"]


def test_push_merges_duplicate_hint(stub):
    stub(body={"ok": True, "id": 12, "deduped": True, "level": "info"})
    r = nc.push("巡房完成")
    assert r["ok"] is True and r["deduped"] is True
    assert "合并" in r["detail"]                          # 让模型知道护士只看到一条


def test_push_empty_message_does_not_touch_backend(stub):
    s = stub()
    r = nc.push("   ")
    assert r["ok"] is False and "不能为空" in r["error"]
    assert s.calls == []                                  # 归一化在本地就拦住了，不发请求


def test_push_unknown_level_falls_back_to_info(stub):
    s = stub()
    nc.push("小事一桩", level="bogus")                     # 认不出的词 → info（服务端还会再兜一次）
    assert s.calls[0]["body"]["level"] == "info"


def test_normalize_level_tolerates_case_and_synonyms():
    """级别归一：**只降不升是危险的**（把"跌倒"写成 Critical/high 会变最低档、不响蜂鸣）。"""
    assert nc.normalize_level("Critical") == "critical"
    assert nc.normalize_level("  WARNING ") == "warning"
    assert nc.normalize_level("high") == "critical"       # 模型常见近义词，往上归
    assert nc.normalize_level("severe") == "critical"
    assert nc.normalize_level("medium") == "warning"
    assert nc.normalize_level("urgent") == "critical"
    assert nc.normalize_level(None) == "info"
    assert nc.normalize_level(7) == "info"
    assert nc.normalize_level("") == "info"


def test_push_truncates_overlong_message(stub):
    s = stub()
    r = nc.push("哗" * 600)
    assert r["ok"] is True and "截断" in r["detail"]
    assert len(s.calls[0]["body"]["message"]) == nc.MESSAGE_MAX


def test_push_http_error_returns_ok_false(stub):
    s = stub(status=500, body={"detail": "内部错误"})
    r = nc.push("测试")
    assert r["ok"] is False and "500" in r["error"] and "内部错误" in r["error"]
    assert len(s.calls) == 1


def test_push_backend_error_body_returns_ok_false(stub):
    stub(status=200, body={"ok": False, "error": "type 不能为空"})
    r = nc.push("测试")
    assert r["ok"] is False and "type 不能为空" in r["error"]


def test_push_reports_effective_level_from_backend(stub):
    """返回的 level 是**库里实际生效**的级别：合并升级后可能高于/低于本次传的值。"""
    stub(body={"ok": True, "id": 9, "deduped": True, "level": "critical"})
    r = nc.push("老人摔倒了", level="info")          # 本次报 info，但库里那行已是 critical
    assert r["ok"] is True and r["level"] == "critical"


def test_push_requires_explicit_backend_success(stub):
    """只有"后端明确说成功"（`{"ok": true}`）才算成功：别的响应形状一律当没投出去。

    这条不变量决定模型会不会对老人说"我已经通知护士了"—— 宁可报失败让它改口，也不能撒谎。
    """
    stub(status=200, body={"status": "ok", "message": "accepted"})   # 无 ok 字段
    r = nc.push("测试")
    assert r["ok"] is False and "没有确认" in r["error"]

    stub(status=200, body={"ok": False, "id": 7})                    # 有 id 但明说失败 → 也不许当成功
    r2 = nc.push("测试")
    assert r2["ok"] is False

    stub(status=200, body={"id": None})                              # 空壳响应
    assert nc.push("测试")["ok"] is False


def test_push_normalizes_non_str_params(stub):
    """畸形参数（数字/None）不许把工具调用炸掉，也不许把非法级别带进库。"""
    s = stub()
    r = nc.push(123456, level=5, uid=None, title=None)     # type: ignore[arg-type]
    assert r["ok"] is True and r["level"] == "info"
    body = s.calls[0]["body"]
    assert body["message"] == "123456" and body["uid"] == "" and body["title"] == ""
    assert nc.push(None)["ok"] is False                    # type: ignore[arg-type]


def test_push_connection_refused_returns_ok_false(monkeypatch):
    monkeypatch.setattr(nc, "BACKEND_URL", f"http://127.0.0.1:{_closed_port()}")
    r = nc.push("测试")
    assert r["ok"] is False and "连不上护士后台" in r["error"]


def test_push_timeout_returns_ok_false(stub, monkeypatch):
    monkeypatch.setattr(nc, "TIMEOUT", 0.3)
    stub(delay=1.2)
    r = nc.push("测试")
    assert r["ok"] is False and "超时" in r["error"]


def test_push_non_json_response_returns_ok_false(stub):
    stub(body=b"<html>502 Bad Gateway</html>")
    r = nc.push("测试")
    assert r["ok"] is False and "不是 JSON" in r["error"]


def test_backend_url_normalization_and_endpoint():
    assert nc._resolve_backend("http://10.0.0.5:9000/") == "http://10.0.0.5:9000"
    assert nc._resolve_backend("  ") == nc._DEFAULT_BACKEND
    assert nc._resolve_backend(None) == nc._DEFAULT_BACKEND
    assert nc._resolve_backend("/") == nc._DEFAULT_BACKEND
    assert nc.endpoint() == nc.BACKEND_URL + "/api/notifications"


# ---------------------------------------------------------------------------
# MCP 层：notice_server
# ---------------------------------------------------------------------------
def _server_or_skip():
    srv = ns.build_server()
    if srv is None:
        pytest.skip("python-mcp 不可用（可选能力缺失，后端照常降级运行）")
    return srv


def test_build_server_registers_single_tool_with_expected_schema():
    srv = _server_or_skip()
    listed = asyncio.run(srv.list_tools())
    assert [t.name for t in listed] == ["notify_nurse"]
    tool = listed[0]
    props = (tool.input_schema or {}).get("properties") or {}
    assert set(props) == {"message", "level", "uid"}
    assert (tool.input_schema or {}).get("required") == ["message"]
    # docstring 就是模型看到的描述：必须写清"何时用"与"不确定 uid 就留空"
    assert "护士" in tool.description and "robot_stop" in tool.description
    assert "uid" in tool.description


def test_tool_call_through_mcp_layer_returns_json_text(stub):
    s = stub(body={"ok": True, "id": 3, "deduped": False, "level": "warning"})
    srv = _server_or_skip()
    res = asyncio.run(srv.call_tool("notify_nurse",
                                    {"message": "李奶奶说头晕", "level": "warning", "uid": "elder_002"}))
    # mcp_client 只读文本块：必须是 TextContent，且内容是 JSON 字符串
    text = res.content[0].text
    data = json.loads(text)
    assert data["ok"] is True and "已通知护士" in data["detail"]
    assert s.calls[0]["body"]["uid"] == "elder_002"


def test_notify_nurse_wrapper_never_raises_on_backend_failure(monkeypatch):
    monkeypatch.setattr(nc, "BACKEND_URL", f"http://127.0.0.1:{_closed_port()}")
    out = ns._notify_nurse("测试")
    assert isinstance(out, str)                           # 必须 return str（dict 会丢文本块）
    assert json.loads(out)["ok"] is False


def test_notify_nurse_logs_every_call_without_message_text(monkeypatch):
    """每次调用都要在 notice_mcp.log 留痕（规格 §7 验收第 4 步），但**不许记 message 原文**。"""
    seen = []
    monkeypatch.setattr(ns, "_log", lambda msg: seen.append(msg))

    stub_backend = {"ok": True, "id": 5, "deduped": True, "level": "warning"}
    monkeypatch.setattr(nc, "push", lambda *a, **kw: stub_backend)
    secret = "PRIVATE_NOTICE_TEXT_4c1a"
    out = ns._notify_nurse(secret, level="Critical", uid="elder_9")

    assert json.loads(out)["ok"] is True
    assert len(seen) == 1
    line = seen[0]
    assert line.startswith("notify_nurse level=warning")   # 用返回里实际生效的级别
    assert "has_uid=True" in line and "ok=True" in line and "deduped=True" in line and "id=5" in line
    assert secret not in line                              # 脱敏口径：正文不入日志

    # 异常路径同样要留痕（否则线上只看到模型收到一个类名，日志里什么都没有）
    seen.clear()
    monkeypatch.setattr(nc, "push", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert json.loads(ns._notify_nurse("x"))["ok"] is False
    assert seen and "boom" in seen[0]


# ---------------------------------------------------------------------------
# 接线：conf.MCP_SERVERS
# ---------------------------------------------------------------------------
def test_notice_mcp_configuration():
    import sys

    entry = conf.MCP_SERVERS["notice"]
    assert entry["command"] == sys.executable
    assert entry["args"] == [str(conf.BASE_DIR / "LLM" / "notice_mcp" / "notice_server.py")]
    assert entry["env"] == conf._notice_mcp_env()
    assert entry["env"]["NOTICE_BACKEND_URL"] == conf.NOTICE_BACKEND_URL
    assert entry["env"]["NOTICE_BACKEND_URL"] != ""
    assert entry["enabled"] is True
    # 三层都给：声纹识别失败会 fail-closed 落到 ward 层，那里堵死等于"老人求助喊不出来"
    assert entry["roles"] == ["elder", "ward", "admin"]


def test_notice_mcp_env_passes_optional_names_only_when_set(monkeypatch):
    """`NOTICE_TIMEOUT_S` / `NOTICE_MCP_LOG` 只有真的设了才传（空串会覆盖子进程默认值）。"""
    monkeypatch.delenv("NOTICE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("NOTICE_MCP_LOG", raising=False)
    assert set(conf._notice_mcp_env()) == {"NOTICE_BACKEND_URL"}
    monkeypatch.setenv("NOTICE_TIMEOUT_S", "2.5")
    assert conf._notice_mcp_env()["NOTICE_TIMEOUT_S"] == "2.5"


def test_notice_mcp_is_not_shadowed_by_a_local_tool():
    """MCP 工具与本地工具重名时**本地静默优先**（tools.py：`if name in _TOOL_REGISTRY: continue`）。

    将来谁在 `LLM/tool/` 下加一个同名工具，这个 MCP 版会无声消失 —— 用断言把它钉住。
    """
    assert "notify_nurse" not in tools._TOOL_REGISTRY


def test_message_max_matches_backend_truncation():
    """客户端截断长度必须与后端 `NOTIFY_BODY_MAX` 一致（否则工具回话的"已截断"提示会撒谎）。"""
    assert nc.MESSAGE_MAX == conf.NOTIFY_BODY_MAX


def test_notice_backend_url_is_nonempty_without_trailing_slash():
    assert conf.NOTICE_BACKEND_URL.startswith("http")
    assert not conf.NOTICE_BACKEND_URL.endswith("/")


# ---------------------------------------------------------------------------
# 闸门（两道 + 全局总开关）
# ---------------------------------------------------------------------------
def _principal(role):
    return {"role": role, "uid": "u", "slot": "kiosk", "ward_uid": "", "locked": False}


def _mcp_snapshot():
    return {"notify_nurse": {"server": "notice", "schema": {
        "type": "function",
        "function": {"name": "notify_nurse", "description": "", "parameters": {}}}}}


def _names(settings, principal):
    return [t["function"]["name"] for t in tools.effective_tools(settings, principal)]


@pytest.mark.parametrize("role", ["elder", "ward", "admin"])
def test_notify_nurse_visible_to_all_three_roles(monkeypatch, role):
    monkeypatch.setattr(tools.mcp_client, "tools", _mcp_snapshot)
    assert "notify_nurse" in _names({"mcp_enabled": True}, _principal(role))


def test_notify_nurse_hidden_when_mcp_disabled(monkeypatch):
    monkeypatch.setattr(tools.mcp_client, "tools", _mcp_snapshot)
    assert "notify_nurse" not in _names({"mcp_enabled": False}, _principal("elder"))


def test_notify_nurse_denied_when_server_roles_exclude_caller(monkeypatch):
    """服务器级 roles 是第二道闸门：把 notice 收窄到 admin，elder 就必须被拒。"""
    seen = []
    monkeypatch.setattr(tools.mcp_client, "tools", _mcp_snapshot)
    # `run_tool` 会读 settings 查总开关：这里打桩，别让用例依赖真库（跨文件跑时 DB_PATH 可能被
    # 别的用例改到不可写目录 —— 那是沙箱/隔离问题，与本用例的判别力无关）。
    monkeypatch.setattr("LLM.store.db.get_settings", lambda: {"mcp_enabled": True})
    monkeypatch.setattr("LLM.core.log.log", lambda event, **fields: seen.append((event, fields)))
    patched = dict(conf.MCP_SERVERS)
    patched["notice"] = {**conf.MCP_SERVERS["notice"], "roles": ["admin"]}
    monkeypatch.setattr(conf, "MCP_SERVERS", patched)

    r = tools.run_tool("notify_nurse", {"message": "测试", "level": "info"}, _principal("elder"))
    # 精确文案 + 审计 reason：总开关关闭分支的文案里也有"不允许"，只断言子串会因**错误原因**变绿。
    assert r["ok"] is False
    assert r["error"] == "当前身份不允许调用工具 notify_nurse"
    assert seen[-1][1]["reason"] == "tool_roles_mismatch"


def test_audit_args_redact_notify_nurse_message():
    """拒绝记录里绝不能出现要传达的原文（可能是老人私聊）。"""
    secret = "老人说他昨晚偷偷哭了"
    out = tools._audit_args("notify_nurse", {"message": secret, "level": "critical", "uid": "e1"})
    assert out == {"level": "critical", "has_uid": True}
    assert secret not in json.dumps(out, ensure_ascii=False)
    assert tools._audit_args("notify_nurse", None) == {"level": "", "has_uid": False}


def test_policy_deny_audit_has_no_message_text(monkeypatch):
    """走到真 dispatch 路径上再验一次：被拒时审计字段里不含 message。"""
    seen = []
    monkeypatch.setattr(tools.mcp_client, "tools", _mcp_snapshot)
    monkeypatch.setattr("LLM.store.db.get_settings", lambda: {"mcp_enabled": False})
    monkeypatch.setattr("LLM.core.log.log", lambda event, **fields: seen.append((event, fields)))

    secret = "PRIVATE_LLM_NOTICE_7f3a"
    r = tools.run_tool("notify_nurse", {"message": secret, "level": "warning"}, _principal("elder"))
    assert r["ok"] is False
    assert seen[-1][0] == "policy_deny"
    assert secret not in json.dumps(seen[-1][1], ensure_ascii=False)
    assert seen[-1][1]["args"] == {"level": "warning", "has_uid": False}
