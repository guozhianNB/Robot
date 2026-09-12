# IMU 接入 STM32 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** IMU（STM32 USART3/PB10-PB11）的 yaw 与 yaw_rate 经 USB 协议新增帧 `0x83` 送到 ROS `/imu`，进 Stage 1 已有的 EKF `imu0`（只融 `vyaw`），专治麦轮原地转打滑导致的航向漂移。

**架构：** `imu.c` 改自包含 UART（自己 bring-up PB10/PB11 + 主循环轮询收字节，不动 CubeMX 文件）→ `usb_proto.c` 以 20Hz 发 `0x83`（仅在有新解析帧时）→ `chassis_driver` 解析并发布 `sensor_msgs/Imu`（协方差非零）→ EKF `imu0` 融 `vyaw`。

**技术栈：** STM32F103 HAL（寄存器级 UART）+ CMake/Ninja/arm-none-eabi-gcc（本地可编译）+ ROS2 Humble（robot_chassis / robot_bringup）+ robot_localization。

**规格：** `docs/superpowers/specs/2026-09-10-imu-stm32-integration-design.md`

**前提：** 本地有完整 ARM 工具链（STM32CubeCLT 1.19：`arm-none-eabi-gcc`/`cmake`/`ninja` 均在 PATH）；烧录与台架测试由用户在硬件侧做（Windows 端只做编译级验证）。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `stm32/control/Core/Src/imu.c` | IMU 驱动：自包含 UART bring-up + 轮询收字节 + 寄存器级发送 + 流失效自愈 | 修改 |
| `stm32/control/Core/Inc/imu.h` | 新增 `IMU_UART_PollRx` / `IMU_UART_EnsureStreaming` 声明 | 修改 |
| `stm32/control/Core/Src/main.c` | USER CODE 段调用 `IMU_Init()` / `IMU_Process()` | 修改 |
| `stm32/control/CMakeLists.txt` | `target_sources` 注册 `Core/Src/imu.c` | 修改 |
| `stm32/control/Core/Inc/usb_proto.h` | 新增 `UP_CMD_IMU 0x83` | 修改 |
| `stm32/control/Core/Src/usb_proto.c` | `up_send_imu()` + 50ms 周期 + 新鲜度门控 | 修改 |
| `docs/目标文档及说明/USB车控接口.md` | 协议 v1.1：`0x83` 帧布局 | 修改 |
| `ros2_car/src/robot_chassis/robot_chassis/usb_protocol.py` | `CMD_IMU` / `IMU_PAYLOAD_LEN` / `decode_imu()` | 修改 |
| `ros2_car/src/robot_chassis/robot_chassis/chassis_driver.py` | 解析 `0x83` → 发 `/imu` | 修改 |
| `ros2_car/src/robot_chassis/config/chassis_params.yaml` | IMU 话题/坐标系/符号/方差 | 修改 |
| `ros2_car/src/robot_bringup/urdf/car.urdf` | `imu_link` + 固定关节 | 修改 |
| `ros2_car/src/robot_bringup/config/ekf_params.yaml` | 新增 `imu0` 块 | 修改 |
| `ros2_car/tools/verify_odom_modes.py` | 加 `0x83` 常量与 `decode_imu` 往返、EKF `imu0` 位断言 | 修改 |
| `ros2_car/README.md` | IMU 标定与验收步骤 | 修改 |

---

## 任务 1：固件 `imu.c` 自包含化 + 注册 + 主循环调用

**文件：**
- 修改：`stm32/control/Core/Src/imu.c:17-33`（includes + 移植宏）
- 修改：`stm32/control/Core/Src/imu.c:284-292`（`IMU_UART_Init`）
- 修改：`stm32/control/Core/Src/imu.c:400-413`（`IMU_UART_SendByte/SendArray`）
- 修改：`stm32/control/Core/Src/imu.c:431-434`（`IMU_Process`）+ 新增两个函数
- 修改：`stm32/control/Core/Inc/imu.h:70-77`（加两个声明）
- 修改：`stm32/control/CMakeLists.txt:46-53`（注册源文件）
- 修改：`stm32/control/Core/Src/main.c:27-35 / 237-245 / 266-274`（include + 调用）

- [ ] **步骤 1：改 includes 与移植宏（`imu.c` 第 17-33 行整段替换）**

