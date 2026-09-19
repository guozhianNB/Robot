"""lidar_loc 纯逻辑单元测试（不需要 ROS 安装，需要 numpy）。

对标上游 ROS1 `jie_ware/src/lidar_loc.cpp`：扫描端点 → 障碍代价场 → 15 个候选
（3 偏航 × 5 平移）爬山。本仓库重写时做了三处**有意**的改动，每处都有测试锁死：

1. **坐标系按本仓库口径走**：位姿一律用 map 系米/弧度 `(x, y, yaw)`，像素用图像行号
   （y 向下，与 `LLM/maps/mapserver.meters_to_pixel` 同一约定）。上游把 yaw 取负、
   又在像素空间里绕，等价但极易写错。
2. **爬山必须有迭代上限**：上游 `while(ros::ok())` 只在"连续 10 帧位移 < 5 像素/5°"
   时才 break，估计在两个极小之间振荡就永不退出（会挂死回调）。这里改成
   "没有候选能提高得分就停" —— 单调且有界。
3. **不做"雷达倒装"启发式**：上游靠 TF 的 roll/pitch 猜雷达是否倒装再翻 x/y。
   本车 URDF 有正确的 `laser_link`，驱动也配了 `inverted: true`，再做一次翻转
   就是**双重翻转**。故本实现只吃 TF 给的外参。

代价场的等价性：上游 `processMap()` 给每个障碍像素铺 101×101 线性衰减核再逐像素取
max，即 `cost = max(0, 1 - 欧氏距离/50)`；本实现用一次**截断欧氏距离变换**得到同一函数，
复杂度从 O(障碍数 × 101²) 降到 O(像素数)。
"""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
BRINGUP_SRC = ROOT / "src" / "robot_bringup"
if str(BRINGUP_SRC) not in sys.path:
    sys.path.insert(0, str(BRINGUP_SRC))

from robot_bringup.lidar_loc_core import (  # noqa: E402
    candidate_poses,
    compose,
    correction_within_limits,
    cost_field_from_occupancy,
    crop_bounds,
    hill_climb,
    invert,
    occupancy_grid_to_image,
    project_to_pixels,
    sample_cost_bilinear,
    scan_endpoints_base,
    score_pose,
    yaw_from_quaternion,
)


# --------------------------------------------------------------------------- 工具
def _grid(rows, cols, occupied=()):
    """构造 OccupancyGrid 风格的 int8 占用栅格：0=free, 100=occupied, -1=unknown。"""
    arr = np.zeros((rows, cols), dtype=np.int8)
    for r, c in occupied:
        arr[r, c] = 100
    return arr


class OccupancyGridOrientationTests(unittest.TestCase):
    def test_grid_rows_are_flipped_into_image_order(self):
        # OccupancyGrid: 行 0 在最下方。宽度 3、高度 2：
        #   下方一行 [1,2,3] → 图像里是最后一行
        #   上方一行 [4,5,6] → 图像里是第一行
        img = occupancy_grid_to_image([1, 2, 3, 4, 5, 6], width=3, height=2)
        np.testing.assert_array_equal(img, [[4, 5, 6], [1, 2, 3]])

    def test_flip_is_an_involution_for_square_grids(self):
        data = list(range(4))
        once = occupancy_grid_to_image(data, width=2, height=2)
        twice = occupancy_grid_to_image(once.ravel(), width=2, height=2)
        np.testing.assert_array_equal(twice, [[0, 1], [2, 3]])


class QuaternionTests(unittest.TestCase):
    @staticmethod
    def _zyx(yaw, pitch=0.0, roll=0.0):
        """按 ZYX 欧拉序构造四元数（roll→pitch→yaw 的标准公式）。"""
        cy, sy = math.cos(yaw / 2.0), math.sin(yaw / 2.0)
        cp, sp = math.cos(pitch / 2.0), math.sin(pitch / 2.0)
        cr, sr = math.cos(roll / 2.0), math.sin(roll / 2.0)
        return (sr * cp * cy - cr * sp * sy,
                cr * sp * cy + sr * cp * sy,
                cr * cp * sy - sr * sp * cy,
                cr * cp * cy + sr * sp * sy)

    def test_identity_gives_zero_yaw(self):
        self.assertAlmostEqual(yaw_from_quaternion(0.0, 0.0, 0.0, 1.0), 0.0)

    def test_pure_yaw_rotations_round_trip(self):
        for yaw in (0.3, -1.2, math.pi / 2, 2.9):
            z = math.sin(yaw / 2.0)
            w = math.cos(yaw / 2.0)
            self.assertAlmostEqual(yaw_from_quaternion(0.0, 0.0, z, w), yaw, places=9)

    def test_yaw_is_recovered_under_small_roll_and_pitch(self):
        # 雷达/底盘装配总有几度倾斜；通用式子必须仍然给出正确的 yaw
        yaw, pitch, roll = 0.7, math.radians(4.0), math.radians(7.0)
        x, y, z, w = self._zyx(yaw, pitch, roll)
        self.assertAlmostEqual(yaw_from_quaternion(x, y, z, w), yaw, places=9)

    def test_large_pitch_still_recovers_yaw(self):
        yaw, pitch, roll = -1.1, math.radians(20.0), 0.0
        x, y, z, w = self._zyx(yaw, pitch, roll)
        self.assertAlmostEqual(yaw_from_quaternion(x, y, z, w), yaw, places=9)


