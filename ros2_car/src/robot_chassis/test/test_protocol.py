# -*- coding: utf-8 -*-
"""usb_protocol 单元测试。运行: pytest test/test_protocol.py"""

import pytest

from robot_chassis.usb_protocol import (
    build_frame,
    build_set_car_vel,
    build_stop,
    decode_status,
    FrameParser,
    CMD_SET_CAR_VEL,
    CMD_STATUS,
)


def _xor(frame: bytes) -> int:
    v = 0
    for b in frame[:-1]:
        v ^= b
    return v


def test_frame_header_and_xor():
    frame = build_frame(0x01)
    assert frame[:2] == b"\xAA\x55"
    assert frame[2] == 0 and frame[3] == 0x01
    assert _xor(frame) == frame[-1]


def test_set_car_vel_payload():
    frame = build_set_car_vel(200, 0, 0)
    assert frame[2] == 6
    assert frame[3] == CMD_SET_CAR_VEL
    assert frame[4:8] == (200).to_bytes(2, "little") + (0).to_bytes(2, "little")
    assert _xor(frame) == frame[-1]


def test_stop():
    assert build_stop() == b"\xAA\x55\x00\x01" + bytes([0xAA ^ 0x55 ^ 0x00 ^ 0x01])


def test_parser_full_stream():
    parser = FrameParser()
    frame = build_set_car_vel(100, 50, 30)
    parser.feed(frame)
    cmd, payload = parser.next_frame()
    assert cmd == CMD_SET_CAR_VEL
    assert len(payload) == 6


def test_parser_incremental():
    parser = FrameParser()
    frame = build_set_car_vel(100, 50, 30)
    # 逐字节喂入
    for i, b in enumerate(frame):
        parser.feed(bytes([b]))
        if i < len(frame) - 1:
            assert parser.next_frame() is None
    cmd, payload = parser.next_frame()
    assert cmd == CMD_SET_CAR_VEL


def test_parser_resync_on_garbage():
    parser = FrameParser()
    parser.feed(b"\x00\x01\x02" + build_stop())
    cmd, payload = parser.next_frame()
    assert cmd == 0x01


def test_parser_rejects_bad_checksum():
    parser = FrameParser()
    bad = bytearray(build_stop())
    bad[-1] ^= 0xFF
    parser.feed(bytes(bad) + build_stop())
    cmd, payload = parser.next_frame()
    assert cmd == 0x01  # 坏帧被丢弃，下一帧正常


def test_decode_status():
    payload = bytes([0x07]) + (60).to_bytes(2, "little") + (60).to_bytes(2, "little") \
        + (60).to_bytes(2, "little") + (60).to_bytes(2, "little") \
        + (1000).to_bytes(4, "little") + (1000).to_bytes(4, "little") \
        + (1000).to_bytes(4, "little") + (1000).to_bytes(4, "little") \
        + bytes([0x00])
    seq, rpm, enc, flags = decode_status(payload)
    assert seq == 0x07
    assert rpm == (60, 60, 60, 60)
    assert enc == (1000, 1000, 1000, 1000)
    assert flags == 0x00


def test_decode_status_wrong_len():
    with pytest.raises(ValueError):
        decode_status(b"\x00\x00")
