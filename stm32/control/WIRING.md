# STM32F103ZETX 麦克纳姆轮小车 —— 接线图与接线逻辑核对

> 本文档根据工程代码（`motor.c` / `tim.c` / `gpio.c` / `main.h` / `stm32f1xx_hal_msp.c` / `control.ioc`）反推整理。
> 最后核对时间：2026-08-07。若改过 CubeMX 引脚或重排线，请同步更新本文档。

---

## 1. 总览

```
                            STM32F103ZETX
┌──────────────────────────────────────────────────────────────┐
│  TIM1 (PWM, 1kHz)    TIM2/3/4/5 (编码器模式)    GPIO (方向)   │
│  PE9  CH1 ──► 电机A PWM  PA15/PB3 ──► 电机A 编码器  PB12/13 ──► 电机A IN1/IN2 │
│  PE11 CH2 ──► 电机B PWM  PA6/PA7  ──► 电机B 编码器  PB14/15 ──► 电机B IN1/IN2 │
│  PE13 CH3 ──► 电机C PWM  PD12/PD13 ──► 电机C 编码器  PD8/9  ──► 电机C IN1/IN2 │
│  PE14 CH4 ──► 电机D PWM  PA0/PA1  ──► 电机D 编码器  PD10/11 ──► 电机D IN1/IN2 │
└──────────────────────────────────────────────────────────────┘
        │                              │
        ▼                              ▼
  电机驱动板（4×H 桥，IN1/IN2 + PWM）    带霍尔/光电编码器的直流减速电机
```

**电机布局**（俯视，车头朝上）：

| 电机号 | 名称 | 位置 |
|---|---|---|
| 1 | 电机 A | **左前轮** |
| 2 | 电机 B | **右前轮** |
| 3 | 电机 C | **左后轮** |
| 4 | 电机 D | **右后轮** |

---

## 2. 接线表

### 2.1 PWM 输出 —— TIM1（~1kHz，四通道**全重映射**到 PE 口）

| 电机 | TIM 通道 | STM32 引脚 | 说明 |
|---|---|---|---|
| A（左前） | TIM1_CH1 | **PE9** | `__HAL_AFIO_REMAP_TIM1_ENABLE()` |
| B（右前） | TIM1_CH2 | **PE11** | ↑ |
| C（左后） | TIM1_CH3 | **PE13** | ↑ |
| D（右后） | TIM1_CH4 | **PE14** | ↑ |

> ⚠️ TIM1 不是默认引脚 PA8~PA11，而是**全重映射**到 PE9/11/13/14。若按默认复用接 PA 口将不输出。
> PWM 参数：Prescaler=71、Period=1000 → 1kHz；占空比量程 0~1000（1000=100%）。

### 2.2 方向引脚 —— GPIO 推挽输出（接 H 桥 IN1/IN2）

| 电机 | IN1（正转） | IN2（反转） | 组 |
|---|---|---|---|
| A（左前） | **PB12** (AIN1) | **PB13** (AIN2) | GPIOB |
| B（右前） | **PB14** (BIN1) | **PB15** (BIN2) | GPIOB |
| C（左后） | **PD8** (CIN1) | **PD9** (CIN2) | GPIOD |
| D（右后） | **PD10** (DIN1) | **PD11** (DIN2) | GPIOD |

> 命名 AIN/BIN/CIN/DIN 是典型双 H 桥模块（TB6612 / L298N 类）接法。
> 制动逻辑：`IN1=IN2=高` 为短接制动（brake=true）。

### 2.3 编码器反馈 —— TIM2/3/4/5 编码器模式（TI1 计数）

| 电机 | 定时器 | **A 相 → CH1 (TI1)** | **B 相 → CH2 (TI2)** | 重映射 |
|---|---|---|---|---|
| A（左前） | TIM2 | **PA15** | **PB3** | 部分重映射 1 |
| B（右前） | TIM3 | **PA6** | **PA7** | 无 |
| C（左后） | TIM4 | **PD12** | **PD13** | 全重映射 |
| D（右后） | TIM5 | **PA0** | **PA1** | 无 |

> - 编码器模式 `TIM_ENCODERMODE_TI1`：只统计 CH1（A 相）上升沿，方向由 A/B 相位差判断。
> - 接反 A/B 相只会方向反（可由 `motor_enc_sign[]` 修正）；**务必确保 A 相接 CH1**。
> - PA15/PB3 原是 JTAG 引脚，代码已 `__HAL_AFIO_REMAP_SWJ_NOJTAG()` 释放，**调试只能用 SWD，不能接 JTAG**。

### 2.4 电源与调试

| 项 | 说明 |
|---|---|
| 电源 | 驱动板与逻辑板共地（GND 必须相连），电机电源按驱动模块要求（L298N 类 6~12V，TB6612 类 2.7~13.5V） |
| 调试 | ST-Link **SWD**（SWDIO/SWCLK/GND），端口 61234，`connect_under_reset` |
| USB | PA11/PA12 已初始化 PCD 但无库/描述符，**不可用**，勿接 |

