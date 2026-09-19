"""lidar_loc 接线契约（静态检查，不需要 ROS 安装）。

锁的是一批"错了就静默失效"的接线不变量，尤其是两条安全属性：
**不能被默认启动**（会和 AMCL 抢 map→odom）、**/map 必须用 TRANSIENT_LOCAL**
（否则永远等不到 latched 的地图）。
"""
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRINGUP = ROOT / "src" / "robot_bringup"
NODE = BRINGUP / "robot_bringup" / "lidar_loc.py"
CORE = BRINGUP / "robot_bringup" / "lidar_loc_core.py"
CONFIG = BRINGUP / "config" / "lidar_loc.yaml"
LAUNCH = BRINGUP / "launch" / "lidar_loc.launch.py"
SETUP = BRINGUP / "setup.py"
URDF = BRINGUP / "urdf" / "car.urdf"
SLAM_PARAMS = BRINGUP / "config" / "slam_toolbox_params.yaml"
LIDAR_PARAMS = BRINGUP / "config" / "lidar_tmini_plus.yaml"
NAV_LAUNCH = BRINGUP / "launch" / "navigation.launch.py"
BRINGUP_LAUNCH = BRINGUP / "launch" / "bringup.launch.py"
ROBOT_BASE_LAUNCH = BRINGUP / "launch" / "robot_base.launch.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """剥掉注释与三引号字符串，只留可执行/可生效的部分。

    不这么做的话，"注释里解释**为什么不用**某种写法"会被误判成"代码里用了它"。
    """
    text = re.sub(r'""".*?"""', '""', text, flags=re.S)
    text = re.sub(r"'''.*?'''", "''", text, flags=re.S)
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _method_body(source: str, name: str) -> str:
    tail = source.split("def %s" % name, 1)[1]
    return tail.split("\n    def ", 1)[0]


class LidarLocSafetyTests(unittest.TestCase):
    """两条安全属性：不能被默认启动；不能和 AMCL 抢 TF。"""

    def test_not_wired_into_any_default_bringup(self):
        for path in (NAV_LAUNCH, BRINGUP_LAUNCH, ROBOT_BASE_LAUNCH):
            self.assertNotIn("lidar_loc", _read(path),
                             "%s 不能默认拉 lidar_loc（会与 AMCL 抢 map→odom）"
                             % path.name)

    def test_node_warns_about_amcl_conflict(self):
        source = _read(NODE)
        self.assertIn("不要与 AMCL 同时运行", source)

    def test_launch_is_self_contained_without_amcl(self):
        source = _read(LAUNCH)
        self.assertIn("nav2_map_server", source)
        self.assertIn("nav2_lifecycle_manager", source)
        self.assertNotIn("amcl", source)
        self.assertIn("node_names': ['map_server']", source)

    def test_map_subscription_is_transient_local(self):
        # map_server 是 latched 发布：VOLATILE 的后来者收不到那张已发过的图
        source = _read(NODE)
        self.assertIn("DurabilityPolicy.TRANSIENT_LOCAL", source)
        self.assertIn("durability=DurabilityPolicy.TRANSIENT_LOCAL", source)

    def test_scan_subscription_uses_sensor_data_qos(self):
        source = _read(NODE)
        self.assertIn("qos_profile_sensor_data", source)

    def test_pose_is_published_on_the_amcl_pose_topic(self):
        # 后端 LLM/maps/roslink.py 默认订阅 /amcl_pose；少这个输出就断了位姿链路
        self.assertIn("pose_topic', '/amcl_pose'", _read(NODE))
        self.assertIn("pose_topic: /amcl_pose", _read(CONFIG))

    def test_tf_is_broadcast_from_map_to_odom(self):
        source = _read(NODE)
        self.assertIn("TransformBroadcaster", source)
        self.assertIn("tf.header.frame_id = 'map'", source)
        self.assertIn("tf.child_frame_id = self.odom_frame", source)


class LidarLocRegressionTests(unittest.TestCase):
    """上游 lidar_loc.cpp 的三个坑，逐个用测试钉住。"""

    def test_receiving_a_map_does_not_reset_the_pose(self):
        # 上游 crop_map() 末尾会构造 (0,0,0) 的假 initialpose 把自己清零 →
        # 配上每 5 秒发图的 slam_toolbox 就永远收敛不了
        body = _method_body(_read(NODE), "_on_map")
        code = _code_only(body)
        self.assertNotIn("_map_to_base", code)
        self.assertNotIn("_map_to_odom", code)
        self.assertIn("这里**不**动", body)   # 注释里显式写明这条不变量

    def test_hill_climb_has_an_iteration_cap(self):
        body = _code_only(_method_body(_read(CORE), "hill_climb"))
        self.assertIn("max_iters", body)
        self.assertIn("while iters < max_iters", body)
        self.assertNotIn("while True", body)

    def test_no_lidar_inversion_heuristic(self):
        # 上游靠 TF 的 roll/pitch 猜雷达倒装再翻 x/y；本车 URDF + 驱动已经处理过，
        # 再做一次就是双重翻转
        core = _code_only(_read(CORE))
        self.assertNotIn("inverted", core)
        self.assertNotIn("lidar_is_inverted", core)

    def test_odom_prior_is_used_to_seed_the_climb(self):
        source = _read(NODE)
        body = _method_body(source, "_on_scan")
        self.assertIn("compose(self._map_to_odom, odom_to_base)", body)

    def test_per_scan_correction_is_bounded(self):
        # 上板实测：种子给错时会一路跑飞（8 帧漂 3.2m），必须有修正量上限
        body = _method_body(_read(NODE), "_on_scan")
        self.assertIn("correction_within_limits", body)
        self.assertIn("pose = seed", body)
        config = _read(CONFIG)
        self.assertRegex(config, r"(?m)^\s+max_correction_m:")
        self.assertRegex(config, r"(?m)^\s+max_correction_yaw_deg:")

    def test_matching_does_not_starve_tf_publishing(self):
        source = _read(NODE)
        self.assertIn("MultiThreadedExecutor", source)
        self.assertIn("MutuallyExclusiveCallbackGroup", source)

    def test_malformed_map_is_rejected_before_reshape(self):
        body = _method_body(_read(NODE), "_on_map")
        self.assertIn("len(msg.data) !=", body)


class LidarLocWiringTests(unittest.TestCase):
    def test_core_has_no_ros_dependency(self):
        core = _read(CORE)
        self.assertNotIn("import rclpy", core)
        self.assertNotIn("from rclpy", core)

    def test_executable_is_registered(self):
        self.assertIn("lidar_loc = robot_bringup.lidar_loc:main", _read(SETUP))

    def test_reuses_the_existing_covariance_helper(self):
        # 别再写一份 diag6 → 36 的展开
        source = _read(NODE)
        self.assertIn("from .odom_fusion import diag6_to_covariance36", source)

    def test_frames_match_the_urdf_and_the_rest_of_the_stack(self):
        urdf = _read(URDF)
        self.assertIn('link name="base_link"', urdf)
        self.assertIn('link name="laser_link"', urdf)

        config = _read(CONFIG)
        self.assertIn("base_frame: base_link", config)
        self.assertIn("laser_frame: laser_link", config)
        self.assertIn("odom_frame: odom", config)
        # 上游默认值是 base_footprint / laser，本车没有这一级 —— 防止照抄
        self.assertNotIn("base_footprint", _code_only(config))

        self.assertIn("frame_id: laser_link", _read(LIDAR_PARAMS))
        self.assertIn("base_frame: base_link", _read(SLAM_PARAMS))

    def test_launch_declares_map_and_initial_pose_switches(self):
        source = _read(LAUNCH)
        for name in ("'map'", "'set_initial_pose'", "'initial_pose_x'",
                     "'initial_pose_y'", "'initial_pose_yaw'"):
            self.assertIn(name, source)

    def test_config_declares_every_declared_parameter(self):
        config = _read(CONFIG)
        for key in ("max_dist_px", "pad_px", "step_xy_px", "step_yaw_deg",
                    "max_iters", "min_scan_points", "tf_rate",
                    "transform_timeout", "pose_covariance"):
            self.assertRegex(config, r"(?m)^\s+%s:" % key)

    def test_max_iters_default_is_bounded_and_sane(self):
        got = re.search(r"(?m)^\s+max_iters:\s*(\d+)", _read(CONFIG))
        self.assertIsNotNone(got)
        self.assertLessEqual(int(got.group(1)), 64)


if __name__ == "__main__":
    unittest.main()
