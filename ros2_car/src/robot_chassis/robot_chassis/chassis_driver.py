# -*- coding: utf-8 -*-
"""STM32 麦轮底盘 ROS2 驱动节点。

职责（板卡侧只发"三轴整车速度"，不做电机控制）:

- 订阅 ``/cmd_vel`` (geometry_msgs/Twist) → 限速 + 加速度斜坡 → 组 ``SET_CAR_VEL`` 帧下发 STM32
- 周期发送（默认 50ms）：同时充当链路保活；看门狗超时（默认 0.5s 未收到 cmd_vel）自动补发全零制动
- 订阅 ``/robot/cmd_stop`` (std_msgs/Bool) 急停：一收即停（高优先级）
- 接收 STM32 ``STATUS`` 心跳（默认 100ms）→ 四轮 RPM 麦轮逆运动学 → 积分 → 发布 ``/odom`` + ``odom→base_link`` tf
- 串口断开自动重连（退避），重连成功自动恢复

参数见 config/chassis_params.yaml。麦轮轴正负号需真机标定（见 README"标定"节）。
"""

import math
import time

import rclpy
from rclpy.node import Node

from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from std_msgs.msg import Bool
from tf2_ros import TransformBroadcaster
from geometry_msgs.msg import TransformStamped

try:
    import serial
except ImportError:
    serial = None

from .usb_protocol import (
    CMD_STATUS,
    build_set_car_vel,
    build_stop,
    decode_status,
    FrameParser,
)

RAD_PER_RPM = 2.0 * math.pi / 60.0          # RPM → rad/s
RAD_S_TO_TENTH_DEG = 180.0 / math.pi * 10.0  # rad/s → 0.1°/s


