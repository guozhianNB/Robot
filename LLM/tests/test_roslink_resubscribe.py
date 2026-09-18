# -*- coding: utf-8 -*-
r"""roslink 订阅生命周期测试（2026-09-19）。

盯的是一条**极易静默失效**的不变量：rosbridge 的 ``subscribe`` 是**连接级**的，
换一条 websocket 就全没了。旧实现只用一个 `_subscribed` 布尔，30s 复用窗口到期
重连后 `subscribe()` 直接早退 —— 现象是「rosbridge 明明已连接，却一条数据都收不到」
（2026-09-19 真机实测：强制重连后 `drain()` 恒为 0，而 rosbridge 日志里只有一次
`Subscribed to /map`）。上层 `locator.pose_payload()/current_map()` 每轮都先
`subscribe()` 再 `drain()`，所以这里锁死了「同连接幂等、换连接补发」。

全部用假 websocket 模块，不碰网络。
"""
import json

import pytest

from LLM.maps import roslink


class _FakeSock:
    """假连接：记录发出的帧，recv() 按脚本吐消息。"""

    def __init__(self, msgs=None, send_error=None):
        self.sent = []
        self.closed = False
        self.timeout = None
        self._msgs = list(msgs or [])
        self._send_error = send_error

    def send(self, payload):
        if self._send_error:
            raise self._send_error
        self.sent.append(json.loads(payload))

    def recv(self):
        if self._msgs:
            return self._msgs.pop(0)
        raise TimeoutError("no message")      # drain 里正常路径：超时继续磨

    def settimeout(self, t):
        self.timeout = t

    def close(self):
        self.closed = True


class _FakeWSModule:
    """替掉 `roslink.websocket`；每条新连接可指定脚本与发送故障。"""

    def __init__(self, msgs=None, send_error=None):
        self.connections = []
        self._msgs = msgs
        self._send_error = send_error

    def create_connection(self, url, timeout=None):
        sock = _FakeSock(msgs=self._msgs, send_error=self._send_error)
        self.connections.append(sock)
        return sock


def _topics(sock):
    return [m.get("topic") for m in sock.sent]


def _map_msg(width=88, height=107, resolution=0.05, origin=(-1.2, -1.38)):
    """一条真实形态的 /map 帧（rosbridge op=publish）。"""
    return json.dumps({
        "op": "publish", "topic": "/map",
        "msg": {"info": {"width": width, "height": height, "resolution": resolution,
                         "origin": {"position": {"x": origin[0], "y": origin[1], "z": 0.0},
                                    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}}},
    })


@pytest.fixture()
def fake_ws(monkeypatch):
    """装上假 websocket 模块并清干净全局态。"""
    fake = _FakeWSModule()
    monkeypatch.setattr(roslink, "websocket", fake)
    roslink.reset_for_test()
    yield fake
    roslink.reset_for_test()


def _force_reconnect():
    """把连接时间戳推到 30s 复用窗口之外，逼 `_connect()` 重建连接。"""
    roslink._ws_at -= 31


def test_subscribe_is_idempotent_on_same_connection(fake_ws):
    assert roslink.subscribe() is True
    assert roslink.subscribe() is True
    assert len(fake_ws.connections) == 1, "同一连接上不该重复建连"
    assert _topics(fake_ws.connections[0]) == ["/amcl_pose", "/map"]


def test_resubscribes_after_reconnect(fake_ws):
    """核心回归：重连后必须在新连接上补发订阅（旧实现在这里恒为空）。"""
    assert roslink.subscribe() is True
    _force_reconnect()
    assert roslink.subscribe() is True

    assert len(fake_ws.connections) == 2
    assert _topics(fake_ws.connections[1]) == ["/amcl_pose", "/map"]
    assert fake_ws.connections[0].closed is True


def test_drain_backfills_subscription_after_reconnect(fake_ws):
    """调用方只轮询 drain（不显式 subscribe）时，也要自愈。"""
    assert roslink.subscribe() is True
    _force_reconnect()
    roslink.drain(0.01)

    assert len(fake_ws.connections) == 2
    assert _topics(fake_ws.connections[1]) == ["/amcl_pose", "/map"]


def test_failed_send_marks_connection_dead(fake_ws):
    """发送失败 = 这条连接不能用：清掉它并让订阅态归零，下次重建。"""
    fake_ws._send_error = OSError("broken pipe")
    assert roslink.subscribe() is False
    assert roslink._ws is None
    assert roslink.status()["subscribed"] is False


def test_status_reports_subscribed_only_on_live_connection(fake_ws):
    assert roslink.status()["subscribed"] is False
    assert roslink.subscribe() is True
    assert roslink.status()["subscribed"] is True
    _force_reconnect()
    roslink.drain(0.01)
    assert roslink.status()["subscribed"] is True


def test_map_metadata_reaches_cache(monkeypatch):
    """端到端（假连接）：订阅 → 收 /map → latest_map 有元数据，供指纹反查用。"""
    fake = _FakeWSModule(msgs=[_map_msg()])
    monkeypatch.setattr(roslink, "websocket", fake)
    roslink.reset_for_test()
    try:
        assert roslink.subscribe() is True
        got = roslink.drain(0.01)
        assert got >= 1
        meta = roslink.latest_map()
        assert meta is not None
        assert (meta["width"], meta["height"]) == (88, 107)
        assert meta["resolution"] == pytest.approx(0.05)
        assert meta["origin"][:2] == pytest.approx([-1.2, -1.38])
    finally:
        roslink.reset_for_test()