---

## 3. 逻辑核对结论（已逐行核对代码）

| 检查项 | 结果 |
|---|---|
| TIM1 PWM 引脚 PE9/11/13/14 + 全重映射 | ✅ 与 `tim.c` `HAL_TIM_MspPostInit` 一致 |
| 方向引脚 PB12~15 / PD8~11 | ✅ 与 `gpio.c` / `main.h` 一致 |
| 编码器引脚（TIM2=PA15/PB3、TIM3=PA6/7、TIM4=PD12/13、TIM5=PA0/1） | ✅ 与 `tim.c` / `control.ioc` 一致 |
| 麦克纳姆逆运动学公式 | ✅ 标准 **X 型**布置公式（见 §4） |
| 电机 ↔ 定时器映射（1→TIM2, 2→TIM3, 3→TIM4, 4→TIM5） | ✅ 与 `motor_enc_tim[]` 一致 |
| 编码器 CPR=330、轮径 80mm | ✅ 见 `motor.c` 顶部（需按实物标定） |

---

## 4. 麦克纳姆轮运动学（`contorl_car`）

输入 `(f_speed 前进, l_speed 左移, rotate_speed 旋转)`，单位 mm/s：

```c
wheel1(左前) = f + l + r
wheel2(右前) = f − l − r
wheel3(左后) = f − l + r
wheel4(右后) = f + l − r
```

这是 **X 型辊子布置**的标准逆运动学公式（已核对）。

- ⚠️ **假设**：实物必须是 X 型麦克纳姆轮（辊子与轮轴成 45°，V 形朝前）。
  若实物是 **O 型** 或辊子方向相反，`l`、`r` 两项的符号需全部取反。
- 轮速换算：`RPM = 线速度 × 60 / (π × 轮径)`，轮径在 `MOTOR_WHEEL_DIAMETER_MM` 配置。

---

## 5. 发现的疑点 / 待处理项（按优先级）

### 🔴 P1：TIM5 预分频与其他编码器定时器不一致（`control.ioc` / `tim.c`）

```c
htim2.Init.Prescaler = 0;      // 电机A
htim3.Init.Prescaler = 0;      // 电机B
htim4.Init.Prescaler = 0;      // 电机C
htim5.Init.Prescaler = 2-1;    // 电机D ← 唯一不是 0 的！
```

TIM5（电机 D，右后轮）Prescaler=1。若编码器模式下预分频生效，**电机 D 的编码器计数会减半 → 测得 RPM 减半 → PID 输出加倍 → 右后轮转速异常**。

**处理**：在 CubeMX 里把 TIM5 的 Prescaler 改回 0 后重新生成（`tim.c` 是生成文件，勿手改，否则会被覆盖）。

### 🟡 P2：主循环当前是"开环直驱"状态，闭环演示被注释

`main.c` 主循环当前每 10ms 调用 `set_pwm_directly(500)` —— 四路同时输出 50% 占空比、方向全正转，**绕过 PID 闭环**，用于开环验证接线。
原 `contorl_car(300/0/0)` 演示状态机已被注释。若要做闭环调试，需恢复 `motor_speed_control` / `contorl_car` 调用。

### 🟡 P3：编码器方向符号与旋转正方向需实物验证

- `motor_enc_sign[]` 目前全为 `1`：若某电机转向与目标相反，改为 `-1`（或对调该电机 A/B 两相）。
- 代码注释把 `rotate_speed>0` 描述为"顺时针旋转"，而运动学标准约定通常为逆时针——实际旋转方向以实物为准。

### 🟢 P4：参数标定

- `MOTOR_ENCODER_CPR = 330.0f`：让电机空转 N 圈，累计计数 ÷ N 标定。
- `MOTOR_WHEEL_DIAMETER_MM = 80.0f`：按实际轮径修改（常见 60/80/97mm）。

---

## 6. 上电前接线核对清单

- [ ] PWM 接 PE9/PE11/PE13/PE14（不是 PA8~PA11）
- [ ] 方向引脚：A=PB12/13、B=PB14/15、C=PD8/9、D=PD10/11
- [ ] 编码器 A 相接 CH1（PA15/PA6/PD12/PA0），B 相接 CH2（PB3/PA7/PD13/PA1）
- [ ] 电机 1↔左前、2↔右前、3↔左后、4↔右后（与 `contorl_car` 分配一致）
- [ ] 驱动板与 MCU 共地
- [ ] ST-Link 用 SWD 模式（PA15/PB3 已让给编码器，JTAG 不可用）
- [ ] TIM5 Prescaler 已改回 0（见 §5 P1）
- [ ] 上电后先用 `set_pwm_directly` 开环确认四轮都能转、方向一致
