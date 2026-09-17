# -*- coding: utf-8 -*-
from pathlib import Path

from LLM import conf
from LLM.agent import chat


def _ward_principal():
    return {"role": "ward", "uid": "ward-1", "ward_uid": ""}


def test_react_tool_round_limit_is_a_constant_not_a_setting():
    assert conf.REACT_MAX_TOOL_ROUNDS == 4
    assert "react_max_tool_rounds" not in conf.DEFAULT_SETTINGS


def test_react_prompt_file_uses_marker_and_is_loaded_after_role(monkeypatch):
    prompt_file = Path(__file__).with_name("react_prompt_marker.tmp.md")
    prompt_file.write_text("供人阅读的说明，不应发给模型\n\n<!-- PROMPT -->\n\nREACT_SENTINEL", encoding="utf-8")
    monkeypatch.setattr(chat, "REACT_PROMPT_FILE", prompt_file)
    monkeypatch.setattr(chat, "_load_role_prompt", lambda role: "ROLE_SENTINEL")
    monkeypatch.setattr(chat, "_ward_context", lambda *args: "")
    monkeypatch.setattr(chat.rag, "recall_v3", lambda *args: {"context": ""})
    monkeypatch.setattr(chat.db, "get_summary", lambda *args: "")
    monkeypatch.setattr(chat, "_expression_hint", lambda *args: "")

    try:
        system = chat.build_system("ward-1", {}, principal=_ward_principal())

        assert "供人阅读的说明" not in system
        assert "REACT_SENTINEL" in system
        assert system.index("ROLE_SENTINEL") < system.index("REACT_SENTINEL")
        assert system.index("REACT_SENTINEL") < system.index("【当前时间】")
    finally:
        prompt_file.unlink(missing_ok=True)


def test_missing_react_prompt_uses_fallback_and_warns_once_until_recovery(monkeypatch):
    prompt_file = Path(__file__).with_name("react_prompt_recovery.tmp.md")
    prompt_file.unlink(missing_ok=True)
    audits = []
    monkeypatch.setattr(chat, "REACT_PROMPT_FILE", prompt_file)
    monkeypatch.setattr(chat, "_react_prompt_warned", False)
    monkeypatch.setattr(chat.audit, "log", lambda *args, **kwargs: audits.append((args, kwargs)))

    try:
        first = chat._load_react_prompt()
        second = chat._load_react_prompt()

        assert first == second
        assert first
        assert len(audits) == 1
        assert audits[0][0] == ("chat",)
        assert audits[0][1]["action"] == "prompt_react_missing"

        prompt_file.write_text("<!-- PROMPT -->\nRECOVERED_SENTINEL", encoding="utf-8")
        assert chat._load_react_prompt() == "RECOVERED_SENTINEL"
        prompt_file.unlink()
        chat._load_react_prompt()

        assert len(audits) == 2
    finally:
        prompt_file.unlink(missing_ok=True)


def test_react_prompt_contains_required_tool_decision_rules():
    text = chat._load_react_prompt()
    for expected in (
        "仅必要时调用",
        "Observation",
        "成功和充分性",
        "改参",
        "换工具",
        "如实停止",
        "信息充分即答",
        "不输出内部 Thought",
        "协议",
        "虚构结果",
        "最多一句",
    ):
        assert expected in text
