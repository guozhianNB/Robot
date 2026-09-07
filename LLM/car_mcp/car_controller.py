#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""高层车载控制器核心（与 MCP/传输解耦，可在 VM 上对模拟底盘做端到端自测）。

真机语义回顾（见 car_mcp/README.md）：真机 `chassis_driver` 把 ``/cmd_vel`` 当作
"持续速度保持"，自身不带"走 x 米 / 转 x 度后停"概念。所以**高层运动"到没到、该停
没停"的判断逻辑（控制器）放在这一层** —— 这正是"在高层 MCP 侧做工具"的落点。

本类只依赖 ROS2（rclpy），**不 import MCP**，因此：
  - 可被 MCP 服务端(car_server.py)以"车控工具实现"身份 import，跑在 RDK X5 驱动真底盘；
  - 也可被 CLI/自测脚本直接驱动，在 VM 上配合 odom_sim_driver.py 当"假底盘"端到端测试
    "下发→看 odom→判到位→发停"，无需 python-mcp 依赖。→ 满足"VM 上用底盘数据测试"。

对外工具动作（每个都是"有明确终点、到位自动停"的语义动作）：
  - robot_move(direction, distance_m)    直线/横移前进 distance 米
  - robot_turn(angle_deg)                 原地转到指定角度
  - robot_stop()                          立即急停
  - robot_status()                        读当前位姿 / 是否运动中 (供模型先查再动)

【线程模型】
  ROS 节点 + 多线程 executor 跑在独立后台守护线程（收到 /odom 持续刷新最新位姿）；
  命令方法跑在调用方线程（MCP 工具调用 / CLI 主线程），用 time 循环持续 publish
  /cmd_vel + 读取后台刷新的位姿判到位。
