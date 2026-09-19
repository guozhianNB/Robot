# -*- coding: utf-8 -*-
"""lidar_loc 的纯逻辑层（numpy，零 ROS 依赖，便于离线单测）。

对标上游 ROS1 `jie_ware/src/lidar_loc.cpp`，语义与三处有意改动见
`ros2_car/tests/test_lidar_loc_core.py` 的模块 docstring。

坐标系约定（全模块统一，别再引入第二套）：

* 位姿 ``pose = (x, y, yaw)``：map 系，米 / 弧度，yaw 逆时针为正；
* 像素 ``(px, py)``：**图像坐标，py 是行号、向下为正**，与
  `LLM/maps/mapserver.meters_to_pixel` 同一约定（那边注释写着"y 轴翻转是最高频错误"）；
* 激光点先转到 ``base_link`` 系（ROS 约定 ``y = +r*sin(a)``），再由位姿转到 map 系；
* 代价场按**裁剪后**地图的行列号索引，故投影时要减掉裁剪偏移。
"""
import math

import numpy as np

__all__ = [
    "candidate_poses",
    "compose",
    "correction_within_limits",
    "cost_field_from_occupancy",
    "crop_bounds",
    "hill_climb",
    "invert",
    "occupancy_grid_to_image",
    "project_to_pixels",
    "sample_cost_bilinear",
    "scan_endpoints_base",
    "score_pose",
    "yaw_from_quaternion",
]

# nav_msgs/OccupancyGrid 的"占据"取值
OCCUPIED = 100
# 上游的 5 个平移候选（单位：map 轴上的步长）
NEIGHBOUR_OFFSETS = ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))
# 上游的 3 个偏航候选（乘以 step_yaw_rad）
YAW_OFFSETS = (0.0, 1.0, -1.0)


def _wrap_angle(angle):
    """把角度归一化到 [-pi, pi]。"""
    return math.remainder(angle, 2.0 * math.pi)


def yaw_from_quaternion(x, y, z, w):
    """四元数 → 偏航角（弧度）。

    平面车只看 yaw，但**别用** ``2*atan2(z, w)`` 那种简化式：URDF/TF 里的四元数
    哪怕只有很小的 roll/pitch（例如雷达装歪一点），简化式也会把误差直接算进 yaw。
    """
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


# ---------------------------------------------------------------------------
# 地图：裁剪 + 代价场
# ---------------------------------------------------------------------------
def occupancy_grid_to_image(data, width, height):
    """``nav_msgs/OccupancyGrid.data`` → 图像序二维数组（行 0 = 图的最上方）。

    ⚠️ **必须转**：OccupancyGrid 的行 0 是最**下**方（origin 所在角，y 最小、向上递增），
    而像素约定（以及 `LLM/maps/mapserver.meters_to_pixel`）的行 0 是最**上**方。
    不翻转的话代价场上下颠倒 —— 现象是"定位结果在 y 方向镜像"，极难查。
    """
    return np.asarray(data, dtype=np.int8).reshape(height, width)[::-1]


def crop_bounds(occupancy, occupied_value=OCCUPIED, pad_px=50):
    """占用栅格的包围盒（外扩 pad_px 并裁到图内）。

    返回 ``(row0, col0, height, width)``；地图里没有任何占据栅格时返回 ``None``。
    未知(-1)与空闲(0)都不算障碍。

    为什么要裁：全图代价场里绝大部分是空的，裁剪后像素数能小一个量级，
    而上游同样裁剪（`crop_map()`）。
    """
    mask = np.asarray(occupancy) == occupied_value
    if not mask.any():
        return None
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    row0 = max(int(rows[0]) - pad_px, 0)
    row1 = min(int(rows[-1]) + pad_px, mask.shape[0] - 1)
    col0 = max(int(cols[0]) - pad_px, 0)
    col1 = min(int(cols[-1]) + pad_px, mask.shape[1] - 1)
    return row0, col0, row1 - row0 + 1, col1 - col0 + 1


def _truncated_distance_px(occupied_mask, max_dist_px):
    """到最近障碍像素的欧氏距离（像素），超过 max_dist_px 记 ``inf``。

    可分离精确算法：先逐行算出"本行内最近的障碍列"的一维距离平方（用
    ``maximum.accumulate`` / ``minimum.accumulate`` 向量化），再对竖直方向
    ``|dy| <= max_dist_px`` 取最小值。因为只关心截断半径内的距离，竖直窗口
    截断即可保证精确。
    """
    mask = np.asarray(occupied_mask, dtype=bool)
    height, width = mask.shape
    xs = np.arange(width, dtype=np.float64)[None, :]

    left = np.maximum.accumulate(np.where(mask, xs, -np.inf), axis=1)
    right = np.minimum.accumulate(np.where(mask, xs, np.inf)[:, ::-1], axis=1)[:, ::-1]
    horiz = np.minimum(xs - left, right - xs)
    with np.errstate(invalid="ignore"):
        horiz_sq = np.where(np.isfinite(horiz), horiz * horiz, np.inf)

    best = np.full((height, width), np.inf)
    for dy in range(-max_dist_px, max_dist_px + 1):
        if dy == 0:
            shifted = horiz_sq
        else:
            shifted = np.full_like(horiz_sq, np.inf)
            if dy > 0:
                # 目标行 y 取源行 y+dy
                shifted[:height - dy, :] = horiz_sq[dy:, :]
            else:
                shifted[-dy:, :] = horiz_sq[:height + dy, :]
        np.minimum(best, shifted + float(dy * dy), out=best)

    return np.sqrt(best)


