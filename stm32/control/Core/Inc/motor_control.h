#ifndef __MOTOR_CONTROL_H__
#define __MOTOR_CONTROL_H__

#include "main.h"
#include <stdbool.h>
#include <stdint.h>

/* ==================== 单电机恒速 PID 闭环层（motor_control） ====================
 * 依赖 motor_driver（编码器/PWM/方向原语），实现每个电机的恒速闭环。
 * 只做闭环控制，不含堵转保护 —— 上层按需实现。
 *
 * 电机编号 way = 1~4（与 motor_driver 一致）：
 *   way 1 = 左前轮（编码器 TIM2）  way 2 = 右前轮（编码器 TIM3）
 *   way 3 = 左后轮（编码器 TIM4）  way 4 = 右后轮（编码器 TIM5）
 *
 * 用法：
 *   mc_init();                         // 启动前初始化一次
 *   主循环每 10ms 调一次 mc_update_all(); // 四电机闭环
 *   需要控制某电机时调用 mc_set_target(way, rpm);
 * ==================================================================== */

/* 初始化：清零四个电机的闭环状态。main 中外设与 md_init() 之后调用一次 */
void mc_init(void);

/* 设置目标转速（RPM，>0 正转 / <0 反转 / 0 制动）。
 * 设置后由 mc_update_all() 周期性闭环维持该转速；
 * 目标 0 时立即制动并清积分 */
void mc_set_target(int way, int target_rpm);

/* 读取指定电机最近一次计算的实际转速 (RPM) */
int mc_get_speed_rpm(int way);

/* 在线调整全局 PID 参数（传 0 表示不修改对应项，所有电机共享） */
void mc_pid_tune(float kp, float ki, float kd);

/* 每个控制周期（建议 10ms）调用一次：对四个电机执行
 * 读编码器 → 换算 RPM → PID → 输出 PWM 的完整闭环 */
void mc_update_all(void);

#endif /* __MOTOR_CONTROL_H__ */