```c
#include "imu.h"
#include "stm32f1xx_hal.h"
#include <string.h>

/* ===================== 移植配置（自包含，不依赖 usart.c / huart3） =========
 * 本工程没有 usart.c（一个 USART 都没初始化过），故 UART 由本文件自行 bring-up：
 *   - 时钟/引脚/波特率：寄存器级 IMU_UART_Init()
 *   - 收字节：主循环轮询 IMU_UART_PollRx()（不用中断，不碰 10ms 硬约束）
 *   - 发字节：寄存器级 TXE 写 IMU_UART_SendByte()
 * 移植到其它串口只需改下面 5 个宏（保持 USART3 默认映射，PD8/PD9 留给电机方向脚）。 */
#define IMU_PORT_UART_INSTANCE  USART3        /* 寄存器实例（默认映射 PB10=TX / PB11=RX） */
#define IMU_PORT_GPIO           GPIOB
#define IMU_PORT_TX_PIN         GPIO_PIN_10
#define IMU_PORT_RX_PIN         GPIO_PIN_11
#define IMU_PORT_BAUD           115200u

#define IMU_PORT_RX_IDLE_MS     1000u         /* 超过此时长无新帧 → 主动请求一次 */
#define IMU_PORT_REQ_PERIOD_MS  200u          /* 主动请求的最小间隔（限频） */

/* 自愈状态：观察"解析帧数是否还在涨" */
static uint32_t s_seen_frames   = 0u;
static uint32_t s_last_progress = 0u;
static uint32_t s_last_req_time = 0u;
```

- [ ] **步骤 2：改 `IMU_UART_Init()`（第 284-292 行整段替换）**

```c
void IMU_UART_Init(void)
{
    GPIO_InitTypeDef gpio = {0};

    /* ① 时钟：GPIOB + USART3（APB1）+ AFIO */
    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_USART3_CLK_ENABLE();
    __HAL_RCC_AFIO_CLK_ENABLE();

    /* ② 引脚：PB10=TX（复用推挽）、PB11=RX（浮空输入）；
     *    USART3 保持默认映射（PB10/PB11），PD8/PD9 留给电机方向脚 */
    CLEAR_BIT(AFIO->MAPR, AFIO_MAPR_USART3_REMAP);

    gpio.Pin   = IMU_PORT_TX_PIN;
    gpio.Mode  = GPIO_MODE_AF_PP;
    gpio.Speed = GPIO_SPEED_FREQ_HIGH;
    HAL_GPIO_Init(IMU_PORT_GPIO, &gpio);

    gpio.Pin  = IMU_PORT_RX_PIN;
    gpio.Mode = GPIO_MODE_INPUT;
    gpio.Pull = GPIO_NOPULL;
    HAL_GPIO_Init(IMU_PORT_GPIO, &gpio);

    /* ③ 波特率：由 PCLK1 现算 BRR，不写死魔数（115200 @36MHz → 312） */
    IMU_PORT_UART_INSTANCE->BRR =
        (uint16_t)((HAL_RCC_GetPCLK1Freq() + IMU_PORT_BAUD / 2u) / IMU_PORT_BAUD);

    /* ④ 8N1、收发使能、开串口；不开中断（主循环轮询收字节） */
    IMU_PORT_UART_INSTANCE->CR2 = 0u;
    IMU_PORT_UART_INSTANCE->CR3 = 0u;
    IMU_PORT_UART_INSTANCE->CR1 = USART_CR1_TE | USART_CR1_RE | USART_CR1_UE;

    s_seen_frames   = IMU_UART_GetFrameCount();
    s_last_progress = HAL_GetTick();
    s_last_req_time = HAL_GetTick();
}
```

- [ ] **步骤 3：改发送函数（第 400-413 行整段替换）**

```c
void IMU_UART_SendByte(uint8_t data)
{
    /* 寄存器级发送：等 TXE 后写 DR（最坏 ~87us/字节，仅初始化/自愈请求时用） */
    while ((IMU_PORT_UART_INSTANCE->SR & USART_SR_TXE) == 0u)
    {
    }
    IMU_PORT_UART_INSTANCE->DR = (uint16_t)data;
}

void IMU_UART_SendArray(uint8_t *pData, uint8_t length)
{
    if (pData == NULL) return;
    for (uint8_t i = 0u; i < length; i++)
        IMU_UART_SendByte(pData[i]);
}
```

- [ ] **步骤 4：`IMU_Process()` 加轮询 + 自愈（第 431-434 行整段替换）**

