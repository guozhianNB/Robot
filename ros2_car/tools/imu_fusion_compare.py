#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""融合对照探针：手转车体（车轮不转）时，同时采 /imu、/odom、/odom_laser、/odom_filtered 的 yaw。

目的：把"打滑场景"原地重演 —— 车轮里程计看不见旋转，IMU 看得见，
      而 /odom_filtered（EKF 输出）应当跟着 IMU 走，这就是 IMU 的实用价值证据。

用法（板卡上，需先 source ROS 且 base 以 fused 模式在跑；只读，不发运动指令）:
    python3 ~/tools/imu_fusion_compare.py --secs 120
"""
import argparse
import math
import time

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Imu
from nav_msgs.msg import Odometry

THRESH_DPS = 5.0        # 认为"在转"的 |wz| 阈值
SOURCES = ('imu', 'odom', 'odom_laser', 'odom_filtered')


def yaw_from_quat(q):
    return math.degrees(math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                                   1.0 - 2.0 * (q.y * q.y + q.z * q.z)))


def norm180(d):
    return (d + 180.0) % 360.0 - 180.0


class Cmp(Node):
    def __init__(self):
        super().__init__('imu_fusion_compare')
        self.t0 = time.time()
        self.last_print = 0.0
        self.wz = 0.0
        self.samples = {s: [] for s in SOURCES}     # [(t, yaw)]
        self.create_subscription(Imu, '/imu', self.on_imu, 20)
        self.create_subscription(Odometry, '/odom', lambda m: self.on_odom('odom', m), 20)
        self.create_subscription(Odometry, '/odom_laser', lambda m: self.on_odom('odom_laser', m), 20)
        self.create_subscription(Odometry, '/odom_filtered',
                                 lambda m: self.on_odom('odom_filtered', m), 20)

    def on_imu(self, msg):
        t = time.time() - self.t0
        self.wz = math.degrees(msg.angular_velocity.z)
        self.samples['imu'].append((t, yaw_from_quat(msg.orientation)))
        if t - self.last_print >= 0.5:
            self.last_print = t
            print("t=%6.1fs wz=%7.2f°/s | " % (t, self.wz)
                  + "  ".join("%s=%8.2f°" % (s, self.samples[s][-1][1]) if self.samples[s] else "%s=---" % s
                              for s in SOURCES), flush=True)

    def on_odom(self, key, msg):
        self.samples[key].append((time.time() - self.t0, yaw_from_quat(msg.pose.pose.orientation)))


def delta_yaw(rows, t_lo, t_hi):
    """窗口 [t_lo, t_hi] 内的 yaw 变化（取窗口内首末样本，绕 ±180 归一）。"""
    win = [r for r in rows if t_lo <= r[0] <= t_hi]
    if len(win) < 2:
        return None, 0
    return norm180(win[-1][1] - win[0][1]), len(win)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--secs', type=float, default=120.0)
    ap.add_argument('--csv', default='/tmp/imu_fusion_compare.csv')
    a = ap.parse_args()

    rclpy.init()
    n = Cmp()
    t_start = time.time()
    while rclpy.ok() and time.time() - t_start < a.secs:
        rclpy.spin_once(n, timeout_sec=0.1)
    samples = {k: list(v) for k, v in n.samples.items()}
    n.destroy_node()
    rclpy.shutdown()

    try:
        with open(a.csv, 'w') as f:
            f.write('source,t,yaw_deg\n')
            for s in SOURCES:
                for t, y in samples[s]:
                    f.write('%s,%.4f,%.4f\n' % (s, t, y))
        print("\n原始数据已存 %s" % a.csv)
    except OSError as exc:
        print("CSV 写失败：%s" % exc)

    print("\n================ 对照结论 ================")
    for s in SOURCES:
        rows = samples[s]
        print("%-15s 样本 %s" % (s, len(rows) if rows else 0))
    if not samples['imu']:
        print("✗ /imu 无数据：base 没跑或没 source ROS")
        return

    # 找"在转"的窗口：用 IMU 自身 yaw 变化率（不依赖 wz 记录）
    imu = samples['imu']
    idx = []
    for i in range(1, len(imu)):
        dt = imu[i][0] - imu[i - 1][0]
        if dt > 0 and abs(norm180(imu[i][1] - imu[i - 1][1]) / dt) >= THRESH_DPS:
            idx.append(i)
    if not idx:
        print("⚠ IMU 视角下没检测到转动：请重跑并真的转动车体")
        return
    t_lo = imu[max(0, min(idx) - 1)][0]
    t_hi = imu[min(len(imu) - 1, max(idx) + 1)][0]
    print("检测到旋转窗口：t=%.1fs ~ %.1fs（%.1fs）" % (t_lo, t_hi, t_hi - t_lo))

    deltas = {}
    for s in SOURCES:
        d, cnt = delta_yaw(samples[s], t_lo, t_hi)
        deltas[s] = d
        print("  %-15s Δyaw = %s°（窗口内 %d 帧）"
              % (s, "无数据" if d is None else "%+8.2f" % d, cnt))

    di, do, dl, df = deltas['imu'], deltas['odom'], deltas['odom_laser'], deltas['odom_filtered']
    print("\n判读：")
    if di is None:
        print("  /imu 在窗口内样本不足，延长 --secs 重试")
        return
    print("  ① 车轮里程计 /odom  Δyaw = %s°（|Δyaw| 远小于 IMU 的 %.1f° → 轮子没转，看不到旋转）"
          % ("无数据" if do is None else "%+.2f" % do, abs(di)))
    if dl is not None:
        print("  ② 激光里程计 /odom_laser Δyaw = %+.2f°（rf2o 对纯自转本就退化，预期也跟不上）" % dl)
    if df is None:
        print("  ✗ /odom_filtered 无数据：EKF 没出输出，先查 base fused 是否起全")
        return
    if abs(df) >= 0.6 * abs(di):
        print("  ✓ /odom_filtered Δyaw = %+.2f° ≈ IMU 的 %+.2f°（%.0f%%）"
              % (df, di, 100.0 * abs(df) / abs(di)))
        print("    → EKF 成功用 IMU 兜住了「车轮打滑/悬空」时的航向，这就是 imu0 融 vyaw 的价值")
    elif abs(df) <= 0.25 * abs(di):
        print("  ✗ /odom_filtered Δyaw = %+.2f° 基本没跟 IMU（仅 %.0f%%）"
              % (df, 100.0 * abs(df) / abs(di)))
        print("    → EKF 没真正吃到 imu0：查 imu0_config[vyaw]、imu0_differential、话题名 /imu")
    else:
        print("  ⚠ /odom_filtered Δyaw = %+.2f° 跟了一部分（%.0f%%）"
              % (df, 100.0 * abs(df) / abs(di)))
        print("    → 融合在起作用但被别的输入（rf2o/轮速）拉住，可看协方差权重是否合理")


if __name__ == '__main__':
    main()
