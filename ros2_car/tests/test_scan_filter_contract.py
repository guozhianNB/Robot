"""scan_filter 接线契约（静态检查，不需要 ROS 安装）。

锁死的是"接线"而不是"算法"：算法在 test_scan_filter.py 里测。
这些断言防的是本仓库最容易犯的一类错 —— 改了节点却忘了改 launch/config，
或者**过早**把消费者切到 /scan_filtered。
"""
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRINGUP = ROOT / "src" / "robot_bringup"
NODE = BRINGUP / "robot_bringup" / "scan_filter.py"
CORE = BRINGUP / "robot_bringup" / "scan_filter_core.py"
LAUNCH = BRINGUP / "launch" / "scan_filter.launch.py"
LIDAR_LAUNCH = BRINGUP / "launch" / "lidar.launch.py"
SLAM_LAUNCH = BRINGUP / "launch" / "slam.launch.py"
CONFIG = BRINGUP / "config" / "scan_filter.yaml"
NAV2_PARAMS = BRINGUP / "config" / "nav2_params.yaml"
SETUP = BRINGUP / "setup.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class ScanFilterWiringTests(unittest.TestCase):
    def test_core_has_no_ros_dependency(self):
        # 纯逻辑层必须能在没有 rclpy 的机器上被单测导入
        source = _read(CORE)
        self.assertNotIn("import rclpy", source)
        self.assertNotIn("from rclpy", source)

    def test_node_uses_sensor_data_qos_on_both_ends(self):
        # ydlidar 以 best_effort 发布；订阅端用默认 RELIABLE 会静默收不到数据
        source = _read(NODE)
        self.assertEqual(source.count("qos_profile_sensor_data"), 3,
                         "订阅与发布都必须显式使用 sensor_data QoS")
        self.assertIn("from rclpy.qos import qos_profile_sensor_data", source)

    def test_node_forwards_every_laserscan_field(self):
        source = _read(NODE)
        for field in ("angle_min", "angle_max", "angle_increment", "time_increment",
                      "scan_time", "range_min", "range_max", "header"):
            self.assertIn("out.%s" % field, source)
        # range_min/range_max 必须是**消息里的**值，不能用常量
        self.assertIn("range_min=msg.range_min", source)
        self.assertIn("range_max=msg.range_max", source)

    def test_node_keeps_intensities_only_when_upstream_has_them(self):
        source = _read(NODE)
        self.assertIn("if len(msg.intensities) > 0:", source)
        self.assertIn("zero_intensities(msg.intensities, removed)", source)

    def test_executable_is_registered(self):
        self.assertIn("scan_filter = robot_bringup.scan_filter:main", _read(SETUP))

    def test_launch_remaps_to_absolute_topics(self):
        source = _read(LAUNCH)
        self.assertIn("'scan_filter'", source)
        self.assertIn("('scan', '/scan')", source)
        self.assertIn("('scan_filtered', '/scan_filtered')", source)

    def test_config_declares_the_node_name_and_params(self):
        source = _read(CONFIG)
        self.assertIn("scan_filter:", source)
        self.assertIn("ros__parameters:", source)
        self.assertIn("outlier_threshold:", source)
        self.assertIn("clip_range:", source)

    def test_config_defaults_to_not_clipping(self):
        # 裁剪会改变量程语义，默认必须关闭，由人显式打开
        lines = [ln.strip() for ln in _read(CONFIG).splitlines()
                 if ln.strip().startswith("clip_range:")]
        self.assertEqual(lines, ["clip_range: 0.0"])

    def test_lidar_launch_starts_the_filter_and_can_disable_it(self):
        source = _read(LIDAR_LAUNCH)
        self.assertIn("use_scan_filter", source)
        self.assertIn("scan_filter.launch.py", source)

    # ------------------------------------------------ 消费者尚未切换（防过早生效）
    def test_consumers_still_read_scan_until_verified_on_the_board(self):
        nav2 = _read(NAV2_PARAMS)
        # 用行锚定，避免把 amcl 的 `scan_topic: /scan` 也算成 costmap 的观测源
        self.assertEqual(len(re.findall(r"^\s+topic: /scan$", nav2, re.M)), 2,
                         "两个 costmap 的观测源应仍是 /scan")
        self.assertIn("scan_topic: /scan", nav2)
        self.assertIn("'scan_topic': '/scan'", _read(SLAM_LAUNCH))
        self.assertNotIn("/scan_filtered", nav2)
        self.assertNotIn("/scan_filtered", _read(SLAM_LAUNCH))


if __name__ == "__main__":
    unittest.main()
