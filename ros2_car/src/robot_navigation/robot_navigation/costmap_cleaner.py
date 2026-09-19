#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代价地图清除：收到 `/initialpose` 就清空 global + local 代价地图。

对应上游 ROS1 `jie_ware/src/costmap_cleaner.cpp`（那边调 `/move_base/clear_costmaps`）。
用途：在 rviz 里用 "2D Pose Estimate" 重定位后，代价地图里往往还留着按**旧**位姿
打上的障碍（"鬼影"），清一下再让 Nav2 重新规划。

⚠️ 这是**人机交互便利**，不是新能力：Nav2 的恢复行为树里本来就有 ClearCostmap
   恢复节点。它救不了的场景（比如障碍是真实动态物）清多少次也没用。

设计要点：
- 非阻塞：`call_async` + 回调，绝不在回调里等 service（否则会把 executor 挂住）。
- 可降级：nav2 没起来时服务不存在 → 启动自检告警 + 之后静默跳过，不崩、不刷屏。
- 只在 `/initialpose` 上动作，不订阅别的、不发布任何消息。

用法:
    ros2 run robot_navigation costmap_cleaner
    ros2 run robot_navigation costmap_cleaner --ros-args -p costmap_names:="['global_costmap']"
"""
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav2_msgs.srv import ClearEntireCostmap
import rclpy
from rclpy.node import Node

from .costmap_cleaner_core import (
    DEFAULT_COSTMAP_NAMES,
    clear_service_names,
    missing_services,
)

# 启动后多久做一次服务自检 / 告警节流（秒）
SELF_CHECK_DELAY_S = 5.0
WARN_THROTTLE_S = 30.0


class CostmapCleaner(Node):
    def __init__(self):
        super().__init__('costmap_cleaner')
        costmap_names = list(self.declare_parameter(
            'costmap_names', list(DEFAULT_COSTMAP_NAMES)).value)
        self._service_names = clear_service_names(costmap_names)

        self._clients = [
            self.create_client(ClearEntireCostmap, name)
            for name in self._service_names
        ]
        # /initialpose 用默认 QoS（reliable）—— 与 AMCL、rviz 的发布端一致
        self._sub = self.create_subscription(
            PoseWithCovarianceStamped, 'initialpose', self._on_initial_pose, 10)

        self._clears = 0
        self._skipped = 0
        self._checked = False
        self.create_timer(SELF_CHECK_DELAY_S, self._self_check)

        self.get_logger().info(
            'costmap_cleaner: /initialpose -> %s'
            % ', '.join(self._service_names))

    # ---------------------------------------------------------------- 自检
    def _self_check(self):
        """启动一次后检查服务在不在，把"静默失效"变成显式告警。"""
        if self._checked:
            return
        self._checked = True
        available = [name for name, client in
                     zip(self._service_names, self._clients)
                     if client.service_is_ready()]
        absent = missing_services(self._service_names, available)
        if absent:
            self.get_logger().warn(
                'costmap_cleaner: 这些服务当前不存在，收到 /initialpose 时会跳过：%s'
                '（若导航没起属正常；导航起了还没有就是服务名对不上）' % ', '.join(absent))

    # ---------------------------------------------------------------- 回调
    def _on_initial_pose(self, _msg):
        for name, client in zip(self._service_names, self._clients):
            if not client.service_is_ready():
                self._skipped += 1
                self.get_logger().warn(
                    'costmap_cleaner: %s 不可用，跳过' % name,
                    throttle_duration_sec=WARN_THROTTLE_S)
                continue
            future = client.call_async(ClearEntireCostmap.Request())
            future.add_done_callback(
                lambda fut, n=name: self._on_cleared(fut, n))

    def _on_cleared(self, future, name):
        error = future.exception()
        if error is not None:
            self.get_logger().error(
                'costmap_cleaner: %s 调用异常：%s' % (name, error))
            return
        self._clears += 1
        self.get_logger().info(
            'costmap_cleaner: 已清空 %s（累计 %d 次）' % (name, self._clears))


def main(args=None):
    rclpy.init(args=args)
    node = CostmapCleaner()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