"""
import math
import threading
import time

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.executors import MultiThreadedExecutor
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    from std_msgs.msg import Bool
    _ROS_AVAILABLE = True
    _MISSING = []
except Exception as e:                     # 遵循 AGENTS「系统稳健性」：缺依赖可降级
    rclpy = Node = MultiThreadedExecutor = None
    Twist = Odometry = Bool = None
    _ROS_AVAILABLE = False
    _MISSING = [str(e)]


# --------------------------------------------------------------------------
# 默认运动参数（保守养老院场景；真机/模拟共用，可在 __init__ 覆盖）
DEFAULT_SPEED_VX = 0.2        # 直行 m/s
DEFAULT_SPEED_VY = 0.15       # 横移 m/s
DEFAULT_SPEED_WZ_DEG = 20.0   # 转弯 deg/s
DEFAULT_MAX_DIST_M = 5.0      # 单次 move 距离上限 (m)
DEFAULT_MAX_ANGLE_DEG = 360.0 # 单次 turn 角度上限 (deg)
CMD_CADENCE = 0.05            # 下发 /cmd_vel 周期 s（20Hz）
ARRIVE_EPS = 0.02             # 到位判停位移裕度 (m)
ARRIVE_EPS_ANG = 2.0          # 到位判停角度裕度 (deg)


# 方向 → (vx, vy)，单位速度；与真机 odom 系一致：forward=+x, left=+y
_DIRECTION_VEC = {
    "forward": (1, 0),
    "back": (-1, 0),
    "left": (0, 1),
    "right": (0, -1),
}


class CarController:
    """高层车载控制器：move/turn/stop/status，输送到 /cmd_vel + 读 /odom 判到位。"""

    def __init__(self, *, 
                 cmd_vel_topic="/cmd_vel",
                 cmd_stop_topic="/robot/cmd_stop",
                 odom_topic="/odom",
                 speed_vx=DEFAULT_SPEED_VX,
                 speed_vy=DEFAULT_SPEED_VY,
                 speed_wz_deg=DEFAULT_SPEED_WZ_DEG,
                 max_dist_m=DEFAULT_MAX_DIST_M,
                 max_angle_deg=DEFAULT_MAX_ANGLE_DEG):
        self._topics = dict(cmd_vel=cmd_vel_topic, cmd_stop=cmd_stop_topic, odom=odom_topic)
        self._svx = speed_vx; self._svy = speed_vy
        self._swz = math.radians(speed_wz_deg)
        self._max_dist = max_dist_m
        self._max_ang = max_angle_deg

        self._ready = False
        self._ok = False
        self._ready_evt = threading.Event()
        self._pose = (0.0, 0.0, 0.0)       # (x,y,yaw)，由 odom 回调刷新（yaw 为 [-pi,pi]）
        self._pose_lock = threading.Lock()
        self._yaw_cum = 0.0                # 连续累计 yaw（不包裹），供 >180° 转向正确累加
        self._yaw_cum_set = False
        self._node = None
        self._exec = None
        self._pub_cmd = None
        self._pub_stop = None
        self._stop_flag = threading.Event()  # 供外部随时急停/取消当前动作
        self._busy = False
        self._thread = None

        # 是否已在 MCP 进程中初始化（避免多实例重复 rclpy.init）
        self._owns_rclpy = False

    # ------------------------------------------------------------- 生命周期
    @property
    def ros_available(self) -> bool:
        return _ROS_AVAILABLE

    @property
    def missing(self) -> list:
        return list(_MISSING)

    def start(self, timeout_s: float = 8.0) -> bool:
        """在后台线程起 rclpy 节点 + 多线程 executor。成功返回 True。"""
        if self._ok:
            return True
        if not _ROS_AVAILABLE:
            return False
        try:
            if not rclpy.ok():
                rclpy.init()
                self._owns_rclpy = True
            self._ready_evt = threading.Event()   # 每次 start 用全新就绪事件
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
            # 等后台 spin 线程建好节点（就绪事件）
            if self._ready_evt.wait(timeout=timeout_s):
                self._ok = True
                return True
            return False
        except Exception as e:
            self._ok = False
            return False

    def _spin(self):
        node = None
        try:
            node = Node("car_controller")
            self._pub_cmd = node.create_publisher(Twist, self._topics["cmd_vel"], 10)
            self._pub_stop = node.create_publisher(Bool, self._topics["cmd_stop"], 10)
            node.create_subscription(Odometry, self._topics["odom"], self._on_odom, 10)
            self._node = node
            exec_ = MultiThreadedExecutor()
            self._exec = exec_
            exec_.add_node(node)
            self._ready_evt.set()          # 通知 start() 已就绪
            exec_.spin()                   # spin 直到 shutdown() 停掉它
        except Exception:
            pass
        finally:
            # spin 结束后在本线程内销毁节点（不要在别的线程 destroy 正在 spin 的节点，会 SIGABRT）
            if node is not None:
                try:
                    node.destroy_node()
                except Exception:
                    pass
            self._node = None
            self._exec = None

    def shutdown(self):
        """停止后台 spin 线程与 rclpy。

        顺序安全：先让 executor 退出（spin 返回）→ join 后台线程（线程内自行销毁节点）
        → 再 rclpy.shutdown()。不要在别处 destroy 正在 spin 的节点。
        """
        self._stop_flag.set()              # 打断可能进行中的 move/turn
        exec_ = self._exec
        if exec_ is not None:
            try:
                exec_.shutdown(timeout_sec=2.0)   # 让 spin() 返回
            except Exception:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._owns_rclpy and rclpy and rclpy.ok():
            try:
                rclpy.shutdown()
            except Exception:
                pass
        self._ok = False

    # ------------------------------------------------------------- 内部
    def _on_odom(self, msg: Odometry):
        p = msg.pose.pose.position
        q = msg.pose.pose.orientation
        yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                         1.0 - 2.0 * (q.y * q.y + q.z * q.z))
        with self._pose_lock:
            if not self._yaw_cum_set:
                self._yaw_cum = yaw            # 首个采样作为累计基点
                self._yaw_cum_set = True
            else:
                # 用最短角差做连续累计：绕 ±180° 不跳变，>180° 转向能正确累加
                self._yaw_cum += self._ang_diff(yaw, self._pose[2])
            self._pose = (p.x, p.y, yaw)

    def _current_yaw_cum(self):
        """返回累计连续 yaw（当前执行线程读）。"""
        with self._pose_lock:
            return self._yaw_cum

    def _read_pose(self):
        with self._pose_lock:
            return self._pose

    def _send_vel(self, vx, vy, wz):
        """发布一次 /cmd_vel。"""
        if self._pub_cmd is None:
            return
        m = Twist()
        m.linear.x = float(vx)
        m.linear.y = float(vy)
        m.angular.z = float(wz)
        self._pub_cmd.publish(m)

    def _warn(self, msg):
        try:
            self._node.get_logger().warn(msg)
        except Exception:
            pass

    def _info(self, msg):
        try:
            self._node.get_logger().info(msg)
        except Exception:
            pass

    # ------------------------------------------------------------- 动作实现
    def robot_move(self, direction: str, distance_m: float) -> dict:
        """朝 direction 直行/横移 distance_m 米，到位自动停。

        direction: forward / back / left / right；distance_m > 0。
        """
        if not self._ok:
            return {"ok": False, "error": "控制器未启动/未连接 ROS2（底盘在跑吗？）"}
        d = (direction or "forward").lower()
        if d not in _DIRECTION_VEC:
            return {"ok": False, "error": f"未知方向: {direction}，可选 forward/back/left/right"}
        try:
            dist = float(distance_m)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"distance_m 非法: {distance_m}"}
        if dist <= 0:
            return {"ok": False, "error": "distance_m 必须为正"}
        if dist > self._max_dist:
            return {"ok": False, "error": f"单次直线移动超过上限 {self._max_dist}m，已拒绝（安全护栏）"}

        unit = _DIRECTION_VEC[d]
        # 用直行/横移各自速度
        if d in ("forward", "back"):
            vx = self._svx * unit[0]
            vy = 0.0
        else:  # left/right 横移（麦轮）
            vx = 0.0
            vy = self._svy * unit[1]

        x0, y0, _ = self._read_pose()
        # 到位判据：当前点到起点间的欧氏距离 >= dist - eps（直线运动朝向不变，等价累计路程）
        # 运动预算：路程/速度 ×3 + 3s，防底盘无响应死循环（单位一致：米 / 米每秒）
        budget = (dist / max(vx if vx else abs(vy), 1e-6)) * 3 + 3
        done = self._run_velocity(x_speed=vx, y_speed=vy,
                                  angular=0.0,
                                  target_metric=lambda x, y, yaw: self._planar_dist(x, y, x0, y0),
                                  target_value=dist,
                                  eps=ARRIVE_EPS,
                                  budget_s=budget)
        if not done:
            # 被停止 / 超时取消 → 按 "已走距离" 友好回执，便于模型判断
            return {"ok": False, "error": "移动被打断或未到位", "moved_m": round(self._planar_dist(*self._read_pose()[:2], x0, y0), 3)}
        moved = self._planar_dist(*self._read_pose()[:2], x0, y0)
        return {"ok": True, "moved_m": round(moved, 3), "action": f"move_{d}",
                "target_m": round(dist, 3)}

    def robot_turn(self, angle_deg: float) -> dict:
        """原地转到指定角度（正=左转，负=右转），到位自动停。

        内部统一用【弧度】累加/比较，绝不让 度/弧度 混算（曾导致转不到位）。
        """
        if not self._ok:
            return {"ok": False, "error": "控制器未启动/未连接 ROS2（底盘在跑吗？）"}
        try:
            ang = float(angle_deg)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"angle_deg 非法: {angle_deg}"}
        if abs(ang) > self._max_ang:
            return {"ok": False, "error": f"单次转向超过上限 {self._max_ang}°，已拒绝（安全护栏）"}
        sign = 1.0 if ang >= 0 else -1.0
        # 用「连续累计 yaw」做绝对转动目标：支持 >180°（一次最多 360°）正确累加不绕圈
        yaw0 = self._current_yaw_cum()

        # 全程弧度：目标 = 累计旋转量；判据用累计 yaw 差值（同单位，避度/弧度混算）
        target_rad = math.radians(abs(ang))
        eps_rad = math.radians(ARRIVE_EPS_ANG)
        goal = yaw0 + sign * target_rad          # 目标累计（带方向）
        # 预算：目标角 / 角速度 ×3 + 3s（弧度/每弧度每秒，单位一致）
        budget = (target_rad / max(abs(self._swz), 1e-6)) * 3 + 3

        done = self._run_velocity(x_speed=0.0, y_speed=0.0,
                                  angular=self._swz * sign,
                                  target_metric=lambda x, y, yaw: self._reached(yaw0, goal, sign),
                                  target_value=1.0,          # reached() 返回 0/1，≥(1-eps) 即判到
                                  eps=0.5,
                                  budget_s=budget)
        turned_deg = math.degrees(self._current_yaw_cum() - yaw0)
        if not done:
            return {"ok": False, "error": "转向被打断或未到位", "turned_deg": round(turned_deg, 2)}
        return {"ok": True, "turned_deg": round(turned_deg, 2), "target_deg": round(ang, 2)}

    def _reached(self, yaw0: float, goal: float, sign: float) -> float:
        """转向到位判据：累计 yaw 是否越过 goal（含 2° 裕度）。返回 0 未到 / 1 已到。"""
        cur = self._current_yaw_cum()
        eps = math.radians(ARRIVE_EPS_ANG)
        done_at = goal - sign * eps              # 到位线（带上裕度，同方向）
        if sign > 0:
            return 1.0 if cur >= done_at else 0.0
        return 1.0 if cur <= done_at else 0.0

    def robot_stop(self) -> dict:
        """立即急停：发 /robot/cmd_stop + 全零 /cmd_vel。"""
        self._stop_flag.set()
        if self._pub_stop is not None:
            b = Bool()
            b.data = True
            self._pub_stop.publish(b)
        self._send_vel(0.0, 0.0, 0.0)
        return {"ok": True, "result": "已急停"}

    def robot_status(self) -> dict:
        """读当前位姿与运动状态。"""
        x, y, yaw = self._read_pose()
        return {
            "ok": True,
            "moving": self._busy,
            "pose_m": {"x": round(x, 3), "y": round(y, 3)},
            "yaw_deg": round(math.degrees(yaw), 2),
        }

    # ------------------------------------------------------------- 底层执行
    def _run_velocity(self, x_speed, y_speed, angular, target_metric, target_value, eps, budget_s=30.0):
        """按固定 cadence 持续下发速度，直到 target_metric(...) >= target_value - eps，发停返回 True。

        注意 target_value / eps / target_metric 返回值必须用【同一单位】（调用方保证，
        常用米 或 弧度），budget_s 为秒级兜底超时（单位已由调用方算好）。

        任何时刻 stop_now / robot_stop 触发（从别的线程置 _stop_flag）→ 中断返回 False。
        """
        if not self._ok:
            return False
        self._stop_flag.clear()
        self._busy = True
        start = time.time()
        try:
            while not self._stop_flag.is_set():
                if target_metric(*self._read_pose()) >= (target_value - eps):
                    break
                self._send_vel(x_speed, y_speed, angular)
                self._sleep_cadence()
                if time.time() - start > budget_s:      # 兜底超时，防底盘无响应死循环
                    self._warn(f"运动超时兜底停止 (budget={budget_s:.1f}s)")
                    break
            self._send_vel(0.0, 0.0, 0.0)     # 到位/超时 → 显式停
            return True
        finally:
            self._busy = False
            self._send_vel(0.0, 0.0, 0.0)     # 无论中断/异常都保证停

    def _sleep_cadence(self):
        time.sleep(CMD_CADENCE)

    @staticmethod
    def _planar_dist(x, y, x0, y0):
        return math.hypot(x - x0, y - y0)

    @staticmethod
    def _ang_diff(a, b):
        d = (a - b) % (2 * math.pi)
        if d > math.pi:
            d -= 2 * math.pi
        return d
