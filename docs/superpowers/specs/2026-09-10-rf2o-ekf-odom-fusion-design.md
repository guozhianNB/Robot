# rf2o + 轮速计 EKF 双源里程计融合设计文档（Stage 1）

日期：2026-09-10
状态：已批准（用户确认「批准 Stage 1」：只做 rf2o + 轮速 EKF 双源融合，可一条命令回退）
范围：`ros2_car/src/robot_bringup/`（launch / config / 新节点），不动 `robot_chassis`、不动 `stm32`
前置研究：见本文「一、研究结论」三节，均有源码/官方文档依据

## 目标

麦轮打滑时定位被拖偏的根因是 **odom 是唯一的运动预测源**。本设计让 `odom→base_link`
这段 TF 改由 robot_localization EKF 融合 **轮速计速度 + rf2o 激光里程计位姿** 产生，
AMCL 的运动预测取自这段 TF，因而自动获得"对打滑免疫"的激光信息。

不改任何现有可用链路：新增 `odom_source:=fused` 模式，默认仍是 `chassis`，
出问题一条命令退回原状。

## 一、研究结论（踩坑前置）

1. **rf2o 与 EKF 之间没有 TF 循环依赖**。[rf2o_laser_odometry/CLaserOdometry2DNode.cpp](https://github.com/MAPIRlab/rf2o_laser_odometry) 中
   `setLaserPoseFromTf()` 只在**第一帧**查一次 `base_link ↔ scan.frame_id`，之后自己内部积分位姿，
   **从不查 odom→base_link**。故"EKF 发 TF → rf2o 需要 TF → 又喂回 EKF"的死循环不存在。
2. **rf2o 的 odom 消息协方差全为 0**（`publish()` 里没有填写任何协方差字段，`twist.linear.y` 恒 0）。
   [robot_localization 官方文档](https://docs.ros.org/en/noetic/api/robot_localization/html/preparing_sensor_data.html)
   明确：被融合变量的方差若为 0，滤波器只加 `1e-6` epsilon → **等于无条件完全信任该测量**，
   EKF 会退化成"rf2o 复读机"。rf2o 是编译好的 C++ 二进制，改不动，**必须插中继节点填协方差**。
3. **融合策略**（同一份官方文档）：里程计同时给位置和线速度时**融线速度**；同时给朝向和角速度时
   **融朝向**；两个朝向源时只融更准的那个。据此：
   - 轮速计：位置会漂 → **只融速度**（vx, vy, wz；横移 vy 只有它能给）
   - rf2o：**融位姿**（x, y, yaw）—— 位姿是扫描匹配结果，打滑时它是唯一可信的位置锚点
   - 设计过程中曾考虑"两路都只融速度"，已否决：持续打滑时轮速的假速度仍会把估计拖走
     （详见「五、被否决的方案」）。位姿融合作为主方案，速度融合作为可切换的备用旋钮。

## 二、架构与数据流

```
/scan ─┬─> rf2o_laser_odometry ──> /odom_laser_raw ─> [odom_relay 新增] ─> /odom_laser ─┐
       └─> AMCL(scan + TF) ──> map→odom TF                                             │
STM32 ──> chassis_driver ──> /odom ─────────────────────────────────────────────────> [ekf_filter_node]
                                                                                        │
                                      /odom_filtered  +  odom→base_link TF <────────────┘
```

收益路径：AMCL 的粒子运动预测是从 **`odom→base_link` TF** 取的（不是话题），
所以 TF 一换成融合结果，AMCL 立刻吃到激光里程计，不需要改 AMCL 一行参数。

**TF 责任表（每段 TF 恰好一个发布者，REP-105）**

| TF | 发布者 | 设置 |
|---|---|---|
| base_link→laser_link | robot_state_publisher | 不动（URDF 静态） |
| **odom→base_link** | **ekf_filter_node** | `publish_tf: true` |
| map→odom | AMCL | `tf_broadcast: true`（不动） |
| ~~odom→base_link~~ | chassis_driver / rf2o / odom_to_tf | 三种 fused 参与者全部 `publish_tf: false` |

## 三、启动模式表（`odom.launch.py`）

| `odom_source` | chassis_driver | chassis 发 TF | rf2o | odom_relay | EKF | odom_to_tf |
|---|---|---|---|---|---|---|
| `chassis`（默认，现状） | ✓ | true | ✗ | ✗ | ✗ | ✗ |
| `chassis` + `use_ekf:=true` | ✓ | false | ✗ | ✗ | ✓ | ✗ |
| `rf2o`（无底盘兜底） | ✗ | – | ✓ | ✗ | ✗ | ✓ |
| **`fused`（本次新增）** | ✓ | **false** | ✓（`publish_tf:=false`，`odom_topic:=/odom_laser_raw`） | ✓ | ✓ | ✗ |

launch 参数决定逻辑从现有的 `PythonExpression` 字符串拼接改为**纯函数 + 表**：
新增 `robot_bringup/odom_fusion.py`（**纯逻辑、不 import ROS**）提供两个函数：
`plan_odom_sources(odom_source, use_ekf)` 返回每个节点是否启动、各自的 `publish_tf` 取值；
`diag6_to_covariance36(diag)` 把 6 维协方差对角展开成 ROS 的 36 元素行优先数组。
launch 与 relay 节点都 import 它。好处：Windows 上无 ROS 也能直接断言四种组合与协方差索引
（现有 `tools/verify_ekf_expr.py` 靠 `eval` 字符串表达式，且已因 launch 改写而失效，本次由新校验脚本取代）。

## 四、参数与节点设计

### 4.1 `config/ekf_params.yaml`（改双源）

```yaml
ekf_filter_node:
  ros__parameters:
    frequency: 20.0
    two_d_mode: true
    sensor_timeout: 0.5      # rf2o 断供 0.5s 后自动只用轮速（10Hz 下=连丢 5 帧）
    reset_on_time_jump: true
    publish_tf: true
    map_frame: map
    odom_frame: odom
    base_link_frame: base_link
    world_frame: odom

    # 输入1：底盘编码器 /odom —— 只取速度（位置会漂，不取）
    odom0: /odom
    odom0_config: [false, false, false,   # x, y, z
                   false, false, false,   # roll, pitch, yaw
                   true,  true,  false,   # vx, vy（横移只有轮速能给）
                   false, false, true,    # vroll, vpitch, vyaw
                   false, false, false]
    odom0_differential: false
    odom0_queue_size: 5

    # 输入2：rf2o 激光里程计（经 odom_relay 修过协方差+时间戳）—— 取位姿
    odom1: /odom_laser
    odom1_config: [true,  true,  false,   # x, y（打滑时唯一可信的位置锚点）
                   false, false, true,    # yaw
                   false, false, false,
                   false, false, false,
                   false, false, false]
    odom1_differential: false
    odom1_queue_size: 5
```

`process_noise_covariance` 保持现状（225 元素对角线形式，已验证可用），不引入
`initial_estimate_covariance`（用 RL 默认值），也不引入未验证的 rejection 参数名。

### 4.2 新节点 `robot_bringup/odom_relay.py`

职责单一：订一个 odom 话题 → 修时间戳与协方差 → 发另一个话题。接口全参数化，
不写死任何机器人专属常量。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `input_topic` | `/odom_laser_raw` | 输入 |
| `output_topic` | `/odom_laser` | 输出 |
| `restamp` | `true` | 用 `now()` 覆盖 `header.stamp`（rf2o 用雷达扫描时间戳，时间基准可能与 ROS 墙钟不一致；RL 靠消息时间戳排序/判定新鲜度） |
| `pose_covariance` | `[0.05, 0.05, 1e6, 1e6, 1e6, 0.02]` | 6 维对角线（x,y,z,roll,pitch,yaw）；未融合维度给大值属卫生写法，真正的门是 EKF 的 `odomN_config` |
| `twist_covariance` | `[0.03, 1e6, 1e6, 1e6, 1e6, 0.02]` | 同上；rf2o 的 vy 恒 0，必须给大值否则会当作"横移速度=0 且绝对可信" |

行为：原样透传 pose/twist 数值，只改 `header.stamp`（可选）、`header.frame_id`/`child_frame_id`（可选覆盖）
与两个协方差数组（6×6 行优先，对角线索引 0,7,14,21,28,35）。不发布任何 TF。
风格对齐既有 `robot_bringup/odom_to_tf.py`；入口注册进 `setup.py` 的 `console_scripts`。

**为什么不能省掉它**：没有它 EKF 会 100% 信任 rf2o（结论 2）；且 rf2o 的时间戳来源不可控（结论 2 同一处）。
这是一个 ~60 行、无状态、可单测的节点，代价远低于"EKF 静默退化成复读机"的返工。

### 4.3 `launch/odom.launch.py`（加 `fused` 模式）

- 文件结构改为 `OpaqueFunction` + `plan_odom_sources()`，消除现有 `PythonExpression`
  字符串拼条件（该写法历史上已出过一次尾逗号 tuple bug）。
- 对外 launch 参数名与默认值**保持不变**：`odom_source`（默认 `rf2o`）、`use_ekf`（默认 `false`）。
- `fused` 模式内部强制：`chassis_driver.publish_tf=false`、`rf2o.publish_tf=false` +
  `odom_topic=/odom_laser_raw`、起 `odom_relay`、起 `ekf_filter_node`、不起 `odom_to_tf`。

### 4.4 `launch/robot_base.launch.py`

现在把 `use_ekf:=false` 写死传给 `odom.launch.py`，导致从 robot_base 起永远开不了 EKF。
改为：新增 `use_ekf` 启动参数（默认 `false`，保持现状），透传下去；
`odom_source:=fused` 时 EKF 由 `fused` 语义自带，不受 `use_ekf` 影响。

## 五、被否决的方案（避免重复讨论）

| 方案 | 否决原因 |
|---|---|
| 两路（轮速+rf2o）**都只融速度** | 持续打滑时轮速的假速度仍会持续拖走估计（架空空转实验中会看到 odom_filtered 缓慢漂移）。保留为备用旋钮：把 `odom1_config` 改成速度 + `odom1_pose_*` 关掉即可 |
| 直接把 rf2o 当 odom→base_link（不接 EKF） | 放弃轮速的低噪声/高频率预测，rf2o 单点跳变直接进 TF；且无传感器回退 |
| 用 rf2o 话题直接喂 EKF（不加 relay） | 结论 2：协方差全 0 → EKF 退化成复读机 |
| slam_toolbox localization 取代 AMCL | 另一条独立路线，与本次正交，不在 Stage 1 范围 |
| AMCL 换 `OmniMotionModel`、`chassis_driver` dt 硬编码修复、IMU | Stage 2，Stage 1 上板验证通过后再做（同时改多处无法归因）；**IMU 已改为接 STM32 板，接入路径见 §十** |

## 六、改动清单

| # | 文件 | 类型 |
|---|---|---|
| 1 | `ros2_car/src/robot_bringup/robot_bringup/odom_relay.py` | 新增 |
| 2 | `ros2_car/src/robot_bringup/robot_bringup/odom_fusion.py` | 新增（纯逻辑：模式表 + 协方差展开） |
| 3 | `ros2_car/src/robot_bringup/setup.py` | 注册 `odom_relay` 入口 |
| 4 | `ros2_car/src/robot_bringup/config/ekf_params.yaml` | 改双源 |
| 5 | `ros2_car/src/robot_bringup/launch/odom.launch.py` | 加 `fused` 模式 |
| 6 | `ros2_car/src/robot_bringup/launch/robot_base.launch.py` | 透传 `use_ekf` |
| 7 | `ros2_car/tools/verify_odom_modes.py` | 新增本地校验 |
| 8 | `ros2_car/tools/verify_ekf_expr.py` | 删除（被 #7 取代） |

## 七、验证

**本地（Windows，无 ROS）**
1. `python tools/verify_odom_modes.py`：断言四种模式组合的节点启停与 `publish_tf` 取值
   （表三逐行核对），并断言 `ekf_params.yaml` 的 `odom0_config`/`odom1_config` 恰好 15 个布尔、
   `odom1` 话题名与 `odom_relay.output_topic` 一致（数组长度写错会让 EKF 启动即崩，是最常见返工点）。
2. `python -m py_compile` 全部改动的 .py。
3. 同一脚本内断言 `odom_fusion.diag6_to_covariance36` 的对角线落在索引 0/7/14/21/28/35，
   且非对角元素为 0（协方差索引写错是"滤波器静默变傻"的典型来源）。

**上板（板卡开机后）**
1. `ros2 launch robot_bringup robot_base.launch.py odom_source:=fused`
2. `ros2 topic hz /odom /odom_laser_raw /odom_laser /odom_filtered` —— 分别 ~10/10/10/20 Hz
3. `ros2 run tf2_tools view_frames.py` —— 确认 `odom→base_link` 只有 `ekf_filter_node` 一个发布者
4. `ros2 topic echo /odom_filtered --once` —— 协方差非 0
5. **打滑验收实验（关键）**：车架空、四轮离地 → 发 `cmd_vel` 让轮子空转
   - 期望：`/odom` 位置一路飞走；`/odom_laser` 基本不动；`/odom_filtered` 明显比 `/odom` 稳
   - 记录三者 10 秒后的位移，作为协方差调参依据（若 filtered 仍漂太多 → 调小 `pose_covariance` 的 x/y）
6. 地面实测：先跑 AMCL 定位看 `/amcl_pose` 是否更稳，再跑一次完整导航到点

## 八、风险与回退

| 风险 | 对策 |
|---|---|
| rf2o 时间戳基准与 ROS 墙钟不一致 | relay 默认 `restamp: true`，可参数关掉 |
| rf2o 在长走廊/空旷处扫描匹配退化 | `sensor_timeout: 0.5` 自动回退到单轮速源 |
| rf2o 中途重启导致其位姿原点与轮速 odom 原点不一致 → 位姿融合会打架 | 两种源同时启动（原点均为 0）；必要时给 rf2o 传 `init_pose_from_topic:=/odom` 用轮速位姿做初始位姿 |
| rf2o 单次跳变被当作真值 | 协方差 + `queue_size` 已可抑制；如仍跳变，改备用旋钮（只融速度） |
| 板卡 CPU（RDK X5）负载增加 | rf2o 已在 `yahboomcar_ws` 编译好、10Hz 下开销小；上板用 `top` 观察 |
| 整体不可用 | `odom_source` 默认仍是 `chassis`，一条命令回退 |

## 九、范围（YAGNI）

- 不做：Stage 2 各项（AMCL 模型/recovery、dt 修复、IMU）、`robot_chassis` 与 `stm32` 改动、
  Nav2 参数调整、`bt_navigator` 的 `odom_topic` 改指向 `/odom_filtered`。
- 不动：`nav2_params.yaml`、`slam_toolbox_params.yaml`、`car.urdf`、`bringup.launch.py`。

## 十、Stage 2 · IMU 接入路径（2026-09-10 调整）

**结论：IMU 改为接在 STM32 板上**（此前记录的"直插 RDK X5 板卡"作废）。因此 IMU 数据要经
`STM32 → USB CDC 协议 → robot_chassis → /imu`，**涉及固件改动，不能只写板卡侧节点**。

固件现状（2026-09-10 实查，工作量由此决定）：

| 项 | 现状 |
|---|---|
| `stm32/control/Core/Src/imu.c` | 736 行驱动已写好（帧头 `0x7E 0x23`，功能码 0x01/0x04/0x0A/0x10/0x16/0x26/0x60/0x61/0x80…，环形缓冲 + 校验），**但从未编译过** |
| CMake `target_sources` | 未注册 `imu.c`（显式列表，漏加不报错也不链接） |
| UART 外设 | 工程里**根本没有 `usart.c` / `huart3`**——一个 USART 都没初始化；`imu.c` 却 `#include "usart.h"` 并 `#define IMU_PORT_UART_HANDLE huart3` → **当前不可编译** |
| 中断 | `IMU_UART_IRQHandler()` 靠 CubeMX 生成的 `stm32f1xx_it.c::USART3_IRQHandler` 调用，该 handler 不存在 |
| 主循环 | `main.c` 10ms 周期硬约束内调 `up_poll()` + `mc_update_all()`；IMU 解析需并入且不得阻塞 |
| 协议 | USB v1.0 的 `STATUS(0x82)` 26 字节里没有 IMU 字段，需新增上行帧 |

**推荐路径（最小风险，不动 CubeMX 文件）**：把 `imu.c` 改成自包含——自带 GPIOB/PB10-PB11 与
USART3 寄存器级初始化（115200 8N1），在 `IMU_Process()` 里用**主循环轮询**读 SR/DR 收字节
（115200 下 10ms 约 115 字节，寄存器读几微秒，不碰 10ms 硬约束），既不需要 NVIC、也不需要改
`stm32f1xx_it.c` 或重新生成 `.ioc`。另一条路是用 CubeMX 重新生成加 USART3（更规范，但要
CubeMX 工具 + `control.ioc`，且会覆盖 CubeMX 文件）。

后续四步：①固件 bring-up + 注册 CMake；②USB 协议 v1.1 新增上行帧 `0x83`（IMU，建议 payload
6~8 字节：yaw int16/0.01° + gyro_z int16/0.1°/s + 可选 roll/pitch），同步更新
`docs/目标文档及说明/USB车控接口.md`；③`robot_chassis` 解析 `0x83` 发 `sensor_msgs/Imu`
（REP-103 ENU），URDF 加 `base_link→imu_link` 静态 TF；④EKF 加 `imu0` 只融 `vyaw`
（Stage 1 的 `ekf_params.yaml` 已留好位置）。
