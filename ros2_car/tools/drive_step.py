#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""底盘点动工具（安全版）：用 ROS 的 /cmd_vel 发一段定速指令，到点后必补零速并复核。

安全设计：
  - 每次运动固定时长，绝不长时间持续；时长上限 --max-secs（默认 6s）
  - 结束必发 3 次零速（对抗 0.5s 看门狗）
  - --dry-run 只打印计划，不动电机
  - 打印运动前后 /odom 与 /odom_filtered 位姿，便于判断里程计是否响应

用到时车旁必须有人；四周留空。

用法（板卡，已 source 环境）:
    python3 tools/drive_step.py --vx 0.15 --secs 3
    python3 tools/drive_step.py --wz 0.4  --secs 5
    python3 tools/drive_step.py --vx 0.1 --vy 0.1 --secs 2
"""
import argparse
import math
import sys
import time

import rclpy
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node


class Driver(Node):
    def __init__(self):
        super().__init__('drive_step')
        self.pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.odom = None
        self.flt = None
        self.create_subscription(Odometry, '/odom', self._on_odom, 10)

    def _on_odom(self, m):
        self.odom = m

    def sample(self, topic):
        """同步抓一次指定里程计位姿（简易：直接取最近一帧）。"""
        latest = {'v': None}

        def cb(m):
            latest['v'] = m
        sub = self.create_subscription(Odometry, topic, cb, 10)
        t0 = time.time()
        while latest['v'] is None and time.time() - t0 < 3.0:
            rclpy.spin_once(self, timeout_sec=0.1)
        self.destroy_subscription(sub)
        m = latest['v']
        if m is None:
            return None
        p = m.pose.pose
        yaw = math.degrees(2.0 * math.atan2(p.orientation.z, p.orientation.w))
        return (p.position.x, p.position.y, yaw)

    def send(self, vx, vy, wz, secs, hz=10.0):
        t = Twist()
        t.linear.x, t.linear.y, t.angular.z = vx, vy, wz
        t0 = time.time()
        while time.time() - t0 < secs:
            self.pub.publish(t)
            time.sleep(1.0 / hz)
        for _ in range(6):
            self.pub.publish(Twist())
            time.sleep(0.05)
        for _ in range(3):
            self.pub.publish(Twist())
            time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--vx', type=float, default=0.0)
    ap.add_argument('--vy', type=float, default=0.0)
    ap.add_argument('--wz', type=float, default=0.0)
    ap.add_argument('--secs', type=float, default=2.0)
    ap.add_argument('--max-secs', type=float, default=6.0)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if args.secs > args.max_secs:
        print(f'[拒绝] secs={args.secs} 超过上限 {args.max_secs}')
        return 1
    if not any((args.vx, args.vy, args.wz)):
        print('[拒绝] 三个速度全为 0')
        return 1

    print(f'计划: vx={args.vx} vy={args.vy} wz={args.wz} 持续 {args.secs}s'
          f'（前进 {(args.vx*args.secs):.2f}m 左移 {(args.vy*args.secs):.2f}m'
          f' 转 {math.degrees(args.wz*args.secs):.0f}°）')
    if args.dry_run:
        print('[dry-run] 不发指令')
        return 0

    rclpy.init()
    n = Driver()
    for _ in range(10):
        rclpy.spin_once(n, timeout_sec=0.1)

    before_w = n.sample('/odom')
    before_f = n.sample('/odom_filtered')
    print(f'前  轮速 odom: {before_w}')
    print(f'前  融合 odom: {before_f}')

    n.send(args.vx, args.vy, args.wz, args.secs)
    print('已停车（零速×3）')
    time.sleep(0.5)

    after_w = n.sample('/odom')
    after_f = n.sample('/odom_filtered')
    print(f'后  轮速 odom: {after_w}')
    print(f'后  融合 odom: {after_f}')
    if before_w and after_w:
        print(f'轮速 odom 位移: Δx={after_w[0]-before_w[0]:+.3f} '
              f'Δy={after_w[1]-before_w[1]:+.3f} Δyaw={after_w[2]-before_w[2]:+.1f}°')
    if before_f and after_f:
        print(f'融合 odom 位移: Δx={after_f[0]-before_f[0]:+.3f} '
              f'Δy={after_f[1]-before_f[1]:+.3f} Δyaw={after_f[2]-before_f[2]:+.1f}°')
    rclpy.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
