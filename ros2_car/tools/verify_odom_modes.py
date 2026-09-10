# -*- coding: utf-8 -*-
"""本地静态校验：里程计融合的启动模式表 + 协方差展开（不需要 ROS）。

跑法（在 ros2_car 目录下）:
    python tools/verify_odom_modes.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.abspath(os.path.join(HERE, '..', 'src', 'robot_bringup'))
sys.path.insert(0, PKG_ROOT)

from robot_bringup.odom_fusion import (  # noqa: E402
    COV_DIAG_INDICES,
    WHEEL_ODOM,
    LASER_ODOM_RAW,
    LASER_ODOM,
    diag6_to_covariance36,
    plan_odom_sources,
)


def check_zero():
    """话题名常量必须是设计文档里定死的三个。"""
    assert WHEEL_ODOM == '/odom'
    assert LASER_ODOM_RAW == '/odom_laser_raw'
    assert LASER_ODOM == '/odom_laser'
    print('  [ok] 话题名常量')


def check_modes():
    """四种模式逐行核对（设计文档「三、启动模式表」）。"""
    assert plan_odom_sources('chassis') == {
        'chassis': True, 'chassis_publish_tf': True,
        'rf2o': False, 'rf2o_publish_tf': None,
        'relay': False, 'ekf': False, 'odom_to_tf': False}

    assert plan_odom_sources('chassis', 'true') == {
        'chassis': True, 'chassis_publish_tf': False,
        'rf2o': False, 'rf2o_publish_tf': None,
        'relay': False, 'ekf': True, 'odom_to_tf': False}

    assert plan_odom_sources('rf2o') == {
        'chassis': False, 'chassis_publish_tf': None,
        'rf2o': True, 'rf2o_publish_tf': False,
        'relay': False, 'ekf': False, 'odom_to_tf': True}

    assert plan_odom_sources('fused') == {
        'chassis': True, 'chassis_publish_tf': False,
        'rf2o': True, 'rf2o_publish_tf': False,
        'relay': True, 'ekf': True, 'odom_to_tf': False}

    # 每种组合下 odom→base_link 的发布者必须恰好一个（REP-105）
    for src, ekf in (('chassis', 'false'), ('chassis', 'true'),
                     ('rf2o', 'false'), ('fused', 'false')):
        p = plan_odom_sources(src, ekf)
        publishers = []
        if p['chassis'] and p['chassis_publish_tf']:
            publishers.append('chassis_driver')
        if p['rf2o'] and p['rf2o_publish_tf']:
            publishers.append('rf2o')
        if p['ekf']:
            publishers.append('ekf_filter_node')
        if p['odom_to_tf']:
            publishers.append('odom_to_tf')
        assert len(publishers) == 1, (src, ekf, publishers)

    # 非法 odom_source 必须报错，不能静默按 rf2o 起
    try:
        plan_odom_sources('bogus')
    except ValueError:
        pass
    else:
        raise AssertionError('非法 odom_source 未报错')
    print('  [ok] 四种模式 + 单一 TF 发布者 + 非法值报错')


def check_covariance():
    """6 维对角展开成 36 元素行优先数组，对角线索引固定。"""
    assert COV_DIAG_INDICES == (0, 7, 14, 21, 28, 35)
    cov = diag6_to_covariance36([1, 2, 3, 4, 5, 6])
    assert len(cov) == 36
    for i, idx in enumerate(COV_DIAG_INDICES):
        assert cov[idx] == float(i + 1), (idx, cov[idx])
    assert all(v == 0.0 for i, v in enumerate(cov) if i not in COV_DIAG_INDICES)
    try:
        diag6_to_covariance36([1, 2, 3])
    except ValueError:
        pass
    else:
        raise AssertionError('长度不为 6 未报错')
    print('  [ok] 协方差对角展开')


def main():
    check_zero()
    check_modes()
    check_covariance()
    print('全部通过')


if __name__ == '__main__':
    main()