class CorrectionLimitTests(unittest.TestCase):
    """上板实测逼出来的护栏。

    种子给错时（原点但车不在原点），逐帧爬山会持续"朝某个局部极大走"，
    实测 8 帧内从 (0,0) 漂到 3.2m 外、yaw 从 0 转到 -60° —— 即**放任跑飞**。
    所以每帧相对里程计先验的修正量必须设上限，超了就不认这一帧的解。
    """

    def test_small_correction_is_within_limits(self):
        self.assertTrue(correction_within_limits(
            (0.1, -0.1, 0.05), (0.0, 0.0, 0.0), 0.5, math.radians(20)))

    def test_large_translation_is_rejected(self):
        self.assertFalse(correction_within_limits(
            (1.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.5, math.radians(20)))

    def test_large_yaw_is_rejected(self):
        self.assertFalse(correction_within_limits(
            (0.0, 0.0, math.radians(45)), (0.0, 0.0, 0.0), 0.5, math.radians(20)))

    def test_yaw_wrap_around_pi_is_not_treated_as_large(self):
        # 种子 yaw=179°、解 yaw=-179° → 实际只差 2°，不能因为跳变就拒掉
        self.assertTrue(correction_within_limits(
            (0.0, 0.0, math.radians(-179)), (0.0, 0.0, math.radians(179)),
            0.5, math.radians(5)))

    def test_boundary_is_inclusive(self):
        self.assertTrue(correction_within_limits(
            (0.5, 0.0, 0.0), (0.0, 0.0, 0.0), 0.5, math.radians(20)))


class CropBoundsTests(unittest.TestCase):
    def test_bounds_wrap_occupied_cells_with_padding(self):
        occ = _grid(5, 5, [(2, 3)])
        self.assertEqual(crop_bounds(occ, pad_px=1), (1, 2, 3, 3))

    def test_padding_is_clamped_to_the_map(self):
        occ = _grid(5, 5, [(0, 0)])
        self.assertEqual(crop_bounds(occ, pad_px=50), (0, 0, 5, 5))

    def test_map_without_occupied_cells_has_no_bounds(self):
        self.assertIsNone(crop_bounds(_grid(4, 4, [])))

    def test_unknown_and_free_cells_are_not_obstacles(self):
        occ = _grid(4, 4, [(1, 1)])
        occ[0, 0] = -1
        occ[3, 3] = 0
        self.assertEqual(crop_bounds(occ, pad_px=0), (1, 1, 1, 1))

    def test_multi_cell_bbox_is_honoured(self):
        occ = _grid(6, 6, [(1, 2), (4, 5)])
        self.assertEqual(crop_bounds(occ, pad_px=1), (0, 1, 6, 5))


class CostFieldTests(unittest.TestCase):
    def test_cost_is_one_on_obstacles_and_decays_linearly(self):
        occ = _grid(5, 5, [(2, 2)])
        cost, meta = cost_field_from_occupancy(occ, max_dist_px=2, pad_px=2)
        self.assertEqual((meta["row0"], meta["col0"]), (0, 0))
        self.assertEqual(cost.shape, (5, 5))
        self.assertAlmostEqual(cost[2, 2], 1.0)
        self.assertAlmostEqual(cost[2, 3], 0.5)          # 1 px / 2 px
        self.assertAlmostEqual(cost[2, 4], 0.0)          # 恰好到截断半径
        self.assertAlmostEqual(cost[0, 0], 0.0)          # 2.83 px > 2

    def test_cost_uses_euclidean_not_chebyshev_distance(self):
        # 对角 2.828 px，截断 4 px → 1 - 2.828/4 = 0.2929
        # 若误用切比雪夫(2 px) 会得 0.5、误用曼哈顿(4 px) 会得 0.0
        occ = _grid(5, 5, [(2, 2)])
        cost, _ = cost_field_from_occupancy(occ, max_dist_px=4, pad_px=2)
        self.assertAlmostEqual(cost[0, 0], 1.0 - math.hypot(2, 2) / 4.0, places=6)

    def test_cost_field_is_all_zero_without_obstacles(self):
        occ = _grid(4, 4, [])
        cost, _ = cost_field_from_occupancy(occ, max_dist_px=3, pad_px=2)
        self.assertTrue(np.all(cost == 0.0))

    def test_meta_offsets_locate_the_crop_in_the_full_map(self):
        occ = _grid(9, 9, [(4, 5)])
        cost, meta = cost_field_from_occupancy(occ, max_dist_px=1, pad_px=1)
        self.assertEqual((meta["row0"], meta["col0"], meta["height"], meta["width"]),
                         (3, 4, 3, 3))
        self.assertEqual(cost[1, 1], 1.0)                 # 障碍在裁剪图的中心

    def test_cost_does_not_exceed_one(self):
        occ = _grid(6, 6, [(2, 2), (2, 3)])
        cost, _ = cost_field_from_occupancy(occ, max_dist_px=3, pad_px=2)
        self.assertLessEqual(float(cost.max()), 1.0)
        self.assertGreaterEqual(float(cost.min()), 0.0)


class ScanEndpointsTests(unittest.TestCase):
    def test_forward_beam_lands_on_positive_x(self):
        pts = scan_endpoints_base([1.0], angle_min=0.0, angle_increment=0.1,
                                  range_min=0.03, range_max=12.0)
        self.assertEqual(pts.shape, (1, 2))
        self.assertAlmostEqual(pts[0, 0], 1.0)
        self.assertAlmostEqual(pts[0, 1], 0.0)

    def test_left_beam_uses_ros_sign_convention(self):
        # ROS 约定 y = +r*sin(a)；（上游在激光系里取 y 负、再到像素空间翻回来，等价）
        pts = scan_endpoints_base([2.0], angle_min=math.pi / 2, angle_increment=0.0,
                                  range_min=0.03, range_max=12.0)
        self.assertAlmostEqual(pts[0, 0], 0.0, places=9)
        self.assertAlmostEqual(pts[0, 1], 2.0)

    def test_invalid_readings_are_dropped(self):
        pts = scan_endpoints_base([math.nan, 0.01, 20.0, 3.0],
                                  angle_min=0.0, angle_increment=0.0,
                                  range_min=0.03, range_max=12.0)
        self.assertEqual(pts.shape, (1, 2))
        self.assertAlmostEqual(pts[0, 0], 3.0)

    def test_laser_extrinsic_translation_is_applied(self):
        pts = scan_endpoints_base([1.0], angle_min=0.0, angle_increment=0.0,
                                  range_min=0.03, range_max=12.0,
                                  laser_xy=(0.1, 0.0))
        self.assertAlmostEqual(pts[0, 0], 1.1)

    def test_laser_extrinsic_yaw_rotates_into_base(self):
        pts = scan_endpoints_base([1.0], angle_min=0.0, angle_increment=0.0,
                                  range_min=0.03, range_max=12.0,
                                  laser_yaw=math.pi / 2)
        self.assertAlmostEqual(pts[0, 0], 0.0, places=9)
        self.assertAlmostEqual(pts[0, 1], 1.0)

    def test_no_valid_readings_yields_empty_array(self):
        pts = scan_endpoints_base([math.nan, math.inf],
                                  angle_min=0.0, angle_increment=0.1,
                                  range_min=0.03, range_max=12.0)
        self.assertEqual(pts.shape, (0, 2))


class ProjectToPixelsTests(unittest.TestCase):
    # 与 LLM/maps/mapserver.meters_to_pixel 同一约定：py 是图像行号（y 向下）
    def test_point_on_positive_x_moves_right(self):
        px, py = project_to_pixels(np.array([[0.1, 0.0]]), (0.0, 0.0, 0.0),
                                   resolution=0.05, origin_xy=(0.0, 0.0),
                                   map_height=10)[0]
        self.assertAlmostEqual(px, 2.0)
        self.assertAlmostEqual(py, 9.0)

    def test_point_on_positive_y_moves_up_the_image(self):
        _, py = project_to_pixels(np.array([[0.0, 0.1]]), (0.0, 0.0, 0.0),
                                  resolution=0.05, origin_xy=(0.0, 0.0),
                                  map_height=10)[0]
        self.assertAlmostEqual(py, 7.0)

    def test_yaw_rotates_the_body_point(self):
        px, py = project_to_pixels(np.array([[0.1, 0.0]]), (0.0, 0.0, math.pi / 2),
                                   resolution=0.05, origin_xy=(0.0, 0.0),
                                   map_height=10)[0]
        self.assertAlmostEqual(px, 0.0, places=9)
        self.assertAlmostEqual(py, 7.0)

    def test_map_origin_offsets_are_honoured(self):
        px, py = project_to_pixels(np.array([[0.0, 0.0]]), (0.0, 0.0, 0.0),
                                   resolution=0.05, origin_xy=(-1.0, -2.0),
                                   map_height=10)[0]
        self.assertAlmostEqual(px, 20.0)
        self.assertAlmostEqual(py, -31.0)

    def test_crop_offsets_are_subtracted_to_index_the_cropped_cost_field(self):
        # 全图 px = (0-(-1))/0.05 = 20，裁剪从 col0=5 起 → 裁剪图里是 15
        # 全图 py = 9 - (0-(-2))/0.05 = -31，row0=3 → -34
        px, py = project_to_pixels(np.array([[0.0, 0.0]]), (0.0, 0.0, 0.0),
                                   resolution=0.05, origin_xy=(-1.0, -2.0),
                                   map_height=10, row0=3, col0=5)[0]
        self.assertAlmostEqual(px, 15.0)
        self.assertAlmostEqual(py, -34.0)


class SampleCostTests(unittest.TestCase):
    COST = np.array([[0.0, 1.0],
                     [1.0, 0.0]])

    def test_exact_corner_samples(self):
        got = sample_cost_bilinear(self.COST, np.array([[0.0, 0.0], [1.0, 0.0]]))
        np.testing.assert_allclose(got, [0.0, 1.0])

    def test_bilinear_midpoint_averages_four_corners(self):
        got = sample_cost_bilinear(self.COST, np.array([[0.5, 0.5]]))
        np.testing.assert_allclose(got, [0.5])

    def test_out_of_bounds_samples_zero_and_does_not_wrap(self):
        got = sample_cost_bilinear(self.COST, np.array([[2.0, 0.0], [-1.0, 0.0]]))
        np.testing.assert_allclose(got, [0.0, 0.0])

    def test_partially_out_of_bounds_uses_zero_for_the_missing_side(self):
        # x=1.5 → 右半权重给越界的 0.0，左半给 cost[0,1]=1.0
        got = sample_cost_bilinear(self.COST, np.array([[1.5, 0.0]]))
        np.testing.assert_allclose(got, [0.5])

    def test_nonfinite_pixels_sample_zero(self):
        got = sample_cost_bilinear(self.COST, np.array([[math.nan, 0.0]]))
        np.testing.assert_allclose(got, [0.0])


class ScorePoseTests(unittest.TestCase):
    def test_score_sums_the_sampled_costs(self):
        cost = np.full((5, 5), 0.25)
        pts = np.array([[0.0, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]])
        score = score_pose(pts, (0.0, 0.0, 0.0), cost,
                           resolution=0.05, origin_xy=(0.0, 0.0), map_height=5)
        self.assertAlmostEqual(score, 1.0)

    def test_empty_scan_scores_zero(self):
        cost = np.full((5, 5), 1.0)
        score = score_pose(np.zeros((0, 2)), (0.0, 0.0, 0.0), cost,
                           resolution=0.05, origin_xy=(0.0, 0.0), map_height=5)
        self.assertAlmostEqual(score, 0.0)


class HillClimbTests(unittest.TestCase):
    """人造代价场：峰值在像素 (10,10)，带平滑梯度（单格峰值没有梯度、爬不动）。"""

    RES = 0.05
    HEIGHT = 21

    def _cost(self):
        yy, xx = np.mgrid[0:self.HEIGHT, 0:21]
        dist = np.hypot(xx - 10.0, yy - 10.0)
        return np.clip(1.0 - dist / 20.0, 0.0, 1.0)

    def _pose_at_pixel(self, px, py):
        # px = x/res ; py = (H-1) - y/res
        return (px * self.RES, (self.HEIGHT - 1 - py) * self.RES, 0.0)

    def _climb(self, pose, **kw):
        kwargs = dict(resolution=self.RES, origin_xy=(0.0, 0.0),
                      map_height=self.HEIGHT, max_iters=50)
        kwargs.update(kw)
        return hill_climb(np.array([[0.0, 0.0]]), pose, self._cost(), **kwargs)

    def test_climbs_from_a_neighbouring_pixel_onto_the_peak(self):
        pose, info = self._climb(self._pose_at_pixel(10, 9))
        self.assertTrue(info["converged"])
        self.assertAlmostEqual(pose[0], 0.5, places=6)
        self.assertAlmostEqual(pose[1], 0.5, places=6)

    def test_score_never_decreases(self):
        pose0 = self._pose_at_pixel(10, 9)
        before = score_pose(np.array([[0.0, 0.0]]), pose0, self._cost(),
                            resolution=self.RES, origin_xy=(0.0, 0.0),
                            map_height=self.HEIGHT)
        _, info = self._climb(pose0)
        self.assertGreaterEqual(info["score"], before)

    def test_iterations_are_bounded_by_max_iters(self):
        # 从像素 (0,0) 爬到 (10,10) 需要 10 步左右；只给 2 次机会 → 必须停下且未收敛
        _, info = self._climb(self._pose_at_pixel(0, 0), max_iters=2)
        self.assertEqual(info["iters"], 2)
        self.assertFalse(info["converged"])

    def test_converges_immediately_when_already_at_the_peak(self):
        _, info = self._climb(self._pose_at_pixel(10, 10))
        self.assertTrue(info["converged"])
        self.assertEqual(info["iters"], 0)

    def test_a_far_off_start_reaches_the_peak_with_enough_iters(self):
        pose, info = self._climb(self._pose_at_pixel(0, 0), max_iters=500)
        self.assertTrue(info["converged"])
        self.assertAlmostEqual(pose[0], 0.5, places=3)
        self.assertAlmostEqual(pose[1], 0.5, places=3)


class CandidateTests(unittest.TestCase):
    def test_candidates_cover_three_yaws_and_five_translations(self):
        cands = candidate_poses((0.5, 0.5, 0.0), step_xy_m=0.05,
                                step_yaw_rad=math.radians(1.0))
        self.assertEqual(len(cands), 15)
        self.assertEqual(len({round(c[2], 12) for c in cands}), 3)
        self.assertEqual(len({(round(c[0], 12), round(c[1], 12)) for c in cands}), 5)

    def test_candidates_include_the_unchanged_pose(self):
        cands = candidate_poses((0.5, 0.5, 0.0), step_xy_m=0.05,
                                step_yaw_rad=math.radians(1.0))
        rounded = [(round(a, 12), round(b, 12), round(c, 12)) for a, b, c in cands]
        self.assertIn((0.5, 0.5, 0.0), rounded)

    def test_candidate_translations_are_one_step_in_map_axes(self):
        cands = candidate_poses((0.0, 0.0, 0.0), step_xy_m=0.25,
                                step_yaw_rad=math.radians(1.0))
        offsets = {(round(a, 12), round(b, 12)) for a, b, _ in cands}
        self.assertEqual(offsets, {(0.0, 0.0), (0.25, 0.0), (-0.25, 0.0),
                                   (0.0, 0.25), (0.0, -0.25)})


class TransformTests(unittest.TestCase):
    def test_invert_of_a_rotation_translation(self):
        x, y, yaw = invert((1.0, 0.0, math.pi / 2))
        self.assertAlmostEqual(x, 0.0, places=9)
        self.assertAlmostEqual(y, 1.0, places=9)
        self.assertAlmostEqual(yaw, -math.pi / 2)

    def test_compose_rotates_then_translates(self):
        x, y, yaw = compose((1.0, 0.0, math.pi / 2), (0.0, 1.0, 0.0))
        self.assertAlmostEqual(x, 0.0, places=9)
        self.assertAlmostEqual(y, 0.0, places=9)
        self.assertAlmostEqual(yaw, math.pi / 2)

    def test_compose_with_inverse_is_identity(self):
        for pose in [(0.3, -1.2, 0.7), (-2.0, 0.0, -math.pi / 3)]:
            x, y, yaw = compose(pose, invert(pose))
            self.assertAlmostEqual(x, 0.0, places=9)
            self.assertAlmostEqual(y, 0.0, places=9)
            self.assertAlmostEqual(yaw, 0.0, places=9)

    def test_map_to_odom_reproduces_the_map_pose(self):
        # T_map_odom = T_map_base ∘ inv(T_odom_base)：o 再乘回去必须还原 base 在 map 的位置
        map_to_base = (2.0, 1.0, 0.5)
        odom_to_base = (0.2, -0.4, 0.1)
        map_to_odom = compose(map_to_base, invert(odom_to_base))
        self.assertEqual(len(map_to_odom), 3)
        back = compose(map_to_odom, odom_to_base)
        for got, want in zip(back, map_to_base):
            self.assertAlmostEqual(got, want, places=9)


if __name__ == "__main__":
    unittest.main()
