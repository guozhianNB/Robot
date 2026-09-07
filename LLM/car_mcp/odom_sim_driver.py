#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""VM 上用「底盘数据」测试高层 MCP 车控用的**模拟底盘驱动节点**。

它**镜像真机　`ros2_car/.../chassis_driver.py` 的语义**（不依赖 STM32 / 串口），
让 VM 上跑的高层 MCP 车控控制器（car_controller.py）能用一个"会响应 /cmd_vel 的
假底盘"做端到端自测 —— 同让同一份 MCP 代码将来在 RDK X5 上驱动真底盘时零改动。

模拟的行为与真机 chassis_driver 保持一致：
  - 订阅 ``/cmd_vel`` (geometry_msgs/Twist)：当作"持续速度保持"（不自己停）；
  - 看门狗：超过 ``watchdog_timeout`` 没收到新 /cmd_vel → 目标清零（车辆自然停）；
  - 订阅 ``/robot/cmd_stop`` (std_msgs/Bool)：高优先级急停，一收即停，需再收 false 解除；
  - 用当前 (斜坡后) 速度积分出位姿 → 发布 ``/odom`` + ``odom→base_link`` tf。

跑法（本机需已 source /opt/ros/jazzy/setup.bash）::

    python3 LLM/car_mcp/odom_sim_driver.py [--rate 100]

然后另开终端::

    ros2 topic echo /odom                      # 验证有数据
    ros2 topic pub /cmd_vel geometry_msgs/msg/Twist \
        "{linear:{x:0.2}, angular:{z:0.0}}" -r 20   # 下发 0.2 m/s 看它动起来

