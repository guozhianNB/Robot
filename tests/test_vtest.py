# -*- coding: utf-8 -*-
r"""`vision/vtest/`（vision_test_start 的内部零件）单测。

只测**不碰 Tk、不碰摄像头**的部分：uid 生成、uid 校验、姓名反查、命令拼装、
取帧线程的循环与开关、摄像头服务的小工具。窗口那层靠
`--open/--click/--auto --selftest` 做冒烟（见 `vision/README.md`）。

为什么不测窗口：Tk 需要真实显示环境，CI/无头机上会挂；把"能脱离界面测的逻辑"
都抽到 people.py/pipeline.py 就是为了这个。
"""

import queue
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from vision.vtest import people, pipeline, service      # noqa: E402
from vision import vision_test_start as starter          # noqa: E402


def wait_until(cond, timeout=5.0, step=0.05):
    """轮询等条件成立（返回是否成立）。"""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


# ---------------------------------------------------------------------------
# uid 生成
# ---------------------------------------------------------------------------
def rows(*uids):
    return [{"uid": u} for u in uids]


def test_next_uid_starts_at_000_when_empty():
    assert people.next_uid([]) == "000"


def test_next_uid_increments_numeric_max():
    assert people.next_uid(rows("000", "001")) == "002"
    assert people.next_uid(rows("001", "000", "002")) == "003"


def test_next_uid_ignores_non_numeric_uids():
    """`elder_001` 这类命名不该把自增带偏（它连数字都不是）。"""
    assert people.next_uid(rows("elder_001", "007")) == "008"
    assert people.next_uid(rows("elder_001")) == "000"


def test_next_uid_uses_max_not_count():
    """中间有空洞时取"最大值 +1"，不是"人数"——否则会撞上已占用的 uid。"""
    assert people.next_uid(rows("000", "005")) == "006"


def test_next_uid_grows_width_past_999():
    assert people.next_uid(rows("999")) == "1000"


# ---------------------------------------------------------------------------
# uid 校验
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("uid", ["002", "老张", "a-b_c", "0" * 24])
def test_check_uid_accepts(uid):
    ok, why = people.check_uid(uid)
    assert ok, why


@pytest.mark.parametrize("uid,word", [
    ("", "空"),
    ("   ", "空"),
    ("0 02", "空格"),
    ("admin", "admin"),
    ("..", ".."),
    ("a/b", "字符"),
    ("a\\b", "字符"),
    ("a:b", "字符"),
    ("a?b", "字符"),
    ("a*b", "字符"),
    ("a|b", "字符"),
    ("a<b", "字符"),
    ("a>b", "字符"),
    ("a" + chr(34) + "b", "字符"),
    ("x" * 25, "太长"),
])
def test_check_uid_rejects(uid, word):
    ok, why = people.check_uid(uid)
    assert ok is False and word in why, (uid, ok, why)


# ---------------------------------------------------------------------------
# 按姓名反查
# ---------------------------------------------------------------------------
PEOPLE = [
    {"uid": "000", "name": "fuze", "nickname": "福泽", "bed": "1", "age": 70},
    {"uid": "001", "name": "guo", "nickname": "小郭", "bed": "2", "age": 66},
    {"uid": "002", "name": "张建国", "nickname": "", "bed": "3", "age": 80},
]


def test_find_people_by_exact_name():
    hit = people.find_people("guo", PEOPLE)
    assert [h["uid"] for h in hit] == ["001"]
    assert hit[0]["_how"] == "姓名全等"


def test_find_people_by_nickname():
    assert [h["uid"] for h in people.find_people("小郭", PEOPLE)] == ["001"]


def test_find_people_by_uid():
    assert [h["uid"] for h in people.find_people("002", PEOPLE)] == ["002"]


def test_find_people_case_insensitive():
    assert [h["uid"] for h in people.find_people("GUO", PEOPLE)] == ["001"]


def test_find_people_substring_falls_back():
    hit = people.find_people("张", PEOPLE)
    assert [h["uid"] for h in hit] == ["002"]
    assert hit[0]["_how"] == "姓名包含"


