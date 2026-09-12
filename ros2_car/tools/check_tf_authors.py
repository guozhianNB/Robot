#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统计 /tf 上各段变换的**实际**发布频率，用于确认每段 TF 只有一个真实发布者。

为什么需要它：`ros2 topic info /tf --verbose` 会把「创建了 TransformBroadcaster 但没发」
的节点也算成发布者（rf2o 与 chassis_driver 都无条件创建），光看发布者数量会误判成"双发布"。
频率才是证据：odom→base_link 由 EKF 独占时应 ≈ EKF 的 frequency（20Hz）；
若 chassis_driver（10Hz）或 rf2o（10Hz）也在发，会看到 ~30Hz。

用法:
    python3 tools/check_tf_authors.py [秒数]
"""
import collections
import sys
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from tf2_msgs.msg import TFMessage


class TfAuthorCounter(Node):
    def __init__(self):
        super().__init__('tf_author_counter')
        self.dynamic = collections.Counter()
        self.static = collections.Counter()
        self.create_subscription(TFMessage, '/tf', self._on_dynamic, 50)
        # /tf_static 是 transient_local（latched），用默认 volatile QoS 收不到历史消息
        static_qos = QoSProfile(
            depth=50,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            reliability=QoSReliabilityPolicy.RELIABLE)
        self.create_subscription(TFMessage, '/tf_static', self._on_static, static_qos)

    def _on_dynamic(self, msg):
        for t in msg.transforms:
            self.dynamic[(t.header.frame_id, t.child_frame_id)] += 1

    def _on_static(self, msg):
        for t in msg.transforms:
            self.static[(t.header.frame_id, t.child_frame_id)] += 1


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    rclpy.init()
    node = TfAuthorCounter()
    t0 = time.time()
    while time.time() - t0 < secs:
        rclpy.spin_once(node, timeout_sec=0.1)

    print('/tf（动态）各段变换实际频率：')
    for (parent, child), n in sorted(node.dynamic.items(), key=lambda kv: -kv[1]):
        print('  %-16s → %-14s %6.1f Hz' % (parent, child, n / secs))
    if node.static:
        print('/tf_static：')
        for (parent, child), n in sorted(node.static.items()):
            print('  %-16s → %-14s 静态' % (parent, child))

    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
