# -*- coding: utf-8 -*-
"""命令行发送 Nav2 导航目标（免 rviz）。

用法（需先启动导航模式）:
    ros2 run robot_navigation navigate_to_pose --x 1.0 --y 0.5 --yaw 90
    ros2 run robot_navigation navigate_to_pose --x 2.0 --y 0.0 --frame_id map --timeout 120

参数:
    --x, --y     目标位置（米，地图系）
    --yaw        目标朝向（度，逆时针为正；默认保持当前朝向附近 0°）
    --frame_id   坐标系（默认 map）
    --timeout    等待超时秒数（默认 60）
"""

import argparse
import math

import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient

from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose


class NavigateToPoseClient(Node):
    def __init__(self, x, y, yaw_deg, frame_id, timeout_s):
        super().__init__("navigate_to_pose")
        self._client = ActionClient(self, NavigateToPose, "navigate_to_pose")
        self._x = x
        self._y = y
        self._yaw = math.radians(yaw_deg)
        self._frame_id = frame_id
        self._timeout_s = timeout_s

    def send(self) -> bool:
        if not self._client.wait_for_server(timeout_sec=10.0):
            self.get_logger().error("找不到 navigate_to_pose action server（Nav2 是否已启动？）")
            return False

        goal = NavigateToPose.Goal()
        goal.pose = PoseStamped()
        goal.pose.header.frame_id = self._frame_id
        goal.pose.header.stamp = self.get_clock().now().to_msg()
        goal.pose.pose.position.x = self._x
        goal.pose.pose.position.y = self._y
        goal.pose.pose.orientation.z = math.sin(self._yaw / 2.0)
        goal.pose.pose.orientation.w = math.cos(self._yaw / 2.0)

        self.get_logger().info(
            f"发送导航目标: ({self._x}, {self._y}) yaw={math.degrees(self._yaw):.1f}° 系={self._frame_id}")

        future = self._client.send_goal_async(goal)
        rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
        if future.result() is None or not future.result().accepted:
            self.get_logger().error("目标被 Nav2 拒绝（可能在地图外/障碍内）")
            return False

        goal_handle = future.result()
        self.get_logger().info("目标已接受，等待执行…")
        result_future = goal_handle.get_result_async()
        rclpy.spin_until_future_complete(self, result_future, timeout_sec=self._timeout_s)
        if result_future.result() is None:
            self.get_logger().warn("等待超时（可能仍在导航或卡住）")
            return False

        status = result_future.result().status
        ok = status == 4  # STATUS_SUCCEEDED
        self.get_logger().info(f"导航结束 status={status} {'✅ 成功' if ok else '❌ 失败'}")
        return ok


def main(args=None):
    rclpy.init(args=args)
    parser = argparse.ArgumentParser(description="发送 Nav2 导航目标")
    parser.add_argument("--x", type=float, default=0.0)
    parser.add_argument("--y", type=float, default=0.0)
    parser.add_argument("--yaw", type=float, default=0.0, help="目标朝向（度）")
    parser.add_argument("--frame_id", default="map")
    parser.add_argument("--timeout", type=float, default=60.0)
    argv = rclpy.utilities.remove_ros_args(args or [])
    parsed, _ = parser.parse_known_args(argv)

    node = NavigateToPoseClient(
        parsed.x, parsed.y, parsed.yaw, parsed.frame_id, parsed.timeout)
    ok = node.send()
    node.destroy_node()
    rclpy.shutdown()
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
