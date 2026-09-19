# -*- coding: utf-8 -*-
"""激光扫描离群点剔除的纯逻辑（零 ROS 依赖，便于离线单测）。

对标上游 ROS1 `jie_ware/src/lidar_filter_node.cpp` 的算法，但把语义显式钉死：
见 `ros2_car/tests/test_scan_filter.py` 的模块 docstring。这里是"跑得快、测得动"
的纯函数层，ROS 侧只负责搬运消息（`scan_filter.py`）。

为什么不用 `ros-humble-laser-filters` 的 `ScanShadowsFilter`：
板卡实测 `/opt/ros/humble/share/` 下**没有** laser_filters，装它要 sudo + apt
（板卡网络慢，见 docs/log.md 那次 `ros-humble-navigation2` 装了一半的记录）。
本仓库的口径是"能用 stdlib/numpy 就绝不引入外部依赖"，故自研零依赖节点。
"""
import math

__all__ = ["filter_scan_ranges", "zero_intensities"]


def _is_valid_reading(reading, range_min, range_max):
    """读数本身是否有效：有限 且 落在 [range_min, range_max] 内。"""
    return math.isfinite(reading) and range_min <= reading <= range_max


def _disagrees(reading, neighbour, threshold):
    """邻居与当前读数是否"不一致"。

    非有限邻居（inf/nan）一律算不一致 —— 这不是笔误，而是**功能所需**：
    "空区里的一根孤立回波"两侧都是 inf，若把 inf 当作"没有证据"，这类噪点
    就永远剔不掉，而那正是走廊尽头噪点的典型形态（有测试锁死该行为）。
    """
    if not math.isfinite(neighbour):
        return True
    return abs(reading - neighbour) > threshold


def filter_scan_ranges(ranges, *, range_min, range_max, outlier_threshold,
                       clip_range=0.0):
    """剔除单点离群回波。

    参数
        ranges             原始 ranges（可含 nan/inf），**不被修改**
        range_min/max      来自扫描消息，用于判定"读数本身是否有意义"
        outlier_threshold  邻居差值阈值（米），严格大于才算不一致
        clip_range         >0 时把超过该值的有效读数标成无回波（0 = 不裁）

    返回
        (新 ranges, 被剔除的下标列表)

    规则
        1. 只有**自身有效**的点才是候选（无效点原样保留，也不改成 inf）；
        2. 左右**两侧**都必须不一致才剔除；
        3. 首尾两点永不作为候选（没有一侧邻居可比）；
        4. 剔除 = 置 math.inf（无回波，消费者据此做 ray-trace 清除）；
        5. clip_range 的裁剪**不计入**剔除列表（那是量程问题，不是噪点）。
    """
    out = list(ranges)
    removed = []
    n = len(out)

    if n >= 3:
        for i in range(1, n - 1):
            if not _is_valid_reading(out[i], range_min, range_max):
                continue
            if (_disagrees(out[i], out[i - 1], outlier_threshold)
                    and _disagrees(out[i], out[i + 1], outlier_threshold)):
                removed.append(i)

        for i in removed:
            out[i] = math.inf

    if clip_range > 0.0:
        for i, value in enumerate(out):
            if math.isfinite(value) and value > clip_range:
                out[i] = math.inf

    return out, removed


def zero_intensities(intensities, indices):
    """把指定下标的强度置 0（上游对被剔除点也这么做）；下标越界静默跳过。"""
    out = list(intensities)
    for i in indices:
        if 0 <= i < len(out):
            out[i] = 0.0
    return out
