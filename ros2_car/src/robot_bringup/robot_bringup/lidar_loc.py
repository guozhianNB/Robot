#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""激光雷达定位：`/map` + `/scan` → `map→odom` TF 与位姿。

对应上游 ROS1 `jie_ware/src/lidar_loc.cpp`（算法纯逻辑在 `lidar_loc_core.py`，
有单测 + 真实地图离线端到端验证）。相对上游有四处**有意**改动：

1. **收到 `/map` 不再把位姿清零**：上游 `crop_map()` 末尾会构造一个 (0,0,0)
   的假 `initialpose` 直接重置自己 —— 配上每 5 秒发一次图的 slam_toolbox，
   位姿会被反复清零、永远收敛不了，所以它与建图模式从根本上互斥。这里只在
   **第一次**拿到地图时建代价场，之后换图才重建，且不动已收敛的位姿。
2. **同时输出 `/amcl_pose` 形状的位姿**：上游只广播 TF。本仓库后端
   `LLM/maps/roslink.py` 默认订阅 `/amcl_pose`、`LLM/maps/locator.py` 靠它判
   "位姿可疑" —— 少了这个输出，替换 AMCL 会直接把后端的位姿链路打断。
3. **有里程计先验**：上游每帧只从上一帧估计继续爬山，不用 `/odom` 增量；
   本车 0.8 rad/s 转向时每帧转 4.6°，而偏航步长只有 1°，很容易跟丢。
   这里用上一次的 `map→odom` ∘ 当前 `odom→base` 当初始猜测。
4. **爬山有界**：见 `lidar_loc_core.hill_climb`。
5. **每帧的修正量有上限**（上板实测逼出来的）：种子给错时爬山会持续"朝某个
   局部极大走"，每帧都有改进，于是越走越远（实测 8 帧从 (0,0) 漂到 3.2m 外、
   yaw 转了 60°）。现在要求解必须与里程计先验自洽，超限就不认这一帧的解。

⚠️ **绝不能与 AMCL 同时运行**：两者都广播 `map→odom`，TF 会出现两个发布者
   （违反 REP-105）。本节点故意**不接进** `navigation.launch.py`，要单独起。
⚠️ 它是**局部**跟踪器，没有全局重定位/恢复能力。使用前必须给初始位姿
   （rviz 的 2D Pose Estimate，或 `set_initial_pose:=true`）。

用法:
    ros2 launch robot_bringup lidar_loc.launch.py map:=<map.yaml>
    ros2 run robot_bringup lidar_loc --ros-args -p set_initial_pose:=true -p initial_pose_x:=1.0
