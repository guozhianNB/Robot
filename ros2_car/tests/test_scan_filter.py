"""scan_filter 纯逻辑单元测试（不需要 ROS 安装）。

对标上游 ROS1 `jie_ware/src/lidar_filter_node.cpp` 的离群点剔除，
但语义在本仓库被显式定义并被下列用例锁死（上游没写测试，行为靠读代码推断）：

1. 候选点必须是**自身有效**的读数（finite 且落在 [range_min, range_max]）；
2. **两侧**都必须"不一致"才删除（`abs(r[i]-r[j]) > outlier_threshold`，
   非有限邻居一律算不一致 —— 这是为了能剔除"空区里的孤立回波"）；
3. 索引 0 与 n-1 永远不动（上游循环从 1 到 n-2）；
4. 删除 = 置为 inf（无回波），强度置 0；
5. `clip_range` 只把超过量程的读数标成 inf，**不**计入删除数。
"""
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRINGUP_SRC = ROOT / "src" / "robot_bringup"
if str(BRINGUP_SRC) not in sys.path:
    sys.path.insert(0, str(BRINGUP_SRC))

from robot_bringup.scan_filter_core import (  # noqa: E402
    filter_scan_ranges,
    zero_intensities,
)

RANGE_MIN = 0.03
RANGE_MAX = 12.0
THRESHOLD = 0.1


def _run(ranges, **overrides):
    kwargs = dict(
        range_min=RANGE_MIN,
        range_max=RANGE_MAX,
        outlier_threshold=THRESHOLD,
    )
    kwargs.update(overrides)
    return filter_scan_ranges(ranges, **kwargs)


class ScanFilterTests(unittest.TestCase):
    # ---------------------------------------------------------------- 基本剔除
    def test_single_point_spike_is_replaced_with_no_return(self):
        out, removed = _run([5.0, 5.0, 5.0, 1.0, 5.0, 5.0, 5.0])
        self.assertEqual(removed, [3])
        self.assertEqual(out[3], math.inf)
        self.assertEqual(out[:3], [5.0, 5.0, 5.0])
        self.assertEqual(out[4:], [5.0, 5.0, 5.0])

    def test_smooth_surface_is_untouched(self):
        out, removed = _run([1.0, 1.02, 1.04, 1.06, 1.08, 1.10])
        self.assertEqual(removed, [])
        self.assertEqual(out, [1.0, 1.02, 1.04, 1.06, 1.08, 1.10])

    def test_input_is_not_mutated(self):
        ranges = [5.0, 5.0, 1.0, 5.0, 5.0]
        _run(ranges)
        self.assertEqual(ranges, [5.0, 5.0, 1.0, 5.0, 5.0])

    # ---------------------------------------------------------------- 边界与无效点
    def test_too_few_points_pass_through_unchanged(self):
        out, removed = _run([1.0, 9.0])
        self.assertEqual(removed, [])
        self.assertEqual(out, [1.0, 9.0])

    def test_endpoints_are_never_candidates(self):
        out, removed = _run([1.0, 5.0, 5.0, 5.0, 5.0, 5.0])
        self.assertEqual(removed, [])
        self.assertEqual(out[0], 1.0)

    def test_invalid_target_reading_is_left_alone(self):
        # 0.0 低于 range_min，本身无效 —— 不算离群候选，也不改成 inf
        out, removed = _run([5.0, 0.0, 5.0])
        self.assertEqual(removed, [])
        self.assertEqual(out[1], 0.0)

    def test_out_of_range_high_target_is_left_alone(self):
        out, removed = _run([5.0, 20.0, 5.0])
        self.assertEqual(removed, [])
        self.assertEqual(out[1], 20.0)

    # ---------------------------------------------------------------- 非有限邻居
    def test_edge_of_wall_is_kept_when_one_side_agrees(self):
        # 墙在 5m，墙外无回波：墙上的点只有一侧"不一致"，必须保留
        out, removed = _run([5.0, 5.0, 5.0, math.inf, math.inf, math.inf])
        self.assertEqual(removed, [])
        self.assertEqual(out[:3], [5.0, 5.0, 5.0])

    def test_lone_echo_surrounded_by_no_return_is_removed(self):
        # 空区里的一根孤立回波（走廊尽头噪点的典型形态）必须被剔除
        out, removed = _run([math.inf] * 3 + [4.0] + [math.inf] * 3)
        self.assertEqual(removed, [3])
        self.assertEqual(out[3], math.inf)

    def test_nan_neighbour_counts_as_disagreement(self):
        out, removed = _run([math.nan, 4.0, math.nan])
        self.assertEqual(removed, [1])
        self.assertEqual(out[1], math.inf)

    # ---------------------------------------------------------------- 细障碍的已知界限
    def test_two_beam_obstacle_survives(self):
        # 占 2 束的细障碍：对每个障碍点来说总有一侧邻居与自己一致 → 保住
        out, removed = _run([12.0, 12.0, 3.0, 3.0, 12.0, 12.0])
        self.assertEqual(removed, [])
        self.assertEqual(out[2:4], [3.0, 3.0])

    def test_single_beam_obstacle_is_indistinguishable_from_noise(self):
        # ⚠️ 已知界限（文档中明示）：只占 1 束的真实细障碍会被当成噪点删掉。
        #    404 束 / 360° ≈ 0.89°/束，3m 处一根 5cm 细杆正好只占 1 束。
        out, removed = _run([12.0, 12.0, 3.0, 12.0, 12.0])
        self.assertEqual(removed, [2])
        self.assertEqual(out[2], math.inf)

    def test_threshold_boundary_is_exclusive(self):
        # 差值恰好等于阈值不算不一致
        out, removed = _run([5.0, 5.0, 5.1, 5.0, 5.0])
        self.assertEqual(removed, [])

    # ---------------------------------------------------------------- clip_range
    def test_clip_range_disabled_by_default(self):
        out, removed = _run([5.0, 9.0, 9.0, 5.0])
        self.assertEqual(out, [5.0, 9.0, 9.0, 5.0])

    def test_clip_range_marks_far_readings_as_no_return_without_counting_them(self):
        out, removed = _run([5.0, 9.0, 9.0, 5.0], clip_range=8.0)
        self.assertEqual(removed, [])
        self.assertEqual(out, [5.0, math.inf, math.inf, 5.0])

    def test_clip_range_keeps_readings_at_the_boundary(self):
        out, _ = _run([8.0, 8.0, 8.0], clip_range=8.0)
        self.assertEqual(out, [8.0, 8.0, 8.0])

    def test_clip_range_does_not_touch_already_invalid_readings(self):
        out, removed = _run([math.nan, 0.0, 9.0], clip_range=8.0)
        self.assertEqual(removed, [])
        self.assertTrue(math.isnan(out[0]))
        self.assertEqual(out[1], 0.0)

    # ---------------------------------------------------------------- 强度
    def test_zero_intensities_only_touches_given_indices(self):
        intensities = [10.0, 20.0, 30.0, 40.0]
        out = zero_intensities(intensities, [1, 3])
        self.assertEqual(out, [10.0, 0.0, 30.0, 0.0])

    def test_zero_intensities_tolerates_short_intensity_arrays(self):
        out = zero_intensities([10.0], [0, 5])
        self.assertEqual(out, [0.0])


if __name__ == "__main__":
    unittest.main()