```c
void IMU_UART_PollRx(void)
{
    /* 只读"已经到达"的字节：无新字节立刻返回，绝不阻塞主循环 */
    while ((IMU_PORT_UART_INSTANCE->SR & USART_SR_RXNE) != 0u)
    {
        uint8_t byte = (uint8_t)(IMU_PORT_UART_INSTANCE->DR & 0xFFu);
        ++s_rx_byte_count;
        _rxbuf_push(byte);
        _debug_push(byte);
    }
    if ((IMU_PORT_UART_INSTANCE->SR & USART_SR_ORE) != 0u)
    {
        (void)IMU_PORT_UART_INSTANCE->DR;   /* 读 SR 后再读 DR 清 ORE */
        ++s_overrun_count;
    }
}

/* 模块"只答不问"时自愈：1s 内帧数不涨 → 每 200ms 主动要一次欧拉角 + 原始陀螺 */
void IMU_UART_EnsureStreaming(void)
{
    uint32_t now    = HAL_GetTick();
    uint32_t frames = IMU_UART_GetFrameCount();

    if (frames != s_seen_frames)
    {
        s_seen_frames   = frames;
        s_last_progress = now;
        return;
    }
    if ((now - s_last_progress) < IMU_PORT_RX_IDLE_MS) return;
    if ((now - s_last_req_time) < IMU_PORT_REQ_PERIOD_MS) return;

    s_last_req_time = now;
    (void)IMU_UART_RequestData(IMU_FUNC_EULER);
    (void)IMU_UART_RequestData(IMU_FUNC_RAW_GYRO);
}

void IMU_Process(void)
{
    IMU_UART_PollRx();            /* ① 已到达字节 → 环形缓冲 */
    IMU_UART_Process();           /* ② 解析完整帧，更新缓存数据 */
    IMU_UART_EnsureStreaming();   /* ③ 无帧自愈（限频） */
}
```

- [ ] **步骤 5：`imu.h` 加声明（第 70-77 行区域，挨着 `IMU_UART_IRQHandler` 之后插入）**

```c
/** @brief 轮询 USART3 已到达字节并推入环形缓冲（主循环调用，不阻塞） */
void IMU_UART_PollRx(void);

/** @brief 模块长期无帧时主动请求数据（限频，防"只答不问"） */
void IMU_UART_EnsureStreaming(void);
```

（同区域 `IMU_UART_IRQHandler` 的注释补一句"轮询模式下不启用、未注册 NVIC；如改中断需在 `stm32f1xx_it.c` 的 USER CODE 段加 `USART3_IRQHandler`"。）

- [ ] **步骤 6：CMake 注册源文件**

```cmake
target_sources(${CMAKE_PROJECT_NAME} PRIVATE
    # Add user sources here
    Core/Src/motor_driver.c
    Core/Src/motor_control.c
    Core/Src/usb_proto.c
    Core/Src/oled.c
    Core/Src/font.c
    Core/Src/imu.c
)
```

- [ ] **步骤 7：`main.c` 三处 USER CODE 编辑**

① `USER CODE BEGIN Includes` 段（第 27-35 行）加一行：

```c
#include "motor_driver.h"
#include "motor_control.h"
#include "usb_proto.h"
#include "oled.h"
#include "imu.h"
#include <stdio.h>
```

② `USER CODE BEGIN 2` 段、`up_init();` 之后（第 244 行后）加：

```c
  up_init();               /* 清零 USB 车控协议状态 */
  IMU_Init();              /* IMU：USART3/PB10-PB11 自包含 bring-up（115200 8N1） */
```

③ `USER CODE BEGIN 3` 段主循环（第 271-273 行）加一行 `IMU_Process()`：

```c
    up_poll();                                 /* USB 协议：命令分发 + 心跳 */
    IMU_Process();                             /* IMU：轮询收字节 + 解析 + 无帧自愈 */
    mc_update_all();                           /* 10ms 周期闭环  */
    HAL_Delay(10);                             /* 10ms 周期      */
```

- [ ] **步骤 8：本地编译验证（关键：`imu.c` 首次进编译）**

运行：
```powershell
cd D:\_project\Robot\stm32\control
cmake --preset Debug
cmake --build build/Debug
```
预期：configure 成功；build 生成 `build/Debug/control.elf` + `control.bin`，**无 error**。
若报 `usart.h: No such file` → 步骤 1 的 include 段没替换干净；
若报 `AFIO_MAPR_USART3_REMAP` 未定义 → 确认包含 `stm32f1xx_hal.h`；
若报 `undefined reference to IMU_UART_PollRx` → 步骤 5 的声明与步骤 4 的定义名字不一致。

- [ ] **步骤 9：Commit**

```bash
git add stm32/control/Core/Src/imu.c stm32/control/Core/Inc/imu.h stm32/control/CMakeLists.txt stm32/control/Core/Src/main.c
git commit -m "feat(stm32): IMU 驱动自包含化（USART3/PB10-PB11 轮询收发）并接入 10ms 主循环"
```

