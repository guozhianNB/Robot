# -*- coding: utf-8 -*-
r"""思考档位阶梯 + 思维链上屏（规格 docs/superpowers/specs/2026-09-17-thinking-mode-switch-design.md）。

覆盖：
  - `_apply_thinking_mode` 的五档组合（含「选不思考但敏感词命中仍加深」这条安全网）；
  - `_resolve_thinking_mode` 的请求体 > settings > auto 优先级与旧值 on/off 兼容；
  - `_thinking_extra`：effort 必须走**顶层** `reasoning_effort`（塞进 extra_body 会被
    DeepSeek 静默忽略 → 用户这边就是「选了强制也不思考」）；
  - `chat_stream` 端到端：meta 上报 mode/effort、none 下无 reasoning、敏感词下仍有 reasoning、
    low/high/max 各自把 effort 透到 API、只思考不作答时降级重答。
"""
import pytest

from LLM.agent import chat
from LLM.conf import DEFAULT_SETTINGS

SENSITIVE = "降压药能减半吗"      # 命中 THINKING_KEYWORDS（"药"/"降压药"）
DAILY = "今天天气怎么样"          # 规则/情绪都不命中且长度 < ROUTER_LLM_MIN_LEN → 路由 off


def _routed(on, reason="日常闲聊", method="off"):
    return {"on": on, "reason": reason, "method": method}


# ---------------------------------------------------------------- D1/D5 档位阶梯
def test_default_setting_is_auto():
    assert DEFAULT_SETTINGS["thinking_mode"] == "auto"


def test_auto_follows_router():
    assert chat._apply_thinking_mode("auto", _routed(False))[0] is None
    assert chat._apply_thinking_mode("auto", _routed(True, "主题「药」", "keyword"))[:3] == (
        "high", "主题「药」", "keyword")


@pytest.mark.parametrize("mode,effort,cn", [
    ("low", "low", "轻度"), ("high", "high", "中度"), ("max", "max", "重度"),
])
def test_forced_levels_pick_effort(mode, effort, cn):
    got_effort, reason, method, got_mode = chat._apply_thinking_mode(mode, _routed(False))
    assert got_effort == effort and method == "manual" and got_mode == mode
    assert cn in reason


def test_none_suppresses_daily_thinking():
    effort, reason, method, mode = chat._apply_thinking_mode("none", _routed(False))
    assert (effort, reason, method, mode) == (None, "用户手动关闭", "manual", "none")


@pytest.mark.parametrize("routed,method", [
    (_routed(True, "主题「降压药」", "keyword"), "keyword"),
    (_routed(True, "情绪「受不了」", "emotion"), "emotion"),
    (_routed(True, "LLM预判：涉及用药", "llm"), "llm"),
])
def test_none_keeps_sensitive_safety_net(routed, method):
    """手选「不思考」也关不掉安全网：敏感/情绪/预判命中一律照旧加深，且不谎报成 manual。"""
    effort, reason, got_method, mode = chat._apply_thinking_mode("none", routed)
    assert effort == chat.DEFAULT_ROUTED_EFFORT
    assert got_method == method, "命中来源必须如实上报，不许标成 manual"
    assert reason.startswith("敏感话题已自动加深：")
    assert mode == "none"          # 档位仍是用户选的 none，但本轮实际加深


# ---------------------------------------------------------------- 档位来源优先级
def test_request_body_wins_over_settings():
    assert chat._resolve_thinking_mode("none", {"thinking_mode": "max"}) == "none"
    assert chat._resolve_thinking_mode("max", {}) == "max"
    assert chat._resolve_thinking_mode("auto", {"thinking_mode": "max"}) == "auto"


def test_empty_body_defers_to_settings():
    """语音轮次不过前端（worker/voice_api 传空串）→ 只有 settings 能带手动档位。"""
    assert chat._resolve_thinking_mode("", {"thinking_mode": "none"}) == "none"
    assert chat._resolve_thinking_mode(None, {"thinking_mode": "max"}) == "max"


def test_legacy_on_off_values_still_work():
    """旧三档值（缓存里的老前端 bundle / 老设置）别名到新阶梯，不需要数据迁移。"""
    assert chat._resolve_thinking_mode("on", {}) == "high"
    assert chat._resolve_thinking_mode("off", {}) == "none"
    assert chat._resolve_thinking_mode("", {"thinking_mode": "off"}) == "none"
    assert chat._resolve_thinking_mode("", {"thinking_mode": "on"}) == "high"


def test_broken_settings_fall_back_to_auto():
    for bad in ("ON", "manual", "ultra", 1, None, ""):
        assert chat._resolve_thinking_mode("", {"thinking_mode": bad}) == "auto"


# ---------------------------------------------------------------- 请求参数形状（本轮的 bug 根因）
def test_effort_must_be_top_level_param():
    """effort 若塞进 extra_body，DeepSeek 当未知字段忽略 → 强制档位形同虚设（本轮真 bug）。"""
    assert chat._thinking_extra("high") == {"thinking": {"type": "enabled"}}
    assert chat._thinking_extra(None) == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in chat._thinking_extra("high")


# ---------------------------------------------------------------- chat_stream 端到端
class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)


