#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旋转自动接应探针：一直蹲守 /imu，一旦"车在转"就自动记录这段旋转，
并对比 /imu、/odom、/odom_laser、/odom_filtered 的 Δyaw。

设计目的：不再和人对表。运行后您**随时**手转车体即可，探针自己捕捉窗口。
归因说明（2026-09-12 晚起）：ekf_params.yaml 里 odom1(/odom_laser) 的 yaw 已关，
故此刻若 /odom_filtered 跟住了手转（车轮没转），功劳只能是 IMU 的。

用法（板卡上，需先 source ROS 且 base fused 在跑；只读，不发运动指令）:
    python3 ~/tools/imu_rotation_watch.py --wait 600
"""
import argparse
import math
import time

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Imu
from nav_msgs.msg import Odometry

SOURCES = ('imu', 'odom', 'odom_laser', 'odom_filtered')
MOVE_DPS = 5.0          # 判定"在转"
STILL_DPS = 2.0         # 判定"转完了"
STILL_HOLD_S = 2.5      # 静止持续这么久就收窗
MAX_EVENT_S = 40.0      # 单次旋转最长记录时长


def yaw_from_quat(q):
    return math.degrees(math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                                   1.0 - 2.0 * (q.y * q.y + q.z * q.z)))


def norm180(d):
    return (d + 180.0) % 360.0 - 180.0


class Watch(Node):
    def __init__(self):
        super().__init__('imu_rotation_watch')
        self.t0 = time.time()
        self.rows = {s: [] for s in SOURCES}
        self.wz = 0.0
        self.state = 'idle'          # idle | recording | done
        self.ev_t0 = self.ev_t1 = 0.0
        self.last_move = 0.0
        self.peak = 0.0
        self.create_subscription(Imu, '/imu', self.on_imu, 20)
        self.create_subscription(Odometry, '/odom', lambda m: self.on_odom('odom', m), 20)
        self.create_subscription(Odometry, '/odom_laser', lambda m: self.on_odom('odom_laser', m), 20)
        self.create_subscription(Odometry, '/odom_filtered',
                                 lambda m: self.on_odom('odom_filtered', m), 20)

    def on_imu(self, msg):
        t = time.time() - self.t0
        self.wz = math.degrees(msg.angular_velocity.z)
        self.rows['imu'].append((t, yaw_from_quat(msg.orientation)))

        if self.state == 'idle':
            if abs(self.wz) >= MOVE_DPS:
                self.ev_t0 = max(0.0, t - 3.0)     # 多留 3 秒前情
                self.state = 'recording'
                self.last_move = t
                self.peak = abs(self.wz)
                print("\n>>> 检测到转动！开始记录（t=%.1fs, wz=%.1f°/s）" % (t, self.wz), flush=True)
        elif self.state == 'recording':
            if abs(self.wz) > self.peak:
                self.peak = abs(self.wz)
            if abs(self.wz) >= STILL_DPS:
                self.last_move = t
            elif (t - self.last_move >= STILL_HOLD_S) or (t - self.ev_t0 >= MAX_EVENT_S):
                self.ev_t1 = t
                self.state = 'done'
                print(">>> 转动结束（t=%.1fs，峰值 %.1f°/s），出结论" % (t, self.peak), flush=True)

    def on_odom(self, key, msg):
        self.rows[key].append((time.time() - self.t0, yaw_from_quat(msg.pose.pose.orientation)))


def delta(rows, lo, hi):
    win = [r for r in rows if lo <= r[0] <= hi]
    if len(win) < 2:
        return None, 0
    total = sum(norm180(win[i][1] - win[i - 1][1]) for i in range(1, len(win)))
    return total, len(win)


def report(node):
    lo, hi = node.ev_t0, node.ev_t1
    print("\n================ 旋转对照结论（t=%.1f~%.1fs，%.1fs）================" % (lo, hi, hi - lo))
    d = {}
    for s in SOURCES:
        val, cnt = delta(node.rows[s], lo, hi)
        d[s] = val
        print("  %-15s Δyaw = %s（%d 帧）"
              % (s, "无数据" if val is None else "%+8.2f°" % val, cnt))
    di, do, dl, df = d['imu'], d['odom'], d['odom_laser'], d['odom_filtered']
    if di is None or abs(di) < 1.0:
        print("  ⚠ IMU 视角 Δyaw 太小，这次不算有效样本")
        return
    print("\n  判读：")
    print("    车轮 /odom 只看到 %s（占 IMU 的 %.0f%%）→ 轮子没真转，是打滑场景"
          % ("无数据" if do is None else "%+.2f°" % do, 0 if not do else 100.0 * abs(do) / abs(di)))
    if dl is not None:
        print("    /odom_laser  %+.2f°（激光朝向已不在融合回路里，仅供对照）" % dl)
    if df is None:
        print("    ✗ /odom_filtered 无数据")
        return
    ratio = 100.0 * abs(df) / abs(di)
    if ratio >= 70:
        print("    ✓ /odom_filtered %+.2f° = IMU 的 %.0f%% —— **归功于 IMU**（激光 yaw 已关、轮子没转），"
              "即 IMU 在打滑场景下把航向拉住了" % (df, ratio))
    elif ratio <= 25:
        print("    ✗ /odom_filtered %+.2f° 只跟了 %.0f%% —— IMU 没真正进航向回路，需再查" % (df, ratio))
    else:
        print("    ⚠ /odom_filtered %+.2f° = IMU 的 %.0f%% —— 部分跟随，需细看时间对齐/协方差" % (df, ratio))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wait', type=float, default=600.0, help='最长蹲守秒数')
    a = ap.parse_args()

    rclpy.init()
    n = Watch()
    t_start = time.time()
    print("蹲守中：请随时手转车体（俯视逆时针约 90° → 停 → 转回）；最长等 %.0f 秒。" % a.wait, flush=True)
    while rclpy.ok() and time.time() - t_start < a.wait and n.state != 'done':
        rclpy.spin_once(n, timeout_sec=0.1)
    ok = n.state == 'done'
    if ok:
        # 多收 1 秒尾巴
        t_end = time.time() + 1.0
        while rclpy.ok() and time.time() < t_end:
            rclpy.spin_once(n, timeout_sec=0.1)
        report(n)
    else:
        print("超时：%.0f 秒内没检测到转动（|wz| >= %.0f°/s）" % (a.wait, MOVE_DPS))
    n.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
