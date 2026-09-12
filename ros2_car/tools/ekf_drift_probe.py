#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静止漂移探针：测 /imu、/odom_laser、/odom_filtered 的"静止假漂"速率（°/s）。

用途：验证 EKF 是否真在用 IMU 约束航向。
    - 改配置前基准 ≈ 0.15~0.25°/s（/odom_filtered 跟着 rf2o 漂）
    - 修好后期望 ≈ 0（IMU 说没转，EKF 就该不转）
只读，不发运动指令。要求车体**不动**（别碰车、别过风）。

用法（板卡上，需先 source ROS 且 base 以 fused 跑着）:
    python3 ~/tools/ekf_drift_probe.py --secs 180
"""
import argparse
import math
import time

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Imu
from nav_msgs.msg import Odometry

SOURCES = ('imu', 'odom_laser', 'odom_filtered')
SKIP_S = 5.0            # 前 5 秒不计入漂移（滤波器收敛）


def yaw_from_quat(q):
    return math.degrees(math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                                   1.0 - 2.0 * (q.y * q.y + q.z * q.z)))


def norm180(d):
    return (d + 180.0) % 360.0 - 180.0


class Drift(Node):
    def __init__(self):
        super().__init__('ekf_drift_probe')
        self.t0 = time.time()
        self.last_print = 0.0
        self.rows = {s: [] for s in SOURCES}
        self.create_subscription(Imu, '/imu', self.on_imu, 20)
        self.create_subscription(Odometry, '/odom_laser', lambda m: self.on_odom('odom_laser', m), 20)
        self.create_subscription(Odometry, '/odom_filtered',
                                 lambda m: self.on_odom('odom_filtered', m), 20)

    def on_imu(self, msg):
        t = time.time() - self.t0
        self.rows['imu'].append((t, yaw_from_quat(msg.orientation)))
        if t - self.last_print >= 10.0:
            self.last_print = t
            print("t=%6.1fs  " % t + "  ".join(
                "%s=%9.3f°" % (s, self.rows[s][-1][1]) if self.rows[s] else "%s=---" % s
                for s in SOURCES), flush=True)

    def on_odom(self, key, msg):
        self.rows[key].append((time.time() - self.t0, yaw_from_quat(msg.pose.pose.orientation)))


def drift_rate(rows, t_lo):
    win = [r for r in rows if r[0] >= t_lo]
    if len(win) < 2:
        return None, 0, 0.0
    dt = win[-1][0] - win[0][0]
    if dt <= 0:
        return None, len(win), 0.0
    # 累加相邻差（绕 ±180），避免整圈误差
    total = sum(norm180(win[i][1] - win[i - 1][1]) for i in range(1, len(win)))
    return total, len(win), total / dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--secs', type=float, default=180.0)
    ap.add_argument('--skip', type=float, default=SKIP_S)
    a = ap.parse_args()

    rclpy.init()
    n = Drift()
    t_start = time.time()
    while rclpy.ok() and time.time() - t_start < a.secs:
        rclpy.spin_once(n, timeout_sec=0.1)
    rows = {k: list(v) for k, v in n.rows.items()}
    elapsed = time.time() - t_start
    n.destroy_node()
    rclpy.shutdown()

    print("\n================ 静止漂移结论（%.0fs，跳过前 %.0fs）================" % (elapsed, a.skip))
    rates = {}
    for s in SOURCES:
        tot, cnt, rate = drift_rate(rows[s], a.skip)
        rates[s] = rate
        if tot is None:
            print("%-15s 样本不足（%d）" % (s, cnt))
        else:
            print("%-15s 累计 %+8.3f°  %6.1fs  →  %+7.4f °/s（%d 帧）"
                  % (s, tot, elapsed - a.skip, rate, cnt))

    f = rates.get('odom_filtered', 0.0)
    imu = rates.get('imu', 0.0)
    rf = rates.get('odom_laser', 0.0)
    print("\n判读：")
    print("  /imu 自身漂移      %.4f °/s（IMU 好坏的下限参照）" % imu)
    print("  rf2o(/odom_laser) %.4f °/s（静止假漂，是误差源）" % rf)
    if abs(f) <= 0.05:
        print("  ✓ /odom_filtered  %.4f °/s —— 静止基本不漂（<0.05°/s）：IMU 已把航向拉住" % f)
    elif abs(f) < 0.6 * abs(rf) and abs(rf) > 0.05:
        print("  ⚠ /odom_filtered  %.4f °/s —— 比 rf2o 的 %.4f 小，但仍未归零：融合在起作用但权重不足"
              % (f, rf))
    else:
        print("  ✗ /odom_filtered  %.4f °/s ≈ rf2o %.4f °/s —— 仍在跟着 rf2o 漂，IMU 没拉住"
              % (f, rf))
        print("    → 下一步候选：把 odom1(/odom_laser) 的 yaw 位关掉，或调 rf2o pose 协方差权重")


if __name__ == '__main__':
    main()