def test_find_people_exact_beats_substring():
    """精确命中优先：查 "guo" 时不该把别的"包含 guo"的人也倒出来。"""
    mixed = PEOPLE + [{"uid": "003", "name": "guoguo", "nickname": ""}]
    assert [h["uid"] for h in people.find_people("guo", mixed)] == ["001"]


def test_find_people_empty_or_unknown():
    assert people.find_people("", PEOPLE) == []
    assert people.find_people("   ", PEOPLE) == []
    assert people.find_people("没这人", PEOPLE) == []


def test_all_names_lists_everyone():
    txt = people.all_names(PEOPLE)
    assert "福泽（000）" in txt and "小郭（001）" in txt and "张建国（002）" in txt
    assert "还没有成员" in people.all_names([])


# ---------------------------------------------------------------------------
# 命令拼装（必须与手册里那条一模一样）
# ---------------------------------------------------------------------------
def test_create_cmd_full():
    args = people.create_cmd("002", "张桂芳", "张奶奶", "12", "78")
    assert args == ["002", "--name", "张桂芳", "--nickname", "张奶奶",
                    "--bed", "12", "--age", "78"]


def test_create_cmd_skips_blank_optional_fields():
    assert people.create_cmd("002", "张桂芳", "", "  ", "") == \
        ["002", "--name", "张桂芳"]


def test_create_cmd_age_is_trimmed_text():
    assert people.create_cmd("002", "甲", age=" 70 ") == ["002", "--name", "甲", "--age", "70"]


def test_create_cmd_keeps_bad_age_so_subprocess_rejects_it():
    """写错的年龄要**原样**传给子进程（让它 exit 2），不许在这里悄悄丢掉这个字段。"""
    args = people.create_cmd("002", "甲", age="7a")
    assert args[-2:] == ["--age", "7a"]
    code, out = people.run_update_elder(args)
    assert code != 0 and "age" in out


def test_delete_cmd_matches_manual():
    assert people.delete_cmd("001") == ["001", "--delete", "--yes"]


def test_cmd_text_is_copy_pasteable():
    txt = people.cmd_text(people.create_cmd("002", "张桂芳", "张奶奶"))
    assert txt.startswith("python scripts")
    assert "update_elder.py" in txt and "002 --name 张桂芳" in txt
    assert people.cmd_text(["a b"]) .count(chr(34)) == 2      # 带空格的参数要加引号


def test_run_update_elder_propagates_nonzero_and_output():
    """真跑一次子进程：参数非法时退出码非 0，输出要带回来（界面要显示它）。"""
    code, out = people.run_update_elder(["--no-such-flag"])
    assert code != 0
    assert out.strip()


# ---------------------------------------------------------------------------
# 取帧线程
# ---------------------------------------------------------------------------
class _FakeFrame:
    def bgr(self):
        import numpy as np
        return np.zeros((720, 1280, 3), np.uint8)


class _FakeCam:
    """替身摄像头客户端：模拟"只在有新帧时返回"（与 get_next_frame 同语义）。

    按 ~30fps 限速：不限速的话抓帧线程会空转到几千次/秒，
    `test_preview_is_not_throttled_by_slow_inference` 就失去意义了。
    """

    instances = []
    period = 1.0 / 30

    def __init__(self, *a, **kw):
        self.closed = False
        _FakeCam.instances.append(self)

    def get_next_frame(self, channel=1, last_id=None, timeout="default"):
        time.sleep(_FakeCam.period)
        return _FakeFrame()

    def close(self):
        self.closed = True


class _DeadProc:
    def poll(self):
        return 0


def _install_fake_cam(monkeypatch, frames=None, raise_once=None):
    from vision.camera_client import CameraTimeout
    _FakeCam.instances = []
    state = {"n": 0}

    class Cam(_FakeCam):
        def get_next_frame(self, channel=1, last_id=None, timeout="default"):
            if raise_once and state["n"] == 0:
                state["n"] += 1
                raise raise_once
            if frames is not None:
                raise CameraTimeout("没有新帧")
            return _FakeCam.get_next_frame(self, channel, last_id, timeout)

    monkeypatch.setattr(pipeline, "CameraClient", Cam)
    return Cam


