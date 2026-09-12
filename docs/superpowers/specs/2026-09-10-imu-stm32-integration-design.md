# IMU 接入 STM32（经 USB 协议进 EKF）设计文档

日期：2026-09-10
状态：待用户审查（用户已定：IMU 接 STM32 的 **USART3 / PB10-PB11**，先出规格+计划再动代码）
上游文档：`docs/superpowers/specs/2026-09-10-rf2o-ekf-odom-fusion-design.md`（Stage 1 双源里程计融合，已实现）

## 目标

把 IMU（接在 STM32 的 USART3）的偏航角与绕 Z 角速度送进 ROS，作为 `robot_localization`
EKF 的 `imu0` 输入，只融 `vyaw`——用来治**麦轮原地转打滑导致的航向漂移**：轮子打滑时
陀螺仪测的角速度仍然是真的。

数据链路：

```
IMU ──(UART 115200, 0x7E23 帧)──> STM32 imu.c ──> usb_proto 新增上行帧 0x83
   ──(USB CDC)──> robot_chassis/chassis_driver ──> /imu (sensor_msgs/Imu)
   ──> robot_localization EKF imu0 ──> odom→base_link TF（与 Stage 1 的 rf2o+轮速融合同一个 EKF）
```

## 一、固件现状（2026-09-10 实查，工作量由此决定）

| 项 | 现状 |
|---|---|
| `Core/Src/imu.c`（736 行） | 协议/环形缓冲/校验都写好了（帧头 `0x7E 0x23`、功能码 0x01/0x04/0x0A/0x10/0x16/0x26/0x60/0x61/0x80…），**但从未编译过** |
| `CMakeLists.txt` 的 `target_sources` | 未注册 `imu.c`（显式列表，漏加不报错也不链接） |
| UART 外设 | 工程里**没有 `usart.c`、没有 `huart3`**——一个 USART 都没初始化；`imu.c` 却 `#include "usart.h"` 且 `IMU_UART_SendByte()` 用 `HAL_UART_Transmit(&huart3,…)` → **当前编译不过** |
| 中断 | `IMU_UART_IRQHandler()` 要靠 CubeMX 生成的 `stm32f1xx_it.c::USART3_IRQHandler` 调用，该 handler 不存在 |
| 引脚占用 | I2C1（OLED）= PB6/PB7；电机方向 PB12-15 / PD8-11；PWM PE9/11/13/14 → **PB10/PB11 空闲，USART3 默认映射即在此**（无重映射） |
| 主循环 | `main.c` 10ms 硬约束内调 `up_poll()` + `mc_update_all()`；`USER CODE BEGIN 2/3` 段是官方允许的用户代码位 |
| USB 协议 | v1.0 的 `STATUS(0x82)` 26B 里没有 IMU 字段 → 需新增上行帧 |
| 本地工具链 | ✅ `arm-none-eabi-gcc` + `cmake` + `ninja` 均在 PATH（STM32CubeCLT 1.19）→ **固件可在 Windows 本地编译验证** |

## 二、关键设计决策

1. **`imu.c` 改为「自包含」UART 实现，不动任何 CubeMX 文件。**
   自己使能 GPIOB/USART3 时钟、配 PB10(AF_PP)/PB11(INPUT)、由 `HAL_RCC_GetPCLK1Freq()`
   现算 BRR（不写死魔数）；用 `CLEAR_BIT(AFIO->MAPR, AFIO_MAPR_USART3_REMAP)` 明确走默认映射，
   保住 PD8/PD9 给电机方向。删除对 `usart.h`/`huart3` 的依赖。
   *替代方案*：用 CubeMX 重新生成加 USART3——更"规范"，但需要 CubeMX 工具、会覆盖 CubeMX 文件、
   且 `control.ioc` 改动不好 review。**否决**。
2. **收字节用主循环轮询，不用中断。**
   `IMU_Process()` 内先 `IMU_UART_PollRx()`：`while (SR & RXNE) 读 DR 入环形缓冲`。115200 下
   10ms 最多来 ~115 字节，寄存器读几微秒，**不碰 10ms 硬约束**；也不需要 NVIC、不需要在
   `stm32f1xx_it.c` 里加 `USART3_IRQHandler`（那是 CubeMX 文件）。
   `IMU_UART_SendByte/SendArray` 改成寄存器级 TXE 写（原样保留 HAL_UART_Transmit 就走不通）。
3. **模块"只在答不问"时的自愈**：若 1s 内没有解析到任何 IMU 帧，则每 200ms 主动
   `IMU_UART_RequestData(EULER)` + `(RAW_GYRO)` 一次（最坏 0.7ms 的 TXE 等待，可接受）。
   避免"模块默认不主动上报"导致台架上一片空白。
