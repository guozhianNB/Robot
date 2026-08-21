# -*- coding: utf-8 -*-
"""急停节点：订阅 /robot/cmd_stop (std_msgs/Bool)。

收到 True 时：
1. 取消 Nav2 所有进行中的目标（navigate_to_pose / navigate_through_poses）
2. 发布全零 /cmd_vel（底盘驱动自身也有看门狗，双保险）

对应大模型端契约 topic：robot/cmd_stop（见 docs/目标文档及说明/ROS底盘接口需求.md）。

用法:
    ros2 run robot_navigation cmd_stop
    ros2 topic pub -1 /robot/cmd_stop std_msgs/msg/Bool "{data: true}"
"""

import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient

from geometry_msgs.msg import Twist
from std_msgs.msg import Bool
from nav2_msgs.action import NavigateToPose, NavigateThroughPoses


class CmdStop(Node):
    def __init__(self):
        super().__init__("cmd_stop")
        self._sub = self.create_subscription(Bool, "/robot/cmd_stop", self._on_stop, 10)
        self._vel_pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self._clients = {
            "navigate_to_pose": ActionClient(self, NavigateToPose, "navigate_to_pose"),
            "navigate_through_poses": ActionClient(self, NavigateThroughPoses, "navigate_through_poses"),
        }
        self.get_logger().info("cmd_stop 已就绪：订阅 /robot/cmd_stop")

    def _on_stop(self, msg: Bool):
        if not msg.data:
            return
        self.get_logger().warn("⚠️ 收到急停指令 → 取消导航目标 + 发布零速")
        for name, client in self._clients.items():
            if client.server_is_ready():
                future = client.cancel_all_goals_async()
                # 不阻塞等待
                future.add_done_callback(lambda f, n=name: self.get_logger().info(f"{n} 目标已取消"))
        zero = Twist()
        self._vel_pub.publish(zero)


def main(args=None):
    rclpy.init(args=args)
    node = CmdStop()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
