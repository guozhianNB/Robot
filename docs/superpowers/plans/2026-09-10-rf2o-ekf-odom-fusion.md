# rf2o + 轮速计 EKF 双源里程计融合 实现计划（Stage 1）

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 `odom→base_link` TF 改由 robot_localization EKF 融合「轮速计速度 + rf2o 激光里程计位姿」产生，使 AMCL 的运动预测不再被麦轮打滑带偏。

**架构：** 新增 `robot_bringup/odom_fusion.py`（纯逻辑：启动模式表 + 协方差展开，不 import ROS，Windows 上可断言）→ 新增 `odom_relay` 节点（给 rf2o 的零协方差 odom 补协方差、重打时间戳）→ `ekf_params.yaml` 改双源 → `odom.launch.py` 新增 `odom_source:=fused` 模式（默认仍是 `chassis`，可一条命令回退）。

**技术栈：** ROS2 Humble、robot_localization（EKF）、rf2o_laser_odometry、Python 3.10、launch OpaqueFunction。

**规格：** `docs/superpowers/specs/2026-09-10-rf2o-ekf-odom-fusion-design.md`

**开发位置：** Windows 端 `D:\_project\Robot\ros2_car`（改完只做本地静态校验；上板实测需板卡在线，当前 ssh 不通）。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/robot_bringup/robot_bringup/odom_fusion.py` | 纯逻辑：三种/四种启动模式表、协方差对角展开、话题名常量 | 创建 |
| `src/robot_bringup/robot_bringup/odom_relay.py` | ROS 节点：odom 话题中继，补协方差 + 可选重打时间戳，不发 TF | 创建 |
| `src/robot_bringup/config/ekf_params.yaml` | EKF 双源配置（轮速=速度，rf2o=位姿） | 修改 |
| `src/robot_bringup/launch/odom.launch.py` | 四种模式装配（改为 OpaqueFunction + 模式表） | 修改 |
| `src/robot_bringup/launch/robot_base.launch.py` | 透传 `use_ekf`（现在写死 false） | 修改 |
| `src/robot_bringup/launch/bringup.launch.py` | 仅更新参数说明文案（加 `fused`） | 修改 |
| `src/robot_bringup/setup.py` | 注册 `odom_relay` 入口 | 修改 |
| `tools/verify_odom_modes.py` | 本地静态校验（模式表 / 协方差 / yaml 接线），取代 `verify_ekf_expr.py` | 创建 |
| `tools/verify_ekf_expr.py` | 已因 launch 改写失效，被上者取代 | 删除 |
| `ros2_car/README.md` | 补 `fused` 模式启动与验收步骤 | 修改 |

---

## 任务 1：纯逻辑模块 `odom_fusion.py` + 本地校验脚本

**文件：**
- 创建：`ros2_car/src/robot_bringup/robot_bringup/odom_fusion.py`
- 创建：`ros2_car/tools/verify_odom_modes.py`

- [ ] **步骤 1：先写校验脚本（此时必然失败）**

创建 `ros2_car/tools/verify_odom_modes.py`：

```python
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
```

- [ ] **步骤 2：运行校验，确认失败**

运行：`cd ros2_car && python tools/verify_odom_modes.py`
预期：FAIL，`ModuleNotFoundError: No module named 'robot_bringup'`（目录里只有空的 `__init__.py`）

- [ ] **步骤 3：实现 `odom_fusion.py`**

创建 `ros2_car/src/robot_bringup/robot_bringup/odom_fusion.py`：

```python
# -*- coding: utf-8 -*-
"""里程计融合的纯逻辑：启动模式表 + 协方差展开。

本模块**不 import rclpy / launch**，因此可在 Windows 开发机（无 ROS）上直接断言，
见 tools/verify_odom_modes.py。launch 文件与 odom_relay 节点共用这里的逻辑，
避免话题名/开关在多处各写一份而漂移。
"""