---

## 任务 2：USB 协议新增上行帧 `0x83` + 协议文档 v1.1

**文件：**
- 修改：`stm32/control/Core/Inc/usb_proto.h:41-42`
- 修改：`stm32/control/Core/Src/usb_proto.c:1-5 / 25-28 / 186-195`
- 修改：`docs/目标文档及说明/USB车控接口.md`

- [ ] **步骤 1：加命令号（`usb_proto.h`）**

```c
/* ---------------- 上行命令（STM32 → 地瓜派） ---------------- */
#define UP_CMD_ACK         0x81u  /* 2B：reply:uint8 code:uint8        */
#define UP_CMD_STATUS      0x82u  /* 状态帧：见 up_send_status()       */
#define UP_CMD_IMU         0x83u  /* 8B：yaw:int16(0.01°) yaw_rate:int16(0.1°/s)
                                   *     roll:int16(0.01°) pitch:int16(0.01°)
                                   * 周期 50ms，仅当 IMU 有新帧时发送 */
```

- [ ] **步骤 2：`usb_proto.c` 加 include 与状态量**

第 1-5 行的 include 段改为：

```c
#include "usb_proto.h"
#include "motor_control.h"
#include "motor_driver.h"
#include "imu.h"
#include "usbd_cdc_if.h"
#include <string.h>
```

第 16-28 行的周期宏与静态量后追加：

```c
/* IMU 上报周期 (ms) */
#define UP_IMU_PERIOD_MS   50u

/* 上次 IMU 上报时刻 / 上次上报时的解析帧数（新鲜度门控） */
static uint32_t s_last_imu        = 0U;
static uint32_t s_last_imu_frames = 0U;
```

- [ ] **步骤 3：加浮点定标辅助与 `up_send_imu()`（放在 `up_send_status()` 之后）**

```c
/* 浮点 × 比例 → int16，四舍五入 + 饱和（不用 libm，避免链接 -lm） */
static int16_t up_imu_round_i16(float v, float scale)
{
    float s = v * scale;
    s += (s >= 0.0f) ? 0.5f : -0.5f;
    if (s >  32767.0f) s =  32767.0f;
    if (s < -32768.0f) s = -32768.0f;
    return (int16_t)s;
}

/* 封装并发送一帧 IMU（yaw / yaw_rate / roll / pitch，小端） */
static void up_send_imu(void)
{
    uint32_t frames = IMU_UART_GetFrameCount();
    if (frames == s_last_imu_frames)
        return;                     /* IMU 无新数据（未接/无帧）→ 不发，避免发零值骗 EKF */
    s_last_imu_frames = frames;

    float euler[3] = {0.0f, 0.0f, 0.0f};
    float gyro[3]  = {0.0f, 0.0f, 0.0f};
    IMU_GetEuler(euler);            /* {roll, pitch, yaw} 度 */
    IMU_GetGyro(gyro);              /* {gx, gy, gz} rad/s */

    int16_t yaw   = up_imu_round_i16(euler[2], 100.0f);              /* 0.01°  */
    int16_t rate  = up_imu_round_i16(gyro[2], 57.2957795f * 10.0f);  /* 0.1°/s */
    int16_t roll  = up_imu_round_i16(euler[0], 100.0f);
    int16_t pitch = up_imu_round_i16(euler[1], 100.0f);

    uint8_t p[8];
    p[0] = (uint8_t)(yaw & 0xFF);        p[1] = (uint8_t)((yaw >> 8) & 0xFF);
    p[2] = (uint8_t)(rate & 0xFF);       p[3] = (uint8_t)((rate >> 8) & 0xFF);
    p[4] = (uint8_t)(roll & 0xFF);       p[5] = (uint8_t)((roll >> 8) & 0xFF);
    p[6] = (uint8_t)(pitch & 0xFF);      p[7] = (uint8_t)((pitch >> 8) & 0xFF);

    up_send(UP_CMD_IMU, p, (uint8_t)sizeof(p));
}
```

- [ ] **步骤 4：`up_poll()` 加 20Hz 上报（第 186-195 行整段替换）**

```c
void up_poll(void)
{
    uint32_t now = HAL_GetTick();

    /* 心跳状态上报（100ms） */
    if (now - s_last_sts >= UP_STS_PERIOD_MS)
    {
        s_last_sts = now;
        up_send_status();
    }

    /* IMU 上报（50ms，仅当 IMU 有新解析帧） */
    if (now - s_last_imu >= UP_IMU_PERIOD_MS)
    {
        s_last_imu = now;
        up_send_imu();
    }
}
```

