"""costmap_cleaner 单元与接线测试（不需要 ROS 安装）。

上游 ROS1 `jie_ware/src/costmap_cleaner.cpp` 是在收到 `/initialpose` 时调用
`/move_base/clear_costmaps`（move_base 时代的单一服务）。ROS2 Nav2 没有这个
总服务，必须分别清 **global 与 local 两个代价地图**，且服务名有坑：

    服务名 = "clear_entirely_" + costmap.getName()          ← 看 nav2 源码
    完整名 = /<costmap 名>/clear_entirely_<costmap 名>
    ✅ /global_costmap/clear_entirely_global_costmap
    ❌ /global_costmap/clear_entirely_global_costmap_costmap   （多写一个 _costmap）

来源：nav2_costmap_2d/src/clear_costmap_service.cpp（humble 分支）
      `node->create_service<ClearEntirely>("clear_entirely_" + costmap_.getName(), ...)`
被下面 test_service_names_do_not_repeat_the_costmap_suffix 钉死。
"""
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NAV_SRC = ROOT / "src" / "robot_navigation"
if str(NAV_SRC) not in sys.path:
    sys.path.insert(0, str(NAV_SRC))

from robot_navigation.costmap_cleaner_core import (  # noqa: E402
    DEFAULT_COSTMAP_NAMES,
    clear_service_names,
    missing_services,
)

NODE = NAV_SRC / "robot_navigation" / "costmap_cleaner.py"
SETUP = NAV_SRC / "setup.py"
LAUNCH = ROOT / "src" / "robot_bringup" / "launch" / "navigation.launch.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class ServiceNameTests(unittest.TestCase):
    def test_default_service_names_match_nav2_humble(self):
        self.assertEqual(
            clear_service_names(),
            [
                "/global_costmap/clear_entirely_global_costmap",
                "/local_costmap/clear_entirely_local_costmap",
            ],
        )

    def test_service_names_do_not_repeat_the_costmap_suffix(self):
        # 这是最容易写错的形式：把 getName() 当成 "global" 而不是 "global_costmap"
        for name in clear_service_names():
            self.assertNotIn("costmap_costmap", name)
            self.assertTrue(name.endswith("/clear_entirely_" + name.strip("/").split("/")[0]))

    def test_custom_costmap_names_are_honored(self):
        self.assertEqual(
            clear_service_names(["foo_costmap"]),
            ["/foo_costmap/clear_entirely_foo_costmap"],
        )

    def test_default_names_are_the_two_nav2_costmaps(self):
        self.assertEqual(tuple(DEFAULT_COSTMAP_NAMES),
                         ("global_costmap", "local_costmap"))


class MissingServiceTests(unittest.TestCase):
    def test_missing_services_reports_absent_ones(self):
        expected = ["/global_costmap/clear_entirely_global_costmap",
                    "/local_costmap/clear_entirely_local_costmap"]
        available = ["/local_costmap/clear_entirely_local_costmap", "/amcl/get_state"]
        self.assertEqual(missing_services(expected, available),
                         ["/global_costmap/clear_entirely_global_costmap"])

    def test_missing_services_is_empty_when_all_present(self):
        expected = ["/a", "/b"]
        self.assertEqual(missing_services(expected, ["/a", "/b", "/c"]), [])


class CostmapCleanerWiringTests(unittest.TestCase):
    def test_core_has_no_ros_dependency(self):
        core = NAV_SRC / "robot_navigation" / "costmap_cleaner_core.py"
        source = _read(core)
        self.assertNotIn("import rclpy", source)
        self.assertNotIn("from rclpy", source)

    def test_node_uses_the_nav2_clear_service_type(self):
        source = _read(NODE)
        self.assertIn("from nav2_msgs.srv import ClearEntireCostmap", source)
        self.assertIn("geometry_msgs.msg import PoseWithCovarianceStamped", source)

    def test_node_subscribes_initialpose_and_clears_on_it(self):
        source = _read(NODE)
        self.assertIn("'initialpose'", source)
        self.assertIn("def _on_initial_pose", source)

    def test_node_never_blocks_the_executor(self):
        # wait_for_service 只能用带超时的形式；真正的调用必须走 call_async
        source = _read(NODE)
        self.assertIn("call_async", source)
        self.assertNotIn("client.call(request)", source)
        for line in source.splitlines():
            if "wait_for_service" in line:
                self.assertIn("timeout_sec", line,
                              "wait_for_service 必须带超时，否则 nav2 没起来时会挂死节点")

    def test_node_tolerates_nav2_not_running(self):
        source = _read(NODE)
        self.assertIn("service_is_ready", source)
        self.assertIn("missing_services", source)

    def test_executable_is_registered(self):
        self.assertIn(
            "costmap_cleaner = robot_navigation.costmap_cleaner:main", _read(SETUP))

    def test_navigation_launch_starts_it_behind_a_switch(self):
        source = _read(LAUNCH)
        self.assertIn("executable='costmap_cleaner'", source)
        self.assertIn("'use_costmap_cleaner'", source)
        self.assertIn("IfCondition(use_costmap_cleaner)", source)


if __name__ == "__main__":
    unittest.main()
