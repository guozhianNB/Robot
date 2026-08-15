#ifndef __MOTOR_H__
#define __MOTOR_H__

#include "main.h"
#include "tim.h"
#include "gpio.h"
#include <stdbool.h>
#include <stdint.h>

/* ==================== 基础电机驱动 ==================== */

/* 初始化 PWM（启动 TIM1 四通道） */
void motor_init_pwm(void);

/* 初始化电机 */
void motor_init(void);

/* 开环设置 PWM 占空比（0~1000，1000 = 100%），不控制方向 */
void set_motor(int way, int speed);

/* 直接设置 PWM 占空比（0~1000，1000 = 100%），不控制方向 */
void set_pwm_directly(int speed);

/* 方向 + PWM 控制。brake=true 短接制动；否则按 speed 符号设方向并输出 |speed| */
void control_motor(int way, int speed, bool brake);

/* 整车控制（麦克纳姆轮，输入速度单位 mm/s）：
 *   f_speed / l_speed / rotate_speed 均为 mm/s，
 *   内部换算成 RPM 后进入四轮恒速闭环 */
void control_car(float f_speed, float l_speed, float rotate_speed);

/* ==================== 编码器恒速闭环（单电机） ==================== */

/* 启动编码器定时器（TIM2/3/4/5 编码器模式），在 motor_init 后调用一次 */
void motor_encoder_init(void);

/* 编码器方向自动标定：逐个电机开环正转 500ms 后按原始计数方向写 motor_enc_sign。
 * 在 motor_encoder_init 之后、闭环前调用一次（自检/首次调试用，会短转四个电机） */
void motor_encoder_sign_autocal(void);

/* 读取指定电机最近一次计算的实际转速 (RPM) */
int motor_get_speed_rpm(int way);

/* 车级堵转查询：任一电机处于堵转制动保持期返回 true（供 control_car 整车保护） */
bool motor_any_stalled(void);

/* 单电机恒速闭环：每个控制周期（建议 10ms）调用一次。
 * way = 1~4；target_rpm > 0 正转，< 0 反转，= 0 制动。
 * 返回当前实际转速 (RPM)。 */
int motor_speed_control(int way, int target_rpm);

/* 在线调整 PID 参数（传 0 或负值表示不修改对应项） */
void motor_pid_tune(float kp, float ki, float kd);

#endif /* __MOTOR_H__ */