- [ ] **步骤 5：本地编译验证**

运行：
```powershell
cd D:\_project\Robot\stm32\control; cmake --build build/Debug
```
预期：无 error（若报 `IMU_UART_GetFrameCount` 未定义 → 确认 `imu.h` 已 include）。

- [ ] **步骤 6：协议文档升到 v1.1**

`docs/目标文档及说明/USB车控接口.md`：
- 第 3 行版本改为 `> 版本：v1.1 ｜ 日期：2026-09-10 ｜ …`
- 上行命令表（第 64 行附近 `0x82 STATUS` 后）加一行：

```markdown
| 上行 | `0x83` | `IMU` | 8 | IMU 姿态/角速度（周期 50ms，仅当 IMU 有新帧时发送） |
```

- 在 `### 6.2 STATUS 0x82` 之后新增小节：

```markdown
### 6.3 IMU `0x83`

`payload` 8 字节，小端：

| 偏移 | 字段 | 类型 | 单位 | 说明 |
|---|---|---|---|---|
| 0-1 | `yaw` | int16 | 0.01° | 偏航角（正=逆时针/左转，待台架标定符号） |
| 2-3 | `yaw_rate` | int16 | 0.1°/s | 绕 Z 轴角速度（正=逆时针） |
| 4-5 | `roll` | int16 | 0.01° | 仅诊断用 |
| 6-7 | `pitch` | int16 | 0.01° | 仅诊断用 |

- 周期 50ms（20Hz）。**仅当 STM32 已解析到新的 IMU 帧时才发送**：IMU 未接/无数据时
  不会有 `0x83` 帧，地瓜派侧据此判定"无 IMU"。
- 与 `STATUS` 相互独立，不参与 ACK / 不需应答。
- IMU 物理接口：STM32 USART3（PB10=TX / PB11=RX），115200 8N1，模块协议帧头 `0x7E 0x23`。
```

- [ ] **步骤 7：Commit**

```bash
git add stm32/control/Core/Inc/usb_proto.h stm32/control/Core/Src/usb_proto.c "docs/目标文档及说明/USB车控接口.md"
git commit -m "feat(stm32): USB 协议 v1.1 新增 IMU 上行帧 0x83（20Hz，带新鲜度门控）"
```

---

## 任务 3：ROS 侧协议解码 + 本地往返测试

**文件：**
- 修改：`ros2_car/src/robot_chassis/robot_chassis/usb_protocol.py`
- 修改：`ros2_car/tools/verify_odom_modes.py`

- [ ] **步骤 1：先写测试（此时必然失败）**

`verify_odom_modes.py` 顶部加路径与 import：

```python
CHASSIS_ROOT = os.path.abspath(os.path.join(HERE, '..', 'src', 'robot_chassis'))
sys.path.insert(0, CHASSIS_ROOT)
from robot_chassis.usb_protocol import (  # noqa: E402
    CMD_IMU,
    IMU_PAYLOAD_LEN,
    decode_imu,
)
```

新增函数并在 `main()` 末尾调用：

```python
def check_imu_protocol():
    """IMU(0x83) 帧契约：命令号、长度、编解码往返（与固件 usb_proto.c 一致）。"""
    import struct as _struct

    assert CMD_IMU == 0x83
    assert IMU_PAYLOAD_LEN == 8

    # 与固件 up_send_imu() 完全同构的组包：yaw 0.01° / rate 0.1°/s / roll,pitch 0.01°
    def pack(yaw_cdeg, rate_ddps, roll_cdeg, pitch_cdeg):
        return _struct.pack('<4h', yaw_cdeg, rate_ddps, roll_cdeg, pitch_cdeg)

    payload = pack(-12345, -250, 100, -300)
    yaw, rate, roll, pitch = decode_imu(payload)
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
```

- [ ] **步骤 2：运行确认失败**

运行：`cd ros2_car && python tools/verify_odom_modes.py`
预期：FAIL，`ImportError: cannot import name 'CMD_IMU'`

- [ ] **步骤 3：实现解码（`usb_protocol.py`）**

模块 docstring 第 14 行改为：

```python
- 上行: 0x81 ACK / 0x82 STATUS(26B payload) / 0x83 IMU(8B payload)
```

命令号区加：

```python
CMD_STATUS = 0x82
CMD_IMU = 0x83
```

长度常量区加：

```python
STATUS_PAYLOAD_LEN = 26
IMU_PAYLOAD_LEN = 8
```

`decode_status()` 之后新增：

