# -*- coding: utf-8 -*-
r"""摄像头共享服务测试（vision/）——**完全不依赖板卡/摄像头/opencv**。

全部走 ``--mock`` 合成后端，因此可在任意机器（含 Windows PC）上跑。

覆盖：
  * protocol：帧头打包/解包往返、魔数与版本号校验、NV12 尺寸
  * 协议往返：INFO / PING / GET / NEXT / SUB / 未知命令 / 坏通道
  * 错误恢复：ERR 之后同一连接仍可用（不串话）
  * get_next_frame：严格递增、last_id 续传、**超时不再永久挂死**、超时后自动重连
  * frames()：连接在生成器结束/异常/提前 break 后都被释放
  * JPEG：帧头宽高报**编码器实际输出尺寸**（16 对齐后的），而非通道原始尺寸
  * 并发：多客户端同时取帧互不干扰
  * 参数解析：parse_channels 的合法与非法输入
"""
from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from vision import protocol as P  # noqa: E402
from vision.camera_client import (CameraClient, CameraNotRunning,  # noqa: E402
                                 CameraServerError, CameraTimeout, Frame)
from vision.camera_server import (CameraServer, MockBackend, align16,  # noqa: E402
                                  parse_channels)

CHANNELS = [(320, 240), (128, 128)]


