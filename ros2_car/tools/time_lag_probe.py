#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断 slam_toolbox 丢帧：测量 /scan 与 /odom 的时间戳相对当前 ROS 时间的滞后。

用法（板卡，已 source 环境）:
    python3 tools/time_lag_probe.py --secs 8

关注：滞后 > transform_timeout(0.2s) 时，slam 的 message filter 会把帧判为过期。
"""
import argparse
import sys
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from sensor_msgs.msg import LaserScan
from nav_msgs.msg import Odometry


class Probe(Node):
    def __init__(self):
        super().__init__('time_lag_probe')
        self.scan = []
        self.odom = []
        qos = QoSProfile(depth=5, reliability=ReliabilityPolicy.BEST_EFFORT,
                         history=HistoryPolicy.KEEP_LAST)
        self.create_subscription(LaserScan, '/scan', self._on_scan, qos)
        self.create_subscription(Odometry, '/odom', self._on_odom, 5)

    def _now(self):
        return self.get_clock().now().nanoseconds / 1e9

    def _on_scan(self, m):
        self.scan.append((m.header.stamp.sec + m.header.stamp.nanosec * 1e-9, self._now()))

    def _on_odom(self, m):
        self.odom.append((m.header.stamp.sec + m.header.stamp.nanosec * 1e-9, self._now()))


def report(name, rows):
    if not rows:
        print(f'{name}: 未收到消息')
        return
    lags = [(now - st) for st, now in rows]
    lags = [x for x in lags if abs(x) < 60]
    if not lags:
        print(f'{name}: 时间戳与系统时间差过大（时钟/use_sim_time 问题）')
        return
    lags.sort()
    print(f'{name}: 收到 {len(rows)} 条，滞后 min={lags[0]:.3f}s '
          f'中位={lags[len(lags)//2]:.3f}s max={lags[-1]:.3f}s')
    stale = sum(1 for x in lags if x > 0.2)
    print(f'  滞后 > 0.2s（transform_timeout）的比例: {100.0*stale/len(lags):.0f}%')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--secs', type=float, default=8.0)
    args = ap.parse_args()
    rclpy.init()
    n = Probe()
    t0 = time.time()
    while time.time() - t0 < args.secs:
        rclpy.spin_once(n, timeout_sec=0.1)
    report('/scan', n.scan)
    report('/odom', n.odom)
    rclpy.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
