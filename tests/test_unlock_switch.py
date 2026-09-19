# -*- coding: utf-8 -*-
"""锁定/解锁下的声纹自动切换（回归：用户报告问题 2）。

场景：锁定 elder_001 → 声纹识别到 elder_003 → 不切换（只记审计）。
      解锁后识别到 elder_003 → 切换 current_uid + 广播 user_changed(voiceprint)。

**2026-09-15 重写**：原用例停在两代之前的契约上，一直被 `LLM/bus.py` 的收尾死锁
挡在后面看不见；死锁修好后暴露出来（3 条全挂在构造那一步）。两处口径都变了：

1. `VoiceWorker.__init__` 的第一个参数从 `chat_fn`（返回**字符串**）改成
   `stream_fn`（返回 chat_stream **事件流**）——旧写法直接 TypeError；
   旧用例还按 `_handle_speech(seg, settings)` 两参调用，现行签名是三参
   `(seg, text, settings)`。
2. **锁定语义已移交会话层**（`LLM/session.py` 的 `_shared["locked"]` + 主体 uid）：
   直接赋值 `w.locked_uid` 不参与任何判定（那只是语音可用时被同步的兼容镜像，
   语音降级/管理台登出后会陈旧）。所以这里改成驱动会话层。

本文件只测 worker 的"这轮算谁说的"行为，所以把**会读库**的两处挡在门外：
`session.derive_role`（角色推导）与 `session.db.get_profile`（病房归属）换成内存替身；
库/角色自身的口径由 `tests/test_session_api.py` 等用例负责。
"""
import pytest

from LLM import session as role_session
from LLM.voice import worker as worker_mod


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """每个用例一套干净的会话层状态；审计只进内存，不写真实 audit.jsonl。"""
    role_session.reset_for_test()
    monkeypatch.setattr(worker_mod.audit, "log",
                        lambda event, **kw: audit_calls.append((event, kw)))
    # 只测 worker 行为：角色推导/档案查询（都会读库）换内存替身
    monkeypatch.setattr(role_session, "derive_role",
                        lambda uid: "admin" if uid == "admin" else ("elder" if uid else "ward"))
    monkeypatch.setattr(role_session.db, "get_profile", lambda uid: {})
    yield
    role_session.reset_for_test()


audit_calls: list = []


@pytest.fixture(autouse=True)
def _clear_audit_calls():
    audit_calls.clear()
    yield
    audit_calls.clear()


def _stream_fn(uid, text):
    """新契约：返回 chat_stream 事件流（旧契约是直接返回字符串）。"""
    return [{"type": "content", "content": "回复：" + text}, {"type": "done"}]


def _make_worker():
    events = []

    def pub(ev, **payload):
        events.append((ev, payload))

    w = worker_mod.VoiceWorker(
        stream_fn=_stream_fn,
        post_turn_fn=lambda uid, user, assistant: None,
        publish_fn=pub,
    )
    return w, events


def _fusion_for(candidate, score):
    """构造返回指定 candidate 的 fake 融合层。"""
    return type(
        "Fusion", (),
        {"resolve": lambda self, seg: type(
            "Vote", (),
            {"candidate_uid": candidate, "confidence": score, "source": "voiceprint"})()},
    )()


def _run(w, text="你好", settings=None):
    """跑一轮"听到一句话"：只走到身份判定/广播那一步。

    应答链路（chat_stream 消费 + TTS 入队）另有职责，这里把它摘掉，避免测试里
    起真线程而产生时序不确定 —— 本文件断言的是 current_uid 与广播事件。
    """
    w._start_answer = lambda *a, **kw: None
    w._handle_speech("seg", text, settings or {"asr_enabled": True, "tts_enabled": False})


def test_locked_ignores_voiceprint_switch():
    """锁定时识别到锁定外用户：不切换、不广播 user_changed，只记审计。"""
    role_session.set_subject("elder_001", locked=True, slot="kiosk", source="manual")
    w, events = _make_worker()
    w.current_uid = "elder_001"
    w.fusion = _fusion_for("elder_003", 0.9)

    _run(w)

    assert w.current_uid == "elder_001"                       # 锁定：不切换
    assert "user_changed" not in [e[0] for e in events]        # 锁定：不广播切换
    # 但 voice_state 里仍是锁定的人（前端显示的身份不能被声纹带跑）
    rec = [e for e in events if e[0] == "voice_state"]
    assert rec and rec[0][1]["uid"] == "elder_001"
    # "只提示不切换"的审计痕迹必须留下（排查"为什么没切"靠它）
    assert any(kw.get("action") == "locked_ignored" for _ev, kw in audit_calls)


def test_unlocked_switches_uid_and_broadcasts():
    """解锁后识别到不同用户：切换 current_uid + 广播 user_changed(voiceprint)。"""
    role_session.set_subject("elder_001", locked=False, slot="kiosk", source="manual")
    w, events = _make_worker()
    w.current_uid = "elder_001"                                # 上一个用户
    w.fusion = _fusion_for("elder_003", 0.9)

    _run(w)

    assert w.current_uid == "elder_003"                        # 解锁：切到识别结果
    uc = [e for e in events if e[0] == "user_changed"]
    assert uc, "解锁后应广播 user_changed"
    # payload 必须与 server.py 手动切换**严格同形**（前端 parseBusPayload 只认这三个键）
    assert uc[0][1] == {"uid": "elder_003", "locked": False, "source": "voiceprint"}


def test_unlocked_low_confidence_keeps_current():
    """解锁但没认出来（candidate=None）：沿用当前 uid（宁问勿猜），不广播。"""
    role_session.set_subject("elder_001", locked=False, slot="kiosk", source="manual")
    w, events = _make_worker()
    w.current_uid = "elder_001"
    w.fusion = _fusion_for(None, 0.2)                          # 低置信/未识别

    _run(w)

    assert w.current_uid == "elder_001"                        # 宁问勿猜：沿用
    assert "user_changed" not in [e[0] for e in events]


def test_locked_low_confidence_stays_locked_subject():
    """锁定 + 没认出来：既不切换也不广播（锁定优先于"宁问勿猜"）。"""
    role_session.set_subject("elder_001", locked=True, slot="kiosk", source="manual")
    w, events = _make_worker()
    w.current_uid = "elder_001"
    w.fusion = _fusion_for(None, 0.0)

    _run(w)

    assert w.current_uid == "elder_001"
    assert "user_changed" not in [e[0] for e in events]
    assert not any(kw.get("action") == "locked_ignored" for _ev, kw in audit_calls)
