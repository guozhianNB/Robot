#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""里程计中继：补协方差 + 可选重打时间戳，专治 rf2o 的 odom 消息。

为什么需要它（详见 docs/superpowers/specs/2026-09-10-rf2o-ekf-odom-fusion-design.md）:
- rf2o 发布的 nav_msgs/Odometry 协方差**全为 0**。robot_localization 官方文档明确：
  被融合变量的方差若为 0，滤波器只加 1e-6 epsilon → 等于 100% 信任该测量，EKF 会退化成
  "rf2o 复读机"，轮速计白接。rf2o 是编译好的二进制，只能在中继里补协方差。
- rf2o 用激光扫描的 header.stamp 打时间戳，时间基准可能与 ROS 墙钟不一致；RL 按消息
  时间戳排序与判新鲜度，故默认重打 now()（参数 restamp:=false 可关）。
- rf2o 的 twist.linear.y 恒为 0（二维扫描匹配测不出横移），必须给大方差，否则会被
  当作"横移速度=0 且绝对可信"。

本节点不发布任何 TF。

用法:
    ros2 run robot_bringup odom_relay
    ros2 run robot_bringup odom_relay --ros-args -p input_topic:=/odom_x -p restamp:=false
"""
import rclpy
from rclpy.node import Node
from nav_msgs.msg import Odometry

from .odom_fusion import LASER_ODOM, LASER_ODOM_RAW, diag6_to_covariance36

# 默认协方差对角线 [x, y, z, roll, pitch, yaw]
DEFAULT_POSE_COV = [0.05, 0.05, 1e6, 1e6, 1e6, 0.02]
DEFAULT_TWIST_COV = [0.03, 1e6, 1e6, 1e6, 1e6, 0.02]


class OdomRelay(Node):
    def __init__(self):
        super().__init__('odom_relay')
        self.input_topic = self.declare_parameter('input_topic', LASER_ODOM_RAW).value
        self.output_topic = self.declare_parameter('output_topic', LASER_ODOM).value
        self.restamp = bool(self.declare_parameter('restamp', True).value)
        self.frame_id = self.declare_parameter('frame_id', '').value
        self.child_frame_id = self.declare_parameter('child_frame_id', '').value
        pose_cov = list(self.declare_parameter('pose_covariance', DEFAULT_POSE_COV).value)
        twist_cov = list(self.declare_parameter('twist_covariance', DEFAULT_TWIST_COV).value)
        self._pose_cov = diag6_to_covariance36(pose_cov)
        self._twist_cov = diag6_to_covariance36(twist_cov)

        self._pub = self.create_publisher(Odometry, self.output_topic, 10)
        self._sub = self.create_subscription(
            Odometry, self.input_topic, self._on_odom, 10)
        self.get_logger().info(
            'odom_relay: %s -> %s (restamp=%s) pose_cov=%s twist_cov=%s'
            % (self.input_topic, self.output_topic, self.restamp,
               ['%.3g' % v for v in pose_cov], ['%.3g' % v for v in twist_cov]))

    def _on_odom(self, msg):
        out = Odometry()
        out.header.stamp = (self.get_clock().now().to_msg()
                            if self.restamp else msg.header.stamp)
        out.header.frame_id = self.frame_id or msg.header.frame_id
        out.child_frame_id = self.child_frame_id or msg.child_frame_id
        out.pose.pose = msg.pose.pose
        out.pose.covariance = self._pose_cov
        out.twist.twist = msg.twist.twist
        out.twist.covariance = self._twist_cov
        self._pub.publish(out)


def main(args=None):
    rclpy.init(args=args)
    node = OdomRelay()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