```python
def decode_imu(payload: bytes):
    """解析 IMU(0x83) 的 8 字节 payload。

    布局（小端，与固件 usb_proto.c::up_send_imu 一致）:
        yaw      int16  0.01°    偏航角（正=逆时针/左转）
        yaw_rate int16  0.1°/s   绕 Z 角速度（正=逆时针）
        roll     int16  0.01°    仅诊断
        pitch    int16  0.01°    仅诊断

    :return: (yaw_deg, yaw_rate_dps, roll_deg, pitch_deg)
    """
    if len(payload) != IMU_PAYLOAD_LEN:
        raise ValueError(f"IMU payload 长度错误: {len(payload)}，应为 {IMU_PAYLOAD_LEN}")
    yaw, rate, roll, pitch = struct.unpack_from("<4h", payload, 0)
    return yaw / 100.0, rate / 10.0, roll / 100.0, pitch / 100.0
```

- [ ] **步骤 4：运行确认通过**

运行：`cd ros2_car && python tools/verify_odom_modes.py`
预期：PASS，新增一行 `[ok] IMU(0x83) 帧契约与编解码往返`

- [ ] **步骤 5：Commit**

```bash
git add ros2_car/src/robot_chassis/robot_chassis/usb_protocol.py ros2_car/tools/verify_odom_modes.py
git commit -m "feat(ros2): usb_protocol 支持 IMU 上行帧 0x83 解码（含本地往返测试）"
```

---

## 任务 4：`chassis_driver` 发布 `/imu`

**文件：**
- 修改：`ros2_car/src/robot_chassis/robot_chassis/chassis_driver.py`
- 修改：`ros2_car/src/robot_chassis/config/chassis_params.yaml`

- [ ] **步骤 1：加 import 与解码器**

```python
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Imu
from std_msgs.msg import Bool
from tf2_ros import TransformBroadcaster
from geometry_msgs.msg import TransformStamped
```

```python
from .usb_protocol import (
    CMD_IMU,
    CMD_STATUS,
    build_set_car_vel,
    build_stop,
    decode_imu,
    decode_status,
    FrameParser,
)
```

- [ ] **步骤 2：加参数声明**

在 `send_period`/`watchdog_timeout` 声明之后加：

```python
        # ---- IMU（STM32 USART3 转发，0x83 帧 → /imu）----
        self.declare_parameter("publish_imu", True)
        self.declare_parameter("imu_topic", "/imu")
        self.declare_parameter("imu_frame_id", "imu_link")
        # 符号标定：手托车体逆时针转，yaw/angular_velocity.z 应增大；反了改 -1
        self.declare_parameter("imu_sign_yaw", 1.0)
        self.declare_parameter("imu_sign_wz", 1.0)
        # 协方差（必须非零：robot_localization 对 0 方差只加 1e-6 = 完全信任）
        self.declare_parameter("imu_yaw_var", 0.02)   # rad^2
        self.declare_parameter("imu_wz_var", 0.01)    # (rad/s)^2
```

并把它们加进 `get_parameters([...])` 的显式列表（Humble 没有 `get_parameter_names()`）：

```python
        self._p = {p.name: p.value for p in self.get_parameters([
            "serial_port", "baudrate", "cmd_vel_topic", "odom_topic", "cmd_stop_topic",
            "odom_frame_id", "base_frame_id", "publish_tf", "wheel_radius", "rotate_radius",
            "wheel_signs", "sign_vx", "sign_vy", "sign_wz",
            "max_vx", "max_vy", "max_wz", "accel_limit", "ang_accel_limit",
            "send_period", "watchdog_timeout",
            "publish_imu", "imu_topic", "imu_frame_id",
            "imu_sign_yaw", "imu_sign_wz", "imu_yaw_var", "imu_wz_var",
        ])}
```

- [ ] **步骤 3：加发布器（在 `self._odom_pub` 之后）**

```python
        self._imu_pub = None
        if self._p["publish_imu"]:
            self._imu_pub = self.create_publisher(Imu, self._p["imu_topic"], 10)
```

- [ ] **步骤 4：分发新帧（`_process_upstream()`）**

```python
            if cmd == CMD_STATUS:
                self._on_status(payload)
            elif cmd == CMD_IMU:
                self._on_imu(payload)
            # ACK 忽略（仅日志级信息可扩展）
```

- [ ] **步骤 5：加 `_on_imu()`（放在 `_on_status()` 之后）**

