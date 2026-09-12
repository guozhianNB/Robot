#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""帧布局自动判定：收集真实帧字节，对多种候选布局逐一做 XOR 校验，报告哪种一致。

候选（LEN 字段 = 6 号字节的值）：
  A: 头(2) LEN(1) BODY(LEN) XOR(1)           帧长 = LEN+4
  B: 头(2) LEN(1) BODY(LEN+1) XOR(1)         帧长 = LEN+5
  C: 头(2) LEN(1) BODY(LEN-1) XOR(1)         帧长 = LEN+3
判定：取“整段流里全部帧校验都通过”的候选中，覆盖率最高的那个。

用法: python3 tools/frame_layout_judge.py --secs 4
"""
import argparse
import sys
import time

HEAD = b'\xaa\x55'


def xor_all(bs):
    v = 0
    for b in bs:
        v ^= b
    return v


def judge(data):
    """返回 [(候选名, 帧长表达式, 通过数, 失败数, 首帧详情)]"""
    results = []
    cands = [('A', lambda ln: ln + 4), ('B', lambda ln: ln + 5), ('C', lambda ln: ln + 3)]
    for name, flen in cands:
        i, ok, bad, first = 0, 0, 0, None
        while i + 4 <= len(data):
            if data[i:i + 2] != HEAD:
                i += 1
                continue
            ln = data[i + 2]
            total = flen(ln)
            if i + total > len(data):
                break
            frame = bytes(data[i:i + total])
            good = (xor_all(frame[:-1]) == frame[-1])
            if good:
                ok += 1
            else:
                bad += 1
            if first is None:
                first = (ln, total, frame.hex(' '), frame[-1], xor_all(frame[:-1]))
            i += total
        results.append((name, f'LEN{name}', ok, bad, first))
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='/dev/ttyACM0')
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--secs', type=float, default=4.0)
    args = ap.parse_args()

    import serial
    ser = serial.Serial(args.port, args.baud, timeout=0.2)
    t0 = time.time()
    data = bytearray()
    while time.time() - t0 < args.secs:
        data += ser.read(4096)
    ser.close()
    print(f'收到 {len(data)} 字节，AA55 {data.count(HEAD)} 次\n')

    for name, label, ok, bad, first in judge(bytes(data)):
        total = ok + bad
        pct = (100.0 * ok / total) if total else 0.0
        print(f'候选 {name}: 帧长 = {label} → 通过 {ok}/{total}（{pct:.0f}%）失败 {bad}')
        if first:
            ln, flen, hexs, last, calc = first
            print(f'   LEN={ln} 帧长={flen} 末字节=0x{last:02x} 计算XOR=0x{calc:02x}')
            print(f'   首帧完整字节: {hexs}')
        print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
