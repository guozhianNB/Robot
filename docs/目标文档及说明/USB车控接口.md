# USB 车控接口（地瓜派 ↔ STM32）

> 版本：v1.0 ｜ 日期：2026-08-19 ｜ 适用：`stm32/control`（STM32F103ZETX，麦克纳姆 X 型四轮底盘）

## 1. 概述

地瓜派开发板（上位机，负责感知 / 规划）通过 **USB CDC 虚拟串口** 与 STM32（下位机，负责底盘执行）通信：

- 地瓜派 → STM32：下发**整车速度** `(vx, vy, ω)`、PID 调参、状态查询
- STM32 → 地瓜派：命令**应答 ACK** + 周期**心跳状态上报**（实际转速 / 编码器计数）

STM32 端负责麦克纳姆轮**逆运动学解算**与四电机 PID 闭环；地瓜派只需发"车要往哪走"，无需关心每个轮子的转速。

```
┌─────────────┐   USB CDC 虚拟串口    ┌──────────────────────────┐
│  地瓜派      │ ───────────────────▶  │  STM32                   │
│  (ttyACM0)  │   帧协议(见 §3)        │  usb_proto → mc_car_set  │
│  规划/感知   │ ◀───────────────────  │  → 麦克纳姆逆解 → 四电机PID │
└─────────────┘    ACK / STATUS       └──────────────────────────┘
```

## 2. 物理连接与通信参数

| 项 | 值 |
|---|---|
| 接口 | 地瓜派 USB-A ⟷ STM32 板载 USB（FS 全速） |
| 设备节点 | 地瓜派侧 `/dev/ttyACM0`（如被占用查 `ls /dev/ttyACM*`） |
| 波特率 | `115200`（虚拟串口波特率无实际约束，仅工具填写需要） |
| 数据位 / 停止位 / 校验 | 8 / 1 / None |
| 权限 | 地瓜派 `dialout` 组免 sudo（见板卡硬件规格） |

STM32 端代码位置：`Core/Inc/usb_proto.h`、`Core/Src/usb_proto.c`。

## 3. 帧格式

每帧二进制定长结构（小端）：

```
 字节 0     1       2      3      4 .. 4+len-1   4+len
[0xAA]   [0x55]  [ len ] [ cmd ] [ payload… ]   [ xor ]
```

| 字段 | 长度 | 说明 |
|---|---|---|
| `0xAA` `0x55` | 2 | 帧头，用于同步定位 |
| `len` | 1 | payload 字节数 |
| `cmd` | 1 | 命令号（见 §4） |
| `payload` | `len` | 负载，**小端** |
| `xor` | 1 | 校验 = 除末字节外**全部字节**异或（含帧头、len、cmd、payload） |

`payload` 最大 32 字节。

## 4. 命令总览

命令号分区：`0x0x` 下行（地瓜派 → STM32），`0x8x` 上行（STM32 → 地瓜派）。

| 方向 | cmd | 名称 | payload 长度 | 说明 |
|---|---|---|---|---|
| 下行 | `0x01` | `STOP` | 0 | 立即四轮制动 |
| 下行 | `0x03` | `SET_CAR_VEL` | 6 | 整车速度 `vx vy ω` |
| 下行 | `0x04` | `TUNE_PID` | 12 | 在线调 PID `kp ki kd` |
| 下行 | `0x05` | `GET_STATUS` | 0 | 按需立即回一帧 `STATUS` |
| 上行 | `0x81` | `ACK` | 2 | 命令应答 |
| 上行 | `0x82` | `STATUS` | 26 | 周期心跳状态（默认 100ms） |

> 约定：对 `STOP` / `SET_CAR_VEL` / `TUNE_PID` 均回 `ACK`；`GET_STATUS` 不回 ACK，直接回 `STATUS`。

## 5. 下行命令详述

### 5.1 STOP `0x01`
`payload` 空。立即四轮制动（等价于 `SET_CAR_VEL` 三通道全 0）。

帧示例：`AA 55 00 01 01`（len=0, cmd=0x01, xor=`AA^55^00^01`=`0xFE`... 实际按公式计算，下同）。

### 5.2 SET_CAR_VEL `0x03`
`payload` 6 字节，三通道 `int16` 小端：

| 偏移 | 字段 | 类型 | 单位 | 范围 | 含义 |
|---|---|---|---|---|---|
| 0 | `vx` | int16 | mm/s | ±32767 | 前进速度（正=前进） |
| 2 | `vy` | int16 | mm/s | ±32767 | 左移速度（正=向左） |
| 4 | `ω` | int16 | 0.1°/s | ±3276.7°/s | 旋转角速度（正=左转） |

- 三通道全 `0` = 整车制动。
- 可组合：如 `vx=200, vy=0, ω=0` 直行；`vx=0, vy=200, ω=0` 横移；`vx=0, vy=0, ω=300` 原地左转。

帧示例（前进 200mm/s）：`AA 55 06 03 C8 00 00 00 00 00 <xor>`

### 5.3 TUNE_PID `0x04`
`payload` 12 字节，三个 `float` 小端（IEEE754）：

| 偏移 | 字段 | 类型 | 说明 |
|---|---|---|---|
| 0 | `kp` | float | 比例项，传 0 表示不改 |
| 4 | `ki` | float | 积分项，传 0 表示不改 |
| 8 | `kd` | float | 微分项，传 0 表示不改 |

