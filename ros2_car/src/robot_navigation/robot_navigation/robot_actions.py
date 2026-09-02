# -*- coding: utf-8 -*-
"""大模型端对接执行节点：robot/move、robot/turn、robot/navigate_to + 状态上行。

对应契约 docs/目标文档及说明/ROS底盘接口需求.md 二、三节。
单节点集中实现，保证 /robot/exec_state、/robot/arrived 单一发布者。

- 服务 robot/move        (robot_interfaces/srv/Move)：直线/横移 distance_m，到位自动停
- 服务 robot/turn        (robot_interfaces/srv/Turn)：原地转 angle_deg，到位自动停
- 服务 robot/navigate_to (robot_interfaces/srv/NavigateTo)：place 或 x/y/theta → Nav2 导航
- 话题 /robot/exec_state (std_msgs/String)：idle | moving | navigating | error
- 话题 /robot/arrived    (std_msgs/Bool)：动作成功完成后 true，新动作开始时 false
- 订阅 /odom 判到位；发布 /cmd_vel 驱动底盘（chassis_driver 再转发 USB）
- 订阅 /robot/cmd_stop：急停时中止当前 move/turn（硬件/底盘仍以自身急停为准）

方向约定（REP-103，与 chassis_params.yaml 一致）：
- forward/back → cmd_vel.linear.x（正=前）；left/right 横移 → linear.y（正=左）；turn 正角=左转 → angular.z

架构说明（Humble 稳健性）：
- 服务回调用【同步阻塞 + MultiThreadedExecutor】，避免 rclpy Humble/Jazzy 的
  async 服务回调 wait-set 卡死缺陷（ros2/rclpy#1462）。
- 三个服务放同一个 MutuallyExclusiveCallbackGroup → 同一时刻只执行一个动作；
  订阅（/odom、/robot/cmd_stop）留在默认回调组，与服务组分离，
  服务回调阻塞等待期间订阅回调仍能在其它 executor 线程运行。

用法（由 bringup.launch.py navigation 模式带起，也可单独跑）:
    ros2 run robot_navigation robot_actions
"""

import math
import time

import rclpy
from rclpy.action import ActionClient
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from geometry_msgs.msg import PoseStamped, Twist
from nav_msgs.msg import Odometry
from nav2_msgs.action import NavigateToPose
from std_msgs.msg import Bool, String

from robot_interfaces.srv import Move, NavigateTo, Turn

# ---- 运动参数（养老院场景，保守低速）----
MOVE_SPEED = 0.15        # m/s，直线/横移速度
TURN_SPEED = 0.3         # rad/s，原地转向角速度
POS_TOL = 0.02           # 到位位移容差 m
YAW_TOL = 0.035          # 到位角度容差 rad ≈2°
STUCK_TIME = 0.8         # odom 停滞判定 s
MAX_EXEC_TIME = 30.0     # 单动作最长时间兜底 s
EXECUTOR_THREADS = 4


def _normalize_angle(a: float) -> float:
    """归一到 (-pi, pi]。"""
    while a > math.pi:
        a -= 2.0 * math.pi
    while a <= -math.pi:
        a += 2.0 * math.pi
    return a


