# motor_control 单电机恒速 PID 闭环层 — 设计文档

日期：2026-08-08
状态：已批准（用户确认：新建 `motor_control.c/.h`、目标单位 RPM、PID 参数全局共享一组、纯 PID 不含堵转保护）

## 背景与目标

`motor_driver.c` 提供编码器/PWM/方向原语（不含 PID），`motor（废弃）.c` 的旧闭环实现已被重命名弃用。
需要一个"输入目标转速即可自动闭环跟踪"的 PID 控制层，替代旧实现。

## 关键决策（用户已确认）

| 决策点 | 结论 |
| ------ | ---- |
| 文件位置 | 新建 `Core/Src/motor_control.c` + `Core/Inc/motor_control.h` |
| 目标单位 | RPM（每电机独立指定，与旧 `motor_speed_control` 一致） |
| PID 参数 | **全局共享一组** Kp/Ki/Kd，`mc_pid_tune()` 在线修改 |
| 堵转保护 | 不含（纯 PID），后续需要再加 |

## 接口

```c
void mc_init(void);                            /* 清零四电机状态，启动前调用一次 */
void mc_set_target(int way, int target_rpm);   /* 设置目标转速，持续跟踪 */
int  mc_get_speed_rpm(int way);                /* 读取实际转速 (RPM) */
void mc_pid_tune(float kp, float ki, float kd);/* 在线调参，传 0 表示不修改 */
void mc_update_all(void);                      /* 每 10ms 调用一次，四电机闭环 */
```

用法：`mc_set_target(1, 200)` → 主循环每 10ms 调 `mc_update_all()` → 自动闭环维持 200 RPM。

## 数据隔离（"电机之间参数不混乱"）

- 运行状态（积分/上次误差/时间戳/目标等）存于 `mc_mot[4]` 结构体数组，**按 `way` 索引存取**，天然隔离，互不串扰。
- PID 参数 `g_pid_kp/g_pid_ki/g_pid_kd` 全局共享一组。

```c
typedef struct {
    float    integral;    /* 积分项     —— 每电机独立 */
    float    last_error;  /* 上次误差   —— 每电机独立 */
    uint32_t last_tick;   /* 上次时间戳（算 dt）      */
    int      target;      /* 当前目标转速             */
    int      last_target; /* 上次目标（方向切换检测）  */
    float    rpm;         /* 最近实际转速             */
    bool     inited;      /* 微分首拍标志             */
} McMotor;
```

## 控制流程（`mc_update_all` 内，每电机）

1. `md_get_encoder_delta(way)` 读脉冲增量（驱动层已处理 16 位回绕 + 方向符号）。
2. dt 用 `HAL_GetTick` 实测，异常（<1ms 或 >200ms）回退 10ms —— 遵守 AGENTS.md 硬约束，避免转速高估。
3. 转速换算：`rpm = delta × 60 / (CPR × dt)`，`MC_ENCODER_CPR = 330`。
4. `target == 0` → `md_set_motor(way, 0)` 制动 + 清积分 + 重置首拍。
5. 方向翻转（`target` 与 `last_target` 符号不同）→ 清积分，避免旧方向积分过冲。
6. 位置式 PID：
   - `err = target - rpm`
   - `der = (err - last_error) / dt`（首拍置 0）
   - `output = Kp·err + Ki·integral + Kd·der`
   - **抗积分饱和**：输出触及 ±1000 限幅且误差同向时冻结积分累积（条件积分，避免旧实现"无条件积分→饱和"的坑）
   - 积分项钳位 ±`MC_PID_INTEGRAL_LIMIT`（150），输出钳位 ±1000
7. `md_set_motor(way, (int)output)` 输出（带符号，0 制动）。

## 默认参数

```
MC_PID_KP = 0.8   MC_PID_KI = 2.0   MC_PID_KD = 0.0
MC_PID_INTEGRAL_LIMIT = 150.0   MC_PID_OUTPUT_LIMIT = 1000.0
```

## 构建配置

- 根 `CMakeLists.txt` `target_sources` 添加 `Core/Src/motor_control.c`。
- **移除失效引用** `Core/Src/motor.c`（文件已被重命名为 `motor（废弃）.c`，不存在，不移除构建必失败）。

## 不做的事（YAGNI）

- 不含堵转保护、换向"踢一脚"、整车麦轮运动学 —— 上层/后续按需实现。