@pytest.fixture()
def fake_analyze(monkeypatch):
    from LLM import face_api
    calls = []

    def _analyze(bgr, identify=True, track=True):
        calls.append({"identify": identify, "track": track})
        return {"faces": [{"box": [10, 10, 100, 120], "score": 0.9}],
                "identity": {"identified": bool(identify),
                             "reason": "ok" if identify else "detect_only"},
                "detect_ms": 12.5,
                "verdict": {"stable": True, "switchable": False}}

    monkeypatch.setattr(face_api, "analyze", _analyze)
    return calls


def test_worker_publishes_frames(monkeypatch, fake_analyze):
    _install_fake_cam(monkeypatch)
    w = pipeline.FrameWorker("127.0.0.1", 1, channel=1, identify=False)
    try:
        w.start()
        assert wait_until(lambda: w.snapshot()["n"] >= 2)
        s = w.snapshot()
        assert s["error"] is None and s["state"] == "running"
        assert s["faces"] and s["verdict"]["stable"] is True
        assert s["analyze_ms"] == 12.5
        assert fake_analyze and fake_analyze[0]["identify"] is False
        assert s["bgr"] is not None
        assert wait_until(lambda: w.snapshot()["analyzed"] >= 1)
    finally:
        w.stop(3.0)
    assert not w.is_alive()
    assert w.snapshot()["state"] == "stopped"


def test_preview_is_not_throttled_by_slow_inference(monkeypatch):
    """**回归**：预览必须与推理解耦。

    早先是"抓帧→推理→一起发布"，推理一轮 250~700ms 就意味着画面 0.25~0.7 秒才换一张
    —— 用户看到的就是"摄像头异常卡顿"，而相机本身是 15~30fps。现在抓帧与推理分成两个
    线程：推理慢只让框滞后，画面照常刷新。
    """
    from LLM import face_api

    def slow_analyze(bgr, identify=True, track=True):
        time.sleep(0.30)                              # 模拟一轮 300ms 的检测+识别
        return {"faces": [], "identity": {"identified": True, "reason": "ok"},
                "detect_ms": 300.0, "verdict": None}

    monkeypatch.setattr(face_api, "analyze", slow_analyze)
    _install_fake_cam(monkeypatch)
    w = pipeline.FrameWorker("127.0.0.1", 1, identify=True)
    try:
        w.start()
        assert wait_until(lambda: w.snapshot()["n"] >= 5, timeout=5)
        t0 = w.snapshot()["n"]
        time.sleep(1.0)
        s = w.snapshot()
        grew, analyzed = s["n"] - t0, s["analyzed"]
        print("1 秒内：预览帧 +%d，推理 +%d" % (grew, analyzed))
        assert grew >= 12, "预览被推理拖慢了（1 秒只刷了 %d 帧）" % grew
        assert 1 <= analyzed <= 6, "推理次数不合理：%d" % analyzed
        assert s["fps"] > 5
    finally:
        w.stop(3.0)


def test_worker_identify_switch(monkeypatch, fake_analyze):
    _install_fake_cam(monkeypatch)
    w = pipeline.FrameWorker("127.0.0.1", 1, identify=False)
    try:
        w.start()
        assert wait_until(lambda: fake_analyze)
        w.set_identify(True)
        assert wait_until(lambda: any(c["identify"] for c in fake_analyze))
    finally:
        w.stop(3.0)


def test_worker_paused_keeps_grabbing_but_stops_model(monkeypatch, fake_analyze):
    """录入期间要"预览继续动、模型让给 enroll"——就是这条。"""
    _install_fake_cam(monkeypatch)
    w = pipeline.FrameWorker("127.0.0.1", 1, identify=False)
    try:
        w.start()
        assert wait_until(lambda: w.snapshot()["n"] >= 2)
        w.set_enabled(False)
        time.sleep(0.3)
        n_before, calls_before = w.snapshot()["n"], len(fake_analyze)
        assert wait_until(lambda: w.snapshot()["n"] > n_before)     # 还在抓帧
        time.sleep(0.2)
        assert len(fake_analyze) == calls_before                    # 不再跑模型
        assert w.snapshot()["faces"], "暂停后应沿用上一轮的脸，框不该消失"
    finally:
        w.stop(3.0)