def free_port():
    """向系统要一个当前空闲的端口（避免测试之间抢固定端口）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(channels=CHANNELS, fps=30, backend=None):
    """起一个 TCP 服务端并返回 (srv, port, thread)。

    ``backend`` 为 None 时用 MockBackend；传已 open 的后端可测 webcam 等来源。

    注意 ``CameraServer.start()`` 内部会阻塞在 ``serve_forever``，因此必须
    放在后台线程里调用（生产入口 main() 也是这么做的）。
    """
    port = free_port()
    if backend is None:
        backend = MockBackend(fps, channels)
    srv = CameraServer(backend, channels, fps, jpeg=False,
                       bind_host="127.0.0.1", bind_port=port)
    t = threading.Thread(target=srv.start, name="test-serve", daemon=True)
    t.start()
    # 等服务端真正 accept（bind 完成后端口才可用）
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                break
        except OSError:
            time.sleep(0.02)
    else:
        raise RuntimeError("测试服务端 10s 内未就绪")
    return srv, port, t


def stop_server(srv, t):
    srv.stop()
    t.join(timeout=5)


@pytest.fixture()
def server():
    """起一个 mock 服务端（真 TCP），测完关掉。"""
    srv, port, t = start_server()
    yield srv, port
    stop_server(srv, t)


@pytest.fixture()
def client(server):
    srv, port = server
    c = CameraClient(port=port, wait_timeout=5.0)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# protocol
# ---------------------------------------------------------------------------
def test_frame_header_roundtrip():
    raw = P.pack_frame_header(P.CMD_FRAME, 2, P.FMT_NV12, 320, 240, 42, 123456, 999)
    assert len(raw) == P.FRAME_HEADER_SIZE == 40
    meta = P.unpack_frame_header(raw)
    assert meta == {"cmd": P.CMD_FRAME, "channel": 2, "format": P.FMT_NV12,
                    "width": 320, "height": 240, "frame_id": 42,
                    "ts_us": 123456, "size": 999}


def test_frame_header_rejects_bad_magic_and_version():
    raw = bytearray(P.pack_frame_header(P.CMD_FRAME, 1, P.FMT_NV12, 8, 8, 1, 1, 0))
    raw[0:4] = b"XXXX"
    with pytest.raises(ValueError, match="magic"):
        P.unpack_frame_header(bytes(raw))

    raw = bytearray(P.pack_frame_header(P.CMD_FRAME, 1, P.FMT_NV12, 8, 8, 1, 1, 0))
    raw[4] = P.PROTOCOL_VERSION + 1
    with pytest.raises(ValueError, match="version"):
        P.unpack_frame_header(bytes(raw))


def test_unpack_truncated_header_raises():
    with pytest.raises(ValueError, match="truncated"):
        P.unpack_frame_header(b"VCAM" + b"\x00" * 10)


def test_nv12_size():
    assert P.nv12_size(320, 240) == 320 * 240 * 3 // 2


def test_align16():
    assert align16(1920) == 1920
    assert align16(1080) == 1072        # 这就是 JPEG 尺寸错位的根源
    assert align16(512) == 512


# ---------------------------------------------------------------------------
# 协议往返 / 错误路径
# ---------------------------------------------------------------------------
def test_info_and_ping(client):
    info = client.info()
    assert info["ok"] is True
    assert info["mode"] == "mock"
    assert info["protocol_version"] == P.PROTOCOL_VERSION
    assert set(info["channels"]) == {"1", "2"}
    assert info["channels"]["1"]["width"] == 320
    assert client.ping() is True


def test_get_frame_matches_declared_geometry(client):
    f = client.get_frame(channel=1)
    assert isinstance(f, Frame)
    assert f.fmt == "NV12"
    assert (f.width, f.height) == (320, 240)
    assert len(f.data) == P.nv12_size(320, 240)
    # 第二路走另一个通道号，验证通道隔离
    f2 = client.get_frame(channel=2)
    assert (f2.width, f2.height) == (128, 128)
    assert len(f2.data) == P.nv12_size(128, 128)


def test_bad_channel_is_rejected_and_connection_survives(client):
    """坏通道返回 ERR，且**同一连接**随后仍能正常取帧（不串话）。"""
    with pytest.raises(CameraServerError, match="bad channel"):
        client.get_frame(channel=99)
    f = client.get_frame(channel=1)          # 复用同一连接
    assert f.frame_id > 0
    assert client.ping() is True


def test_unknown_command_is_rejected(client):
    client._connect()
    client._conn.settimeout(5)
    client._conn.sendall(b"X")
    line = client._read_line(client._conn)      # 原始行：b"ERR unknown command"
    assert line.startswith(P.ERR_PREFIX)
    with pytest.raises(CameraServerError, match="unknown command"):
        client._check_err(line)
    # 连接未被服务端关闭：后续 ping 正常
    assert client.ping() is True


def test_client_rejects_out_of_range_channel_locally(client):
    for bad in (0, -1, 256):
        with pytest.raises(ValueError):
            client.get_frame(channel=bad)


def test_jpeg_disabled_reports_error(client):
    with pytest.raises(CameraServerError, match="jpeg disabled"):
        client.get_jpeg(channel=1)


def test_not_running_raises_connect_error():
    c = CameraClient(port=free_port(), connect_timeout=1.0)
    with pytest.raises(CameraNotRunning):
        c.info()
    # CameraNotRunning 必须是 OSError 子类，否则 --status 的 except OSError 抓不住
    assert issubclass(CameraNotRunning, OSError)


def test_status_flag_survives_unreachable_host():
    """--status 在服务未运行时返回 1，而不是抛栈。"""
    from vision.camera_server import main
    rc = main(["--status", "--port", str(free_port()), "--host", "127.0.0.1"])
    assert rc == 1


# ---------------------------------------------------------------------------
# get_next_frame —— 含回归：超时不再永久挂死
# ---------------------------------------------------------------------------
def test_get_next_frame_is_strictly_increasing(client):
    a = client.get_next_frame(channel=1, timeout=5.0)
    b = client.get_next_frame(channel=1, timeout=5.0)
    assert b.frame_id > a.frame_id


def test_get_next_frame_resumes_from_last_id(client):
    a = client.get_next_frame(channel=1, timeout=5.0)
    b = client.get_next_frame(channel=1, last_id=a.frame_id, timeout=5.0)
    assert b.frame_id > a.frame_id
    # 传一个未来值 -> 服务端只会等，不该返回旧帧
    with pytest.raises(CameraTimeout):
        client.get_next_frame(channel=1, last_id=2 ** 63, timeout=1.0)


def test_get_next_frame_times_out_instead_of_hanging(client):
    """回归：旧实现 settimeout(None) + 大 last_id = 永久挂死。"""
    t0 = time.monotonic()
    with pytest.raises(CameraTimeout):
        client.get_next_frame(channel=1, last_id=2 ** 63, timeout=1.5)
    assert time.monotonic() - t0 < 10.0, "超时未生效，仍在挂死"


def test_client_recovers_after_get_next_frame_timeout(client):
    """回归：超时后连接被丢弃并自动重连，同一客户端仍可正常使用。

    旧实现会把服务端那笔迟到应答留在流里，导致后续 info()/取帧读到帧头。
    """
    with pytest.raises(CameraTimeout):
        client.get_next_frame(channel=1, last_id=2 ** 63, timeout=1.0)
    info = client.info()                    # 必须自动重连、读到完整 JSON
    assert info["ok"] is True
    f = client.get_next_frame(channel=1, timeout=5.0)
    assert f.frame_id > 0


def test_wait_timeout_from_constructor_is_used():
    """构造函数的 wait_timeout 作为默认值生效（不显式传 timeout）。"""
    srv, port, t = start_server()
    try:
        c = CameraClient(port=port, wait_timeout=1.0)
        t0 = time.monotonic()
        with pytest.raises(CameraTimeout):
            c.get_next_frame(channel=1, last_id=2 ** 63)
        assert time.monotonic() - t0 < 8.0
        c.close()
    finally:
        stop_server(srv, t)


# ---------------------------------------------------------------------------
# frames() —— 连接释放
# ---------------------------------------------------------------------------
def test_frames_stream_yields_increasing_ids(client):
    got = []
    for fr in client.frames(channel=1):
        got.append(fr.frame_id)
        if len(got) >= 3:
            break
    assert len(got) == 3
    assert got == sorted(got) and len(set(got)) == 3


def test_frames_closes_connection_on_generator_close(client):
    """回归：生成器 close 后订阅连接必须被释放（旧实现只在循环体内 finally）。"""
    gen = client.frames(channel=1)
    next(gen)                     # 建立连接并取到第一帧
    gen.close()
    # 若连接泄漏，服务端线程会一直挂着；这里用"能再开多个订阅"间接验证
    for _ in range(3):
        g = client.frames(channel=1)
        next(g)
        g.close()


def test_frames_connection_closed_when_server_stops():
    """服务端停掉后，客户端生成器应抛错并被清理，而不是永久泄漏。"""
    srv, port, t = start_server()
    c = CameraClient(port=port, wait_timeout=2.0)
    gen = c.frames(channel=1)
    next(gen)
    srv._stop.set()
    stop_server(srv, t)
    with pytest.raises((CameraServerError, OSError)):
        for _ in range(50):
            next(gen)
    c.close()


# ---------------------------------------------------------------------------
# JPEG 帧头尺寸（回归）
# ---------------------------------------------------------------------------
class _FakeEncoder:
    """替身编码器：不需要 JPU，只验证帧头用哪个尺寸。"""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.calls = 0

    def encode_file(self, data):
        self.calls += 1

    def get_img(self):
        return b"\xff\xd8\xff\xe0FAKE" + b"\x00" * 16


def _server_with_fake_jpeg(channels):
    port = free_port()
    srv = CameraServer(MockBackend(30, channels), channels, 30, jpeg=False,
                       bind_host="127.0.0.1", bind_port=port)
    srv._encoder = _FakeEncoder(*(align16(v) for v in channels[0]))
    srv._jpeg_size = (align16(channels[0][0]), align16(channels[0][1]))
    srv._latest[1] = b"\x00" * P.nv12_size(*channels[0])
    srv._meta[1] = (7, 12345)
    return srv


def test_jpeg_header_reports_encoded_size_not_channel_size():
    """回归：1920x1080 被编码成 1920x1072，帧头必须报 1072。"""
    srv = _server_with_fake_jpeg([(1920, 1080)])
    sent = []

    class _Conn:
        def sendall(self, d):
            sent.append(d)

    srv._send_frame(_Conn(), 1, want_jpeg=True)
    meta = struct.unpack(">4sBBB4sHHQQQB", sent[0][:P.FRAME_HEADER_SIZE])
    assert meta[4] == P.FMT_JPEG
    assert (meta[5], meta[6]) == (1920, 1072)
    assert (meta[5], meta[6]) != (1920, 1080), "帧头还在报通道原始尺寸"
    # payload 长度必须等于帧头声明的 size
    assert len(sent[0]) - P.FRAME_HEADER_SIZE == meta[9]


def test_jpeg_header_size_when_already_aligned():
    srv = _server_with_fake_jpeg([(512, 512)])
    sent = []

    class _Conn:
        def sendall(self, d):
            sent.append(d)

    srv._send_frame(_Conn(), 1, want_jpeg=True)
    meta = struct.unpack(">4sBBB4sHHQQQB", sent[0][:P.FRAME_HEADER_SIZE])
    assert (meta[5], meta[6]) == (512, 512)


def test_nv12_header_unchanged_by_jpeg_fix():
    """JPEG 的尺寸改动不能污染 NV12 路径：NV12 仍报通道原始尺寸。"""
    srv = _server_with_fake_jpeg([(1920, 1080)])
    sent = []

    class _Conn:
        def sendall(self, d):
            sent.append(d)

    srv._send_frame(_Conn(), 1, want_jpeg=False)
    meta = struct.unpack(">4sBBB4sHHQQQB", sent[0][:P.FRAME_HEADER_SIZE])
    assert meta[4] == P.FMT_NV12
    assert (meta[5], meta[6]) == (1920, 1080)


def test_info_exposes_jpeg_size():
    srv = _server_with_fake_jpeg([(1920, 1080)])
    assert srv.info()["jpeg_size"] == "1920x1072"


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------
def test_multiple_clients_are_independent(server):
    """多客户端并发取帧互不干扰（这是本模块存在的理由）。"""
    srv, port = server
    results, errors = [], []

    def worker(idx):
        try:
            with CameraClient(port=port, wait_timeout=5.0) as c:
                ids = [c.get_next_frame(channel=idx % 2 + 1,
                                        timeout=5.0).frame_id for _ in range(4)]
                results.append(ids)
        except Exception as e:                      # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors, errors
    assert len(results) == 6
    for ids in results:
        assert ids == sorted(ids) and len(set(ids)) == 4


def test_concurrent_get_frame_and_stream(server):
    """一边订阅、一边轮询取帧，两者都能跑通。"""
    srv, port = server
    errors = []

    def streamer():
        try:
            with CameraClient(port=port, wait_timeout=5.0) as c:
                n = 0
                for _ in c.frames(channel=1):
                    n += 1
                    if n >= 5:
                        break
        except Exception as e:                      # noqa: BLE001
            errors.append("stream:" + repr(e))

    def poller():
        try:
            with CameraClient(port=port, wait_timeout=5.0) as c:
                for _ in range(5):
                    c.get_frame(channel=2)
        except Exception as e:                      # noqa: BLE001
            errors.append("poll:" + repr(e))

    ts = [threading.Thread(target=streamer), threading.Thread(target=poller)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)
    assert not errors, errors


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def test_parse_channels_valid():
    assert parse_channels("1920x1080,512x512") == [(1920, 1080), (512, 512)]
    assert parse_channels(" 640x480 ") == [(640, 480)]


@pytest.mark.parametrize("spec", ["1920x1079", "0x100", "1920", "axb", "", "640x-2"])
def test_parse_channels_invalid(spec):
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        parse_channels(spec)


# ---------------------------------------------------------------------------
# Frame 数据访问（不依赖 opencv）
# ---------------------------------------------------------------------------
def test_frame_nv12_array_shape(client):
    np = pytest.importorskip("numpy")
    f = client.get_frame(channel=2)
    arr = f.nv12_array()
    assert arr.dtype == np.uint8
    assert arr.size == P.nv12_size(128, 128)


def test_frame_save_roundtrip(client, tmp_path):
    f = client.get_frame(channel=2)
    out = tmp_path / "f.yuv"
    f.save(str(out))
    assert out.stat().st_size == P.nv12_size(128, 128)
    assert out.read_bytes() == f.data


def test_frame_repr_does_not_crash(client):
    assert "Frame(" in repr(client.get_frame(channel=2))


# ---------------------------------------------------------------------------
# webbridge：stdlib JPEG 编码器（这是让"PC 浏览器能看图"的关键，必须真能解码）
# ---------------------------------------------------------------------------
def _mock_nv12(w, h):
    b = MockBackend(10, [(w, h)])
    b.open()
    return b.next_frame()[1][2]


@pytest.mark.parametrize("size", [(8, 8), (16, 16), (64, 64), (320, 240)])
def test_software_jpeg_is_decodable(size):
    """回归：早期版本的 DQT 未按 zigzag 排序 + SOF0 分量字节错位 + 位写入器
    会无限膨胀，产出"结构合法但解码报 broken data stream"的图。这里用
    Pillow 真正解码，确保不是仅仅"字节看起来对"。"""
    Image = pytest.importorskip("PIL.Image")
    import io

    from vision.webbridge import nv12_to_jpeg
    w, h = size
    jpg = nv12_to_jpeg(_mock_nv12(w, h), w, h, quality=80, max_width=max(w, 8))
    assert jpg[:2] == b"\xff\xd8" and jpg[-2:] == b"\xff\xd9"
    im = Image.open(io.BytesIO(jpg))
    im.load()                                    # 不抛异常才算通过
    assert im.mode == "RGB"
    assert im.size[0] % 8 == 0 and im.size[1] % 8 == 0


def test_software_jpeg_preserves_brightness_gradient():
    """像素要真的对得上：mock 帧是横向渐变，解码后应仍单调递增。"""
    Image = pytest.importorskip("PIL.Image")
    import io

    from vision.webbridge import nv12_to_jpeg
    w, h = 320, 240
    data = _mock_nv12(w, h)
    jpg = nv12_to_jpeg(data, w, h, quality=90)
    im = Image.open(io.BytesIO(jpg)).convert("L")
    row = [im.getpixel((x, 120)) for x in (5, 100, 200, 315)]
    assert row == sorted(row), "渐变被破坏：%s" % row
    assert row[-1] - row[0] > 150, "对比度丢失：%s" % row


def test_software_jpeg_quality_affects_size():
    from vision.webbridge import nv12_to_jpeg
    data = _mock_nv12(320, 240)
    lo = nv12_to_jpeg(data, 320, 240, quality=20)
    hi = nv12_to_jpeg(data, 320, 240, quality=95)
    assert len(lo) < len(hi)


def test_software_jpeg_downsampling_keeps_decode_valid():
    """320 宽在 max_width=160 时要下采样，且输出的行宽必须与 SOF 一致。"""
    Image = pytest.importorskip("PIL.Image")
    import io

    from vision.webbridge import nv12_to_jpeg
    jpg = nv12_to_jpeg(_mock_nv12(320, 240), 320, 240, quality=80, max_width=160)
    im = Image.open(io.BytesIO(jpg))
    im.load()
    assert im.size[0] <= 160


def test_software_jpeg_rejects_short_buffer():
    from vision.webbridge import nv12_to_jpeg
    with pytest.raises(ValueError):
        nv12_to_jpeg(b"\x00" * 10, 320, 240)


def test_bitwriter_matches_ground_truth():
    """回归：位写入器必须只保留未满 8 位的余量，否则长码字会让 acc 无界膨胀。

    注意：写完后 flush() 会补 1 位结束，所以实际输出是 ground truth 的
    **前缀 + 填充**，断言只比较前缀。
    """
    from vision.webbridge import _BitWriter
    bw = _BitWriter()
    seq = [(0x1FF, 9), (0x2A, 6), (0x3, 2), (0x155, 9), (0x0F, 8)]
    expected = "".join(format(v & ((1 << L) - 1), "0%db" % L) for v, L in seq)
    for v, L in seq:
        bw.write(v, L)
    bw.flush()                                   # 补 1 位结束
    # 去掉字节填充（0xFF 后的 0x00）后与 ground truth 前缀比较
    raw = bytes(bw.buf)
    out = bytearray()
    j = 0
    while j < len(raw):
        out.append(raw[j])
        if raw[j] == 0xFF and j + 1 < len(raw) and raw[j + 1] == 0:
            j += 2
        else:
            j += 1
    got = "".join(format(b, "08b") for b in out)
    assert got[:len(expected)] == expected
    assert got[len(expected):] == "1" * (len(got) - len(expected)), \
        "flush 应该用 1 填充收尾"


# ---------------------------------------------------------------------------
# webbridge：/api/vision/* 的行为（不启后端 lifespan，直接调路由函数）
# ---------------------------------------------------------------------------
@pytest.fixture()
def closed_webbridge_port(monkeypatch):
    """把 webbridge 指向一个**确定没有服务**的端口，验证降级。

    注意：地址来源是 ``webbridge.default_host()/default_port()``（读 conf 的
    VISION_HOST/VISION_PORT），**不是** protocol 的 DEFAULT_PORT —— 后者只是
    conf 的兜底值，直接改它不再生效（2026-09-14 引入远端地址配置后）。
    """
    from vision import webbridge
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(webbridge, "default_host", lambda: "127.0.0.1")
    monkeypatch.setattr(webbridge, "default_port", lambda: free)
    webbridge.reset_cache()
    yield free
    webbridge.reset_cache()


def test_web_status_unavailable_is_ok_true(closed_webbridge_port):
    """查询类：服务健康 ≠ 功能可用 -> ok 必须为 True，status=unavailable。"""
    from vision import webbridge
    st = webbridge.status(force=True)
    assert st["ok"] is True
    assert st["status"] == "unavailable"
    assert "reason" in st


def test_web_snapshot_route_returns_503_when_down(closed_webbridge_port):
    """写/取数类：摄像头不可用时返回 503 且 ok=False。"""
    import asyncio

    from LLM import server as S
    resp = asyncio.run(S.vision_snapshot(channel=1, quality=80, token=None))
    assert resp.status_code == 503
    assert b'"ok":false' in resp.body.replace(b" ", b"") or b'"ok": false' in resp.body


def test_web_stream_ends_instead_of_hanging_when_down(closed_webbridge_port):
    """回归：摄像头不可用时 MJPEG 流必须直接结束，不能无限空转挂住请求。"""
    from vision import webbridge
    t0 = time.monotonic()
    n = sum(1 for _ in webbridge.mjpeg_stream(channel=1, max_frames=3))
    assert n == 0
    assert time.monotonic() - t0 < 20.0


def test_web_snapshot_serves_jpeg_when_running(monkeypatch):
    """摄像头在跑时，快照应返回可解码 JPEG（走硬件或软件编码都算对）。"""
    Image = pytest.importorskip("PIL.Image")
    import asyncio
    import io

    srv, port, t = start_server()
    from vision import webbridge
    monkeypatch.setattr(webbridge, "default_port", lambda: port)
    monkeypatch.setattr(webbridge, "default_host", lambda: "127.0.0.1")
    webbridge.reset_cache()
    try:
        from LLM import server as S
        resp = asyncio.run(S.vision_snapshot(channel=1, quality=80, token=None))
        assert resp.status_code == 200
        assert resp.media_type == "image/jpeg"
        assert resp.headers.get("X-Vision-Source") in ("hardware", "software")
        im = Image.open(io.BytesIO(resp.body))
        im.load()
        assert im.size[0] > 0
    finally:
        webbridge.reset_cache()
        stop_server(srv, t)


def test_web_get_jpeg_falls_back_to_software(client, monkeypatch):
    """服务端没开 --enable-jpeg 时，应自动退回软件编码而不是报错。"""
    from vision import webbridge
    monkeypatch.setattr(webbridge, "default_port", lambda: client.port)
    monkeypatch.setattr(webbridge, "default_host", lambda: "127.0.0.1")
    webbridge.reset_cache()
    try:
        jpg, w, h, source = webbridge.get_jpeg(channel=1, quality=70)
        assert source == "software"          # mock 服务未启用硬件 JPEG
        assert jpg[:2] == b"\xff\xd8"
        assert w == 320 and h == 240
    finally:
        webbridge.reset_cache()


# ===========================================================================
# WebcamBackend / --source 选择（用假 cv2，不需要真摄像头，也不需要装 opencv）
# ===========================================================================
import numpy as np  # noqa: E402

from vision import camera_server as CS  # noqa: E402
from vision import webcam as WC  # noqa: E402


class _FakeCap:
    """假 VideoCapture：可控的尺寸、读帧成败与生命周期。"""

    def __init__(self, device=0, size=(640, 480), opened=True,
                 fail_reads=0, always_fail=False):
        self.device = device
        self.size = size
        self.opened = opened
        self.fail_reads = fail_reads
        self.always_fail = always_fail
        self.released = False
        self.set_calls = []
        self.read_count = 0

    def isOpened(self):
        return self.opened and not self.released

    def set(self, prop, val):
        self.set_calls.append((prop, val))
        return True

    def get(self, prop):
        return self.size[0] if prop == _FakeCv2.CAP_PROP_FRAME_WIDTH else self.size[1]

    def read(self):
        self.read_count += 1
        if self.released or not self.opened:
            return False, None
        if self.always_fail:
            return False, None
        if self.fail_reads > 0:
            self.fail_reads -= 1
            return False, None
        w, h = self.size
        # 内容无关紧要，但要保证尺寸对、且是 BGR 三通道
        return True, np.full((h, w, 3), 64, dtype=np.uint8)

    def release(self):
        self.released = True


class _FakeCv2:
    """假 cv2：只实现 webcam.py 用到的那一小撮 API。"""

    CAP_DSHOW = 700
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4
    INTER_AREA = 1

    class _Logging:
        LOG_LEVEL_SILENT = 0

        @staticmethod
        def setLogLevel(_level):
            pass

    class utils:                       # noqa: N801  （模拟 cv2.utils）
        logging = None

    def __init__(self, devices=None):
        # devices: {设备号: _FakeCap}；缺的号视为打不开
        self.devices = devices or {}
        self.utils = type("utils", (), {"logging": _FakeCv2._Logging})()

    def VideoCapture(self, device, *_a):        # noqa: N802  （模拟 cv2 API）
        if device in self.devices:
            return self.devices[device]
        return _FakeCap(device=device, opened=False)

    def resize(self, frame, size, interpolation=None):   # noqa: N802
        """最近邻缩放（避免为了测试引入真 cv2）。"""
        w, h = int(size[0]), int(size[1])
        sh, sw = frame.shape[0], frame.shape[1]
        ys = (np.arange(h) * sh // h).clip(0, sh - 1)
        xs = (np.arange(w) * sw // w).clip(0, sw - 1)
        return frame[ys][:, xs]


def _use_fake_cv2(monkeypatch, devices=None):
    fake = _FakeCv2(devices)
    monkeypatch.setattr(WC, "_import_cv2", lambda: fake)
    return fake


# ---- bgr_to_nv12 数学 ----
@pytest.mark.parametrize("bgr,exp_y,exp_u,exp_v", [
    ((0, 0, 0), 16, 128, 128),            # 黑
    ((255, 255, 255), 235, 128, 128),     # 白
    ((0, 0, 255), 81, 90, 240),           # 纯红
    ((0, 255, 0), 144, 54, 34),           # 纯绿
    ((255, 0, 0), 40, 240, 109),          # 纯蓝
])
def test_bgr_to_nv12_known_colors(bgr, exp_y, exp_u, exp_v):
    """BT.601 limited range：亮度 16~235，色度 128 为中点。"""
    f = np.zeros((2, 2, 3), np.uint8)
    f[:, :] = bgr
    out = WC.bgr_to_nv12(f, 2, 2)
    assert len(out) == 2 * 2 * 3 // 2
    assert abs(out[0] - exp_y) <= 1            # Y 平面
    assert abs(out[4] - exp_u) <= 1            # UV 平面起点（Y 之后）
    assert abs(out[5] - exp_v) <= 1


def test_bgr_to_nv12_uv_is_interleaved_u_then_v():
    """NV12 的 UV 是交织的 U,V，不是 I420 的"整块 U 再整块 V"。"""
    f = np.zeros((2, 4, 3), np.uint8)          # 宽 4 -> UV 行内 2 对
    f[:, :] = (0, 0, 255)                      # 纯红 -> U≈90, V≈240
    out = WC.bgr_to_nv12(f, 4, 2)
    uv = out[4 * 2:]                           # Y 平面 8 字节之后
    assert len(uv) == 4
    assert abs(uv[0] - uv[2]) <= 1 and abs(uv[1] - uv[3]) <= 1   # 两对一致
    assert uv[1] > uv[0], "第一对应是 U 再 V（V 比 U 大）"


def test_bgr_to_nv12_chroma_is_2x2_average_not_top_left():
    """色度必须 2x2 平均；只取左上角会产生块状色噪。"""
    f = np.zeros((2, 2, 3), np.uint8)
    f[0, 0] = (255, 0, 0)      # 蓝
    f[0, 1] = (0, 255, 0)      # 绿
    f[1, 0] = (0, 0, 255)      # 红
    f[1, 1] = (255, 255, 255)  # 白
    out = WC.bgr_to_nv12(f, 2, 2)
    top_left_only = WC.bgr_to_nv12(np.full((2, 2, 3), (255, 0, 0), np.uint8), 2, 2)
    assert (out[4], out[5]) != (top_left_only[4], top_left_only[5])
    assert abs(out[4] - 128) <= 2 and abs(out[5] - 128) <= 2, "四色均值应接近中性"


def test_bgr_to_nv12_rejects_odd_size_and_bad_shape():
    f = np.zeros((2, 2, 3), np.uint8)
    with pytest.raises(ValueError):
        WC.bgr_to_nv12(f, 3, 2)                # 奇数宽
    with pytest.raises(ValueError):
        WC.bgr_to_nv12(f, 4, 4)                # 形状与声明不符


def test_bgr_to_nv12_roundtrips_through_cv2_if_available():
    """与 cv2 的 NV12->BGR 往返应基本还原（口径配套；装了 cv2 才跑）。

    输入必须是**平滑内容**（真实摄像头画面的形态）：4:2:0 的色度是 2x2 平均，
    平滑内容下块内色度几乎不变，故往返近乎无损。早前这条用例喂的是随机噪声，
    块内四个像素颜色互不相干，误差天然 ~44（任何正确实现都过不了严格容差），
    属于用例期望不成立而非实现有问题 —— 噪声输入的不变量见下一条用例。
    """
    cv2 = pytest.importorskip("cv2")
    w = h = 16
    f = np.zeros((h, w, 3), np.uint8)
    f[:, :, 0] = np.arange(w, dtype=np.uint8)[None, :] * 4      # B 横向渐变
    f[:, :, 1] = np.arange(h, dtype=np.uint8)[:, None] * 4      # G 纵向渐变
    f[:, :, 2] = 128                                           # R 常量
    nv12 = WC.bgr_to_nv12(f, w, h)
    arr = np.frombuffer(nv12, np.uint8).reshape(h * 3 // 2, w)
    back = cv2.cvtColor(arr, cv2.COLOR_YUV2BGR_NV12)
    assert back.shape == f.shape
    # 实测 ~1.5；留一倍余量。容差写死 12 会放过"色序/口径写错"这类真 bug。
    assert np.abs(back.astype(int) - f.astype(int)).mean() < 3


def test_bgr_to_nv12_cv2_fastpath_matches_numpy_reference():
    """cv2 快路径必须与纯 numpy 参考实现**逐像素等价**（±2）。

    2026-09-19：纯 numpy 版在 1280x720 要 **34ms/帧**（两次 float32 全幅运算），
    是"摄像头画面卡顿"的真实来源之一；加了 cv2 快路径后 5.8ms。这条用例防的是
    "为了提速把色彩口径悄悄改坏" —— 顺带记一个坑：**不能整幅直接用
    `COLOR_BGR2YUV_I420`**，它那份色度是取 2x2 块的左上角像素（实测与平均差 55），
    会产生块状色噪；所以快路径只用它拿 Y 平面，色度自己按 2x2 平均算。
    """
    pytest.importorskip("cv2")
    rng = np.random.default_rng(0)
    for w, h in ((16, 16), (64, 48)):
        f = rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)
        fast = np.frombuffer(WC.bgr_to_nv12(f, w, h), np.uint8).astype(int)
        ref = np.frombuffer(WC._bgr_to_nv12_numpy(f, w, h), np.uint8).astype(int)
        hw = w * h
        assert np.abs(fast[:hw] - ref[:hw]).max() <= 2, "Y 平面口径变了"
        assert np.abs(fast[hw:] - ref[hw:]).max() <= 2, "UV 平面（含 U/V 顺序）口径变了"


def test_bgr_to_nv12_falls_back_to_numpy_without_cv2(monkeypatch):
    """没有 cv2 时必须退回纯 numpy 实现（口径完全一致，不是"降级到坏结果"）。"""
    monkeypatch.setattr(WC, "_CV2", None)
    monkeypatch.setattr(WC, "_CV2_TRIED", True)
    rng = np.random.default_rng(1)
    f = rng.integers(0, 256, size=(16, 16, 3), dtype=np.uint8)
    assert WC.bgr_to_nv12(f, 16, 16) == WC._bgr_to_nv12_numpy(f, 16, 16)


def test_bgr_to_nv12_noise_input_is_lossy_but_bounded():
    """随机噪声输入：只断言**必须成立的不变量**，不追求像素级还原。

    噪声图色度块间差异极大，4:2:0 往返误差天然在 ~44 量级（有损但应"有界、
    不崩、亮度口径仍与 cv2 一致"）。这条替代了原先"用噪声图要求 <12"的错误期望。
    """
    cv2 = pytest.importorskip("cv2")
    rng = np.random.default_rng(0)
    w = h = 16
    f = rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)
    nv12 = WC.bgr_to_nv12(f, w, h)
    assert len(nv12) == w * h * 3 // 2
    arr = np.frombuffer(nv12, np.uint8).reshape(h * 3 // 2, w)
    back = cv2.cvtColor(arr, cv2.COLOR_YUV2BGR_NV12)
    assert back.shape == f.shape

    # 亮度平面必须与 cv2 同口径（limited range BT.601），实测 maxdiff <= 1
    y_ours = arr[:h].astype(int)
    y_ref = cv2.cvtColor(f, cv2.COLOR_BGR2YUV_I420)[:h].astype(int)
    assert np.abs(y_ours - y_ref).max() <= 2

    # 色度有损但误差有界（不是彻底崩坏/饱和错位）
    assert np.abs(back.astype(int) - f.astype(int)).mean() < 60


# ---- WebcamBackend ----
def test_webcam_backend_produces_protocol_shaped_frames(monkeypatch):
    _use_fake_cv2(monkeypatch, {0: _FakeCap(size=(320, 240))})
    be = WC.WebcamBackend(device=0, fps=0, channels=[(320, 240)])
    be.open()
    try:
        frames = be.next_frame(timeout=0.1)
        fid, ts, data = frames[1]
        assert fid == 1 and ts > 0
        assert len(data) == 320 * 240 * 3 // 2
        assert be.channels == [(320, 240)]
    finally:
        be.close()


def test_webcam_backend_frame_id_increases(monkeypatch):
    _use_fake_cv2(monkeypatch, {0: _FakeCap(size=(320, 240))})
    be = WC.WebcamBackend(device=0, fps=0, channels=[(320, 240)]).open()
    try:
        ids = [be.next_frame(0.1)[1][0] for _ in range(3)]
        assert ids == sorted(ids) and len(set(ids)) == 3
    finally:
        be.close()


def test_webcam_backend_clamps_to_device_capability(monkeypatch):
    """设备只给 640x480 时，请求 1920x1080 必须按 640x480 产帧。

    否则 NV12 长度与帧头声明不符，下游必错位（2026-09-14 修过同类 bug）。
    """
    _use_fake_cv2(monkeypatch, {0: _FakeCap(size=(640, 480))})
    be = WC.WebcamBackend(device=0, fps=0, channels=[(1920, 1080)]).open()
    try:
        assert be.channels == [(640, 480)]
        _fid, _ts, data = be.next_frame(0.1)[1]
        assert len(data) == 640 * 480 * 3 // 2
    finally:
        be.close()


def test_webcam_backend_downscales_when_device_is_bigger(monkeypatch):
    _use_fake_cv2(monkeypatch, {0: _FakeCap(size=(1280, 960))})
    be = WC.WebcamBackend(device=0, fps=0, channels=[(320, 240)]).open()
    try:
        assert be.channels == [(320, 240)]
        assert len(be.next_frame(0.1)[1][2]) == 320 * 240 * 3 // 2
    finally:
        be.close()


def test_webcam_backend_multichannel_from_one_read(monkeypatch):
    """多通道从同一次读帧缩放而来（一个摄像头不可能同时出两种分辨率）。"""
    cap = _FakeCap(size=(640, 480))
    _use_fake_cv2(monkeypatch, {0: cap})
    be = WC.WebcamBackend(device=0, fps=0,
                          channels=[(640, 480), (320, 240)]).open()
    try:
        baseline = cap.read_count          # open() 会试读一帧，故看增量
        frames = be.next_frame(0.1)
        assert set(frames) == {1, 2}
        assert len(frames[1][2]) == 640 * 480 * 3 // 2
        assert len(frames[2][2]) == 320 * 240 * 3 // 2
        assert cap.read_count - baseline == 1, "一次 next_frame 只该读一次物理帧"
    finally:
        be.close()


def test_webcam_backend_open_is_idempotent(monkeypatch):
    _use_fake_cv2(monkeypatch, {0: _FakeCap(size=(320, 240))})
    be = WC.WebcamBackend(device=0, fps=0, channels=[(320, 240)])
    be.open()
    cap = be._cap
    be.open()                                  # 再次 open 不应换设备
    assert be._cap is cap
    be.close()
    be.close()                                 # close 幂等
    assert be._cap is None


def test_webcam_backend_missing_device_raises(monkeypatch):
    _use_fake_cv2(monkeypatch, {})              # 任何设备号都打不开
    be = WC.WebcamBackend(device=3, fps=0, channels=[(320, 240)])
    with pytest.raises(RuntimeError, match="设备 3 打不开"):
        be.open()


def test_webcam_backend_persistent_read_failure_raises(monkeypatch):
    """读帧持续失败要抛错，不能静默空转。"""
    _use_fake_cv2(monkeypatch, {0: _FakeCap(size=(320, 240), always_fail=True)})
    be = WC.WebcamBackend(device=0, fps=0, channels=[(320, 240)])
    with pytest.raises(RuntimeError, match="读不到画面"):
        be.open()


def test_webcam_backend_releases_on_open_failure(monkeypatch):
    """open 失败必须释放已拿到的设备句柄（不能泄漏）。"""
    cap = _FakeCap(size=(320, 240), always_fail=True)
    _use_fake_cv2(monkeypatch, {0: cap})
    be = WC.WebcamBackend(device=0, fps=0, channels=[(320, 240)])
    with pytest.raises(RuntimeError):
        be.open()
    assert cap.released is True


def test_list_cameras_reports_only_readable(monkeypatch):
    _use_fake_cv2(monkeypatch, {0: _FakeCap(size=(320, 240)),
                                2: _FakeCap(size=(640, 480))})
    assert WC.list_cameras(max_index=5) == [0, 2]


def test_list_cameras_empty_when_no_device(monkeypatch):
    _use_fake_cv2(monkeypatch, {})
    assert WC.list_cameras(max_index=3) == []


def test_import_cv2_missing_gives_actionable_error(monkeypatch):
    """cv2 缺失时错误信息必须能照抄执行（无论本机是否真装了 cv2）。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "cv2":
            raise ImportError("No module named 'cv2'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="pip install opencv-python"):
        WC._import_cv2()


def test_camera_server_imports_without_cv2():
    """红线：camera_server / webbridge 的导入链不得硬依赖 cv2。"""
    import importlib
    for mod in ("vision.webcam", "vision.camera_server", "vision.webbridge"):
        importlib.import_module(mod)
    # cv2 若真未安装，这些模块也必须已经导入成功
    import sys as _sys
    assert "vision.webcam" in _sys.modules


# ---- --source 选择与信息 ----
def test_backend_kind_mapping():
    assert CS.backend_kind(MockBackend(10, CHANNELS)) == "mock"
    # 用 __new__ 避开 CameraBackend.__init__ 的 multiprocessing.Queue
    # （沙箱下创建管道会被拒），这里只验类型判定。
    assert CS.backend_kind(CS.CameraBackend.__new__(CS.CameraBackend)) == "mipi"
    assert CS.backend_kind(WC.WebcamBackend(device=0, fps=10,
                                            channels=CHANNELS)) == "webcam"


def test_source_candidates_explicit():
    for name in ("mock", "mipi", "webcam"):
        assert CS.source_candidates(name) == [name]


def test_source_candidates_auto_on_pc_prefers_webcam(monkeypatch):
    monkeypatch.setattr(CS.sys, "platform", "win32")
    monkeypatch.setattr(CS, "_board_hobot_vio_available", lambda: False)
    # PC 上没有 hobot_vio -> mipi 不可能成功，不该列入候选（否则白起子进程白等）
    assert CS.source_candidates("auto") == ["webcam"]


def test_source_candidates_auto_on_board_prefers_mipi(monkeypatch):
    monkeypatch.setattr(CS.sys, "platform", "linux")
    monkeypatch.setattr(CS, "_board_hobot_vio_available", lambda: True)
    assert CS.source_candidates("auto") == ["mipi", "webcam"]


def test_source_candidates_auto_linux_without_hobot_vio(monkeypatch):
    """Linux 但没有 hobot_vio：仍应能退回 webcam，而不是硬试 mipi。"""
    monkeypatch.setattr(CS.sys, "platform", "linux")
    monkeypatch.setattr(CS, "_board_hobot_vio_available", lambda: False)
    assert CS.source_candidates("auto") == ["webcam"]


def test_make_backend_mock_returns_effective_channels():
    be, chans = CS.make_backend("mock", 0, 10, [(320, 240)])
    try:
        assert CS.backend_kind(be) == "mock"
        assert chans == [(320, 240)]
    finally:
        be.close()


def test_make_backend_webcam_uses_clamped_channels(monkeypatch):
    _use_fake_cv2(monkeypatch, {0: _FakeCap(size=(640, 480))})
    be, chans = CS.make_backend("webcam", 0, 0, [(1920, 1080)])
    try:
        assert chans == [(640, 480)], "必须把收窄后的尺寸交给服务端做帧头"
    finally:
        be.close()


def test_make_backend_aggregates_all_failures(monkeypatch):
    """每个候选的失败原因都要出现在同一条汇总错误里（不只报第一个）。"""
    monkeypatch.setattr(CS, "source_candidates", lambda s: ["webcam", "mipi"])
    monkeypatch.setattr(WC, "_import_cv2",
                        lambda: (_ for _ in ()).throw(RuntimeError("WEB_原因")))

    class _BadMipi:
        """替身：构造成功但 open 必失败（真 CameraBackend 构造就会抢管道）。"""

        def __init__(self, *a, **kw):
            pass

        def open(self):
            raise RuntimeError("MIPI_原因")

        def close(self):
            pass

    monkeypatch.setattr(CS, "CameraBackend", _BadMipi)
    with pytest.raises(RuntimeError) as ei:
        CS.make_backend("auto", 0, 10, [(320, 240)])
    msg = str(ei.value)
    assert "WEB_原因" in msg and "MIPI_原因" in msg
    assert "list-cameras" in msg


def test_make_backend_falls_back_to_second_candidate(monkeypatch):
    """第一个候选失败要能落到第二个候选，而不是直接放弃。"""
    monkeypatch.setattr(CS, "source_candidates", lambda s: ["webcam", "mipi"])
    monkeypatch.setattr(WC, "_import_cv2",
                        lambda: (_ for _ in ()).throw(RuntimeError("no cv2")))

    class _OkBackend:
        """替身后端：open 必成功，用来证明"确实换到了第二个候选"。"""

        def __init__(self, *a, **kw):
            self.channels = [(320, 240)]
            self.opened = False

        def open(self):
            self.opened = True
            return self

        def close(self):
            self.opened = False

    monkeypatch.setattr(CS, "CameraBackend", _OkBackend)
    be, chans = CS.make_backend("auto", 0, 10, [(320, 240)])
    try:
        assert getattr(be, "opened") is True, "应落到第二个候选并成功打开"
        assert chans == [(320, 240)]
    finally:
        be.close()


def test_info_mode_is_webcam_for_webcam_backend():
    srv = CameraServer(WC.WebcamBackend(0, 10, [(320, 240)]), [(320, 240)], 10,
                       jpeg=False, bind_host="127.0.0.1", bind_port=free_port())
    info = srv.info()
    assert info["mode"] == "webcam"
    assert info["source"] == "webcam"
    assert info["device"] == 0


def test_main_rejects_unknown_source():
    """--source 只接受 auto/mipi/webcam/mock，其余由 argparse 直接拒绝。"""
    with pytest.raises(SystemExit):
        CS.main(["--source", "nonsense"])


# ---- 端到端：webcam 来源 -> client -> webbridge JPEG ----
def test_end_to_end_webcam_source_serves_decodable_jpeg(monkeypatch):
    """--source webcam + 假 cv2：整条链路出图，且 JPEG 真能解码。"""
    Image = pytest.importorskip("PIL.Image")
    import io

    _use_fake_cv2(monkeypatch, {0: _FakeCap(size=(320, 240))})
    be, chans = CS.make_backend("webcam", 0, 0, [(320, 240)])
    srv, port, t = start_server(channels=chans, fps=0, backend=be)
    from vision import webbridge
    old = webbridge.default_port()
    monkeypatch.setattr(webbridge, "default_port", lambda: port)
    webbridge.reset_cache()
    try:
        with CameraClient(port=port, wait_timeout=5.0) as c:
            assert c.info()["mode"] == "webcam"
            f = c.get_frame(channel=1)
            assert f.width == 320 and f.height == 240
        jpg, w, h, source = webbridge.get_jpeg(channel=1, quality=80)
        assert source == "software"
        im = Image.open(io.BytesIO(jpg))
        im.load()
        assert im.size == (320, 240)
    finally:
        webbridge.reset_cache()
        stop_server(srv, t)


# ---- webbridge 地址来自配置 ----
def test_webbridge_address_comes_from_conf(monkeypatch):
    from LLM import conf
    from vision import webbridge
    monkeypatch.setattr(conf, "VISION_HOST", "10.9.9.9", raising=False)
    monkeypatch.setattr(conf, "VISION_PORT", 1234, raising=False)
    assert webbridge.default_host() == "10.9.9.9"
    assert webbridge.default_port() == 1234


def test_webbridge_status_reports_target(monkeypatch):
    """状态里带解析出的地址，排查"连的是哪台"时不用猜配置。"""
    from vision import webbridge
    monkeypatch.setattr(webbridge, "default_host", lambda: "127.0.0.1")
    monkeypatch.setattr(webbridge, "default_port", lambda: free_port())
    webbridge.reset_cache()
    st = webbridge.status(force=True)
    assert st["target"]["host"] == "127.0.0.1"
    assert st["status"] == "unavailable"


def test_vision_snapshot_returns_jpeg(monkeypatch):
    """回归守卫：取到帧后必须能返回 JPEG。

    2026-09-15 的编辑器路由搬迁曾连带删掉 server.py 的模块级 `from fastapi.responses
    import Response`，使本路由在"相机可用"这条路径上抛 NameError → 500（相机不可用时
    走 except 分支返回 503，所以既有测试与全量都没抓到）。这里 monkeypatch 掉取帧，
    不依赖真实摄像头。
    """
    from fastapi.testclient import TestClient
    from vision import webbridge
    from LLM.server import app

    monkeypatch.setattr(webbridge, "get_jpeg",
                        lambda *a, **k: (b"\xff\xd8\xff\xd9", 1, 1, "mock"))
    r = TestClient(app).get("/api/vision/snapshot?channel=1")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