def cost_field_from_occupancy(occupancy, occupied_value=OCCUPIED,
                              max_dist_px=50, pad_px=50):
    """由 OccupancyGrid 数据建"障碍代价场"。

    返回 ``(cost, meta)``：``cost`` 是裁剪后地图的 float64 数组（0..1，越高越靠近障碍），
    ``meta`` 含 ``row0/col0/height/width``。

    代价函数与上游 ``processMap()`` 等价：``cost = max(0, 1 - 欧氏距离/max_dist_px)``。
    上游给每个障碍像素铺一个 101×101 线性衰减核再逐像素取 max（O(障碍数 × 101²)），
    这里改成一次截断距离变换（O(像素数)）—— 一块 400×600 的裁剪图约 100 ms，且只在
    换图时算一次。
    """
    arr = np.asarray(occupancy)
    bounds = crop_bounds(arr, occupied_value, pad_px)
    if bounds is None:
        height, width = arr.shape[0], arr.shape[1]
        return (np.zeros((height, width), dtype=np.float64),
                {"row0": 0, "col0": 0, "height": height, "width": width})

    row0, col0, height, width = bounds
    sub = arr[row0:row0 + height, col0:col0 + width]
    dist_px = _truncated_distance_px(sub == occupied_value, max_dist_px)
    with np.errstate(invalid="ignore"):
        cost = np.clip(1.0 - dist_px / float(max_dist_px), 0.0, 1.0)
    cost[~np.isfinite(dist_px)] = 0.0
    return cost, {"row0": row0, "col0": col0, "height": height, "width": width}


# ---------------------------------------------------------------------------
# 扫描端点与投影
# ---------------------------------------------------------------------------
def scan_endpoints_base(ranges, *, angle_min, angle_increment, range_min, range_max,
                        laser_xy=(0.0, 0.0), laser_yaw=0.0):
    """有效回波 → ``base_link`` 系下的 ``(N, 2)`` 端点。

    激光系用 ROS 约定 ``x = r*cos(a)``、``y = r*sin(a)``；外参 ``laser_xy`` /
    ``laser_yaw`` 由 TF（laser_link → base_link）给出。

    **不做**上游那段"雷达倒装"启发式：本车 URDF 已有正确的 laser_link、驱动也配了
    ``inverted: true``，再翻一次就是双重翻转。
    """
    r = np.asarray(ranges, dtype=np.float64).ravel()
    angles = angle_min + np.arange(r.size, dtype=np.float64) * angle_increment
    valid = np.isfinite(r) & (r >= range_min) & (r <= range_max)
    r = r[valid]
    angles = angles[valid]
    if r.size == 0:
        return np.zeros((0, 2), dtype=np.float64)

    x = r * np.cos(angles)
    y = r * np.sin(angles)
    c, s = math.cos(laser_yaw), math.sin(laser_yaw)
    bx = laser_xy[0] + c * x - s * y
    by = laser_xy[1] + s * x + c * y
    return np.stack([bx, by], axis=1)


def project_to_pixels(points_base, pose, *, resolution, origin_xy, map_height,
                      row0=0, col0=0):
    """base 系点 + map 位姿 → **裁剪后**代价场的像素坐标 ``(N, 2)``。

    ``row0`` / ``col0`` 是裁剪偏移，会被**减掉**，所以结果可直接索引 ``cost``。
    """
    pts = np.asarray(points_base, dtype=np.float64).reshape(-1, 2)
    x, y, yaw = float(pose[0]), float(pose[1]), float(pose[2])
    c, s = math.cos(yaw), math.sin(yaw)
    map_x = x + c * pts[:, 0] - s * pts[:, 1]
    map_y = y + s * pts[:, 0] + c * pts[:, 1]
    px = (map_x - origin_xy[0]) / resolution - col0
    py = (map_height - 1) - (map_y - origin_xy[1]) / resolution - row0
    return np.stack([px, py], axis=1)


