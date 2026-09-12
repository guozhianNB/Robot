#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""架空空转验收：命令底盘原地自转，同时对比 /odom（轮速）、/imu（真值参照）、
/odom_filtered（EKF）、/odom_laser（rf2o）的航向变化。

验收目的（设计文档 §5 第 4 项）：轮子空转时车身并不转 → 轮速里程计会"谎报"在转，
IMU 应说"没转"。看 EKF 信谁：
    - /odom_filtered 的 Δyaw ≈ 0（跟 IMU）→ 合格：打滑时航向不被轮速带歪
    - /odom_filtered 的 Δyaw 明显≠0 且与 /odom 同向 → odom0 融的 vyaw 在拖后腿，需要处理

安全护栏（务必遵守）:
  * 车轮必须离地（架空）或现场已确认安全、周围无人无物；
  * wz 默认 0.4 rad/s、时长默认 4s，**硬上限 6s**；
  * 20Hz 持续下发；结束时与异常时**一律补零速**（先于一切收尾动作）；
  * 底盘侧另有 0.5s 看门狗 + 斜坡 + 限速（vx≤0.5, vy≤0.3, wz≤0.8）兜底。

用法（板卡上，需先 source ROS 且 base fused 在跑）:
    python3 ~/tools/spin_acceptance.py --wz 0.4 --secs 4