```python
    def _on_imu(self, payload):
        if self._imu_pub is None:
            return
        try:
            yaw_deg, rate_dps, _roll_deg, _pitch_deg = decode_imu(payload)
        except ValueError as e:
            self.get_logger().warn(f"IMU 解析失败: {e}")
            return

        yaw = math.radians(yaw_deg) * self._p["imu_sign_yaw"]
        wz = math.radians(rate_dps) * self._p["imu_sign_wz"]

        m = Imu()
        m.header.stamp = self.get_clock().now().to_msg()
        m.header.frame_id = self._p["imu_frame_id"]
        # 只有 yaw 可信：roll/pitch 方差给大值（不融合，按 RL 惯例而非靠 config 之外的手段）
        m.orientation.z = math.sin(yaw / 2.0)
        m.orientation.w = math.cos(yaw / 2.0)
        m.orientation_covariance[0] = 1e6
        m.orientation_covariance[4] = 1e6
        m.orientation_covariance[8] = self._p["imu_yaw_var"]
        m.angular_velocity.z = wz
        m.angular_velocity_covariance[0] = 1e6
        m.angular_velocity_covariance[4] = 1e6
        m.angular_velocity_covariance[8] = self._p["imu_wz_var"]
        m.linear_acceleration_covariance[0] = -1.0   # 未提供加速度（REP 约定）
        self._imu_pub.publish(m)
```

- [ ] **步骤 6：参数文件补段（`chassis_params.yaml`，`watchdog_timeout` 之后）**

```yaml
    # ---- IMU（STM32 USART3 转发 → 0x83 帧 → /imu，见 docs/目标文档及说明/USB车控接口.md v1.1）----
    publish_imu: true
    imu_topic: /imu
    imu_frame_id: imu_link
    # 符号标定：手托车体逆时针转，/imu 的 yaw 与 angular_velocity.z 应增大；反了改 -1
    imu_sign_yaw: 1.0
    imu_sign_wz: 1.0
    imu_yaw_var: 0.02     # rad^2（必须非零，否则 robot_localization 当 1e-6 完全信任）
    imu_wz_var: 0.01      # (rad/s)^2
```

- [ ] **步骤 7：语义验证 + Commit**

运行：
```powershell
cd D:\_project\Robot; python -m py_compile ros2_car\src\robot_chassis\robot_chassis\chassis_driver.py; .venv\Scripts\python.exe ros2_car\tools\verify_odom_modes.py
```
预期：py_compile 无输出；校验脚本 `全部通过`

```bash
git add ros2_car/src/robot_chassis/robot_chassis/chassis_driver.py ros2_car/src/robot_chassis/config/chassis_params.yaml
git commit -m "feat(ros2): chassis_driver 解析 IMU 帧并发布 /imu（非零协方差 + 符号可标定）"
```

---

## 任务 5：URDF 加 `imu_link`

**文件：**
- 修改：`ros2_car/src/robot_bringup/urdf/car.urdf`

- [ ] **步骤 1：在 `</robot>` 之前插入**

```xml
  <!-- IMU（接 STM32 USART3，装配假设：平放、轴向与车体对齐、位置可忽略 → 与 base_link 重合）
       装歪了只改这一行 origin（rpy 用弧度） -->
  <link name="imu_link"/>

  <joint name="imu_joint" type="fixed">
    <parent link="base_link"/>
    <child link="imu_link"/>
    <origin xyz="0.0 0.0 0.0" rpy="0 0 0"/>
  </joint>
```

- [ ] **步骤 2：验证 XML 合法 + Commit**

运行：
```powershell
cd D:\_project\Robot; python -c "import xml.etree.ElementTree as ET; ET.parse(r'ros2_car/src/robot_bringup/urdf/car.urdf'); print('URDF XML OK')"
```
预期：`URDF XML OK`

```bash
git add ros2_car/src/robot_bringup/urdf/car.urdf
git commit -m "feat(ros2): URDF 新增 imu_link 静态 TF（装配假设已注明）"
```

---

## 任务 6：EKF 加 `imu0` + 校验脚本 + README

**文件：**
- 修改：`ros2_car/src/robot_bringup/config/ekf_params.yaml`
- 修改：`ros2_car/tools/verify_odom_modes.py`
- 修改：`ros2_car/README.md`

- [ ] **步骤 1：扩展校验脚本（此时必然失败）**

`check_ekf_yaml()` 内、`process_noise_covariance` 断言之后加：

```python
    imu0 = _array(text, 'imu0_config')
    assert len(imu0) == 15, 'imu0_config 必须 15 个布尔，实际 %d' % len(imu0)
    assert imu0[11] == 'true', 'imu0 只融 vyaw（index 11）'
    assert imu0[:11] == ['false'] * 11 and imu0[12:] == ['false'] * 3, \
        'imu0 只融 vyaw，其它位必须 false（避免与 rf2o 的 yaw 两个朝向源打架）'
    assert re.search(r'imu0:\s*/imu\s*$', text, re.M)
    assert re.search(r'imu0_differential:\s*false', text)
```

