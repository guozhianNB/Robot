# -*- coding: utf-8 -*-
"""里程计融合的纯逻辑：启动模式表 + 协方差展开。

本模块**不 import rclpy / launch**，因此可在 Windows 开发机（无 ROS）上直接断言，
见 tools/verify_odom_modes.py。launch 文件与 odom_relay 节点共用这里的逻辑，
避免话题名/开关在多处各写一份而漂移。
"""

# ---- 话题名唯一事实来源 ----
WHEEL_ODOM = '/odom'                # 底盘原始轮速里程计 → EKF odom0
LASER_ODOM_RAW = '/odom_laser_raw'  # rf2o 原始输出（协方差全 0、时间戳=扫描时间）
LASER_ODOM = '/odom_laser'          # odom_relay 修复后 → EKF odom1

# 6x6 协方差矩阵对角线在 36 元素行优先数组中的索引
COV_DIAG_INDICES = (0, 7, 14, 21, 28, 35)


def plan_odom_sources(odom_source='rf2o', use_ekf='false'):
    """返回里程计相关节点的启停与 TF 归属计划。

    :param odom_source: 'chassis' | 'rf2o' | 'fused'
    :param use_ekf: 'true'/'false'，仅 odom_source='chassis' 时有意义
        （'fused' 语义自带 EKF，忽略此项）
    :return: dict
        chassis / chassis_publish_tf(True=自己发 odom→base_link, None=不起该节点)
        rf2o    / rf2o_publish_tf(同上)
        relay / ekf / odom_to_tf: bool
    """
    if odom_source not in ('chassis', 'rf2o', 'fused'):
        raise ValueError("odom_source 只能是 chassis | rf2o | fused，收到: %r" % (odom_source,))
    ekf = str(use_ekf).lower() in ('true', '1', 'yes')

    if odom_source == 'fused':
        # 双源融合：轮速 + 激光，TF 交给 EKF，故两个源都不发 TF
        return {'chassis': True, 'chassis_publish_tf': False,
                'rf2o': True, 'rf2o_publish_tf': False,
                'relay': True, 'ekf': True, 'odom_to_tf': False}
    if odom_source == 'chassis':
        return {'chassis': True, 'chassis_publish_tf': not ekf,
                'rf2o': False, 'rf2o_publish_tf': None,
                'relay': False, 'ekf': ekf, 'odom_to_tf': False}
    # rf2o 兜底：它自己不发 TF（含雷达时间戳问题），由 odom_to_tf 转发
    return {'chassis': False, 'chassis_publish_tf': None,
            'rf2o': True, 'rf2o_publish_tf': False,
            'relay': False, 'ekf': False, 'odom_to_tf': True}


def diag6_to_covariance36(diag):
    """把 6 维协方差对角线展开成 ROS 行优先的 36 元素数组（非对角为 0）。

    ROS 协方差顺序为 x, y, z, roll, pitch, yaw。
    """
    diag = [float(v) for v in diag]
    if len(diag) != 6:
        raise ValueError("需要 6 个对角线元素，收到 %d 个" % len(diag))
    cov = [0.0] * 36
    for i, idx in enumerate(COV_DIAG_INDICES):
        cov[idx] = diag[i]
    return cov
