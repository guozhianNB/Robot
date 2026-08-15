#ifndef __MOTOR_DRIVER_H__
#define __MOTOR_DRIVER_H__

#include "main.h"
#include "tim.h"
#include "gpio.h"
#include <stdbool.h>
#include <stdint.h>

/* ==================== 基础电机驱动（motor_driver） ====================
 * 只做三件事：编码器读取、PWM 输出、正反转控制。
 * 不含 PID / 堵转保护 / 速度换算 —— 全部由上层负责。
 *
 * 电机编号 way = 1~4：
 *   way 1 = 左前轮（PWM TIM1_CH1/PE9，  方向 PB12/13，编码器 TIM2）
 *   way 2 = 右前轮（PWM TIM1_CH2/PE11， 方向 PB14/15，编码器 TIM3）
 *   way 3 = 左后轮（PWM TIM1_CH3/PE13， 方向 PD8/9，  编码器 TIM4）
 *   way 4 = 右后轮（PWM TIM1_CH4/PE14， 方向 PD10/11，编码器 TIM5）
 * ==================================================================== */

/* 初始化：启动 TIM1 四通道 PWM（初始占空比 0）+ TIM2/3/4/5 编码器模式，
 * 清零全部方向引脚与编码器计数。main 中外设初始化后调用一次 */
void md_init(void);

/* 仅 PWM 输出：way = 1~4，duty = 0~1000（1000 = 100% 占空比）。
 * 不修改方向引脚。越界值自动钳位到 0~1000 */
void md_set_pwm(int way, int duty);

/* 仅方向控制：dir = +1 正转 / -1 反转 / 0 短接制动。
 * 不修改 PWM 输出（制动时会同时把 PWM 清 0） */
void md_set_dir(int way, int dir);

/* 方向 + PWM 一体控制：speed > 0 正转 / < 0 反转 / = 0 短接制动。
 * 输出 |speed| 的占空比（0~1000），越界自动钳位 */
void md_set_motor(int way, int speed);

/* 编码器增量：返回自上次调用以来新增的脉冲计数（带方向符号）。
 * 内部用 16 位回绕安全算法，任意调用间隔都准确。
 * 接线方向与物理转向相反时，用 md_set_enc_sign 修正 */
int32_t md_get_encoder_delta(int way);

/* 编码器累计计数：自 md_init / md_clear_encoder 以来的总脉冲数（带符号） */
int32_t md_get_encoder_count(int way);

/* 清零累计计数：way = 0 表示四个电机全部清零 */
void md_clear_encoder(int way);

/* 编码器方向修正符号：sign = +1 / -1。
 * 若某电机正转时计数为负，把对应项设为 -1（默认 +1） */
void md_set_enc_sign(int way, int sign);

/* 编码器方向自动标定：逐个电机开环正转 500ms，按原始计数方向写 sign。
 * 在 md_init 之后、闭环之前调用一次（会让四个电机依次短转，共约 2.8s） */
void md_enc_sign_autocal(void);

#endif /* __MOTOR_DRIVER_H__ */