"""
import math

from geometry_msgs.msg import PoseWithCovarianceStamped, TransformStamped
from nav_msgs.msg import OccupancyGrid
import rclpy
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                       ReliabilityPolicy, qos_profile_sensor_data)
from sensor_msgs.msg import LaserScan
from tf2_ros import Buffer, TransformBroadcaster, TransformListener

from .lidar_loc_core import (
    compose,
    correction_within_limits,
    cost_field_from_occupancy,
    hill_climb,
    invert,
    occupancy_grid_to_image,
    scan_endpoints_base,
    yaw_from_quaternion,
)
from .odom_fusion import diag6_to_covariance36

# 位姿协方差对角线 [x, y, z, roll, pitch, yaw]（米²/弧度²）。
# z/roll/pitch 给大值表示"平面车在这三个自由度上没有信息"（同 odom_relay 口径）。
DEFAULT_POSE_COV = [0.25, 0.25, 1e6, 1e6, 1e6, 0.07]


class LidarLoc(Node):
    def __init__(self):
        super().__init__('lidar_loc')
        self.base_frame = self.declare_parameter('base_frame', 'base_link').value
        self.odom_frame = self.declare_parameter('odom_frame', 'odom').value
        self.laser_frame = self.declare_parameter('laser_frame', 'laser_link').value
        laser_topic = self.declare_parameter('laser_topic', '/scan').value
        map_topic = self.declare_parameter('map_topic', '/map').value
        pose_topic = self.declare_parameter('pose_topic', '/amcl_pose').value

        self.max_dist_px = int(self.declare_parameter('max_dist_px', 50).value)
        self.pad_px = int(self.declare_parameter('pad_px', 50).value)
        self.step_xy_px = float(self.declare_parameter('step_xy_px', 1.0).value)
        self.step_yaw_rad = math.radians(
            float(self.declare_parameter('step_yaw_deg', 1.0).value))
        self.max_iters = int(self.declare_parameter('max_iters', 16).value)
        # 单帧修正量上限：解必须与里程计先验自洽，否则不认这一帧
        self.max_correction_m = float(
            self.declare_parameter('max_correction_m', 0.5).value)
        self.max_correction_yaw_rad = math.radians(
            float(self.declare_parameter('max_correction_yaw_deg', 20.0).value))
        self.min_scan_points = int(self.declare_parameter('min_scan_points', 20).value)
        self.publish_tf = bool(self.declare_parameter('publish_tf', True).value)
        self.tf_rate = float(self.declare_parameter('tf_rate', 20.0).value)
        self.transform_timeout = float(
            self.declare_parameter('transform_timeout', 0.2).value)

        self.set_initial_pose = bool(
            self.declare_parameter('set_initial_pose', False).value)
        initial_x = float(self.declare_parameter('initial_pose_x', 0.0).value)
        initial_y = float(self.declare_parameter('initial_pose_y', 0.0).value)
        initial_yaw = float(self.declare_parameter('initial_pose_yaw', 0.0).value)
        pose_cov = list(self.declare_parameter('pose_covariance', DEFAULT_POSE_COV).value)
        self._pose_cov = diag6_to_covariance36(pose_cov)

        # ---- 状态 ----
        self._occupancy = None        # 图像序（行 0 在最上方）
        self._resolution = None
        self._origin_xy = None
        self._map_height = None
        self._cost = None
        self._meta = None
        self._map_to_base = (initial_x, initial_y, initial_yaw) if self.set_initial_pose else None
        self._map_to_odom = None
        self._no_seed_warned = False

        # ---- TF ----
        self._tf_buffer = Buffer()
        self._tf_listener = TransformListener(self._tf_buffer, self)
        self._tf_broadcaster = TransformBroadcaster(self)

        # ---- 接口 ----
        # ⚠️ /map 必须用 TRANSIENT_LOCAL：map_server 是 latched 发布，一个
        #    VOLATILE 的后来者**收不到**那张已经发过的图，会一直等地图。
        map_qos = QoSProfile(depth=1,
                             history=HistoryPolicy.KEEP_LAST,
                             reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
        # 状态类回调放同一个互斥组，避免并发改 _map_to_odom；TF 定时器留在默认组，
        # 这样匹配（可能几十毫秒）不会把 TF 发布饿死。
        self._state_group = MutuallyExclusiveCallbackGroup()
        self._sub_map = self.create_subscription(
            OccupancyGrid, map_topic, self._on_map, map_qos,
            callback_group=self._state_group)
        self._sub_scan = self.create_subscription(
            LaserScan, laser_topic, self._on_scan, qos_profile_sensor_data,
            callback_group=self._state_group)
        self._sub_init = self.create_subscription(
            PoseWithCovarianceStamped, 'initialpose', self._on_initial_pose, 10,
            callback_group=self._state_group)
        self._pose_pub = self.create_publisher(PoseWithCovarianceStamped, pose_topic, 10)
        self.create_timer(1.0 / max(self.tf_rate, 1.0), self._publish_tf)

        self.get_logger().info(
            'lidar_loc: %s + %s -> map->%s TF + %s '
            '(base_frame=%s laser_frame=%s, max_iters=%d)'
            % (map_topic, laser_topic, self.odom_frame, pose_topic,
               self.base_frame, self.laser_frame, self.max_iters))
        self.get_logger().warn(
            'lidar_loc 会广播 map->%s，**不要与 AMCL 同时运行**（TF 会出现两个发布者）'
            % self.odom_frame)
        if not self.set_initial_pose:
            self.get_logger().info(
                '尚未有初始位姿：请在 rviz 里点 "2D Pose Estimate"，'
                '或用 -p set_initial_pose:=true 指定')

    # ------------------------------------------------------------------ 地图
    def _on_map(self, msg):
        if msg.info.resolution <= 0.0 or msg.info.width == 0 or msg.info.height == 0:
            self.get_logger().error('lidar_loc: 收到无效地图（分辨率/尺寸为 0），忽略')
            return

        # len(data) 与 width*height 不符时（截断的坏消息）必须先拒掉，
        # 否则 reshape 会抛异常把回调打挂。
        if len(msg.data) != msg.info.width * msg.info.height:
            self.get_logger().error(
                'lidar_loc: 地图数据长度 %d != %d×%d，忽略'
                % (len(msg.data), msg.info.width, msg.info.height))
            return

        image = occupancy_grid_to_image(msg.data, msg.info.width, msg.info.height)
        cost, meta = cost_field_from_occupancy(
            image, max_dist_px=self.max_dist_px, pad_px=self.pad_px)

        if float(cost.max()) <= 0.0:
            self.get_logger().warn(
                'lidar_loc: 地图里没有任何占据栅格，代价场全 0，无法定位')

        first = self._cost is None
        self._occupancy = image
        self._resolution = float(msg.info.resolution)
        self._origin_xy = (float(msg.info.origin.position.x),
                           float(msg.info.origin.position.y))
        self._map_height = int(msg.info.height)
        self._cost = cost
        self._meta = meta
        # ⚠️ 这里**不**动 _map_to_base / _map_to_odom（上游会把它清零）
        self.get_logger().info(
            'lidar_loc: %s地图 %dx%d @%.3fm，代价场裁剪 %dx%d @(%d,%d)'
            % ('首次加载' if first else '重新加载', msg.info.width, msg.info.height,
               self._resolution, meta['width'], meta['height'],
               meta['col0'], meta['row0']))

    # ---------------------------------------------------------------- 初始位姿
    def _on_initial_pose(self, msg):
        pose = msg.pose.pose
        self._map_to_base = (
            float(pose.position.x), float(pose.position.y),
            yaw_from_quaternion(pose.orientation.x, pose.orientation.y,
                                pose.orientation.z, pose.orientation.w))
        self.get_logger().info(
            'lidar_loc: 收到初始位姿 (%.3f, %.3f, %.4f rad)'
            % self._map_to_base)

    # ------------------------------------------------------------------ 定位
    def _lookup(self, target, source):
        try:
            tf = self._tf_buffer.lookup_transform(
                target, source, rclpy.time.Time(),
                timeout=Duration(seconds=self.transform_timeout))
        except Exception as exc:                      # noqa: BLE001 - TF 缺失是常态
            self.get_logger().warn(
                'lidar_loc: 查不到 %s→%s：%s' % (source, target, exc),
                throttle_duration_sec=5.0)
            return None
        tr = tf.transform.translation
        rot = tf.transform.rotation
        return (float(tr.x), float(tr.y),
                yaw_from_quaternion(rot.x, rot.y, rot.z, rot.w))

    def _on_scan(self, msg):
        if self._cost is None:
            self.get_logger().warn('lidar_loc: 还没收到地图', throttle_duration_sec=10.0)
            return
        if self._map_to_odom is None and self._map_to_base is None:
            if not self._no_seed_warned:
                self._no_seed_warned = True
                self.get_logger().warn(
                    'lidar_loc: 没有初始位姿，跳过匹配（局部跟踪器无法自行收敛）')
            return

        laser_to_base = self._lookup(self.base_frame, self.laser_frame)
        if laser_to_base is None:
            return
        odom_to_base = self._lookup(self.odom_frame, self.base_frame)
        if odom_to_base is None:
            return

        points = scan_endpoints_base(
            msg.ranges,
            angle_min=float(msg.angle_min),
            angle_increment=float(msg.angle_increment),
            range_min=float(msg.range_min),
            range_max=float(msg.range_max),
            laser_xy=(laser_to_base[0], laser_to_base[1]),
            laser_yaw=laser_to_base[2])
        if points.shape[0] < self.min_scan_points:
            self.get_logger().warn(
                'lidar_loc: 本帧只有 %d 个有效回波，跳过' % points.shape[0],
                throttle_duration_sec=5.0)
            return

        # 里程计先验：上一次的 map→odom 复合当前 odom→base
        seed = (compose(self._map_to_odom, odom_to_base)
                if self._map_to_odom is not None else self._map_to_base)

        pose, info = hill_climb(
            points, seed, self._cost,
            resolution=self._resolution, origin_xy=self._origin_xy,
            map_height=self._map_height,
            row0=self._meta['row0'], col0=self._meta['col0'],
            step_xy_px=self.step_xy_px, step_yaw_rad=self.step_yaw_rad,
            max_iters=self.max_iters)

        # 护栏：解必须与里程计先验自洽。种子给错时爬山会一路跑飞，超限就退回 seed
        # （等价于按里程计推算），并把这件事显式告警出去，而不是假装定位成功。
        if not correction_within_limits(pose, seed, self.max_correction_m,
                                        self.max_correction_yaw_rad):
            self.get_logger().warn(
                'lidar_loc: 本帧解 (%.2f, %.2f, %.1f°) 相对里程计先验偏离过大，'
                '已放弃（请重新给初始位姿）'
                % (pose[0], pose[1], math.degrees(pose[2])),
                throttle_duration_sec=5.0)
            pose = seed
            info = dict(info, converged=False, rejected=True)

        self._map_to_base = pose
        self._map_to_odom = compose(pose, invert(odom_to_base))
        self._publish_pose(pose, info)

        if not info['converged'] and not info.get('rejected'):
            self.get_logger().warn(
                'lidar_loc: 达到迭代上限 %d 仍未收敛（得分 %.2f）'
                % (info['iters'], info['score']), throttle_duration_sec=5.0)

    # ------------------------------------------------------------------ 输出
    def _publish_pose(self, map_to_base, info):
        msg = PoseWithCovarianceStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = 'map'
        msg.pose.pose.position.x = float(map_to_base[0])
        msg.pose.pose.position.y = float(map_to_base[1])
        yaw = float(map_to_base[2])
        msg.pose.pose.orientation.z = math.sin(yaw / 2.0)
        msg.pose.pose.orientation.w = math.cos(yaw / 2.0)
        msg.pose.covariance = self._pose_cov
        self._pose_pub.publish(msg)

    def _publish_tf(self):
        if not self.publish_tf or self._map_to_odom is None:
            return
        x, y, yaw = self._map_to_odom
        tf = TransformStamped()
        tf.header.stamp = self.get_clock().now().to_msg()
        tf.header.frame_id = 'map'
        tf.child_frame_id = self.odom_frame
        tf.transform.translation.x = float(x)
        tf.transform.translation.y = float(y)
        tf.transform.rotation.z = math.sin(yaw / 2.0)
        tf.transform.rotation.w = math.cos(yaw / 2.0)
        self._tf_broadcaster.sendTransform(tf)


def main(args=None):
    rclpy.init(args=args)
    node = LidarLoc()
    # 多线程：匹配可能占几十毫秒，别把 TF 发布饿死
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