真实真机跑法见 car_mcp/README.md（起真 chassis_driver，本文件在真机不运行）。
"""
import argparse
import math

import rclpy
from rclpy.node import Node

from geometry_msgs.msg import Twist
from geometry_msgs.msg import TransformStamped, Quaternion
from nav_msgs.msg import Odometry
from std_msgs.msg import Bool
from tf2_ros import TransformBroadcaster


def _yaw_quat(yaw):
    return Quaternion(x=0.0, y=0.0, z=math.sin(yaw / 2.0), w=math.cos(yaw / 2.0))


class OdomSimDriver(Node):
    """响应 /cmd_vel 的速度保持型模拟底盘（镜像真机 chassis_driver 语义）。"""

    def __init__(self, cli_rate: int | None = None):
        super().__init__("odom_sim_driver")
        rate = cli_rate or self.declare_parameter("rate", 50).value   # Hz
        self.dt = 1.0 / rate

        # 与真机 chassis_driver 对齐的接口（同一套可被 MCP 控制器使用）
        self.declare_parameter("cmd_vel_topic", "/cmd_vel")
        self.declare_parameter("cmd_stop_topic", "/robot/cmd_stop")
        self.declare_parameter("odom_topic", "/odom")
        self.declare_parameter("odom_frame_id", "odom")
        self.declare_parameter("base_frame_id", "base_link")
        # 限速与斜坡（对齐真机 chassis_params.yaml 的保守值）
        self.declare_parameter("max_vx", 0.5)
        self.declare_parameter("max_vy", 0.3)
        self.declare_parameter("max_wz", 0.8)
        self.declare_parameter("accel_limit", 0.5)
        self.declare_parameter("ang_accel_limit", 0.8)
        self.declare_parameter("watchdog_timeout", 0.5)

        p = {x.name: x.value for x in self.get_parameters([
            "cmd_vel_topic", "cmd_stop_topic", "odom_topic",
            "odom_frame_id", "base_frame_id",
            "max_vx", "max_vy", "max_wz", "accel_limit", "ang_accel_limit",
            "watchdog_timeout"])}

        self._odom_frame = p["odom_frame_id"]
        self._base_frame = p["base_frame_id"]
        self._watchdog = p["watchdog_timeout"]
        self._max = (p["max_vx"], p["max_vy"], p["max_wz"])
        self._acc = (p["accel_limit"], p["accel_limit"], p["ang_accel_limit"])

        self._target = (0.0, 0.0, 0.0)               # 期望 (vx, vy, wz)
        self._current = (0.0, 0.0, 0.0)              # 斜坡后实际值
        self._stop_flag = False
        self._last_cmd_time = self._now_s()
        self._pose = (0.0, 0.0, 0.0)                 # (x, y, yaw)

        # ---- ROS 接口（与真机 chassis_driver 同名） ----
        self._cmd_sub = self.create_subscription(Twist, p["cmd_vel_topic"], self._on_cmd_vel, 10)
        self._stop_sub = self.create_subscription(Bool, p["cmd_stop_topic"], self._on_cmd_stop, 10)
        self._odom_pub = self.create_publisher(Odometry, p["odom_topic"], 10)
        self._tf_bc = TransformBroadcaster(self)
        self.create_timer(self.dt, self._tick)

        self.get_logger().info(
            f"odom_sim_driver 启动 @{rate}Hz | max vx={p['max_vx']} vy={p['max_vy']} "
            f"wz={p['max_wz']} | 看门狗 {self._watchdog}s")

    # ------------------------------------------------------------------ 内部
    def _now_s(self):
        return self.get_clock().now().nanoseconds * 1e-9  # 单调时间近似

    def _on_cmd_vel(self, msg: Twist):
        if self._stop_flag:
            return
        self._last_cmd_time = self._now_s()
        self._target = (
            max(-self._max[0], min(self._max[0], msg.linear.x)),
            max(-self._max[1], min(self._max[1], msg.linear.y)),
            max(-self._max[2], min(self._max[2], msg.angular.z)),
        )

    def _on_cmd_stop(self, msg: Bool):
        self._stop_flag = bool(msg.data)
        if self._stop_flag:
            self._target = (0.0, 0.0, 0.0)
            self._current = (0.0, 0.0, 0.0)
            self.get_logger().warn("收到急停 robot/cmd_stop → 已制动")

    def _tick(self):
        now = self._now_s()
        dt = self.dt
        # 看门狗：太久没新指令 → 停（镜像真机）
        if not self._stop_flag and (now - self._last_cmd_time) > self._watchdog:
            self._target = (0.0, 0.0, 0.0)
        # 加速度斜坡（防急冲）
        cur = list(self._current)
        for i, tgt in enumerate(self._target):
            delta = self._acc[i] * dt
            cur[i] = min(tgt, cur[i] + delta) if tgt > cur[i] else max(tgt, cur[i] - delta)
        vx, vy, wz = self._current = tuple(cur)

        # 积分位姿（odom 系：x 前 y 左 z 上，与真机一致性）
        x, y, yaw = self._pose
        yaw += wz * dt
        x += (vx * math.cos(yaw) - vy * math.sin(yaw)) * dt
        y += (vx * math.sin(yaw) + vy * math.cos(yaw)) * dt
        self._pose = (x, y, yaw)

        # ---- odom ----
        odom = Odometry()
        odom.header.stamp = self.get_clock().now().to_msg()
        odom.header.frame_id = self._odom_frame
        odom.child_frame_id = self._base_frame
        odom.pose.pose.position.x = x
        odom.pose.pose.position.y = y
        odom.pose.pose.orientation = _yaw_quat(yaw)
        odom.twist.twist.linear.x = vx
        odom.twist.twist.linear.y = vy
        odom.twist.twist.angular.z = wz
        odom.pose.covariance[0] = 0.01
        odom.pose.covariance[7] = 0.01
        odom.pose.covariance[35] = 0.05
        self._odom_pub.publish(odom)

        # ---- tf odom -> base_link ----
        t = TransformStamped()
        t.header.stamp = self.get_clock().now().to_msg()
        t.header.frame_id = self._odom_frame
        t.child_frame_id = self._base_frame
        t.transform.translation.x = x
        t.transform.translation.y = y
        t.transform.rotation = _yaw_quat(yaw)
        self._tf_bc.sendTransform(t)


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate", type=int, default=50)
    cli_args, _ = parser.parse_known_args()

    rclpy.init(args=args)
    node = OdomSimDriver(cli_rate=cli_args.rate)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