# ---- 话题名唯一事实来源 ----
WHEEL_ODOM = '/odom'              # 底盘原始轮速里程计 → EKF odom0
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
```

- [ ] **步骤 4：运行校验，确认通过**

运行：`cd ros2_car && python tools/verify_odom_modes.py`
预期：PASS，末行输出 `全部通过`

- [ ] **步骤 5：Commit**

```bash
git add ros2_car/src/robot_bringup/robot_bringup/odom_fusion.py ros2_car/tools/verify_odom_modes.py
git commit -m "feat(ros2): 里程计融合纯逻辑模块 + 本地校验脚本"
```

---

## 任务 2：`odom_relay` 中继节点

**文件：**
- 创建：`ros2_car/src/robot_bringup/robot_bringup/odom_relay.py`
- 修改：`ros2_car/src/robot_bringup/setup.py:27-31`（entry_points）
- 修改：`ros2_car/tools/verify_odom_modes.py`（加 relay 一致性断言）

- [ ] **步骤 1：扩展校验脚本（此时必然失败）**

在 `verify_odom_modes.py` 里 import 之后加常量：

```python
RELAY_PY = os.path.join(PKG_ROOT, 'robot_bringup', 'odom_relay.py')
```

新增函数并在 `main()` 中调用（放在 `check_covariance()` 之后）：

```python
def check_relay():
    """relay 节点必须用 odom_fusion 的常量做默认话题，且不发布任何 TF。"""
    src = open(RELAY_PY, encoding='utf-8').read()
    assert 'input_topic' in src and 'LASER_ODOM_RAW' in src
    assert 'output_topic' in src and 'LASER_ODOM' in src
    assert 'restamp' in src
    assert 'TransformBroadcaster' not in src, 'relay 不允许发布 TF'
    assert 'diag6_to_covariance36' in src, 'relay 必须复用协方差展开函数'
    print('  [ok] odom_relay 接线与无 TF 约束')
```

- [ ] **步骤 2：运行校验，确认失败**

运行：`cd ros2_car && python tools/verify_odom_modes.py`
预期：FAIL，`FileNotFoundError: .../odom_relay.py`

- [ ] **步骤 3：实现 `odom_relay.py`**

创建 `ros2_car/src/robot_bringup/robot_bringup/odom_relay.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""里程计中继：补协方差 + 可选重打时间戳，专治 rf2o 的 odom 消息。

为什么需要它（详见 docs/superpowers/specs/2026-09-10-rf2o-ekf-odom-fusion-design.md）:
- rf2o 发布的 nav_msgs/Odometry 协方差**全为 0**。robot_localization 官方文档明确：
  被融合变量的方差若为 0，滤波器只加 1e-6 epsilon → 等于 100% 信任该测量，EKF 会退化成
  "rf2o 复读机"，轮速计白接。rf2o 是编译好的二进制，只能在中继里补协方差。
- rf2o 用激光扫描的 header.stamp 打时间戳，时间基准可能与 ROS 墙钟不一致；RL 按消息
  时间戳排序与判新鲜度，故默认重打 now()（参数 restamp:=false 可关）。
- rf2o 的 twist.linear.y 恒为 0（二维扫描匹配测不出横移），必须给大方差，否则会被
  当作"横移速度=0 且绝对可信"。

本节点不发布任何 TF。

用法:
    ros2 run robot_bringup odom_relay
    ros2 run robot_bringup odom_relay --ros-args -p input_topic:=/odom_x -p restamp:=false
