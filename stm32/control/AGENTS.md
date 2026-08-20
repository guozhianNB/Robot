# AGENTS.md

STM32F103ZETX 四轮麦克纳姆轮小车控制工程（STM32CubeMX 生成骨架 + CMake 构建 + HAL 库）。
业务代码集中在 `Core/Src/` 的用户文件（`motor.c` / `motor_control.c` / `usb_proto.c`），其余大部分文件由 CubeMX 自动生成。

## 构建与烧录

- 构建系统：**CMake (≥3.22) + Ninja**，presets 见 `CMakePresets.json`（`Debug` / `Release`，输出到 `build/<preset>/`）。
- 工具链：`arm-none-eabi-gcc`（通过 `cmake/gcc-arm-none-eabi.cmake` 指定，需在 PATH）。
- 产物：`build/Debug/control.elf`，POST_BUILD 自动 objcopy 生成 `control.bin`。
- 烧录/调试：`control Debug.launch`（ST-Link / SWD / `connect_under_reset`，gdb 端口 61234）。VS Code 按 F5 走该配置。
- ⚠️ 注意存在两套输出：`build/`（CMake+Ninja，**活动构建**）和 `Debug/`（CubeIDE make 遗留产物）。launch 文件指向 `Debug/control.elf`，与 CMake 产物不是同一个文件——改代码后要确认烧的是哪一份。

## 架构与模块

```
Core/Src/
  main.c          入口 + 时钟(72MHz) + 主循环（10ms 调 up_poll + mc_update_all）
  motor.c         电机驱动 + 编码器恒速 PID（中文注释，含 ASCII 框图设计文档）
  motor_control.c 麦克纳姆整车控制：mc_car_set(vx,vy,w)（X 型，mm/s 与 0.1°/s）
  usb_proto.c     USB CDC 帧协议（二进制帧 [AA][55][len][cmd][payload][xor]，命令号 0x0x 下行/0x8x 上行）
  gpio.c          方向引脚 PB12~15 / PD8~11（CubeMX 生成）
  tim.c           htim1=PWM(1kHz, 四通道 PE9/11/13/14)，htim2/3/4/5=编码器，htim8=未使用（CubeMX 生成）
  usb.c           仅初始化 PCD（CubeMX 生成）；实际通信走 USB_DEVICE/App/usbd_cdc_if.c（CDC 虚拟串口）
Drivers/          CMSIS + STM32F1xx_HAL_Driver（HAL 库，勿动）
USB_DEVICE/       ST USB 设备库（usbd_cdc_if.c 提供 CDC_Transmit_FS，接收回调喂 usb_proto）
```

数据流：USB 收帧 → `up_on_rx(buf,len)`（usb_proto 拼包/找帧头/XOR 校验）→ `mc_car_set(vx,vy,w)`（麦克纳姆逆解）→ `motor_speed_control(way, target_rpm)`（PID 闭环）→ `control_motor()`（方向 GPIO + TIM1 PWM）；上行心跳由 `up_poll()`（100ms）发 STATUS。

> USB 车控帧协议详见 `docs/2.pre/USB车控接口.md` + 地瓜派端示例 `docs/2.pre/usb_chassis_demo.py`。**改动协议/命令号前先读该文档。**

## 关键约束（改动前必读）

1. **新增 .c 文件必须注册进根 `CMakeLists.txt` 的 `target_sources`**（头文件加 `target_include_directories`）。目前已显式列出 `Core/Src/motor.c`、`Core/Src/motor_control.c`、`Core/Src/usb_proto.c`（显式列表非 glob）。漏加不报错也不链接——最容易踩的坑。
2. **CubeMX 生成文件勿手改**：`gpio/tim/usb/stm32f1xx_it/stm32f1xx_hal_msp/sysmem/syscalls/main` 及 `Core/Inc/*.h`、`cmake/stm32cubemx/CMakeLists.txt`、`control.ioc`、启动文件和链接脚本。重新生成 `.ioc` 会覆盖它们（`KeepUserCode` 只保留 `USER CODE` 段）。业务代码放 `motor.c` 或新建用户文件。
3. **10ms 控制周期是硬约束**：`motor_speed_control` 用 `HAL_GetTick` 实测 dt 换算 RPM。主循环长时间阻塞（dt > 200ms）会回退默认 10ms，导致转速高估、PID 反复正反转"原地发抖"。**不要在循环里加长阻塞**。
4. **`contorl_car` 是拼写错误但已是现有 API**（"contorl" 而非 "control"）。不要"顺手修正"它，否则破坏所有调用点。
5. **时钟联动**：改 PLL/时钟会破坏 USB 48MHz（PLL/1.5）；TIM1 Prescaler=71、Period=1000 才有 ~1kHz PWM（Period=65535 时仅 15Hz，电机只发抖不转）。
6. **编码器参数**：`MOTOR_ENCODER_CPR=330.0f` 与 `MOTOR_WHEEL_DIAMETER_MM=80.0f`（`motor.c` 顶部）需按实际硬件标定；方向反了改 `motor_enc_sign[]` 为 `-1`。
7. **内存很小**：Stack=0x400、Heap=0x200，避免 malloc 重代码。
8. **调试**：F5 烧录失败先查残留的 `ST-LINK_gdbserver` 僵尸进程是否占用 61234 端口。

## 代码约定

- 用户代码函数前缀：`motor_*` 电机/PID（`set_motor` / `control_motor` / `motor_speed_control`）、`mc_*` 整车运动学（`mc_car_set`）、`up_*` USB 协议（`up_on_rx` / `up_poll`）；`way` = 电机号 1~4。CubeMX 代码用 HAL 规范（`MX_*_Init`、`htimX`、`Error_Handler`）。
- 用户新增注释一律**中文**（CubeMX 样板注释为英文）。
- 风格：2 空格缩进、K&R 花括号、宏全大写蛇形、使用 `stdbool.h` 的 `bool`。
- 自定义头文件保护宏：`__MOTOR_H__` 风格（`#ifndef __XXX_H__`）。
- 在线调 PID：`motor_pid_tune(kp, ki, kd)`（传 0 表示不修改）。
