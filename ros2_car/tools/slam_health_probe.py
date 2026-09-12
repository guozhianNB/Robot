#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SLAM 健康探针：正确 QoS 订阅 /map(TRANSIENT_LOCAL) + /scan(BEST_EFFORT)，
统计「地图是否在更新」以及扫描是否被消费。

用法（板卡，已 source 环境）:
    python3 tools/slam_health_probe.py --secs 25

输出：
  - /scan 频率、最新时间戳是否推进
  - /map 收到次数、每次的 info 时间戳与 width/height（重复即未更新）
  - 若地图更新次数 = 0 而扫描在推进 → slam 侧卡住
"""
import argparse
import sys
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import (QoSProfile, ReliabilityPolicy, HistoryPolicy,
                       DurabilityPolicy)
from sensor_msgs.msg import LaserScan
from nav_msgs.msg import OccupancyGrid


class Probe(Node):
    def __init__(self):
        super().__init__('slam_health_probe')
        self.scans = 0
        self.last_scan_stamp = None
        self.maps = []
        scan_qos = QoSProfile(depth=5, reliability=ReliabilityPolicy.BEST_EFFORT,
                              history=HistoryPolicy.KEEP_LAST)
        map_qos = QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE,
                             history=HistoryPolicy.KEEP_LAST,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(LaserScan, '/scan', self._on_scan, scan_qos)
        self.create_subscription(OccupancyGrid, '/map', self._on_map, map_qos)

    def _on_scan(self, m):
        self.scans += 1
        self.last_scan_stamp = m.header.stamp.sec + m.header.stamp.nanosec * 1e-9

    def _on_map(self, m):
        st = m.info.map_load_time.sec + m.info.map_load_time.nanosec * 1e-9
        self.maps.append((st, m.info.width, m.info.height))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--secs', type=float, default=25.0)
    args = ap.parse_args()

    rclpy.init()
    n = Probe()
    t0 = time.time()
    last_report = 0.0
    while time.time() - t0 < args.secs:
        rclpy.spin_once(n, timeout_sec=0.1)
        el = time.time() - t0
        if el - last_report >= 5.0:
            last_report = el
            print(f'[{el:5.1f}s] 扫描累计 {n.scans} 条（最新时间戳 {n.last_scan_stamp}）'
                  f' | 地图更新 {len(n.maps)} 次')
            sys.stdout.flush()
    rclpy.shutdown()

    print('\n---- 结论 ----')
    print(f'/scan 共 {n.scans} 条（{n.scans/args.secs:.1f} Hz）')
    if not n.maps:
        print('/map：一次都没收到 —— slam 没有发布地图（节点未起来或 QoS 不匹配）')
        return 2
    uniq = {m for m in n.maps}
    print(f'/map 收到 {len(n.maps)} 次，其中不同内容 {len(uniq)} 种')
    for st, w, h in list(uniq)[:5]:
        print(f'   map_load_time={st:.3f} size={w}x{h}')
    if len(uniq) == 1:
        print('[判定] 地图全程只有一种内容 → slam 未更新地图（卡住）')
        return 1
    print('[判定] 地图有更新 → slam 正常在建图')
    return 0


if __name__ == '__main__':
    sys.exit(main())