> 注意：传 0 表示"不修改"，因此**无法把某项调到 0**。四电机共享同一组 PID。

### 5.4 GET_STATUS `0x05`
`payload` 空。STM32 立即回一帧 `STATUS`（§7），不回 ACK。

## 6. 上行命令详述

### 6.1 ACK `0x81`
`payload` 2 字节：

| 偏移 | 字段 | 类型 | 说明 |
|---|---|---|---|
| 0 | `reply` | uint8 | 被应答的命令号（原样回显） |
| 1 | `code` | uint8 | 结果码（见 §8） |

### 6.2 STATUS `0x82`
`payload` 26 字节：

| 偏移 | 字段 | 类型 | 说明 |
|---|---|---|---|
| 0 | `seq` | uint8 | 递增序号，便于丢包检测 |
| 1..8 | `rpm[1..4]` | 4×int16 | 四轮实际转速 (RPM)，小端 |
| 9..24 | `enc[1..4]` | 4×int32 | 四轮编码器累计计数（带符号），小端 |
| 25 | `flags` | uint8 | 状态位，`bit0`=堵转（预留，当前恒 0） |

- 默认每 **100ms** 自动上报一帧（STM32 端 `UP_STS_PERIOD_MS`，改 `usb_proto.c` 即可）。
- `rpm` 顺序 = 电机编号 1~4：左前 / 右前 / 左后 / 右后。
- `enc` 可作里程计（换算见 §9 标定）。

## 7. 整车速度解算（STM32 端内部）

STM32 收到 `SET_CAR_VEL` 后执行麦克纳姆 X 型逆运动学：

```
LF = vx + vy + rot       RF = vx - vy - rot
LR = vx - vy + rot       RR = vx + vy - rot

rot = ω(rad/s) × ROTATE_RADIUS_MM          # 旋转切向速度
RPM = v(mm/s) × 60 / (π × 轮径)
```

参数（`motor_control.c`）：

| 宏 | 默认 | 说明 |
|---|---|---|
| `MC_WHEEL_DIAMETER_MM` | 80 | 麦克纳姆轮直径，**按实际轮子修改** |
| `MC_ROTATE_RADIUS_MM` | 150 | 旋转半径（车架轮距/轴距半对角线），**按车架标定** |

## 8. 应答码（ACK `code`）

| code | 含义 |
|---|---|
| `0x00` | OK |
| `0x01` | 未知命令号 |
| `0x02` | payload 长度与命令不符 |
| `0x03` | 帧校验失败（预留，坏帧直接丢弃不回） |

## 9. 标定

### 9.1 旋转半径 `ROTATE_RADIUS_MM`
- 几何法：`R = √((轮距/2)² + (轴距/2)²)`，测车架实际轮距、轴距代入。
- 实验法：下发原地旋转 `(0,0,ω)`，测量实际转角 θ 与理论 θ′，修正 `R = R × θ′/θ`。

### 9.2 轮径 `MC_WHEEL_DIAMETER_MM`
让车直行一段已知距离 `L`，读 `STATUS` 四轮编码器增量 ΔN（每轮 CPR 见 `motor_control.c` 的 `MC_ENCODER_CPR`），反推轮径。

## 10. 地瓜派端接入示例

见同目录 `usb_chassis_demo.py`（Python + `pyserial`）。核心流程：

```python
import serial, struct

ser = serial.Serial("/dev/ttyACM0", 115200, timeout=0.1)

def frame(cmd, payload=b""):
    f = bytes([0xAA, 0x55, len(payload), cmd]) + payload
    return f + bytes([functools.reduce(operator.xor, f, 0)])

# 前进 200mm/s
ser.write(frame(0x03, struct.pack("<hhh", 200, 0, 0)))
# 原地左转 30°/s
ser.write(frame(0x03, struct.pack("<hhh", 0, 0, 300)))
# 停止
ser.write(frame(0x01))
```

## 11. 注意事项

- **波特率对虚拟串口无意义**，但串口工具/代码里仍应填 115200，避免工具用其他波特率误解码。
- 帧为二进制，**不要**在 TX/RX 上做文本编码（如 `\n`）或字符转换。
- 地瓜派发送速度会立即生效并持续保持（无"目标保持时间"概念）；要停就发全 0 或 `STOP`。
- 上电后 STM32 会先做约 2.8s 编码器方向自动标定（四轮依次短转），期间请勿下发控制。
- 心跳 100ms 可用作链路健康检测：长时间收不到 `STATUS` 说明 USB 断开。
- 新增源码后需在顶层 `stm32/control/CMakeLists.txt` 的 `target_sources` 显式登记（非 glob）。

## 12. 相关代码

| 文件 | 作用 |
|---|---|
| `Core/Inc/usb_proto.h` / `Core/Src/usb_proto.c` | 协议解析、命令分发、心跳上报 |
| `Core/Inc/motor_control.h` / `.c` | 四电机 PID 闭环 + `mc_car_set` 整车解算 |
| `Core/Inc/motor_driver.h` / `.c` | 编码器 / PWM / 方向原语 |
| `USB_DEVICE/App/usbd_cdc_if.c` | CDC 收发，`CDC_Receive_FS` 喂给协议层 |
| `Core/Src/main.c` | 主循环 `up_poll()` + `mc_update_all()` |
