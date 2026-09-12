#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMU 符号标定探针：采 /imu，边采边打 yaw/wz，最后给符号一致性结论。

判定依据（REP-103）：俯视逆时针（CCW）为正 → 车体逆时针转时
    ① yaw 应**增大**；② angular_velocity.z 应为**正**。
两者符号一致 = 当前 imu_sign_wz / imu_sign_yaw 正确。

用法（板卡上，先 source ROS 且 base 在跑；只读，不发运动指令）:
    python3 ~/tools/imu_sign_probe.py --secs 75
"""
import argparse
import math
import sys
import time

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Imu

THRESH_DPS = 5.0          # 判定"在转"的角速度阈值


def yaw_from_quat(x, y, z, w):
    return math.degrees(math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z)))


class Probe(Node):
    def __init__(self):
        super().__init__('imu_sign_probe')
        self.create_subscription(Imu, '/imu', self.cb, 20)
        self.rows = []          # (t, yaw_deg, wz_dps)
        self.t0 = time.time()
        self.last_print = 0.0

    def cb(self, msg):
        t = time.time() - self.t0
        q = msg.orientation
        yaw = yaw_from_quat(q.x, q.y, q.z, q.w)
        wz = math.degrees(msg.angular_velocity.z)
        self.rows.append((t, yaw, wz))
        if t - self.last_print >= 0.5:
            self.last_print = t
            print("t=%6.1fs yaw=%8.2f° wz=%7.2f°/s" % (t, yaw, wz), flush=True)


def summarize(rows, elapsed):
    print("\n================ 标定结论 ================")
    if len(rows) < 5:
        print("✗ 样本太少（%d）：/imu 没数据？先确认 base 在跑、探针 source 了 ROS" % len(rows))
        return
    ts = [r[0] for r in rows]
    ys = [r[1] for r in rows]
    ws = [r[2] for r in rows]
    print("样本 %d 帧 / %.1fs = %.1f Hz" % (len(rows), elapsed, len(rows) / elapsed))
    print("yaw: 首 %8.2f°  末 %8.2f°  Δyaw=%+7.2f°  范围 [%.2f, %.2f]"
          % (ys[0], ys[-1], ys[-1] - ys[0], min(ys), max(ys)))
    print("wz : 范围 [%.2f, %.2f] °/s" % (min(ws), max(ws)))

    # 只在"确实在转"的样本上比符号：数值 yaw 变化率 vs 上报 wz
    pairs = []
    for i in range(1, len(rows)):
        dt = ts[i] - ts[i - 1]
        if dt <= 0:
            continue
        dyaw = (ys[i] - ys[i - 1]) / dt
        if abs(ws[i]) >= THRESH_DPS or abs(dyaw) >= THRESH_DPS:
            pairs.append((dyaw, ws[i]))

    if not pairs:
        print("⚠ 全程 |wz| < %.0f°/s 且 yaw 基本不动：像是没转起来（或 IMU 被拆下）。"
              "请重跑并真的转动车体。" % THRESH_DPS)
        return

    same = sum(1 for d, w in pairs if d * w > 0)
    print("转动样本 %d 帧：数值 dyaw 与上报 wz **同号** %d，异号 %d"
          % (len(pairs), same, len(pairs) - same))
    if same >= 0.8 * len(pairs):
        print("✓ 符号一致：wz>0 时 yaw 在增大 → 当前 imu_sign_wz / imu_sign_yaw 正确，"
              "就是 REP-103「逆时针为正」，不用改")
    else:
        print("✗ 符号相反：wz 与 yaw 变化方向不一致 → 需要翻转符号。")
        print("  改 ros2_car/src/robot_chassis/config/chassis_params.yaml 的 imu_sign_wz / imu_sign_yaw")
        print("  （改完 colcon build + 重启 base 再复验）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--secs', type=float, default=75.0)
    ap.add_argument('--csv', default='/tmp/imu_sign.csv')
    a = ap.parse_args()

    rclpy.init()
    n = Probe()
    t0 = time.time()
    while rclpy.ok() and time.time() - t0 < a.secs:
        rclpy.spin_once(n, timeout_sec=0.1)
    rows = list(n.rows)
    n.destroy_node()
    rclpy.shutdown()

    try:
        with open(a.csv, 'w') as f:
            f.write('t,yaw_deg,wz_dps\n')
            for t, y, w in rows:
                f.write('%.4f,%.4f,%.4f\n' % (t, y, w))
        print("\n原始数据已存 %s" % a.csv)
    except OSError as exc:
        print("CSV 写失败：%s" % exc)

    summarize(rows, time.time() - t0)


if __name__ == '__main__':
    main()
