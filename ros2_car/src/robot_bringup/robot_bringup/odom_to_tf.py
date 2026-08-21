#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 /odom 里程计话题转成 odom→base_link TF。

背景: rf2o 会把 odom→base_link TF 用「激光扫描的 header.stamp」作为时间戳发布,
而 YDLidar 的扫描时间戳不是 ROS 墙钟时间, 导致 slam_toolbox 查不到该 TF
(报 "Failed to compute odom pose")。本节点订阅 /odom, 用当前 ROS 时间重新
广播 odom→base_link TF, 绕开该问题。

用法:
    ros2 run robot_bringup odom_to_tf
    参数: odom_frame(默认 odom) / base_frame(默认 base_link) / odom_topic(默认 /odom)
"""
import rclpy
from rclpy.node import Node

from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry
from tf2_ros import TransformBroadcaster


class OdomToTf(Node):
    def __init__(self):
        super().__init__('odom_to_tf')
        self.odom_frame = self.declare_parameter('odom_frame', 'odom').value
        self.base_frame = self.declare_parameter('base_frame', 'base_link').value
        odom_topic = self.declare_parameter('odom_topic', '/odom').value

        self._tf_broadcaster = TransformBroadcaster(self)
        self._odom_sub = self.create_subscription(
            Odometry, odom_topic, self._odom_callback, 10)
        self.get_logger().info(
            'Broadcasting %s -> %s from %s' % (self.odom_frame, self.base_frame, odom_topic))

    def _odom_callback(self, msg):
        t = TransformStamped()
        # 时间戳直接用里程计消息的 header.stamp（= 雷达扫描时间），
        # 而不是 now()。slam_toolbox 的 getOdomPose(scan->header.stamp)
        # 会按「扫描时间戳」查 odom→base_link，时间戳对齐才不会查不到。
        t.header.stamp = msg.header.stamp
        t.header.frame_id = self.odom_frame
        t.child_frame_id = self.base_frame
        t.transform.translation.x = msg.pose.pose.position.x
        t.transform.translation.y = msg.pose.pose.position.y
        t.transform.translation.z = msg.pose.pose.position.z
        t.transform.rotation = msg.pose.pose.orientation
        self._tf_broadcaster.sendTransform(t)


def main(args=None):
    rclpy.init(args=args)
    node = OdomToTf()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