def sample_cost_bilinear(cost, pixels, out_of_bounds=0.0):
    """双线性采样代价场；越界或非有限的坐标一律记 ``out_of_bounds``。

    用双线性而非最近邻：得分面更光滑，爬山才有梯度可用（上游是最近邻，
    靠 1 像素小步 + 梯度势场硬走）。
    """
    pts = np.asarray(pixels, dtype=np.float64).reshape(-1, 2)
    height, width = cost.shape
    xs = pts[:, 0]
    ys = pts[:, 1]
    finite = np.isfinite(xs) & np.isfinite(ys)
    xs = np.where(finite, xs, -1.0)
    ys = np.where(finite, ys, -1.0)

    x0 = np.floor(xs).astype(np.int64)
    y0 = np.floor(ys).astype(np.int64)
    fx = xs - x0
    fy = ys - y0

    def _gather(ix, iy):
        ok = (ix >= 0) & (ix < width) & (iy >= 0) & (iy < height)
        safe_x = np.clip(ix, 0, width - 1)
        safe_y = np.clip(iy, 0, height - 1)
        return np.where(ok, cost[safe_y, safe_x], out_of_bounds)

    c00 = _gather(x0, y0)
    c10 = _gather(x0 + 1, y0)
    c01 = _gather(x0, y0 + 1)
    c11 = _gather(x0 + 1, y0 + 1)
    return (c00 * (1.0 - fx) * (1.0 - fy) + c10 * fx * (1.0 - fy)
            + c01 * (1.0 - fx) * fy + c11 * fx * fy)


def score_pose(points_base, pose, cost, *, resolution, origin_xy, map_height,
               row0=0, col0=0):
    """位姿得分（越高越好）= 各端点处代价之和。"""
    pixels = project_to_pixels(points_base, pose, resolution=resolution,
                               origin_xy=origin_xy, map_height=map_height,
                               row0=row0, col0=col0)
    return float(sample_cost_bilinear(cost, pixels).sum())


# ---------------------------------------------------------------------------
# 爬山
# ---------------------------------------------------------------------------
def candidate_poses(pose, *, step_xy_m, step_yaw_rad):
    """上游的 15 个候选：5 个平移（含原地）× 3 个偏航（含原角）。"""
    x, y, yaw = pose
    return [(x + dx * step_xy_m, y + dy * step_xy_m, yaw + k * step_yaw_rad)
            for dx, dy in NEIGHBOUR_OFFSETS
            for k in YAW_OFFSETS]


def hill_climb(points_base, pose0, cost, *, resolution, origin_xy, map_height,
               row0=0, col0=0, step_xy_px=1.0, step_yaw_rad=math.radians(1.0),
               max_iters=64):
    """从 ``pose0`` 出发做离散爬山，返回 ``(pose, info)``。

    ``info`` 含 ``iters``（接受的步数）/ ``converged`` / ``score``。

    终止条件是"没有任何候选能提高得分" —— **单调且有界**。上游是
    ``while(ros::ok())`` 且只在"连续 10 帧位移 < 5 像素/5°"时才 break，
    估计在局部极小之间振荡时永不退出，会把回调挂死。``max_iters`` 是最后一道保险。
    """
    pose = (float(pose0[0]), float(pose0[1]), float(pose0[2]))
    step_xy_m = step_xy_px * resolution
    score = score_pose(points_base, pose, cost, resolution=resolution,
                       origin_xy=origin_xy, map_height=map_height,
                       row0=row0, col0=col0)
    iters = 0
    converged = False
    while iters < max_iters:
        best_pose, best_score = pose, score
        for cand in candidate_poses(pose, step_xy_m=step_xy_m,
                                    step_yaw_rad=step_yaw_rad):
            cand_score = score_pose(points_base, cand, cost, resolution=resolution,
                                    origin_xy=origin_xy, map_height=map_height,
                                    row0=row0, col0=col0)
            if cand_score > best_score:
                best_score, best_pose = cand_score, cand
        if best_score <= score:
            converged = True
            break
        pose, score = best_pose, best_score
        iters += 1
    return pose, {"iters": iters, "converged": converged, "score": score}


# ---------------------------------------------------------------------------
# 二维刚体变换（用于算 map→odom）
# ---------------------------------------------------------------------------
def compose(a, b):
    """复合 ``a ∘ b``：点在 b 系里的坐标先经 b、再经 a。"""
    ax, ay, ayaw = a
    bx, by, byaw = b
    c, s = math.cos(ayaw), math.sin(ayaw)
    return (ax + c * bx - s * by,
            ay + s * bx + c * by,
            _wrap_angle(ayaw + byaw))


def invert(p):
    """二维刚体逆变换。"""
    x, y, yaw = p
    c, s = math.cos(yaw), math.sin(yaw)
    return (-(c * x + s * y), -(-s * x + c * y), _wrap_angle(-yaw))


def correction_within_limits(pose, seed, max_translation_m, max_yaw_rad):
    """本帧的解相对里程计先验（``seed``）的修正量是否在容许范围内。

    为什么需要：局部爬山对**种子给错**没有任何抵抗力 —— 种子错时它会持续朝
    某个局部极大走，每帧都"有改进"，于是越走越远（上板实测 8 帧漂 3.2m、
    yaw 转 60°）。给一帧的修正量设上限，等于要求"定位结果必须与里程计自洽"，
    超限就不认这一帧的解（保持按里程计推算的位置），并让上层看到告警。

    yaw 差值必须走角度归一化，否则种子在 ±π 附近时会因为 2π 跳变被误判成超限。
    """
    dx = float(pose[0]) - float(seed[0])
    dy = float(pose[1]) - float(seed[1])
    dyaw = abs(_wrap_angle(float(pose[2]) - float(seed[2])))
    return math.hypot(dx, dy) <= max_translation_m and dyaw <= max_yaw_rad