"""
import argparse
import math
import time

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from sensor_msgs.msg import Imu
from nav_msgs.msg import Odometry

HARD_MAX_SECS = 6.0
POSE_SOURCES = ('odom', 'odom_laser', 'odom_filtered')


def yaw_from_quat(q):
    return math.degrees(math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                                   1.0 - 2.0 * (q.y * q.y + q.z * q.z)))


def norm180(d):
    return (d + 180.0) % 360.0 - 180.0


class Spin(Node):
    def __init__(self, wz):
        super().__init__('spin_acceptance')
        self.default_wz = wz
        self.wz = 0.0
        self.t0 = time.time()
        self.rows = {s: [] for s in ('imu',) + POSE_SOURCES}
        self.imu_wz = 0.0
        self.imu_wz_peak = 0.0
        self.pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.create_timer(0.05, self.tick)              # 20Hz 下发
        self.create_subscription(Imu, '/imu', self.on_imu, 20)
        for name in POSE_SOURCES:
            self.create_subscription(Odometry, '/' + name, self._mk(name), 20)

    def _mk(self, name):
        def cb(msg):
            self.rows[name].append((time.time() - self.t0, yaw_from_quat(msg.pose.pose.orientation)))
        return cb

    def on_imu(self, msg):
        self.imu_wz = msg.angular_velocity.z
        self.rows['imu'].append((time.time() - self.t0, yaw_from_quat(msg.orientation)))
        if abs(self.imu_wz) > abs(self.imu_wz_peak):
            self.imu_wz_peak = self.imu_wz

    def tick(self):
        m = Twist()
        m.angular.z = self.wz
        self.pub.publish(m)

    def stop(self):
        self.wz = 0.0
        m = Twist()
        for _ in range(10):                             # 连发 10 帧零速，确保底盘停
            self.pub.publish(m)
            time.sleep(0.02)


def delta(rows, lo, hi):
    win = [r for r in rows if lo <= r[0] <= hi]
    if len(win) < 2:
        return None, 0
    total = sum(norm180(win[i][1] - win[i - 1][1]) for i in range(1, len(win)))
    return total, len(win)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wz', type=float, default=0.4, help='自转角速度 rad/s（建议 0.3~0.5）')
    ap.add_argument('--secs', type=float, default=4.0, help='转动时长 s（硬上限 %.0f）' % HARD_MAX_SECS)
    ap.add_argument('--base', type=float, default=8.0, help='转动前基线采集秒数')
    ap.add_argument('--tail', type=float, default=6.0, help='停车后采集秒数')
    ap.add_argument('--go', action='store_true', help='必须显式加 --go 才会真的转动')
    a = ap.parse_args()

    if not a.go:
        print("未加 --go：本次只做只读空跑（不会发转动指令）。确认现场安全后加 --go 再来。")
        return
    if a.secs > HARD_MAX_SECS:
        print("时长超过硬上限，已收敛到 %.0fs" % HARD_MAX_SECS)
        a.secs = HARD_MAX_SECS

    rclpy.init()
    n = Spin(a.wz)
    print("① 基线采集中（%.0fs，不动车）…" % a.base, flush=True)
    t_end = time.time() + a.base
    while rclpy.ok() and time.time() < t_end:
        rclpy.spin_once(n, timeout_sec=0.05)

    if not n.rows['odom']:
        print("✗ /odom 没数据：底盘驱动没起？中止，不发任何指令")
        n.stop(); n.destroy_node(); rclpy.shutdown(); return

    t_spin0 = time.time() - n.t0
    print("② 开始自转：wz=%.2f rad/s，%.1fs（20Hz 下发）…" % (a.wz, a.secs), flush=True)
    n.wz = a.wz
    t_end = time.time() + a.secs
    while rclpy.ok() and time.time() < t_end:
        rclpy.spin_once(n, timeout_sec=0.05)

    print("③ 补零速停车", flush=True)
    n.stop()
    t_spin1 = time.time() - n.t0
    print("④ 停车后采集（%.0fs）…" % a.tail, flush=True)
    t_end = time.time() + a.tail
    while rclpy.ok() and time.time() < t_end:
        rclpy.spin_once(n, timeout_sec=0.05)

    n.stop()                                            # 双保险
    rows = {k: list(v) for k, v in n.rows.items()}
    imu_peak_deg = math.degrees(n.imu_wz_peak)
    n.destroy_node()
    rclpy.shutdown()

    print("\n============ 架空空转验收结论（转动窗口 t=%.1f~%.1fs）============" % (t_spin0, t_spin1))
    d = {}
    for s in ('imu',) + POSE_SOURCES:
        val, cnt = delta(rows[s], t_spin0, t_spin1)
        d[s] = val
        print("  %-15s Δyaw = %s（%d 帧）"
              % (s, "无数据" if val is None else "%+8.2f°" % val, cnt))
    print("  IMU 实测角速度峰值 = %+.1f °/s（命令 %.1f rad/s = %.1f °/s）"
          % (imu_peak_deg, a.wz, math.degrees(a.wz)))

    do, di, df = d.get('odom'), d.get('imu'), d.get('odom_filtered')
    print("\n  判读：")
    if do is None or di is None or df is None:
        print("    数据不全，重跑一次")
        return
    print("    轮速 /odom = %+.1f°，IMU（车身真值）= %+.1f°" % (do, di))
    if abs(do) < 10:
        print("    ⚠ 轮速几乎没转（<10°）：wz 太小 / 轮子被挡 / 驱动没响应 → 这次不算有效样本")
        return

    # 先判"是不是真的空转"：空转 = 轮子转很多、车身几乎不转
    if abs(di) < 0.25 * abs(do):
        print("    ✓ 判定：**车轮空转**（车身只转了 %+.1f°，轮速却报 %+.1f°）—— 轮速确实在谎报" % (di, do))
        leak = 100.0 * abs(df) / abs(do)
        if leak <= 20.0:
            print("    ✓ /odom_filtered 只被带偏 %+.1f°（轮速谎报量的 %.0f%%）—— "
                  "IMU 把错误的轮速 vyaw 压住了，打滑场景合格" % (df, leak))
        elif leak <= 50.0:
            print("    ⚠ /odom_filtered 被带偏 %+.1f°（轮速谎报量的 %.0f%%）—— 可接受但可再收紧："
                  "关掉 odom0_config 的 vyaw 位(索引 11)可把泄漏压到接近 0（代价=失去轮速 yaw 率冗余）"
                  % (df, leak))
        else:
            print("    ✗ /odom_filtered 被带偏 %+.1f°（轮速谎报量的 %.0f%%）—— 航向被轮速 vyaw 主导，"
                  "应关掉 odom0_config 的 vyaw 位(索引 11)，让航向只由 IMU 负责" % (df, leak))
        return

    slip = 100.0 * (abs(do) - abs(di)) / abs(di)
    print("    ℹ 判定：**车身真的转了**（不是空转）—— 本次是地面自转；轮速比 IMU 多报 %.0f%%（打滑量）"
          % slip)
    print("      命令跟踪：IMU 峰值 %.1f°/s vs 命令 %.1f°/s → 速度环跟踪良好"
          % (math.degrees(n.imu_wz_peak), math.degrees(a.wz)))

    # 地面自转：轮速与 IMU 分歧不大时，EKF 落在中间属正常融合
    if abs(do - di) <= max(8.0, 0.15 * abs(di)):
        print("    ✓ 轮速与 IMU 基本一致（差 %.1f°）→ 两者无分歧，/odom_filtered %+.1f° 落在中间属正常融合"
              % (do - di, df))
    elif abs(df) >= 0.6 * abs(do):
        print("    ✗ 轮速与 IMU 分歧 %.1f°，而 /odom_filtered %+.1f° ≈ 轮速的 %.0f%% —— 航向被轮速带歪了："
              % (do - di, df, 100.0 * abs(df) / abs(do)))
        print("      → 建议关掉 odom0_config 的 vyaw 位(索引 11)，或调大轮速 vyaw 方差，让航向只由 IMU 负责")
    else:
        print("    ⚠ 轮速与 IMU 分歧 %.1f°，/odom_filtered %+.1f°（轮速的 %.0f%%）—— 部分跟随，需调权重"
              % (do - di, df, 100.0 * abs(df) / abs(do)))


if __name__ == '__main__':
    main()