- [ ] **步骤 2：运行确认失败**

运行：`cd ros2_car && python tools/verify_odom_modes.py`
预期：FAIL，报 `ekf_params.yaml 缺少 imu0_config`

- [ ] **步骤 3：`ekf_params.yaml` 加 `imu0` 块（`odom1_queue_size` 之后）**

```yaml
    # 输入3：IMU（接 STM32 USART3，经 USB 帧 0x83 转发到 /imu）
    # 只融 vyaw：麦轮原地转打滑时，陀螺仪测的角速度仍然可信；绝对 yaw 不融
    # （rf2o 位姿已在提供朝向，两个朝向源协方差不准时会互相打架）
    imu0: /imu
    imu0_config: [false, false, false,
                  false, false, false,
                  false, false, false,
                  false, false, true,
                  false, false, false]
    imu0_differential: false
    imu0_queue_size: 10
```

- [ ] **步骤 4：运行确认通过（两种 python 都跑）**

运行：
```powershell
cd D:\_project\Robot; python ros2_car\tools\verify_odom_modes.py; .venv\Scripts\python.exe ros2_car\tools\verify_odom_modes.py
```
预期：两次都 `全部通过`（.venv 那次多一行 PyYAML 严格解析）

- [ ] **步骤 5：README 补 IMU 小节（放在「双源里程计融合」之后）**

````markdown
## IMU（接 STM32，进 EKF）

- 硬件：IMU 接 STM32 的 `USART3`（PB10=TX / PB11=RX，115200 8N1）；STM32 以 20Hz 上行
  协议帧 `0x83`（yaw / yaw_rate / roll / pitch），`chassis_driver` 转成 `/imu`，EKF `imu0` 只融 `vyaw`
- IMU 未接/无数据时固件不发 `0x83`（新鲜度门控），此时 `/imu` 不存在 —— 属正常，不是故障
- 校验：
  ```bash
  ros2 topic hz /imu                 # ≈ 20Hz
  ros2 topic echo /imu --once       # 协方差必须非零
  ```
- 符号标定：手托车体**逆时针**转 → `/imu` 的 `orientation` 与 `angular_velocity.z` 应增大；
  不对改 `chassis_params.yaml` 的 `imu_sign_yaw` / `imu_sign_wz`
- 打滑验收：车架空、四轮离地发 `cmd_vel` 空转 → `/odom` 的 yaw 疯长，`/imu` 基本不动，
  `/odom_filtered` 明显比 `/odom` 稳
- 回退：`ekf_params.yaml` 删掉 `imu0` 三行即回到纯 rf2o+轮速融合
````

- [ ] **步骤 6：总校验 + Commit**

运行：
```powershell
cd D:\_project\Robot
.venv\Scripts\python.exe ros2_car\tools\verify_odom_modes.py
cmake --build stm32\control\build\Debug
python -m py_compile ros2_car\src\robot_chassis\robot_chassis\chassis_driver.py ros2_car\src\robot_chassis\robot_chassis\usb_protocol.py
```
预期：校验 `全部通过`；固件 build 无 error；py_compile 无输出

```bash
git add ros2_car/src/robot_bringup/config/ekf_params.yaml ros2_car/tools/verify_odom_modes.py ros2_car/README.md
git commit -m "feat(ros2): EKF 增加 imu0（只融 vyaw）+ README IMU 标定验收小节"
```

---

## 台架与板卡验证（用户在硬件侧执行，不属于代码任务）

- [ ] 烧录固件（VS Code `control Debug.launch` / ST-Link；注意 `build/Debug/control.elf` 与遗留 `Debug/control.elf` 不是同一份）
- [ ] 上电后看 OLED/串口：IMU 接上 → 20Hz 出现 `AA 55 08 83 …`；拔掉 IMU → `0x83` 消失
- [ ] 板卡：`ros2 topic hz /imu` ≈ 20Hz；`ros2 topic echo /imu --once` 协方差非零
- [ ] 符号标定（逆时针 → yaw/角速度增大），不对改 `imu_sign_yaw` / `imu_sign_wz`
- [ ] 架空空转打滑实验：记录 10 秒 `/odom` 与 `/odom_filtered` 的 yaw 差；必要时调小 `imu_wz_var`
- [ ] `ros2 topic echo /diagnostics` 确认 EKF 的 `imu0` 有数据
- [ ] 完整导航到点一次，确认 yaw 不再丢位