class RobotActions(Node):
    def __init__(self):
        super().__init__("robot_actions")
        # 三个服务共用一个互斥回调组：同一时刻只跑一个动作；
        # 订阅留在默认组 → 服务阻塞期间 /odom、/robot/cmd_stop 仍能被处理
        self._svc_group = MutuallyExclusiveCallbackGroup()

        self._move_srv = self.create_service(Move, "robot/move",
                                             self._on_move, callback_group=self._svc_group)
        self._turn_srv = self.create_service(Turn, "robot/turn",
                                             self._on_turn, callback_group=self._svc_group)
        self._nav_srv = self.create_service(NavigateTo, "robot/navigate_to",
                                            self._on_navigate, callback_group=self._svc_group)

        # ---- 上行状态 ----
        self._exec_state_pub = self.create_publisher(String, "robot/exec_state", 10)
        self._arrived_pub = self.create_publisher(Bool, "robot/arrived", 10)

        # ---- 运动链路 ----
        self._vel_pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self._odom_sub = self.create_subscription(Odometry, "/odom", self._on_odom, 10)
        self._stop_sub = self.create_subscription(Bool, "/robot/cmd_stop",
                                                  self._on_cmd_stop, 10)
        self._nav_client = ActionClient(self, NavigateToPose, "navigate_to_pose")

        # ---- 状态 ----
        self._odom = None          # 最近一帧 odom（订阅线程写，动作线程读，GIL 下安全）
        self._stop_requested = False
        self._set_exec_state("idle")
        self.get_logger().info(
            "robot_actions 就绪：robot/move | robot/turn | robot/navigate_to | exec_state/arrived")

    # ---------- odom 缓存 ----------
    def _on_odom(self, msg: Odometry):
        self._odom = msg

    def _odom_pose(self):
        """返回 (x, y, yaw)；odom 不可用返回 None。"""
        if self._odom is None:
            return None
        q = self._odom.pose.pose.orientation
        yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                         1.0 - 2.0 * (q.y * q.y + q.z * q.z))
        return (self._odom.pose.pose.position.x,
                self._odom.pose.pose.position.y, yaw)

    def _on_cmd_stop(self, msg: Bool):
        if msg.data:
            self._stop_requested = True

    # ---------- 状态 ----------
    def _set_exec_state(self, state: str):
        self._exec_state_pub.publish(String(data=state))

    def _arrived(self, ok: bool):
        self._arrived_pub.publish(Bool(data=ok))

    def _begin_action(self, state: str) -> bool:
        """动作开始前的公共准备：清急停标记、置状态。返回 False 表示被急停打断过需中止。"""
        self._stop_requested = False
        self._set_exec_state(state)
        self._arrived(False)
        return True

    def _check_stopped(self) -> bool:
        """检查是否收到急停；若收到则补发零速并返回 True。"""
        if self._stop_requested:
            self._vel_pub.publish(Twist())
            return True
        return False

    # ---------- 服务：move ----------
    def _on_move(self, request, response):
        direction = request.direction.strip().lower()
        dist = float(request.distance_m)
        if direction not in ("forward", "back", "left", "right"):
            self._set_exec_state("error")
            response.success = False
            response.message = f"未知方向: {direction}（应为 forward/back/left/right）"
            return response
        if dist <= 0 or dist > 5.0:
            self._set_exec_state("error")
            response.success = False
            response.message = f"distance_m 非法: {dist}（应 0<d≤5）"
            return response

        self.get_logger().info(f"[move] {direction} {dist:.2f}m")
        self._begin_action("moving")
        ok, msg = self._drive_until(direction, dist)
        self._set_exec_state("idle" if ok else "error")
        self._arrived(ok)
        response.success = ok
        response.message = msg
        return response

    # ---------- 服务：turn ----------
    def _on_turn(self, request, response):
        angle = float(request.angle_deg)
        if abs(angle) <= 0 or abs(angle) > 360.0:
            self._set_exec_state("error")
            response.success = False
            response.message = f"angle_deg 非法: {angle}（应 0<|a|≤360）"
            return response

        self.get_logger().info(f"[turn] {angle:.1f}°")
        self._begin_action("moving")
        ok, msg = self._turn_until(math.radians(angle))
        self._set_exec_state("idle" if ok else "error")
        self._arrived(ok)
        response.success = ok
        response.message = msg
        return response

    # ---------- 服务：navigate_to ----------
    def _on_navigate(self, request, response):
        if request.place.strip():
            # place 表后置（依赖建图后标注），先明确拒绝
            self.get_logger().warn(f"[navigate_to] place='{request.place}' 但地点表未配置")
            self._set_exec_state("error")
            response.success = False
            response.message = f"地点表未配置，暂不支持 place='{request.place}'；请改用 x/y/theta"
            return response

        x = float(request.x)
        y = float(request.y)
        yaw = math.radians(float(request.theta))
        self.get_logger().info(f"[navigate_to] ({x:.2f}, {y:.2f}) yaw={math.degrees(yaw):.0f}°")
        self._begin_action("navigating")

        if not self._nav_client.wait_for_server(timeout_sec=10.0):
            self._set_exec_state("error")
            response.success = False
            response.message = "Nav2 navigate_to_pose action server 不可用（导航模式是否已启动？）"
            return response

        goal = NavigateToPose.Goal()
        goal.pose = PoseStamped()
        goal.pose.header.frame_id = "map"
        goal.pose.header.stamp = self.get_clock().now().to_msg()
        goal.pose.pose.position.x = x
        goal.pose.pose.position.y = y
        goal.pose.pose.orientation.z = math.sin(yaw / 2.0)
        goal.pose.pose.orientation.w = math.cos(yaw / 2.0)

        ok, msg = self._await_goal(goal)
        self._set_exec_state("idle" if ok else "error")
        self._arrived(ok)
        response.success = ok
        response.message = msg
        return response

    def _await_goal(self, goal):
        """阻塞等待 Nav2 action 完成（future 由其它 executor 线程解析）。"""
        send_future = self._nav_client.send_goal_async(goal)
        deadline = time.monotonic() + 20.0
        while not send_future.done():
            if time.monotonic() > deadline:
                return (False, "发送导航目标超时")
            time.sleep(0.05)
        if send_future.result() is None or not send_future.result().accepted:
            return (False, "目标被 Nav2 拒绝（可能在地图外/障碍内）")

        goal_handle = send_future.result()
        result_future = goal_handle.get_result_async()
        deadline = time.monotonic() + 180.0
        while not result_future.done():
            if self._check_stopped():
                goal_handle.cancel_goal_async()
                return (False, "收到急停，已取消导航")
            if time.monotonic() > deadline:
                return (False, "导航超时 180s")
            time.sleep(0.05)
        if result_future.result() is None:
            return (False, "导航结果丢失")
        status = result_future.result().status
        if status == 4:  # STATUS_SUCCEEDED
            return (True, "导航到达")
        return (False, f"导航失败 status={status}")

    # ---------- move/turn 共用的到位驱动 ----------
    def _drive_until(self, direction: str, dist: float):
        """沿指定轴累计位移直到 dist，到位自动停（同步阻塞）。"""
        start = self._odom_pose()
        if start is None:
            return (False, "odom 不可用（底盘/里程计未启动）")
        x0, y0, yaw0 = start

        # 目标轴投影（起点坐标系）：forward → 局部 x+；left → 局部 y+
        sign = {"forward": 1.0, "back": -1.0,
                "left": 1.0, "right": -1.0}[direction]
        use_y_axis = direction in ("left", "right")

        vel = Twist()
        if direction in ("forward", "back"):
            vel.linear.x = sign * MOVE_SPEED
        else:
            vel.linear.y = sign * MOVE_SPEED

        t0 = time.monotonic()
        last_pos = None
        last_t = t0
        while time.monotonic() - t0 < MAX_EXEC_TIME:
            if self._check_stopped():
                return (False, "收到急停，动作中止")
            cur = self._odom_pose()
            if cur is not None:
                dx = cur[0] - x0
                dy = cur[1] - y0
                # 旋转到起点朝向的局部坐标
                local = (dx * math.cos(yaw0) + dy * math.sin(yaw0),
                         -dx * math.sin(yaw0) + dy * math.cos(yaw0))
                traveled = local[1] if use_y_axis else local[0]
                if traveled * sign >= dist - POS_TOL:
                    self._vel_pub.publish(Twist())
                    self.get_logger().info(f"[move] 到位，实际位移 {traveled:.3f}m")
                    return (True, f"到位（{traveled:.2f}m）")

                # 停滞检测：odom 位置几乎不动
                now = time.monotonic()
                if last_pos is not None:
                    moved = math.hypot(cur[0] - last_pos[0], cur[1] - last_pos[1])
                    if moved < 0.005 and now - last_t > STUCK_TIME:
                        self._vel_pub.publish(Twist())
                        return (False, "odom 长时间无变化（可能被卡住/底盘未动）")
                if last_pos is None or math.hypot(cur[0] - last_pos[0], cur[1] - last_pos[1]) > 0.001:
                    last_pos = (cur[0], cur[1])
                    last_t = now
            self._vel_pub.publish(vel)
            time.sleep(0.05)

        self._vel_pub.publish(Twist())
        return (False, f"超时 {MAX_EXEC_TIME}s 未到位")

    def _turn_until(self, target_rad: float):
        """原地转 target_rad（带符号），到位自动停（同步阻塞）。"""
        start = self._odom_pose()
        if start is None:
            return (False, "odom 不可用（底盘/里程计未启动）")
        yaw0 = start[2]
        sign = 1.0 if target_rad >= 0 else -1.0

        vel = Twist()
        vel.angular.z = sign * TURN_SPEED

        t0 = time.monotonic()
        last_yaw = None
        last_t = t0
        while time.monotonic() - t0 < MAX_EXEC_TIME:
            if self._check_stopped():
                return (False, "收到急停，动作中止")
            cur = self._odom_pose()
            if cur is not None:
                d_yaw = _normalize_angle(cur[2] - yaw0)
                if d_yaw * sign >= abs(target_rad) - YAW_TOL:
                    self._vel_pub.publish(Twist())
                    self.get_logger().info(
                        f"[turn] 到位，实际转角 {math.degrees(d_yaw):.1f}°")
                    return (True, f"到位（{math.degrees(d_yaw):.1f}°）")

                now = time.monotonic()
                if last_yaw is not None:
                    moved = abs(_normalize_angle(cur[2] - last_yaw))
                    if moved < 0.005 and now - last_t > STUCK_TIME:
                        self._vel_pub.publish(Twist())
                        return (False, "yaw 长时间无变化（可能被卡住/底盘未动）")
                if last_yaw is None or abs(_normalize_angle(cur[2] - last_yaw)) > 0.001:
                    last_yaw = cur[2]
                    last_t = now
            self._vel_pub.publish(vel)
            time.sleep(0.05)

        self._vel_pub.publish(Twist())
        return (False, f"超时 {MAX_EXEC_TIME}s 未到位")


def main(args=None):
    rclpy.init(args=args)
    node = RobotActions()
    # 多线程 executor：一个线程跑阻塞的服务动作，其它线程继续处理订阅回调
    executor = MultiThreadedExecutor(num_threads=EXECUTOR_THREADS)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
