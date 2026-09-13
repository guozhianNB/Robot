#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""我在哪：只读打印小车在 map / odom 下的位姿，并算「距地图边界还有多少余量」。

用法（板卡上，先 source 三份 setup）:
    python3 ~/tools/where_am_i.py            # 默认等 5s 抓 TF 与 /map
    python3 ~/tools/where_am_i.py --secs 8

为什么需要它：
  ① `/amcl_pose` **静止时可能不发布**（AMCL 只在 update_min_d 0.25m / update_min_a 0.2rad 触发时更新），
     而 TF `map→base_link` 是稳的 → 本工具直接读 TF，任何时候都有值。
  ② 2026-09-13 踩过「目标 (1.0, 4.0) 比地图上边界 3.97 高 3cm → off the global costmap → status=6 ABORTED」，
     所以这里把有效范围和余量直接算出来，发目标前扫一眼即可。

只读，不发任何指令，不动车。
"""
import argparse
import math
import sys
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from tf2_ros import (Buffer, ConnectivityException, ExtrapolationException,
                     LookupException, TransformListener)

try:
    from nav_msgs.msg import OccupancyGrid
except ImportError:  # 理论不会发生（nav_msgs 是 Humble 自带）
    OccupancyGrid = None


def yaw_deg(q):
    """四元数 → yaw（度），REP-103：逆时针为正。"""
    return math.degrees(math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                                   1.0 - 2.0 * (q.y * q.y + q.z * q.z)))


class WhereAmI(Node):
    def __init__(self):
        super().__init__('where_am_i')
        self.buf = Buffer()
        self.listener = TransformListener(self.buf, self)
        self.map_info = None
        if OccupancyGrid is not None:
            qos = QoSProfile(depth=1,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             reliability=ReliabilityPolicy.RELIABLE)
            self.create_subscription(OccupancyGrid, '/map', self._on_map, qos)

    def _on_map(self, msg):
        self.map_info = msg.info

    def look(self, target, source):
        try:
            return self.buf.lookup_transform(target, source, rclpy.time.Time())
        except (LookupException, ConnectivityException, ExtrapolationException):
            return None


def fmt_tf(tf):
    t = tf.transform.translation
    return t.x, t.y, yaw_deg(tf.transform.rotation)


def main():
    ap = argparse.ArgumentParser(description='只读打印小车位姿与地图边界余量')
    ap.add_argument('--secs', type=float, default=5.0, help='最长等待时间（秒）')
    args = ap.parse_args()

    rclpy.init()
    node = WhereAmI()
    deadline = time.time() + args.secs
    in_map = in_odom = None
    while time.time() < deadline and rclpy.ok():
        rclpy.spin_once(node, timeout_sec=0.2)
        if in_map is None:
            in_map = node.look('map', 'base_link')
        if in_odom is None:
            in_odom = node.look('odom', 'base_link')
        if in_map is not None and in_odom is not None and node.map_info is not None:
            break

    rc = 0
    if in_map is None:
        print('[err] 拿不到 TF map→base_link：base/nav（或 slam）起了吗？'
              '`ros2 run tf2_ros tf2_echo map base_link` 先看 TF 通不通', file=sys.stderr)
        rc = 1
    else:
        x, y, yaw = fmt_tf(in_map)
        print('=== 我在哪（frame: map，来自 TF map→base_link）===')
        print(f'  位置: x={x:+.3f}  y={y:+.3f}   yaw={yaw:+.1f}°')
        print(f'  距 map 原点: {math.hypot(x, y):.3f} m')
        if in_odom is not None:
            ox, oy, oyaw = fmt_tf(in_odom)
            print(f'  轮速里程计 odom（起点=chassis_driver 启动点）: '
                  f'x={ox:+.3f}  y={oy:+.3f}  yaw={oyaw:+.1f}°')
        else:
            print('  轮速里程计 odom: 拿不到 odom→base_link')

        if node.map_info is None:
            print('  ⚠️ 没收到 /map：没法算地图边界（map_server/AMCL 起了吗？）')
        else:
            i = node.map_info
            x0, y0 = i.origin.position.x, i.origin.position.y
            x1 = x0 + i.width * i.resolution
            y1 = y0 + i.height * i.resolution
            print(f'  地图: {i.width}x{i.height} @ {i.resolution:.3f}m  origin=({x0:.2f}, {y0:.2f})')
            print(f'  有效范围: x∈[{x0:.2f}, {x1:.2f}]  y∈[{y0:.2f}, {y1:.2f}]')
            margins = {'左(x0)': x - x0, '右(x1)': x1 - x, '下(y0)': y - y0, '上(y1)': y1 - y}
            txt = '  '.join(f'{k} {v:+.2f}m' for k, v in margins.items())
            worst = min(margins.values())
            print(f'  边界余量: {txt}')
            if x < x0 or x > x1 or y < y0 or y > y1:
                print('  ❌ 车当前就在地图外（定位跳了？先在 Foxglove 里对齐初始位姿）')
                rc = 2
            elif worst < 0.30:
                print(f'  ⚠️ 距边界最近只剩 {worst:.2f}m：发目标至少留 0.30m 余量，'
                      '否则会 off the global costmap → status=6 ABORTED')
            else:
                print(f'  ✅ 最近边界余量 {worst:.2f}m，附近发目标安全（但仍要避开未知/障碍区）')
            print(f'  建议目标范围（各边缩进 0.3m）: x∈[{x0 + 0.3:.2f}, {x1 - 0.3:.2f}]  '
                  f'y∈[{y0 + 0.3:.2f}, {y1 - 0.3:.2f}]')

    node.destroy_node()
    rclpy.shutdown()
    return rc


if __name__ == '__main__':
    sys.exit(main())
