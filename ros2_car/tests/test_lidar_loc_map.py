"""真实地图上的离线端到端验证：合成扫描 → 从扰动位姿恢复原pose。

不需要 ROS，也不需要板卡：地图用仓库里的 `ros2_car/maps/my_map.pgm` + `.yaml`，
PGM 解析**复用** `LLM/maps/mapserver.py`（仓库已有的纯 stdlib 解析器），不再造一个。

方法：在真实地图上挑几个"周围障碍多"的位置当**真值位姿**，把附近的障碍栅格当成
一次无噪扫描的端点、反投影回 base 系得到合成扫描，再把起点扰动一点丢给爬山，
看能否回到真值。这验证的是整条链路（代价场 → 投影 → 采样 → 爬山 → 坐标约定），
不是某一个函数。

它**替代不了上板实测**：合成扫描没有噪声、没有运动畸变、也没有里程计先验的真实误差。
"""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
BRINGUP_SRC = ROOT / "src" / "robot_bringup"
for _p in (BRINGUP_SRC, REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from robot_bringup.lidar_loc_core import (  # noqa: E402
    cost_field_from_occupancy,
    hill_climb,
    score_pose,
)

MAP_PGM = ROOT / "maps" / "my_map.pgm"
MAP_YAML = ROOT / "maps" / "my_map.yaml"

# 复用仓库已有的 PGM 解析器；环境里没有 LLM 时整组跳过，不让它变成假红
try:
    from LLM.maps import mapserver as _mapserver

    _HAVE_PARSER = True
    _PARSER_ERR = ""
except Exception as exc:  # pragma: no cover - 只在缺 LLM 依赖时走到
    _HAVE_PARSER = False
    _PARSER_ERR = str(exc)

RESOLUTION = 0.05
PAD_PX = 50
MAX_DIST_PX = 50
SCAN_RADIUS_M = 3.5
PERTURB = (0.15, -0.15, math.radians(3.0))
XY_TOL_M = 0.10
YAW_TOL_RAD = math.radians(3.0)


def _load_occupancy():
    """→ (occupancy, origin_xy, height, width)：occupancy 行 0 在图像最上方。"""
    pgm = _mapserver.parse_pgm(MAP_PGM.read_bytes())
    meta = _mapserver.meta_from_yaml(MAP_YAML.read_text(encoding="utf-8"))
    vals = np.frombuffer(bytes(pgm.pixels), dtype=np.uint8)
    vals = vals.reshape(pgm.height, pgm.width).astype(np.float64)
    probability = (float(pgm.maxval or 255) - vals) / float(pgm.maxval or 255)
    occ = np.where(probability > float(meta["occupied_thresh"]), 100, 0).astype(np.int8)
    origin = (float(meta["origin"][0]), float(meta["origin"][1]))
    return occ, origin, pgm.height, pgm.width


def _occupied_in_map_meters(occ, origin, height):
    """→ (N,2) 障碍像素中心的 map 系米坐标（与 project_to_pixels 同一约定）。"""
    rows, cols = np.nonzero(occ == 100)
    mx = origin[0] + cols * RESOLUTION
    my = origin[1] + (height - 1 - rows) * RESOLUTION
    return np.stack([mx, my], axis=1)


def _pick_targets(points_m, count=3, min_sep_m=1.0,
                  x_range=(-3.0, 3.0), y_range=(-2.0, 1.5), grid=0.25):
    """在网格上挑"附近障碍最多"的位置当真值位姿；再按间距去重。"""
    best = []
    xs = np.arange(x_range[0], x_range[1] + 1e-9, grid)
    ys = np.arange(y_range[0], y_range[1] + 1e-9, grid)
    for x in xs:
        for y in ys:
            count_near = int(np.count_nonzero(
                np.hypot(points_m[:, 0] - x, points_m[:, 1] - y) <= SCAN_RADIUS_M))
            best.append((count_near, float(x), float(y)))
    best.sort(reverse=True)

    chosen = []
    for count_near, x, y in best:
        if count_near == 0:
            break
        if any(math.hypot(x - cx, y - cy) < min_sep_m for _, cx, cy in chosen):
            continue
        chosen.append((count_near, x, y))
        if len(chosen) == count:
            break
    return chosen


def _synthetic_scan(points_m, pose):
    """真值位姿附近的障碍点 → base 系端点（真值位姿的逆变换）。"""
    tx, ty, tyaw = pose
    dx = points_m[:, 0] - tx
    dy = points_m[:, 1] - ty
    near = np.hypot(dx, dy) <= SCAN_RADIUS_M
    dx, dy = dx[near], dy[near]
    c, s = math.cos(tyaw), math.sin(tyaw)
    bx = c * dx + s * dy
    by = -s * dx + c * dy
    return np.stack([bx, by], axis=1)


@unittest.skipUnless(_HAVE_PARSER, "需要仓库根的 LLM.maps.mapserver（%s）" % _PARSER_ERR)
class RealMapLocalisationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not MAP_PGM.is_file():  # pragma: no cover
            raise unittest.SkipTest("缺少 %s" % MAP_PGM)
        cls.occ, cls.origin, cls.height, cls.width = _load_occupancy()
        cls.cost, cls.meta = cost_field_from_occupancy(
            cls.occ, max_dist_px=MAX_DIST_PX, pad_px=PAD_PX)
        cls.points_m = _occupied_in_map_meters(cls.occ, cls.origin, cls.height)

    # ------------------------------------------------------------ 前置事实
    def test_map_and_cost_field_are_sane(self):
        self.assertEqual((self.height, self.width), self.occ.shape)
        self.assertGreater(int(np.count_nonzero(self.occ == 100)), 50)
        self.assertGreater(float(self.cost.max()), 0.99)

    def test_picked_targets_expose_the_lidar_geometry(self):
        targets = _pick_targets(self.points_m)
        self.assertTrue(targets, "地图里没找到障碍密集的位置，用例没意义")
        self.assertGreaterEqual(targets[0][0], 40,
                                "最好的目标位姿附近障碍太少，不足以验证匹配")

    # ------------------------------------------------------------ 端到端
    def test_climbs_back_to_the_truth_from_a_perturbed_start(self):
        targets = _pick_targets(self.points_m)
        dx, dy, dyaw = PERTURB
        for count_near, tx, ty in targets:
            with self.subTest(target=(tx, ty), nearby=count_near):
                truth = (tx, ty, 0.0)
                scan = _synthetic_scan(self.points_m, truth)
                self.assertGreater(scan.shape[0], 20)

                start = (tx + dx, ty + dy, dyaw)
                pose, info = hill_climb(
                    scan, start, self.cost, resolution=RESOLUTION,
                    origin_xy=self.origin, map_height=self.height,
                    row0=self.meta["row0"], col0=self.meta["col0"],
                    max_iters=200)

                self.assertLess(math.hypot(pose[0] - tx, pose[1] - ty), XY_TOL_M,
                                "xy 没回到真值：%s → %s" % (start, pose))
                self.assertLess(abs(math.remainder(pose[2] - 0.0, 2 * math.pi)),
                                YAW_TOL_RAD,
                                "yaw 没回到真值：%s" % (pose,))

                # 得分必须比起点高（爬山是单调的）
                before = score_pose(scan, start, self.cost, resolution=RESOLUTION,
                                    origin_xy=self.origin, map_height=self.height,
                                    row0=self.meta["row0"], col0=self.meta["col0"])
                self.assertGreater(info["score"], before)

    def test_a_severely_wrong_start_does_not_claim_success_cheaply(self):
        # 从很远的地方起爬，极可能落在别的局部极大上；这里只要求"不越界、不发散"，
        # 用来提醒：局部爬山没有全局重定位能力（这正是它不能替代 AMCL 的原因）。
        targets = _pick_targets(self.points_m)
        _, tx, ty = targets[0]
        scan = _synthetic_scan(self.points_m, (tx, ty, 0.0))
        far = (tx + 1.5, ty + 1.0, math.radians(20.0))
        pose, info = hill_climb(scan, far, self.cost, resolution=RESOLUTION,
                                origin_xy=self.origin, map_height=self.height,
                                row0=self.meta["row0"], col0=self.meta["col0"],
                                max_iters=200)
        self.assertTrue(np.isfinite(pose).all())
        self.assertGreaterEqual(info["score"], 0.0)


if __name__ == "__main__":
    unittest.main()
