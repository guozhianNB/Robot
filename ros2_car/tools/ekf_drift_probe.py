#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静止漂移探针（航向 + 位置）：测 /imu、/odom_laser、/odom_filtered 静止时的假漂。

用途：验证 EKF 是否真在用 IMU 约束航向，以及激光里程计的位置漂移是否还在污染输出。
    - 航向：关掉 odom1 的 yaw 位前 ≈0.15~0.25°/s，关掉后 ≈0.000°/s（IMU 说了算）
    - 位置：/odom_laser 的 x,y 若也漂，会经 odom1(x,y) 进 EKF → 看 /odom_filtered 的 x,y
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

POSE_SOURCES = ('odom_laser', 'odom_filtered')
ALL_SOURCES = ('imu',) + POSE_SOURCES
SKIP_S = 5.0            # 前 5 秒不计入漂移（滤波器收敛）


def yaw_from_quat(q):
    return math.degrees(math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                                   1.0 - 2.0 * (q.y * q.y + q.z * q.z)))


def norm180(d):
    return (d + 180.0) % 360.0 - 180.0


class Drift(Node):
    """行数据 = (t, x, y, yaw)；IMU 无位置，x/y 记 0。"""

    def __init__(self):
        super().__init__('ekf_drift_probe')
        self.t0 = time.time()
        self.last_print = 0.0
        self.rows = {s: [] for s in ALL_SOURCES}
        self.create_subscription(Imu, '/imu', self.on_imu, 20)
        for name in POSE_SOURCES:
            self.create_subscription(Odometry, '/' + name, self._mk(name), 20)

    def _mk(self, name):
        def cb(msg):
            p = msg.pose.pose
            self.rows[name].append((time.time() - self.t0, p.position.x, p.position.y,
                                    yaw_from_quat(p.orientation)))
        return cb

    def on_imu(self, msg):
        t = time.time() - self.t0
        self.rows['imu'].append((t, 0.0, 0.0, yaw_from_quat(msg.orientation)))
        if t - self.last_print >= 15.0:
            self.last_print = t
            parts = ["imu_yaw=%9.3f°" % self.rows['imu'][-1][3]]
            for name in POSE_SOURCES:
                r = self.rows[name]
                if r:
                    parts.append("%s: yaw=%9.3f° xy=(%+7.3f,%+7.3f)"
                                 % (name, r[-1][3], r[-1][1], r[-1][2]))
            print("t=%6.1fs  " % t + "  ".join(parts), flush=True)


def yaw_drift(rows, t_lo):
    win = [r for r in rows if r[0] >= t_lo]
    if len(win) < 2:
        return None, 0, 0.0
    dt = win[-1][0] - win[0][0]
    if dt <= 0:
        return None, len(win), 0.0
    total = sum(norm180(win[i][3] - win[i - 1][3]) for i in range(1, len(win)))
    return total, len(win), total / dt


def xy_drift(rows, t_lo):
    """返回 (净位移 m, 路程 m, 净漂移速度 m/s, 样本数)。"""
    win = [r for r in rows if r[0] >= t_lo]
    if len(win) < 2:
        return None, None, None, 0
    dt = win[-1][0] - win[0][0]
    net = math.hypot(win[-1][1] - win[0][1], win[-1][2] - win[0][2])
    path = sum(math.hypot(win[i][1] - win[i - 1][1], win[i][2] - win[i - 1][2])
               for i in range(1, len(win)))
    return net, path, (net / dt if dt > 0 else 0.0), len(win)


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

    win_s = elapsed - a.skip
    print("\n=========== 静止漂移结论（总 %.0fs，统计窗口 %.0fs）===========" % (elapsed, win_s))

    print("\n-- 航向 yaw --")
    rates = {}
    for s in ALL_SOURCES:
        tot, cnt, rate = yaw_drift(rows[s], a.skip)
        rates[s] = rate
        print("%-15s 累计 %s°  →  %+8.4f °/s（%d 帧）"
              % (s, "  nan" if tot is None else "%+7.3f" % tot, rate, cnt))

    print("\n-- 位置 x,y --")
    xy = {}
    for s in POSE_SOURCES:
        net, path, rate, cnt = xy_drift(rows[s], a.skip)
        xy[s] = (net, path, rate)
        if net is None:
            print("%-15s 样本不足（%d）" % (s, cnt))
        else:
            print("%-15s 净位移 %6.3f m（路程 %.3f m） →  %+.5f m/s（%d 帧）"
                  % (s, net, path, rate, cnt))

    f = rates.get('odom_filtered', 0.0)
    imu = rates.get('imu', 0.0)
    rf = rates.get('odom_laser', 0.0)
    print("\n-- 判读 --")
    print("  /imu 自身 yaw 漂移 %.4f °/s（IMU 好坏下限参照）" % imu)
    print("  rf2o yaw 漂移      %.4f °/s（已不融进 EKF，仅参照）" % rf)
    if abs(f) <= 0.05:
        print("  ✓ /odom_filtered yaw %.4f °/s —— 静止不漂：IMU 已把航向拉住" % f)
    elif abs(rf) > 0.05 and abs(f) < 0.6 * abs(rf):
        print("  ⚠ /odom_filtered yaw %.4f °/s 比 rf2o 的 %.4f 小但未归零：权重不足" % (f, rf))
    else:
        print("  ✗ /odom_filtered yaw %.4f °/s ≈ rf2o %.4f °/s：仍在被 rf2o 带" % (f, rf))

    if xy.get('odom_laser', (None,))[0] is not None and xy.get('odom_filtered', (None,))[0] is not None:
        ratel = xy['odom_laser'][2]
        netf, _, ratef = xy['odom_filtered']
        print("  rf2o 位置漂移      %.5f m/s" % ratel)
        print("  EKF  位置漂移      %.5f m/s（%.3f m / %.0fs）" % (ratef, netf, win_s))
        if ratel > 0.01 or ratef > 0.01:
            print("  ⚠ 静止时位置也在漂 → odom1 仍融 rf2o 的 x,y，会拖累平移；"
                  "可调大其 pose 协方差、或让平移以轮速/IMU 为主")
        else:
            print("  ✓ 位置漂移很小（<1cm/s），平移不受拖累")


if __name__ == '__main__':
    main()
