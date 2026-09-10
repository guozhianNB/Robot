# -*- coding: utf-8 -*-
"""本地静态校验：里程计融合的启动模式表 + 协方差展开（不需要 ROS）。

跑法（在 ros2_car 目录下）:
    python tools/verify_odom_modes.py
"""
import os
import re
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

RELAY_PY = os.path.join(PKG_ROOT, 'robot_bringup', 'odom_relay.py')
EKF_YAML = os.path.join(PKG_ROOT, 'config', 'ekf_params.yaml')
ODOM_LAUNCH = os.path.join(PKG_ROOT, 'launch', 'odom.launch.py')

CHASSIS_ROOT = os.path.abspath(os.path.join(HERE, '..', 'src', 'robot_chassis'))
sys.path.insert(0, CHASSIS_ROOT)
from robot_chassis.usb_protocol import (  # noqa: E402
    CMD_IMU,
    IMU_PAYLOAD_LEN,
    decode_imu,
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


def check_relay():
    """relay 节点必须用 odom_fusion 的常量做默认话题，且不发布任何 TF。"""
    src = open(RELAY_PY, encoding='utf-8').read()
    assert 'input_topic' in src and 'LASER_ODOM_RAW' in src
    assert 'output_topic' in src and 'LASER_ODOM' in src
    assert 'restamp' in src
    assert 'TransformBroadcaster' not in src, 'relay 不允许发布 TF'
    assert 'diag6_to_covariance36' in src, 'relay 必须复用协方差展开函数'
    print('  [ok] odom_relay 接线与无 TF 约束')


def _array(text, key):
    """取 'key: [ ... ]' 里的元素（本文件数组内不写注释，故可粗解析）。"""
    m = re.search(key + r':\s*\[(.*?)\]', text, re.S)
    assert m, 'ekf_params.yaml 缺少 %s' % key
    return [t.strip() for t in m.group(1).replace('\n', ' ').split(',') if t.strip()]


def check_ekf_yaml():
    """EKF 双源接线：数组长度、融合位、话题名。"""
    text = open(EKF_YAML, encoding='utf-8').read()

    for key in ('odom0_config', 'odom1_config'):
        arr = _array(text, key)
        assert len(arr) == 15, '%s 必须恰好 15 个布尔，实际 %d 个' % (key, len(arr))
        assert all(v in ('true', 'false') for v in arr), (key, arr)

    # 顺序: x, y, z, roll, pitch, yaw, vx, vy, vz, vroll, vpitch, vyaw, ax, ay, az
    odom0 = _array(text, 'odom0_config')
    assert odom0[6] == 'true' and odom0[7] == 'true', 'odom0 要融 vx, vy（横移只有轮速能给）'
    assert odom0[11] == 'true', 'odom0 要融 vyaw'
    assert odom0[0:2] == ['false', 'false'], 'odom0 不融位置（轮速位置会漂）'

    odom1 = _array(text, 'odom1_config')
    assert odom1[0] == 'true' and odom1[1] == 'true', 'odom1 要融 x, y'
    assert odom1[5] == 'true', 'odom1 要融 yaw'
    assert odom1[6:12] == ['false'] * 6, 'odom1 是位姿源，不再融它的速度（避免同一源重复计入）'

    pnc = _array(text, 'process_noise_covariance')
    assert len(pnc) in (15, 225), \
        'process_noise_covariance 必须 15（对角线简写）或 225 个元素，实际 %d' % len(pnc)

    imu0 = _array(text, 'imu0_config')
    assert len(imu0) == 15, 'imu0_config 必须 15 个布尔，实际 %d' % len(imu0)
    assert imu0[11] == 'true', 'imu0 只融 vyaw（index 11）'
    assert imu0[:11] == ['false'] * 11 and imu0[12:] == ['false'] * 3, \
        'imu0 只融 vyaw，其它位必须 false（避免与 rf2o 的 yaw 两个朝向源打架）'
    assert re.search(r'imu0:\s*/imu\s*$', text, re.M)
    assert re.search(r'imu0_differential:\s*false', text)

    assert re.search(r'odom1:\s*' + re.escape(LASER_ODOM) + r'\s*$', text, re.M), \
        'odom1 必须指向 relay 输出 %s' % LASER_ODOM
    assert re.search(r'odom0:\s*' + re.escape(WHEEL_ODOM) + r'\s*$', text, re.M)
    assert re.search(r'world_frame:\s*odom', text), 'world_frame 必须是 odom（TF 归 EKF 时 map→odom 归 AMCL）'
    assert re.search(r'publish_tf:\s*true', text)
    assert re.search(r'odom1_differential:\s*false', text), 'rf2o 位姿是绝对位姿，不能开差分'

    # 有 PyYAML 时再做一次严格解析（板卡上一定有；Windows 可用 .venv\Scripts\python.exe 跑）
    try:
        import yaml
    except ImportError:
        print('  [warn] 本机无 PyYAML，跳过严格解析（板卡上一定有）')
    else:
        root = yaml.safe_load(text)['ekf_filter_node']['ros__parameters']
        assert len(root['odom0_config']) == 15 and len(root['odom1_config']) == 15
        assert len(root['imu0_config']) == 15
        assert len(root['process_noise_covariance']) in (15, 225)
        # rcl 要求 YAML 序列元素同类型：混用 int/float（如 0.05 与 0）会让 ekf_node 启动即崩
        # （实机踩过：Sequence should be of same type. Value type 'integer' do not belong）
        for key, val in root.items():
            if isinstance(val, list):
                types = {type(v).__name__ for v in val}
                assert len(types) == 1, \
                    '%s 元素类型不唯一 %s → rcl 拒绝解析，ekf_node 会崩' % (key, sorted(types))
        assert root['publish_tf'] is True and root['world_frame'] == 'odom'
        assert root['odom0'] == WHEEL_ODOM and root['odom1'] == LASER_ODOM
        print('  [ok] PyYAML %s 严格解析（含序列元素类型一致性）' % yaml.__version__)
    print('  [ok] ekf_params.yaml 双源接线')


def check_launch_wiring():
    """launch 必须走模式表，且 fused 模式下 rf2o 输出到 raw 话题、relay 接 raw→激光话题。"""
    src = open(ODOM_LAUNCH, encoding='utf-8').read()
    assert 'plan_odom_sources' in src, 'launch 必须复用 odom_fusion 的模式表'
    assert 'OpaqueFunction' in src, '改用 OpaqueFunction，不再用 PythonExpression 拼条件'
    assert 'PythonExpression' not in src, 'launch 里不应再有 PythonExpression 字符串条件'
    assert 'LASER_ODOM_RAW' in src and 'LASER_ODOM' in src
    assert 'odom_relay' in src
    assert "'/odom_filtered'" in src, 'EKF 输出重映射到 /odom_filtered'
    print('  [ok] odom.launch.py 接线')


def check_imu_protocol():
    """IMU(0x83) 帧契约：命令号、长度、编解码往返（与固件 usb_proto.c 一致）。"""
    import struct as _struct

    assert CMD_IMU == 0x83
    assert IMU_PAYLOAD_LEN == 8

    # 与固件 up_send_imu() 完全同构的组包：yaw/roll/pitch 0.01°，yaw_rate 0.1°/s
    def pack(yaw_cdeg, rate_ddps, roll_cdeg, pitch_cdeg):
        return _struct.pack('<4h', yaw_cdeg, rate_ddps, roll_cdeg, pitch_cdeg)

    yaw, rate, roll, pitch = decode_imu(pack(-12345, -250, 100, -300))
    assert abs(yaw - (-123.45)) < 1e-9, yaw
    assert abs(rate - (-25.0)) < 1e-9, rate
    assert abs(roll - 1.0) < 1e-9, roll
    assert abs(pitch - (-3.0)) < 1e-9, pitch

    try:
        decode_imu(b'\x00' * 7)
    except ValueError:
        pass
    else:
        raise AssertionError('长度不为 8 未报错')
    print('  [ok] IMU(0x83) 帧契约与编解码往返')


def main():
    check_zero()
    check_modes()
    check_covariance()
    check_ekf_yaml()
    check_relay()
    check_launch_wiring()
    check_imu_protocol()
    print('全部通过')


if __name__ == '__main__':
    main()