4. **不调用 `IMU_UART_GetVersion()`**：它内部 `HAL_Delay(5)×20`，最多阻塞 100ms，会踩 10ms 硬约束。
5. **IMU 上行单独成帧 `0x83`，不扩 `STATUS`。** 理由：`STATUS` 是 26B 定长、板卡侧已有解析器，
   扩字段是破坏性改动；而帧格式自带 `len`，新增命令号是向后兼容的加法。`0x83` 以 **50ms（20Hz）**
   周期发送（IMU 内部 100Hz，20Hz 对 20Hz 的 EKF 足够）。
6. **新鲜度门控**：`0x83` 只在 `IMU_UART_GetFrameCount()` 相比上次发送有变化时才发。
   即"IMU 没接/没数据 → 不发 `0x83` → ROS 没有 `/imu`"，而不是发一堆零值骗 EKF。
7. **`/imu` 必须由 `chassis_driver` 发布，不能另开节点。**
   `/dev/ttyACM0` 是独占的，另一个进程读不到 USB CDC 上行帧。所以 IMU 转发是
   `chassis_driver` 的职责扩展，不是新节点。
8. **协方差必须非零**（Stage 1 教训）：`sensor_msgs/Imu` 里被融合变量的方差写 0 会被
   robot_localization 当成 `1e-6` → 100% 信任。故 roll/pitch 给 `1e6`（不融合）、
   yaw 给 `imu_yaw_var`、`angular_velocity.z` 给 `imu_wz_var`。
9. **EKF 只融 `vyaw`（不融 IMU 的绝对 yaw）。** rf2o 位姿已经在提供 yaw；增加第二个朝向源
   按 robot_localization 官方建议要慎重（两源协方差都不准时会互相打架）。若后面发现航向仍漂，
   再开 `imu0` 的 yaw + `differential: true`（一行配置，留作旋钮）。
10. **`0x83` 帧不参与下行 ACK 逻辑**：它只是上行周期帧，STM32 侧不需要回 ACK，板卡侧只解析。

## 三、接口契约

### 3.1 USB 上行帧 `IMU(0x83)`（协议 v1.1）

帧格式沿用既有：`[0xAA][0x55][len=8][0x83][payload 8B][xor]`，payload 小端：

| 偏移 | 字段 | 类型 | 单位 | 量程/说明 |
|---|---|---|---|---|
| 0-1 | `yaw` | int16 | 0.01° | ±180° → ±18000，正=左转（CCW，待台架标定符号） |
| 2-3 | `yaw_rate` | int16 | 0.1°/s | 绕 Z 角速度，正=左转 |
| 4-5 | `roll` | int16 | 0.01° | 仅诊断/记录，不进 EKF |
| 6-7 | `pitch` | int16 | 0.01° | 仅诊断/记录，不进 EKF |

周期 50ms；仅当 IMU 有新解析帧时发送。同步更新 `docs/目标文档及说明/USB车控接口.md`（v1.0 → v1.1）。

### 3.2 ROS `/imu`（`sensor_msgs/Imu`）契约

| 字段 | 值 |
|---|---|
| `header.frame_id` | `imu_link` |
| `orientation` | 由 `yaw` 构造（roll/pitch 记 0） |
| `orientation_covariance` | `[1e6, 0, 0, 0, 1e6, 0, 0, 0, imu_yaw_var(0.02)]` |
| `angular_velocity` | 仅 `z = yaw_rate`（rad/s，乘 `imu_sign_wz`） |
| `angular_velocity_covariance` | `[1e6, 0, 0, 0, 1e6, 0, 0, 0, imu_wz_var(0.01)]` |
| `linear_acceleration_covariance` | `[0] = -1`（未提供加速度，按 REP 约定） |
| 发布者 | `chassis_driver`，周期 = 固件上行周期（20Hz） |

### 3.3 TF 与 EKF

- URDF 新增 `imu_link` + `base_link→imu_link` 固定关节：**假设 IMU 平放、轴向与车体对齐、
  位置可忽略**，故 `origin xyz="0 0 0" rpy="0 0 0"`（与 `base_link` 重合）。装歪了只改这一行。
- `ekf_params.yaml` 新增：`imu0: /imu`、`imu0_config` 仅 `vyaw` 位为 true、
  `imu0_differential: false`、`imu0_queue_size: 10`。

## 四、改动清单