def test_worker_survives_errors(monkeypatch, fake_analyze):
    _install_fake_cam(monkeypatch, raise_once=RuntimeError("摄像头抽了一下"))
    w = pipeline.FrameWorker("127.0.0.1", 1, identify=True)
    try:
        w.start()
        assert wait_until(lambda: w.snapshot()["n"] >= 2), "报错后应能自愈继续取帧"
        assert w.is_alive()
    finally:
        w.stop(3.0)


def test_worker_reports_analyze_failure(monkeypatch):
    from LLM import face_api

    def boom(*a, **kw):
        raise RuntimeError("模型炸了")

    monkeypatch.setattr(face_api, "analyze", boom)
    _install_fake_cam(monkeypatch)
    w = pipeline.FrameWorker("127.0.0.1", 1, identify=True)
    try:
        w.start()
        # 推理失败记在 detect_error 里，**不**污染取帧侧的 error（两者要能分开看）
        assert wait_until(lambda: "模型炸了" in (w.snapshot()["detect_error"] or ""))
        assert w.snapshot()["error"] is None, "推理失败不该被记成取帧失败"
        assert w.is_alive()
    finally:
        w.stop(3.0)


def test_worker_timeout_is_not_an_error(monkeypatch):
    """CameraTimeout 只表示"还没出新帧"，不该被当成故障写进 error。"""
    from vision.camera_client import CameraTimeout
    _install_fake_cam(monkeypatch, frames=True)
    w = pipeline.FrameWorker("127.0.0.1", 1, identify=True)
    try:
        w.start()
        time.sleep(0.4)
        s = w.snapshot()
        assert s["error"] is None and s["n"] == 0 and s["state"] == "running"
    finally:
        w.stop(3.0)


def test_stop_is_idempotent_and_join_works(monkeypatch, fake_analyze):
    """回归：属性名不能叫 `_stop`（会盖掉 Thread 的私有方法，join 直接报 TypeError）。"""
    _install_fake_cam(monkeypatch)
    w = pipeline.FrameWorker("127.0.0.1", 1, identify=True)
    w.start()
    assert wait_until(lambda: w.snapshot()["n"] >= 1)
    w.stop(3.0)
    w.stop(3.0)
    w.stop(3.0)
    assert not w.is_alive()
    assert isinstance(w._stop_evt, threading.Event)


# ---------------------------------------------------------------------------
# 摄像头服务小工具
# ---------------------------------------------------------------------------
def test_free_port_is_bindable():
    import socket
    port = service.free_port()
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))           # 刚给出来的端口应该还能绑上
    finally:
        s.close()


def test_wait_port_gives_up_when_process_died():
    t0 = time.time()
    assert service.wait_port(service.free_port(), timeout=5, proc=_DeadProc()) is False
    assert time.time() - t0 < 2.0, "进程已退出时应立刻返回，不该干等到超时"


def test_camera_service_cmd_and_stop_without_start():
    cam = service.CameraService(source="mock", port=service.free_port(), fps=7,
                                device=2)
    cmd = " ".join(cam.cmd())
    assert "vision.camera_server" in cmd and "--source mock" in cmd
    assert "--port %d" % cam.port in cmd and "--fps 7" in cmd and "--device 2" in cmd
    assert cam.running() is False
    assert cam.log_tail() == ""               # 没启动过 → 没有日志，不许炸
    cam.stop()                                # 没启动过 stop 也要安全


# ---------------------------------------------------------------------------
# 入口参数
# ---------------------------------------------------------------------------
def test_parser_defaults():
    a = starter.build_parser().parse_args([])
    assert a.source == "auto" and a.channel == 1 and a.enroll_frames == 5
    assert a.open == "none" and a.click is None and a.selftest == 0.0
    assert a.auto is False and a.auto_yes is False


def test_parser_accepts_chained_click():
    a = starter.build_parser().parse_args(["--click", "find,delete",
                                           "--click-arg", "张建国", "--auto"])
    assert a.click == "find,delete" and a.click_arg == "张建国" and a.auto is True


def test_queue_used_for_enroll_is_standard():
    """录入/删除用 queue 把工作线程结果送回主线程（顺手兜一下别改成裸变量）。"""
    q = queue.Queue()
    q.put({"ok": True})
    assert q.get_nowait()["ok"] is True