"""
import rclpy
from rclpy.node import Node
from nav_msgs.msg import Odometry

from .odom_fusion import LASER_ODOM, LASER_ODOM_RAW, diag6_to_covariance36

# 默认协方差对角线 [x, y, z, roll, pitch, yaw]
DEFAULT_POSE_COV = [0.05, 0.05, 1e6, 1e6, 1e6, 0.02]
DEFAULT_TWIST_COV = [0.03, 1e6, 1e6, 1e6, 1e6, 0.02]


class OdomRelay(Node):
    def __init__(self):
        super().__init__('odom_relay')
        self.input_topic = self.declare_parameter('input_topic', LASER_ODOM_RAW).value
        self.output_topic = self.declare_parameter('output_topic', LASER_ODOM).value
        self.restamp = bool(self.declare_parameter('restamp', True).value)
        self.frame_id = self.declare_parameter('frame_id', '').value
        self.child_frame_id = self.declare_parameter('child_frame_id', '').value
        pose_cov = list(self.declare_parameter('pose_covariance', DEFAULT_POSE_COV).value)
        twist_cov = list(self.declare_parameter('twist_covariance', DEFAULT_TWIST_COV).value)
        self._pose_cov = diag6_to_covariance36(pose_cov)
        self._twist_cov = diag6_to_covariance36(twist_cov)

        self._pub = self.create_publisher(Odometry, self.output_topic, 10)
        self._sub = self.create_subscription(
            Odometry, self.input_topic, self._on_odom, 10)
        self.get_logger().info(
            'odom_relay: %s -> %s (restamp=%s) pose_cov=%s twist_cov=%s'
            % (self.input_topic, self.output_topic, self.restamp,
               ['%.3g' % v for v in pose_cov], ['%.3g' % v for v in twist_cov]))

    def _on_odom(self, msg):
        out = Odometry()
        out.header.stamp = (self.get_clock().now().to_msg()
                            if self.restamp else msg.header.stamp)
        out.header.frame_id = self.frame_id or msg.header.frame_id
        out.child_frame_id = self.child_frame_id or msg.child_frame_id
        out.pose.pose = msg.pose.pose
        out.pose.covariance = self._pose_cov
        out.twist.twist = msg.twist.twist
        out.twist.covariance = self._twist_cov
        self._pub.publish(out)


def main(args=None):
    rclpy.init(args=args)
    node = OdomRelay()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
```

- [ ] **步骤 4：注册可执行入口**

修改 `ros2_car/src/robot_bringup/setup.py` 的 `entry_points`：

```python
    entry_points={
        'console_scripts': [
            'odom_to_tf = robot_bringup.odom_to_tf:main',
            'odom_relay = robot_bringup.odom_relay:main',
        ],
    },
```

- [ ] **步骤 5：运行校验 + 语法检查，确认通过**

运行：
```bash
cd ros2_car
python tools/verify_odom_modes.py
python -m py_compile src/robot_bringup/robot_bringup/odom_relay.py src/robot_bringup/robot_bringup/odom_fusion.py
```
预期：`全部通过`，py_compile 无输出（退出码 0）

- [ ] **步骤 6：Commit**

```bash
git add ros2_car/src/robot_bringup/robot_bringup/odom_relay.py ros2_car/src/robot_bringup/setup.py ros2_car/tools/verify_odom_modes.py
git commit -m "feat(ros2): 新增 odom_relay 中继节点，给 rf2o 里程计补协方差/重打时间戳"
```

---

## 任务 3：`ekf_params.yaml` 改双源

**文件：**
- 修改：`ros2_car/src/robot_bringup/config/ekf_params.yaml`（整文件替换）
- 修改：`ros2_car/tools/verify_odom_modes.py`（加 yaml 接线断言）

- [ ] **步骤 1：扩展校验脚本（此时必然失败）**

`verify_odom_modes.py` 顶部加 import 与路径：

```python
import re

EKF_YAML = os.path.join(PKG_ROOT, 'config', 'ekf_params.yaml')
```

新增函数并在 `main()` 中调用（放在 `check_modes()` 之后）：

```python
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
    assert len(pnc) == 225, 'process_noise_covariance 必须 225 个元素（15x15 行优先），实际 %d' % len(pnc)

    assert re.search(r'odom1:\s*' + re.escape(LASER_ODOM) + r'\s*$', text, re.M), \
        'odom1 必须指向 relay 输出 %s' % LASER_ODOM
    assert re.search(r'odom0:\s*' + re.escape(WHEEL_ODOM) + r'\s*$', text, re.M)
    assert re.search(r'world_frame:\s*odom', text), 'world_frame 必须是 odom（TF 归 EKF 时 map→odom 归 AMCL）'
    assert re.search(r'publish_tf:\s*true', text)
    assert re.search(r'odom1_differential:\s*false', text), 'rf2o 位姿是绝对位姿，不能开差分'
    print('  [ok] ekf_params.yaml 双源接线')
```

- [ ] **步骤 2：运行校验，确认失败**

运行：`cd ros2_car && python tools/verify_odom_modes.py`
预期：FAIL，`AssertionError: odom1 必须指向 relay 输出 /odom_laser`（当前 yaml 里 `odom1` 被注释掉）

- [ ] **步骤 3：重写 `ekf_params.yaml`**

整文件替换为：

```yaml
# robot_localization EKF 参数：双源融合（Stage 1）
#   输入1 底盘编码器 /odom      → 只取速度（vx, vy, vyaw）：横移速度只有轮速能给，位置会漂不取
#   输入2 rf2o 激光里程计 /odom_laser → 取位姿（x, y, yaw）：扫描匹配结果，打滑时唯一可信的位置锚点
# rf2o 的原始消息协方差全为 0（robot_localization 会退化成"完全信任"），故经 odom_relay
# 补协方差 + 重打时间戳后再进 EKF。详见 docs/superpowers/specs/2026-09-10-rf2o-ekf-odom-fusion-design.md
# 用法：ros2 launch robot_bringup odom.launch.py odom_source:=fused
#       （此时 chassis_driver 与 rf2o 均 publish_tf=false，odom→base_link 由本 EKF 发布）

ekf_filter_node:
  ros__parameters:
    use_sim_time: false
    frequency: 20.0
    two_d_mode: true
    sensor_timeout: 0.5      # rf2o 断供 0.5s 后自动只用轮速（10Hz 下=连丢 5 帧）
    reset_on_time_jump: true
    print_diagnostics: true
    publish_tf: true
    map_frame: map
    odom_frame: odom
    base_link_frame: base_link
    world_frame: odom

    # 输入1：底盘编码器里程计 —— 只融速度
    # 顺序: x, y, z, roll, pitch, yaw, vx, vy, vz, vroll, vpitch, vyaw, ax, ay, az
    odom0: /odom
    odom0_config: [false, false, false,
                   false, false, false,
                   true,  true,  false,
                   false, false, true,
                   false, false, false]
    odom0_differential: false
    odom0_queue_size: 5

    # 输入2：rf2o 激光里程计（经 odom_relay 修过协方差+时间戳）—— 只融位姿
    odom1: /odom_laser
    odom1_config: [true,  true,  false,
                   false, false, true,
                   false, false, false,
                   false, false, false,
                   false, false, false]
    odom1_differential: false
    odom1_queue_size: 5

    # 过程噪声（对角线展开，225 元素行优先）
    process_noise_covariance: [0.05, 0,    0,    0,    0,    0,    0,     0,     0,    0,    0,    0,    0,    0,    0,
                               0,    0.05, 0,    0,    0,    0,    0,     0,     0,    0,    0,    0,    0,    0,    0,
                               0,    0,    0.06, 0,    0,    0,    0,     0,     0,    0,    0,    0,    0,    0,    0,
                               0,    0,    0,    0.03, 0,    0,    0,     0,     0,    0,    0,    0,    0,    0,    0,
                               0,    0,    0,    0,    0.03, 0,    0,     0,     0,    0,    0,    0,    0,    0,    0,
                               0,    0,    0,    0,    0,    0.06, 0,     0,     0,    0,    0,    0,    0,    0,    0,
                               0,    0,    0,    0,    0,    0,    0.025, 0,     0,    0,    0,    0,    0,    0,    0,
                               0,    0,    0,    0,    0,    0,    0,     0.025, 0,    0,    0,    0,    0,    0,    0,
                               0,    0,    0,    0,    0,    0,    0,     0,     0.04, 0,    0,    0,    0,    0,    0,
                               0,    0,    0,    0,    0,    0,    0,     0,     0,    0.01, 0,    0,    0,    0,    0,
                               0,    0,    0,    0,    0,    0,    0,     0,     0,    0,    0.01, 0,    0,    0,    0,
                               0,    0,    0,    0,    0,    0,    0,     0,     0,    0,    0,    0.02, 0,    0,    0,
                               0,    0,    0,    0,    0,    0,    0,     0,     0,    0,    0,    0,    0.01, 0,    0,
                               0,    0,    0,    0,    0,    0,    0,     0,     0,    0,    0,    0,    0,    0.01, 0,
                               0,    0,    0,    0,    0,    0,    0,     0,     0,    0,    0,    0,    0,    0,    0.015]
```

- [ ] **步骤 4：运行校验，确认通过**

运行：`cd ros2_car && python tools/verify_odom_modes.py`
预期：PASS，`全部通过`

- [ ] **步骤 5：Commit**

```bash
git add ros2_car/src/robot_bringup/config/ekf_params.yaml ros2_car/tools/verify_odom_modes.py
git commit -m "feat(ros2): EKF 改双源融合（轮速速度 + rf2o 位姿）"
```

---

## 任务 4：`odom.launch.py` 新增 `fused` 模式

**文件：**
- 修改：`ros2_car/src/robot_bringup/launch/odom.launch.py`（整文件替换）
- 修改：`ros2_car/tools/verify_odom_modes.py`（加 launch 接线断言）

- [ ] **步骤 1：扩展校验脚本（此时必然失败）**

`verify_odom_modes.py` 加路径常量：

```python
ODOM_LAUNCH = os.path.join(PKG_ROOT, 'launch', 'odom.launch.py')
```

新增函数并在 `main()` 中调用（放在 `check_ekf_yaml()` 之后）：

```python
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
```

- [ ] **步骤 2：运行校验，确认失败**

运行：`cd ros2_car && python tools/verify_odom_modes.py`
预期：FAIL，`AssertionError: launch 必须复用 odom_fusion 的模式表`

- [ ] **步骤 3：重写 `odom.launch.py`**

整文件替换为：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动里程计来源。

odom_source:=chassis → STM32 底盘编码器里程计（robot_chassis/chassis_driver）
odom_source:=rf2o    → rf2o 激光里程计（无底盘时的兜底，官方示例同款）
odom_source:=fused   → 双源融合：底盘编码器速度 + rf2o 激光位姿，odom→base_link TF
                       由 robot_localization EKF 统一发布（麦轮打滑场景推荐）

节点启停与 TF 归属由 robot_bringup/odom_fusion.py::plan_odom_sources 决定，
保证 odom→base_link 每段 TF 只有一个发布者（REP-105）。
详见 docs/superpowers/specs/2026-09-10-rf2o-ekf-odom-fusion-design.md

用法:
    ros2 launch robot_bringup odom.launch.py odom_source:=chassis
    ros2 launch robot_bringup odom.launch.py odom_source:=rf2o
    ros2 launch robot_bringup odom.launch.py odom_source:=fused
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from robot_bringup.odom_fusion import (
    LASER_ODOM,
    LASER_ODOM_RAW,
    WHEEL_ODOM,
    plan_odom_sources,
)


def _build_nodes(context, *args, **kwargs):
    chassis_share = get_package_share_directory('robot_chassis')
    bringup_share = get_package_share_directory('robot_bringup')

    odom_source = LaunchConfiguration('odom_source').perform(context)
    use_ekf = LaunchConfiguration('use_ekf').perform(context)
    plan = plan_odom_sources(odom_source, use_ekf)

    nodes = []

    if plan['chassis']:
        nodes.append(Node(
            package='robot_chassis',
            executable='chassis_driver',
            name='chassis_driver',
            output='screen',
            emulate_tty=True,
            parameters=[
                os.path.join(chassis_share, 'config', 'chassis_params.yaml'),
                {'publish_tf': plan['chassis_publish_tf']},
            ]))

    if plan['rf2o']:
        # fuse 模式下 rf2o 输出到 raw 话题，由 odom_relay 补协方差后再给 EKF
        nodes.append(Node(
            package='rf2o_laser_odometry',
            executable='rf2o_laser_odometry_node',
            name='rf2o_laser_odometry',
            output='screen',
            parameters=[{
                'laser_scan_topic': '/scan',
                'odom_topic': LASER_ODOM_RAW if plan['relay'] else WHEEL_ODOM,
                # rf2o 用激光扫描时间戳发 TF，会让 slam_toolbox 查不到（Failed to
                # compute odom pose），故它自己一律不发 TF，改由 EKF 或 odom_to_tf 发
                'publish_tf': plan['rf2o_publish_tf'],
                'base_frame_id': 'base_link',
                'odom_frame_id': 'odom',
                'init_pose_from_topic': '',
                'freq': 10.0,
            }]))

    if plan['relay']:
        nodes.append(Node(
            package='robot_bringup',
            executable='odom_relay',
            name='odom_relay',
            output='screen',
            emulate_tty=True,
            parameters=[{
                'input_topic': LASER_ODOM_RAW,
                'output_topic': LASER_ODOM,
                'restamp': True,
            }]))

    if plan['ekf']:
        nodes.append(Node(
            package='robot_localization',
            executable='ekf_node',
            name='ekf_filter_node',
            output='screen',
            emulate_tty=True,
            parameters=[os.path.join(bringup_share, 'config', 'ekf_params.yaml')],
            remappings=[('odometry/filtered', '/odom_filtered')]))

    if plan['odom_to_tf']:
        # rf2o 兜底模式：用当前 ROS 时间把 /odom 转发成 odom→base_link TF
        nodes.append(Node(
            package='robot_bringup',
            executable='odom_to_tf',
            name='odom_to_tf',
            output='screen',
            parameters=[{
                'odom_frame': 'odom',
                'base_frame': 'base_link',
                'odom_topic': WHEEL_ODOM,
            }]))

    return nodes


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument(
            'odom_source', default_value='rf2o',
            description='里程计来源: chassis(STM32编码器) | rf2o(激光里程计兜底) | fused(双源融合)'),
        DeclareLaunchArgument(
            'use_ekf', default_value='false',
            description='chassis 模式下是否启用 EKF（fused 模式自带 EKF，忽略此项）'),
        OpaqueFunction(function=_build_nodes),
    ])
```

- [ ] **步骤 4：运行校验 + 语法检查，确认通过**

运行：
```bash
cd ros2_car
python tools/verify_odom_modes.py
python -m py_compile src/robot_bringup/launch/odom.launch.py
```
预期：`全部通过`，py_compile 无输出

- [ ] **步骤 5：Commit**

```bash
git add ros2_car/src/robot_bringup/launch/odom.launch.py ros2_car/tools/verify_odom_modes.py
git commit -m "feat(ros2): odom.launch.py 新增 odom_source:=fused 双源融合模式"
```

---

## 任务 5：`robot_base.launch.py` 透传 `use_ekf`、`bringup` 文案

**文件：**
- 修改：`ros2_car/src/robot_bringup/launch/robot_base.launch.py`
- 修改：`ros2_car/src/robot_bringup/launch/bringup.launch.py:9-12,41-43`

- [ ] **步骤 1：改 `robot_base.launch.py`**

`generate_launch_description()` 中 `declare` 列表与 `odom` include 改为：

```python
def generate_launch_description():
    share_dir = get_package_share_directory('robot_bringup')
    odom_source = LaunchConfiguration('odom_source')
    use_ekf = LaunchConfiguration('use_ekf')
    use_sim_time = LaunchConfiguration('use_sim_time')
    declare = [
        DeclareLaunchArgument('odom_source', default_value='rf2o',
                              description='chassis | rf2o | fused（fused=轮速+激光 EKF 双源融合）'),
        # 原来这里把 use_ekf 写死成 false 传给 odom.launch.py，导致从 robot_base
        # 起永远开不了 EKF；现在提成启动参数透传（默认 false 保持原行为）
        DeclareLaunchArgument('use_ekf', default_value='false',
                              description='chassis 模式下是否启用 EKF 融合（fused 模式自带 EKF）'),
        DeclareLaunchArgument('use_sim_time', default_value='false'),
    ]
    rsp = Node(
        package='robot_state_publisher', executable='robot_state_publisher',
        name='robot_state_publisher', output='screen', parameters=[{
            'robot_description': open(os.path.join(share_dir, 'urdf', 'car.urdf')).read(),
            'use_sim_time': use_sim_time}])
    lidar = IncludeLaunchDescription(PythonLaunchDescriptionSource(
        os.path.join(share_dir, 'launch', 'lidar.launch.py')))
    odom = IncludeLaunchDescription(PythonLaunchDescriptionSource(
        os.path.join(share_dir, 'launch', 'odom.launch.py')),
        launch_arguments={'odom_source': odom_source, 'use_ekf': use_ekf}.items())
    return LaunchDescription(declare + [rsp, lidar, odom])
```

- [ ] **步骤 2：改 `bringup.launch.py` 文案**

`odom_source` 的描述加 `fused`：

```python
        DeclareLaunchArgument('odom_source', default_value='rf2o',
                              description='里程计来源: rf2o | chassis | fused（双源融合，麦轮推荐）'),
```

docstring 里 `odom_source:=rf2o|chassis` 一行改为：

```
    odom_source:=rf2o|chassis|fused  里程计来源（fused=轮速+激光 EKF 双源融合，麦轮打滑场景用）
```

- [ ] **步骤 3：语法检查**

运行：`cd ros2_car && python -m py_compile src/robot_bringup/launch/robot_base.launch.py src/robot_bringup/launch/bringup.launch.py`
预期：无输出（退出码 0）

- [ ] **步骤 4：Commit**

```bash
git add ros2_car/src/robot_bringup/launch/robot_base.launch.py ros2_car/src/robot_bringup/launch/bringup.launch.py
git commit -m "fix(ros2): robot_base 透传 use_ekf（原来写死 false，EKF 从 robot_base 起永远开不了）"
```

---

## 任务 6：清理旧校验脚本 + 文档 + 总校验

**文件：**
- 删除：`ros2_car/tools/verify_ekf_expr.py`
- 修改：`ros2_car/README.md`

- [ ] **步骤 1：删除被取代的脚本**

```bash
git rm ros2_car/tools/verify_ekf_expr.py
```

原因：它 `eval` 的是旧的 `PythonExpression` 字符串条件，launch 改写后已失效；模式表断言由 `tools/verify_odom_modes.py` 承担。

- [ ] **步骤 2：README 补一节**

在 `ros2_car/README.md` 里描述里程计来源的段落追加：

````markdown
### 双源里程计融合（odom_source:=fused，麦轮打滑场景）

```bash
ros2 launch robot_bringup robot_base.launch.py odom_source:=fused
```

- 数据流：`chassis_driver(/odom 轮速速度)` + `rf2o(/odom_laser_raw → odom_relay → /odom_laser 激光位姿)`
  → `ekf_filter_node` → `/odom_filtered` + **odom→base_link TF**（本模式下只有 EKF 发这段 TF）
- 为什么需要 `odom_relay`：rf2o 的 odom 消息协方差全为 0，robot_localization 会当成
  "绝对可信"从而退化成 rf2o 复读机，故由 relay 补协方差并重打时间戳
- 校验：
  ```bash
  ros2 topic hz /odom /odom_laser_raw /odom_laser /odom_filtered   # 约 10/10/10/20 Hz
  ros2 run tf2_tools view_frames.py                                # odom→base_link 只有 ekf_filter_node
  ```
- 打滑验收：车架空、四轮离地后发 `cmd_vel` 让轮子空转 → `/odom` 位置一路飞走、
  `/odom_laser` 基本不动、`/odom_filtered` 明显比 `/odom` 稳。若 filtered 仍漂太多，
  调小 `odom_relay` 的 `pose_covariance` 前两项（x, y）
- 回退：不加 `odom_source:=fused` 即回到原来的单源模式
````

- [ ] **步骤 3：总校验**

运行：
```bash
cd ros2_car
python tools/verify_odom_modes.py
python -m py_compile src/robot_bringup/robot_bringup/odom_fusion.py src/robot_bringup/robot_bringup/odom_relay.py src/robot_bringup/launch/odom.launch.py src/robot_bringup/launch/robot_base.launch.py src/robot_bringup/launch/bringup.launch.py src/robot_bringup/setup.py
```
预期：`全部通过` + 无 py_compile 报错

- [ ] **步骤 4：Commit**

```bash
git add -A ros2_car
git commit -m "docs(ros2): 补 fused 模式说明；删除失效的 verify_ekf_expr.py"
```

---

## 上板实测（板卡开机后，不属于代码任务）

- [ ] 同步到板卡并（因新增了 python 入口）重新 build：
```bash
# Windows
scp -r ros2_car/src sunrise@100.65.82.93:/home/sunrise/Robot/ros2_car/
# 板卡
cd /home/sunrise/Robot/ros2_car && colcon build --symlink-install --packages-select robot_bringup
```
- [ ] `ros2 launch robot_bringup robot_base.launch.py odom_source:=fused`
- [ ] 四路话题频率 + `view_frames.py` 单一 TF 发布者 + `/odom_filtered` 协方差非 0
- [ ] 架空空转打滑实验，记录 10 秒三者位移
- [ ] 地面实测：AMCL 定位稳定性 → 一次完整导航到点 → 记录结论回写记忆/文档
