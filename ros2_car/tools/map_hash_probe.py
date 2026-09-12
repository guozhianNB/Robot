#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盯 /map 栅格数据哈希：判断 slam 是否真的在更新地图内容（而非重复发同一张）。

用法（板卡，已 source 环境）:
    python3 tools/map_hash_probe.py --secs 30

每收到一帧 /map 打印：时间、尺寸、数据 md5、已知格/占用格数量。
"""
import argparse
import hashlib
import sys
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import (QoSProfile, ReliabilityPolicy, HistoryPolicy,
                       DurabilityPolicy)
from nav_msgs.msg import OccupancyGrid


class Probe(Node):
    def __init__(self):
        super().__init__('map_hash_probe')
        qos = QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE,
                         history=HistoryPolicy.KEEP_LAST,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(OccupancyGrid, '/map', self._on_map, qos)

    def _on_map(self, m):
        data = bytes(bytearray((b + 128) for b in m.data)) if False else bytes(m.data)
        h = hashlib.md5(bytes(bytearray([(b & 0xFF) for b in m.data]))).hexdigest()[:10]
        known = sum(1 for b in m.data if b >= 0)
        occ = sum(1 for b in m.data if b > 50)
        print(f'{time.strftime("%H:%M:%S")} size={m.info.width}x{m.info.height} '
              f'md5={h} 已知格={known} 占用格={occ}', flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--secs', type=float, default=30.0)
    args = ap.parse_args()
    rclpy.init()
    n = Probe()
    t0 = time.time()
    while time.time() - t0 < args.secs:
        rclpy.spin_once(n, timeout_sec=0.2)
    print('---- 观察结束（md5 一直相同 = 地图没更新）----')
    rclpy.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