class _FakeCompletions:
    """按 extra_body 判断是否开思考：开则先出 reasoning_content 再出正文；
    `answer_on_thinking` 为 False 时模拟"只想不说"（reasoning 有、content 空）。"""

    def __init__(self, answer_on_thinking=True):
        self.extra_bodies = []
        self.reasoning_efforts = []
        self.answer_on_thinking = answer_on_thinking
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        extra = kwargs.get("extra_body") or {}
        self.extra_bodies.append(extra)
        # reasoning_effort 是顶层参数：这里只认顶层，模拟服务端行为
        self.reasoning_efforts.append(kwargs.get("reasoning_effort"))
        thinking_on = extra.get("thinking", {}).get("type") == "enabled"
        chunks = []
        if thinking_on:
            chunks.append(type("Chunk", (), {"choices": [type("C", (), {
                "delta": type("D", (), {"reasoning_content": "我先想想。",
                                        "content": None, "tool_calls": None})(),
                "finish_reason": "stop" if not self.answer_on_thinking else None})()]})())
        if not thinking_on or self.answer_on_thinking:
            chunks.append(type("Chunk", (), {"choices": [type("C", (), {
                "delta": type("D", (), {"reasoning_content": None,
                                        "content": "请先问护士。", "tool_calls": None})(),
                "finish_reason": "stop"})()]})())
        return _FakeStream(chunks)


class _FakeClient:
    def __init__(self, answer_on_thinking=True):
        self.chat = type("Chat", (), {
            "completions": _FakeCompletions(answer_on_thinking)})()


def _collect(client, user_text, thinking, settings=None, principal=None):
    return list(chat.chat_stream(client, "test-model", "unit_test_uid", user_text, thinking,
                                 settings or {}, principal=principal))


@pytest.fixture(autouse=True)
def _no_persistence(monkeypatch):
    """chat_stream 尾部会落库/审计：单测里全部打桩，保持无副作用。"""
    monkeypatch.setattr(chat.db, "append_history", lambda *a, **k: None)
    monkeypatch.setattr(chat.db, "load_history", lambda *a, **k: [])
    monkeypatch.setattr(chat.db, "get_summary", lambda *a, **k: "")
    monkeypatch.setattr(chat.audit, "log", lambda *a, **k: None)
    monkeypatch.setattr(chat, "_expression_hint", lambda uid: "")
    monkeypatch.setattr(chat, "_load_prompt_base", lambda: "base")
    monkeypatch.setattr(chat, "_load_role_prompt", lambda role: "")
    monkeypatch.setattr(chat, "rag", type("R", (), {"recall_v3": staticmethod(
        lambda uid, q: {"context": ""})})())


def test_chat_stream_none_daily_has_no_reasoning():
    client = _FakeClient()
    events = _collect(client, DAILY, "none")
    meta = events[0]["router"]
    assert meta["on"] is False and meta["mode"] == "none" and meta["effort"] is None
    assert not [e for e in events if e["type"] == "reasoning"]
    assert [e for e in events if e["type"] == "content"]


def test_chat_stream_none_sensitive_still_reasons():
    client = _FakeClient()
    events = _collect(client, SENSITIVE, "none")
    meta = events[0]["router"]
    assert meta["on"] is True and meta["mode"] == "none"
    assert meta["effort"] == chat.DEFAULT_ROUTED_EFFORT
    assert meta["method"] == "keyword"
    assert meta["reason"].startswith("敏感话题已自动加深")
    assert [e for e in events if e["type"] == "reasoning"]


@pytest.mark.parametrize("mode,effort", [("low", "low"), ("high", "high"), ("max", "max")])
def test_chat_stream_forced_level_passes_effort_to_api(mode, effort):
    """强制档位必须真的把 effort 发给模型（顶层参数），否则只是界面好看。"""
    client = _FakeClient()
    events = _collect(client, DAILY, mode)
    assert events[0]["router"]["effort"] == effort
    assert [e for e in events if e["type"] == "reasoning"], "强制档必须真的有思维链"
    completions = client.chat.completions
    assert completions.reasoning_efforts[0] == effort
    assert completions.extra_bodies[0]["thinking"]["type"] == "enabled"


def test_chat_stream_settings_mode_used_when_body_empty():
    """语音轮次路径：voice_api 传 thinking=""，手动档位只可能来自 settings。"""
    client = _FakeClient()
    events = _collect(client, DAILY, "", {"thinking_mode": "high"})
    assert events[0]["router"]["mode"] == "high"
    assert events[0]["router"]["effort"] == "high"


def test_thinking_without_answer_falls_back_to_fast_reply():
    """只想不作答（content 空）→ 降级成不思考重答一次，老人那边必须有话说。"""
    client = _FakeClient(answer_on_thinking=False)
    events = _collect(client, DAILY, "max")
    contents = "".join(e["content"] for e in events if e["type"] == "content")
    assert contents == "请先问护士。", "降级重答必须产出正文"
    metas = [e["router"] for e in events if e["type"] == "meta"]
    assert metas[-1]["on"] is False and "降级重答" in metas[-1]["reason"]
    assert client.chat.completions.calls == 2, "空回复兜底只许重试一次"


def test_voice_api_defers_thinking_to_settings(monkeypatch):
    """锁死 voice_api 不许把档位写死成 auto：写死就顶掉了用户的手动选择（规格 D2）。

    设置从 db 里现取（这里的 stub 只为断言"取的是 settings、传的是空档"），不依赖真库内容。
    """
    from LLM.voice import voice_api
    from LLM import conf

    seen = {}
    marker = {"thinking_mode": "max"}

    def fake_stream(client, model, uid, text, thinking, settings, principal=None):
        seen.update(thinking=thinking, settings=settings)
        return iter(())

    monkeypatch.setattr(voice_api.db, "get_settings", lambda: marker)
    monkeypatch.setattr(chat, "chat_stream", fake_stream)

    fn = voice_api._stream_fn(object(), conf.MODEL)
    fn("elder_001", "你好")

    assert seen["thinking"] == ""                      # 空串 = 交给 settings 定档
    assert seen["settings"] is marker                  # 且真的是用 db.get_settings() 的结果
    assert conf.DEFAULT_SETTINGS["thinking_mode"] == "auto"
