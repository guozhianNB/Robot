#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""激光扫描去噪：`/scan` → `/scan_filtered`（对应上游 jie_ware 的 lidar_filter_node）。

算法纯逻辑在 `scan_filter_core.py`（零 ROS 依赖、有单测），本文件只负责搬运消息。

⚠️ 默认**不改变任何现有消费者**：本节点只是额外多发一路 `/scan_filtered`，
`slam_toolbox` / 两个 costmap / `amcl` 仍然吃 `/scan`。要真正生效，
必须在 rviz 里对照确认无副作用后，把下面三处一起切到 `/scan_filtered`
（漏切任何一处 = "定位用的图" 与 "代价地图用的图" 不是同一份，极难查）：

    1. slam.launch.py 内联的 'scan_topic'（第 ~55 行）
       （以及 config/slam_toolbox_params.yaml 的 scan_topic，作为不经 launch 时的兜底）
    2. config/nav2_params.yaml 的 amcl.scan_topic
    3. config/nav2_params.yaml 的 global_costmap / local_costmap 的 scan.topic（两处）

用法:
    ros2 run robot_bringup scan_filter
    ros2 run robot_bringup scan_filter --ros-args -p clip_range:=8.0
"""
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan

from .scan_filter_core import filter_scan_ranges, zero_intensities

# 统计日志间隔（秒）：给"要不要切消费者"这个决定提供实据
STATS_PERIOD_S = 5.0


class ScanFilter(Node):
    def __init__(self):
        super().__init__('scan_filter')
        self.outlier_threshold = float(
            self.declare_parameter('outlier_threshold', 0.1).value)
        # clip_range=0 表示不裁；调到 ~8.0 可去掉 Tmini Plus 远端不可信的读数
        self.clip_range = float(
            self.declare_parameter('clip_range', 0.0).value)

        # QoS 必须用 sensor_data（best_effort）：ydlidar 驱动按此发布，
        # 订阅端若用默认 RELIABLE 会与之不兼容而**静默收不到任何数据**。
        # 下游 slam_toolbox / costmap / amcl 也全是 SensorDataQoS，故发布同样用它。
        self._pub = self.create_publisher(
            LaserScan, 'scan_filtered', qos_profile_sensor_data)
        self._sub = self.create_subscription(
            LaserScan, 'scan', self._on_scan, qos_profile_sensor_data)

        self._scans = 0
        self._removed = 0
        self._beam_total = 0
        self.create_timer(STATS_PERIOD_S, self._log_stats)

        self.get_logger().info(
            'scan_filter: %s -> %s (outlier_threshold=%.3g m, clip_range=%.3g m)'
            % (self._sub.topic_name, self._pub.topic_name,
               self.outlier_threshold, self.clip_range))

    def _on_scan(self, msg):
        out = LaserScan()
        out.header = msg.header
        out.angle_min = msg.angle_min
        out.angle_max = msg.angle_max
        out.angle_increment = msg.angle_increment
        out.time_increment = msg.time_increment
        out.scan_time = msg.scan_time
        out.range_min = msg.range_min
        out.range_max = msg.range_max

        ranges, removed = filter_scan_ranges(
            msg.ranges,
            range_min=msg.range_min,
            range_max=msg.range_max,
            outlier_threshold=self.outlier_threshold,
            clip_range=self.clip_range,
        )
        out.ranges = ranges
        if len(msg.intensities) > 0:
            out.intensities = zero_intensities(msg.intensities, removed)

        self._pub.publish(out)

        self._scans += 1
        self._removed += len(removed)
        self._beam_total += len(ranges)

    def _log_stats(self):
        if self._scans == 0:
            self.get_logger().warn(
                'scan_filter: 还没收到任何扫描 —— 确认雷达在跑，且 %s 的话题名/QoS 对得上'
                % self._sub.topic_name, throttle_duration_sec=STATS_PERIOD_S)
            return
        self.get_logger().info(
            'scan_filter: %d 帧 / 剔除 %d 点 (平均 %.4f%%/帧, 共 %d 束)'
            % (self._scans, self._removed,
               100.0 * self._removed / max(self._beam_total, 1), self._beam_total))


def main(args=None):
    rclpy.init(args=args)
    node = ScanFilter()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