class ChassisDriver(Node):
    def __init__(self):
        super().__init__("chassis_driver")

        # ---------- 参数 ----------
        self.declare_parameter("serial_port", "/dev/ttyACM0")
        self.declare_parameter("baudrate", 115200)
        self.declare_parameter("cmd_vel_topic", "/cmd_vel")
        self.declare_parameter("odom_topic", "/odom")
        self.declare_parameter("cmd_stop_topic", "/robot/cmd_stop")
        self.declare_parameter("odom_frame_id", "odom")
        self.declare_parameter("base_frame_id", "base_link")
        # 是否由本节点发布 odom→base_link TF。
        # use_ekf:=true 时由 EKF 统一发布 TF（见 robot_bringup/odom.launch.py），本节点应关掉避免双发布。
        self.declare_parameter("publish_tf", True)

        # 麦轮运动学几何（单位 m）—— 真机标定后修改
        self.declare_parameter("wheel_radius", 0.04)      # 轮半径 = 0.08/2（固件 MC_WHEEL_DIAMETER_MM=80）
        self.declare_parameter("rotate_radius", 0.15)     # L+W = 固件 MC_ROTATE_RADIUS_MM=150
        self.declare_parameter("wheel_signs", [1, 1, 1, 1])  # 四轮方向修正（LF,RF,LR,RR），标定用
        self.declare_parameter("sign_vx", 1.0)
        self.declare_parameter("sign_vy", 1.0)
        self.declare_parameter("sign_wz", 1.0)

        # 速度限制（养老院场景，安全优先）
        self.declare_parameter("max_vx", 0.5)    # m/s
        self.declare_parameter("max_vy", 0.3)    # m/s（麦轮横移）
        self.declare_parameter("max_wz", 0.8)    # rad/s
        self.declare_parameter("accel_limit", 0.5)    # m/s^2
        self.declare_parameter("ang_accel_limit", 0.8)  # rad/s^2

        # 时序
        self.declare_parameter("send_period", 0.05)      # 下发周期 s（20Hz）
        self.declare_parameter("watchdog_timeout", 0.5)  # cmd_vel 看门狗 s（键盘遥控需要放宽）

        # Humble rclpy Node 没有 get_parameter_names()，显式列出已声明参数
        self._p = {p.name: p.value for p in self.get_parameters([
            "serial_port", "baudrate", "cmd_vel_topic", "odom_topic", "cmd_stop_topic",
            "odom_frame_id", "base_frame_id", "publish_tf", "wheel_radius", "rotate_radius",
            "wheel_signs", "sign_vx", "sign_vy", "sign_wz",
            "max_vx", "max_vy", "max_wz", "accel_limit", "ang_accel_limit",
            "send_period", "watchdog_timeout",
        ])}
        # launch 层（odom.launch.py）传 publish_tf 时是字符串，归一化为 bool
        if isinstance(self._p["publish_tf"], str):
            self._p["publish_tf"] = self._p["publish_tf"].lower() in ("true", "1", "yes")
        self._p["publish_tf"] = bool(self._p["publish_tf"])

        if serial is None:
            raise RuntimeError("缺少 pyserial，请先安装: pip3 install pyserial")

        # ---------- 状态 ----------
        self._target = (0.0, 0.0, 0.0)      # 期望 (vx, vy, wz)
        self._current = (0.0, 0.0, 0.0)     # 斜坡后的实际下发值
        self._last_cmd_time = 0.0
        self._stop_flag = False             # 急停
        self._last_tick = time.monotonic()
        self._last_status_time = 0.0
        self._pose = (0.0, 0.0, 0.0)        # (x, y, yaw)
        self._odom_ok = False

        self._ser = None
        self._parser = FrameParser()
        self._read_buf = b""

        # ---------- ROS 接口 ----------
        self._cmd_sub = self.create_subscription(
            Twist, self._p["cmd_vel_topic"], self._on_cmd_vel, 10)
        self._stop_sub = self.create_subscription(
            Bool, self._p["cmd_stop_topic"], self._on_cmd_stop, 10)
        self._odom_pub = self.create_publisher(
            Odometry, self._p["odom_topic"], 10)
        self._tf_broadcaster = TransformBroadcaster(self)

        # 发送定时器
        self.create_timer(self._p["send_period"], self._send_tick)
        # 健康检查：超过 1s 没有 STATUS 则 odom 置为不可用（持续发布零速/保持）
        self.create_timer(1.0, self._health_tick)

        self._open_serial()
        self.get_logger().info(
            f"chassis_driver 启动: {self._p['serial_port']}@{self._p['baudrate']} | "
            f"限速 vx≤{self._p['max_vx']} vy≤{self._p['max_vy']} wz≤{self._p['max_wz']}")

    # ---------------- 串口 ----------------
    def _open_serial(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        try:
            self._ser = serial.Serial(
                self._p["serial_port"], self._p["baudrate"],
                timeout=0.05, write_timeout=0.1)
            self._parser = FrameParser()
            self.get_logger().info(f"串口已打开: {self._p['serial_port']}")
        except Exception as e:
            self.get_logger().warn(f"串口打开失败({e})，将自动重试…")
            self._ser = None

    def _send(self, data: bytes):
        if self._ser is None:
            return
        try:
            self._ser.write(data)
        except Exception as e:
            self.get_logger().warn(f"串口写入失败: {e}")
            self._open_serial()

    def _read_serial(self):
        """非阻塞读串口，喂给帧解析器。"""
        if self._ser is None:
            return
        try:
            n = self._ser.in_waiting
            if n > 0:
                self._parser.feed(self._ser.read(n))
        except Exception as e:
            self.get_logger().warn(f"串口读取失败: {e}")
            self._open_serial()

    # ---------------- 回调 ----------------
    def _on_cmd_vel(self, msg: Twist):
        if self._stop_flag:
            return  # 急停期间忽略
        self._last_cmd_time = time.monotonic()
        # 限速
        vx = max(-self._p["max_vx"], min(self._p["max_vx"], msg.linear.x))
        vy = max(-self._p["max_vy"], min(self._p["max_vy"], msg.linear.y))
        wz = max(-self._p["max_wz"], min(self._p["max_wz"], msg.angular.z))
        self._target = (vx, vy, wz)

    def _on_cmd_stop(self, msg: Bool):
        if msg.data:
            self._stop_flag = True
            self._target = (0.0, 0.0, 0.0)
            self._current = (0.0, 0.0, 0.0)
            self._send(build_stop())
            self.get_logger().warn("收到急停 robot/cmd_stop → 已制动")
        else:
            self._stop_flag = False

    # ---------------- 周期任务 ----------------
    def _send_tick(self):
        self._read_serial()
        now = time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now
        if dt <= 0 or dt > 0.5:
            dt = self._p["send_period"]

        # 看门狗：超时无新指令 → 目标清零
        if not self._stop_flag and now - self._last_cmd_time > self._p["watchdog_timeout"]:
            self._target = (0.0, 0.0, 0.0)

        # 加速度斜坡（防急冲）
        cur = list(self._current)
        for i, tgt in enumerate(self._target):
            limit = self._p["accel_limit"] if i < 2 else self._p["ang_accel_limit"]
            delta = limit * dt
            if tgt > cur[i]:
                cur[i] = min(tgt, cur[i] + delta)
            else:
                cur[i] = max(tgt, cur[i] - delta)
        self._current = tuple(cur)

        vx, vy, wz = self._current
        if self._ser is not None:
            if abs(vx) < 1e-6 and abs(vy) < 1e-6 and abs(wz) < 1e-6:
                self._send(build_stop())
            else:
                frame = build_set_car_vel(
                    int(round(vx * 1000)),          # m/s → mm/s
                    int(round(vy * 1000)),
                    int(round(wz * RAD_S_TO_TENTH_DEG)),
                )
                self._send(frame)
        elif time.monotonic() - self._last_cmd_time < 5.0:
            # 串口未就绪但有人下指令时提醒
            self.get_logger().warn_throttle(2.0, "串口未连接，无法下发 cmd_vel")

        # 处理上行帧
        self._process_upstream()

    def _health_tick(self):
        now = time.monotonic()
        if self._ser is None:
            # 尝试重连
            self._open_serial()
        elif now - self._last_status_time > 1.0:
            if self._odom_ok:
                self.get_logger().warn("超过 1s 未收到 STATUS 心跳，odom 标记不可用（检查底盘 USB）")
            self._odom_ok = False

    def _process_upstream(self):
        while True:
            frame = self._parser.next_frame()
            if frame is None:
                break
            cmd, payload = frame
            if cmd == CMD_STATUS:
                self._on_status(payload)
            # ACK 忽略（仅日志级信息可扩展）

    def _on_status(self, payload):
        try:
            seq, rpm, enc, flags = decode_status(payload)
        except ValueError as e:
            self.get_logger().warn(f"STATUS 解析失败: {e}")
            return
        self._last_status_time = time.monotonic()
        self._odom_ok = True
        now = self.get_clock().now().to_msg()

        # 麦轮逆运动学（X 型四轮，rpm 顺序 LF,RF,LR,RR）
        # 对照固件 mc_car_set 正向解：
        #   LF=f+l+rot  RF=f-l-rot  LR=f-l+rot  RR=f+l-rot
        # 反解：vx=(LF+RF+LR+RR)/4
        #       vy=(LF-RF-LR+RR)/4      （正=左移）
        #       wz=(LF-RF+LR-RR)/(4·L)  （正=左转）
        w = [rpm[i] * RAD_PER_RPM * self._p["wheel_signs"][i] for i in range(4)]
        r = self._p["wheel_radius"]
        lw = self._p["rotate_radius"]
        vx = self._p["sign_vx"] * r / 4.0 * ( w[0] + w[1] + w[2] + w[3])
        vy = self._p["sign_vy"] * r / 4.0 * ( w[0] - w[1] - w[2] + w[3])
        wz = self._p["sign_wz"] * r / (4.0 * lw) * ( w[0] - w[1] + w[2] - w[3])

        # 积分位姿（odom 世界系：x 前 y 左 z 上）
        dt = 0.1  # 心跳周期 100ms；若长期偏差可改由时间戳差分
        x, y, yaw = self._pose
        yaw += wz * dt
        x += (vx * math.cos(yaw) - vy * math.sin(yaw)) * dt
        y += (vx * math.sin(yaw) + vy * math.cos(yaw)) * dt
        self._pose = (x, y, yaw)

        # 发布 odom
        odom = Odometry()
        odom.header.stamp = now
        odom.header.frame_id = self._p["odom_frame_id"]
        odom.child_frame_id = self._p["base_frame_id"]
        odom.pose.pose.position.x = x
        odom.pose.pose.position.y = y
        odom.pose.pose.position.z = 0.0
        odom.pose.pose.orientation.z = math.sin(yaw / 2.0)
        odom.pose.pose.orientation.w = math.cos(yaw / 2.0)
        # 协方差给个初值（可标定）
        odom.pose.covariance[0] = 0.01
        odom.pose.covariance[7] = 0.01
        odom.pose.covariance[35] = 0.05
        odom.twist.twist.linear.x = vx
        odom.twist.twist.linear.y = vy
        odom.twist.twist.angular.z = wz
        odom.twist.covariance[0] = 0.01
        odom.twist.covariance[7] = 0.01
        odom.twist.covariance[35] = 0.05
        self._odom_pub.publish(odom)

        # tf odom → base_link（use_ekf 模式下由 EKF 发布，本节点关闭以免双发布）
        if self._p["publish_tf"]:
            t = TransformStamped()
            t.header.stamp = now
            t.header.frame_id = self._p["odom_frame_id"]
            t.child_frame_id = self._p["base_frame_id"]
            t.transform.translation.x = x
            t.transform.translation.y = y
            t.transform.rotation.z = math.sin(yaw / 2.0)
            t.transform.rotation.w = math.cos(yaw / 2.0)
            self._tf_broadcaster.sendTransform(t)


def main(args=None):
    rclpy.init(args=args)
    node = ChassisDriver()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