| # | 文件 | 动作 |
|---|---|---|
| 1 | `stm32/control/Core/Src/imu.c` | 改：自包含 UART 初始化 + 轮询收字节 + 寄存器级发送 + 流失效自愈 |
| 2 | `stm32/control/Core/Src/main.c` | 改：`USER CODE BEGIN 2` 加 `IMU_Init()`，`USER CODE BEGIN 3` 加 `IMU_Process()` |
| 3 | `stm32/control/CMakeLists.txt` | 改：`target_sources` 注册 `Core/Src/imu.c` |
| 4 | `stm32/control/Core/Inc/usb_proto.h` | 改：新增 `UP_CMD_IMU 0x83` 与帧注释 |
| 5 | `stm32/control/Core/Src/usb_proto.c` | 改：`up_send_imu()` + 50ms 周期 + 新鲜度门控 |
| 6 | `docs/目标文档及说明/USB车控接口.md` | 改：v1.1，补 `0x83` 布局 |
| 7 | `ros2_car/src/robot_chassis/robot_chassis/usb_protocol.py` | 改：`CMD_IMU`/`IMU_PAYLOAD_LEN`/`decode_imu()` |
| 8 | `ros2_car/src/robot_chassis/robot_chassis/chassis_driver.py` | 改：解析 `0x83` → 发 `/imu` |
| 9 | `ros2_car/src/robot_chassis/config/chassis_params.yaml` | 改：IMU 话题/坐标系/符号/方差参数 |
| 10 | `ros2_car/src/robot_bringup/urdf/car.urdf` | 改：`imu_link` + 固定关节 |
| 11 | `ros2_car/src/robot_bringup/config/ekf_params.yaml` | 改：新增 `imu0` 块 |
| 12 | `ros2_car/tools/verify_odom_modes.py` | 改：加 `decode_imu` 往返、EKF `imu0` 位、帧常量断言 |
| 13 | `ros2_car/README.md` | 改：IMU 台架标定与验收步骤 |

## 五、验证

**本地（Windows，可做）**
- 固件：`cmake --preset Debug && cmake --build build/Debug` 必须零错误（含 `imu.c` 首次进编译）。
- ROS 侧：`python ros2_car/tools/verify_odom_modes.py`（新增 `decode_imu` 往返、EKF `imu0` 位）+
  `py_compile`。`usb_protocol.py` 只依赖 `struct`，本地可真正做编解码往返测试。

**台架（板卡 + STM32，用户侧烧录）**
1. 烧录后用串口观察：IMU 接上 → 20Hz 出现 `AA 55 08 83 …`；拔掉 → `0x83` 消失（新鲜度门控生效）。
2. `ros2 topic hz /imu` ≈ 20Hz；`ros2 topic echo /imu --once` 看协方差非 0。
3. **符号标定**：手托车体**逆时针**转 → `/imu` 的 `angular_velocity.z` 与 `orientation` yaw 应增大
   （REP-103 CCW 为正）；不对就改 `imu_sign_wz` / `imu_sign_yaw`。
4. **打滑验收（关键）**：车架空四轮离地、地面不动，发 `cmd_vel` 让轮子空转 → `/odom` 的 yaw 疯长，
   而 `/imu` 的 yaw 基本不动、`/odom_filtered` 的 yaw 明显比 `/odom` 稳。这是"IMU 真的治住了
   原地转打滑"的直接证据。
5. `ros2 topic echo /diagnostics`（EKF，`print_diagnostics: true`）确认 `imu0` 有数据、无 waiting。
6. 跑一次完整导航到点，确认横移/旋转不再丢位。

## 六、风险与回退

| 风险 | 对策 |
|---|---|
| IMU 模块默认不主动上报（只答不问） | 决策 3 的 1s 无帧自愈请求（Euler + Raw Gyro） |
| 模块输出帧类型与 `s_roll/s_pitch/s_yaw` 更新路径不匹配（只发四元数不发欧拉角） | 台架第 1 步先看原始帧功能码；若只发四元数，则在 `imu.c` 里由四元数算 yaw（`atan2`），改动局限在 `IMU_UART_GetEuler` 一处 |
| IMU 装歪/轴向不一致 | URDF 的 `origin rpy` 一行 + `imu_sign_*` 参数 |
| 固件改动影响 10ms 闭环 | 轮询只读已到达字节、自愈请求限频 200ms/最坏 0.7ms；建图/导航前先跑一次空载 |
| 协议扩帧影响既有解析 | `0x83` 是新增命令号，`chassis_driver` 不认识的名字原有分支已按未知帧忽略；仍保留 v1.0 兼容 |
| 整体不可用 | `ekf_params.yaml` 里删掉 `imu0` 块 + 不启 `IMU_Process()` 即回到 Stage 1 行为 |

## 七、范围（YAGNI）

- 不做：加速度计/磁力计进 EKF、`robot_pose_ekf`、IMU 温度补偿、`0x83` 的 ACK、
  第二个 EKF（map→odom 仍归 AMCL）。
- 不动：`chassis_driver` 的现有 odom/命令链路、Stage 1 的 `odom_relay`/`odom_fusion`、
  `nav2_params.yaml`、`slam_toolbox_params.yaml`。
