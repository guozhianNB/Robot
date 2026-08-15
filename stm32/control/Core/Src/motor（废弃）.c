#include "main.h"
#include "tim.h"
#include "gpio.h"
#include "motor.h"
#include <math.h>

/*

电机A=左前轮
电机B=右前轮
电机C=左后轮
电机D=右后轮

*/


/* ====================================================================
 * 电机驱动模块（STM32F103ZETX）
 *
 * ┌─────────────────────────────────────────────────────────────────┐
 * │                       模块组成                                   │
 * │                                                                 │
 * │  1) 开环基础驱动：motor_init / set_motor / control_motor         │
 * │     - PWM    ：TIM1 四通道 (PE9/PE11/PE13/PE14，已重映射)        │
 * │     - 方向引脚：电机A/B = PB12~PB15，电机C/D = PD8~PD11          │
 * │     - PWM 量程：0~1000（1000 = 100% 占空比）                     │
 * │                                                                 │
 * │  2) 编码器恒速闭环（单电机）：motor_encoder_init /              │
 * │     motor_speed_control / motor_get_speed_rpm / motor_pid_tune  │
 * │     - 编码器  ：TIM2/3/4/5 编码器模式（TI1），每个电机一个       │
 * │     - 速度单位：RPM（转/分钟），与轮径无关                       │
 * │     - 控制律  ：位置式 PID（比例 + 条件积分 + 可选微分）         │
 * │                                                                 │
 * │  闭环流程（每个控制周期 ~10ms）：                                │
 * │     目标RPM ──→ [误差 = 目标 − 实际] ──→ [PID] ──→ PWM          │
 * │                      ↑                          │               │
 * │                      └── 编码器计数换算 RPM ◄────┘               │
 * └─────────────────────────────────────────────────────────────────┘
 * ==================================================================== */

#define HTIM_MOTOR &htim1
#define CHANNEL_A TIM_CHANNEL_1
#define CHANNEL_B TIM_CHANNEL_2
#define CHANNEL_C TIM_CHANNEL_3
#define CHANNEL_D TIM_CHANNEL_4
#define FORWARD_A AIN1_GPIO_Port, AIN1_Pin
#define BACKWARD_A AIN2_GPIO_Port, AIN2_Pin
#define FORWARD_B BIN1_GPIO_Port, BIN1_Pin
#define BACKWARD_B BIN2_GPIO_Port, BIN2_Pin
#define FORWARD_C CIN1_GPIO_Port, CIN1_Pin
#define BACKWARD_C CIN2_GPIO_Port, CIN2_Pin
#define FORWARD_D DIN1_GPIO_Port, DIN1_Pin
#define BACKWARD_D DIN2_GPIO_Port, DIN2_Pin

/* ====================================================================
 * 编码器恒速闭环（单电机）配置
 * 参考 Car 工程 encoder.c / pid_control.c 的实现思路。
 * 速度单位：RPM（转/分钟），与轮径无关。
 * ==================================================================== */

#define MOTOR_NUM                4        /* 电机数量                      */
#define MOTOR_PWM_FULL_SCALE     1000     /* PWM 满量程（与 set_motor 一致）*/

/* 编码器每圈计数（TIM 编码器 TI1 模式，仅统计 CH1 上升沿）
 * 标定方法：让电机空转 N 圈，看编码器累计计数 → CPR = 总计数 / N     */
#define MOTOR_ENCODER_CPR        330.0f

/* 车轮直径 (mm)：contorl_car 用它将 mm/s 换算成电机 RPM。
 *   RPM = mm/s × 60 / (π × 轮径)
 * 请按实际麦克纳姆轮直径修改（常见 60 / 80 / 97mm）             */
#define MOTOR_WHEEL_DIAMETER_MM  80.0f
#define PI                       3.14159265359f

/* 控制周期 (ms)。motor_speed_control 内部用 HAL_GetTick 算实际 dt，
 * 调用间隔异常（<1ms 或 >200ms）时回退到该值                       */
#define MOTOR_CONTROL_PERIOD_MS  10U

/* ---------------- PID 默认参数 ----------------
 * 参考 Car 工程轮速 PID 经验值，运行时可用 motor_pid_tune() 在线修改。
 * 调参方向：
 *   KP 太小 → 速度跟不上目标（稳态误差大）；KP 太大 → 振荡/啸叫
 *   KI 消除稳态误差，太大 → 超调/震荡
 *   KD 抑制超调，但会放大编码器量化噪声（低速时建议保持 0）        */
#define MOTOR_PID_KP             0.8f   /* 比例增益：误差 × KP → 立即修正   */
#define MOTOR_PID_KI             2.0f   /* 积分增益：累积误差消除稳态误差   */
#define MOTOR_PID_KD             0.0f   /* 微分增益：抑制超调（低速设 0）   */
#define MOTOR_PID_INTEGRAL_LIMIT 150.0f /* 积分项上限：防饱和，也限制堵转时
                                         * 最大持续驱动力（I 项 ≤ 300 = 30% 占空比）*/
#define MOTOR_PID_OUTPUT_LIMIT   1000.0f/* 输出限幅（±1000 = 满量程）       */

/* 最小有效 PWM（克服静摩擦，0 = 关闭）。闭环下开启可能引起小幅度振荡 */
#define MOTOR_MIN_PWM            0

/* 换向"踢一脚"：方向翻转瞬间短时强制大 PWM，帮电机快速越过 0 速完成反向。
 * 反转从"正转高速"开始时若只靠 P 项（~16% 占空比）根本刹不住，1s 内来不及
 * 反向；且轮速滞留 0 附近时会被堵转保护误判制动。踢一脚 200ms
 * （< 堵转超时 300ms）在堵转检测之后覆盖输出，之后交回 PID */
#define MOTOR_KICK_PWM  500.0f  /* 踢一脚的强制 PWM（±50% 占空比） */
#define MOTOR_KICK_MS   200U    /* 踢一脚持续时长 (ms) */

/* 堵转保护参数：目标非 0 时若实际转速远低于目标、且输出已到"尽力驱动"水平，
 * 持续超过 STALL_TIMEOUT_MS 判定为堵转 → 清积分 + 短接制动并保持 HOLD_MS。
 * 防止轮子卡住时积分饱和，障碍移开后电机以满功率猛冲（"疯狂冲刺"）。
 * 保持期结束后自动重新试探驱动，障碍移除即可正常起步 */
#define MOTOR_STALL_RPM_THRESHOLD  15.0f   /* 实际转速低于该值 (RPM) 视为没转起来   */
#define MOTOR_STALL_RATIO          0.25f  /* 实际 < 目标×该比例 视为严重滞后（兼容低目标）*/
#define MOTOR_STALL_PWM_THRESHOLD  250.0f /* 输出达到该值认为已在尽力驱动（约 1/4 满量程）*/
#define MOTOR_STALL_TIMEOUT_MS     300U   /* 持续堵转时间，超时触发保护 (ms)   */
#define MOTOR_STALL_HOLD_MS        1000U  /* 触发后制动保持时间，期间不再驱动 (ms) */

/* 车级堵转保护（control_car 内）：
 * 任一车轮被卡住 → 整车制动保持，避免健康轮全速空转推着车身乱窜/打转。
 * 检测只依赖轮速，比轮级检测（要等 PID 输出冲到阈值，约 1.6s）快得多。
 * 分级判定：正常运行中突然堵转 → 快速判定；起步/换向/重试阶段
 * （PID 扭矩还没建立、轮速低属正常）→ 慢速判定，给足加速时间 */
#define CAR_STALL_DETECT_MS      300U   /* 正常运行中堵转：快速判定窗口 */
#define CAR_STALL_DETECT_SLOW_MS 1500U  /* 起步/重试阶段：慢速判定窗口 */
#define CAR_STALL_GRACE_MS       600U   /* 距上次轮速正常≤该时长视为"行驶中" */
#define CAR_STALL_HOLD_MS        1000U  /* 车级制动保持时长，之后自动重试探 */

/* 电机 → 编码器定时器 映射
 * 本工程 CubeMX 编码器引脚：
 *   TIM2 = PA15/PB3, TIM3 = PA6/PA7, TIM4 = PD12/PD13, TIM5 = PA0/PA1 */
static TIM_HandleTypeDef *const motor_enc_tim[MOTOR_NUM] = {
    &htim2,   /* motor 1 (TIM1_CH1 / PE9)  → TIM2 */
    &htim3,   /* motor 2 (TIM1_CH2 / PE11) → TIM3 */
    &htim4,   /* motor 3 (TIM1_CH3 / PE13) → TIM4 */
    &htim5,   /* motor 4 (TIM1_CH4 / PE14) → TIM5 */
};

/* 编码器方向符号（±1）：若某电机实际转速方向与目标相反，把对应项改为 -1。
 * ⚠️ 2026-08-08 教训：曾仅凭"开环反转正常"误判编码器方向全反、改为 -1，
 * 结果 PID 读到反向转速 → 堵转保护反复制动 → 车轮停止+原地颤抖。
 * 开环测试只能证明方向引脚/驱动板 OK，不能证明编码器符号。已恢复 +1，
 * 真实符号由 motor_encoder_sign_autocal() 在自检时自动标定后写入本表。 */
static int8_t motor_enc_sign[MOTOR_NUM] = { 1, 1, 1, 1 };

static bool g_encoder_started = false;   /* motor_encoder_init 是否已调用 */

/* 每电机闭环状态 */
typedef struct {
    uint32_t previous;    /* 上一次编码器计数值          */
    float    last_rpm;    /* 最近一次实际转速 (RPM)      */
    float    integral;    /* PID 积分项                  */
    float    last_error;  /* 上一次误差                  */
    bool     pid_init;    /* 微分项首拍标志（首拍不取微分）*/
    int32_t  last_target; /* 上一次目标转速（方向切换清积分）*/
    uint32_t last_tick;       /* dt 计算时间戳               */
    uint32_t stall_begin;     /* 堵转检测开始时间戳（0=未在检测）*/
    uint32_t stall_hold_tick; /* 堵转制动保持开始时间戳（0=未保持）*/
    uint32_t kick_until;      /* 换向踢一脚截止时间戳（0=不在踢）*/
} MotorCtrl;

static MotorCtrl motor_ctrl[MOTOR_NUM];

static float g_pid_kp = MOTOR_PID_KP;
static float g_pid_ki = MOTOR_PID_KI;
static float g_pid_kd = MOTOR_PID_KD;

/* 开环 PWM 设置：speed 为 0~1000 占空比，只改 PWM 不动方向
 * （正反转方向由 control_motor 决定；此处把超范围/负值钳位到 0~1000）
 * 占空比 → TIM1 比较值按 ARR 满量程映射：compare = duty × (ARR+1) / 1000
 * 例：duty=500 → 50% 占空比；duty=1000 → 100%（比较值 ≥ ARR，输出恒高） */
void set_motor(int way, int speed)
{
    if (speed > MOTOR_PWM_FULL_SCALE) speed = MOTOR_PWM_FULL_SCALE;  /* 上限 */
    if (speed < 0) speed = 0;                                        /* 下限 */
    uint32_t duty = (uint32_t)speed;
    uint32_t compare = (duty * (htim1.Init.Period + 1U)) / MOTOR_PWM_FULL_SCALE;
    switch (way)
    {
        case 1: __HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_1, compare); break;
        case 2: __HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_2, compare); break;
        case 3: __HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_3, compare); break;
        case 4: __HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_4, compare); break;
        default: break;
    }
}

/* 开环电机测试 */
void set_pwm_directly(int speed)
{
    HAL_GPIO_WritePin(FORWARD_A, GPIO_PIN_SET);
    HAL_GPIO_WritePin(BACKWARD_A, GPIO_PIN_RESET);
    HAL_GPIO_WritePin(FORWARD_B, GPIO_PIN_SET);
    HAL_GPIO_WritePin(BACKWARD_B, GPIO_PIN_RESET);
    HAL_GPIO_WritePin(FORWARD_C, GPIO_PIN_SET);
    HAL_GPIO_WritePin(BACKWARD_C, GPIO_PIN_RESET);
    HAL_GPIO_WritePin(FORWARD_D, GPIO_PIN_SET);
    HAL_GPIO_WritePin(BACKWARD_D, GPIO_PIN_RESET);
    __HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_1, speed);
    __HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_2, speed);
    __HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_3, speed);
    __HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_4, speed);
    return;
}
/* 初始化 PWM：启动 TIM1 四个通道（初始占空比 0，电机不转） */
void motor_init_pwm(void)
{
  HAL_TIM_PWM_Start(&htim1, TIM_CHANNEL_1);
  HAL_TIM_PWM_Start(&htim1, TIM_CHANNEL_2);
  HAL_TIM_PWM_Start(&htim1, TIM_CHANNEL_3);
  HAL_TIM_PWM_Start(&htim1, TIM_CHANNEL_4);
}

/* 方向 + PWM 控制：
 *   brake = true  → 短接制动：IN1/IN2 同时拉高（线圈短接，快速停转），PWM 清零
 *   brake = false → 按 speed 符号设方向（>0 正转 / <0 反转），输出 |speed| PWM
 * speed 范围 ±1000（= ±100% 占空比） */
void control_motor(int way, int speed, bool brake)
{
    if (speed > 1000) speed = 1000;      /* 限幅 */
    if (speed < -1000) speed = -1000;
    if (brake)
    {
        /* 制动：方向引脚全部拉高 → 电机制动线圈短接 */
        switch(way)
        {
            case 1:
                HAL_GPIO_WritePin(FORWARD_A, GPIO_PIN_SET);
                HAL_GPIO_WritePin(BACKWARD_A, GPIO_PIN_SET);
                break;
            case 2:
                HAL_GPIO_WritePin(FORWARD_B, GPIO_PIN_SET);
                HAL_GPIO_WritePin(BACKWARD_B, GPIO_PIN_SET);
                break;
            case 3:
                HAL_GPIO_WritePin(FORWARD_C, GPIO_PIN_SET);
                HAL_GPIO_WritePin(BACKWARD_C, GPIO_PIN_SET);
                break;
            case 4:
                HAL_GPIO_WritePin(FORWARD_D, GPIO_PIN_SET);
                HAL_GPIO_WritePin(BACKWARD_D, GPIO_PIN_SET);
                break;
        }
        set_motor(way, 0);
        return;
    }
    else 
    {
        switch(way)
        {
            case 1:
                if (speed > 0)
                {
                    HAL_GPIO_WritePin(FORWARD_A, GPIO_PIN_SET);
                    HAL_GPIO_WritePin(BACKWARD_A, GPIO_PIN_RESET);
                }
                else
                {
                    HAL_GPIO_WritePin(FORWARD_A, GPIO_PIN_RESET);
                    HAL_GPIO_WritePin(BACKWARD_A, GPIO_PIN_SET);
                }
                break;
            case 2:
                if (speed > 0)
                {
                    HAL_GPIO_WritePin(FORWARD_B, GPIO_PIN_SET);
                    HAL_GPIO_WritePin(BACKWARD_B, GPIO_PIN_RESET);
                }
                else
                {
                    HAL_GPIO_WritePin(FORWARD_B, GPIO_PIN_RESET);
                    HAL_GPIO_WritePin(BACKWARD_B, GPIO_PIN_SET);
                }
                break;
            case 3:
                if (speed > 0)
                {
                    HAL_GPIO_WritePin(FORWARD_C, GPIO_PIN_SET);
                    HAL_GPIO_WritePin(BACKWARD_C, GPIO_PIN_RESET);
                }
                else
                {
                    HAL_GPIO_WritePin(FORWARD_C, GPIO_PIN_RESET);
                    HAL_GPIO_WritePin(BACKWARD_C, GPIO_PIN_SET);
                }
                break;
            case 4:
                if (speed > 0)
                {
                    HAL_GPIO_WritePin(FORWARD_D, GPIO_PIN_SET);
                    HAL_GPIO_WritePin(BACKWARD_D, GPIO_PIN_RESET);
                }
                else
                {
                    HAL_GPIO_WritePin(FORWARD_D, GPIO_PIN_RESET);
                    HAL_GPIO_WritePin(BACKWARD_D, GPIO_PIN_SET);
                }
                break;
        }
        if (speed < 0) speed = -speed;
        set_motor(way, speed);
    }
    
}




/*control car
 小车为麦克纳姆轮，输入为整车速度（单位 mm/s）：
   f_speed      = 前进速度 (mm/s)
   l_speed      = 左移速度 (mm/s)
   rotate_speed = 旋转速度 (mm/s 等效切向速度)
 内部先做麦克纳姆轮逆运动学分配四轮线速度，再换算成 RPM 进入恒速闭环。
  */
/* 车级堵转状态（control_car 内）：
 * 任一车轮被卡住 → 整车制动保持，避免"健康轮全速空转推着车身乱窜/打转"
 * 的失控表现（两个以上轮子卡住时尤其明显）。保持期后自动重试探。 */
static bool     car_blocked      = false;   /* 整车处于堵转制动保持        */
static uint32_t car_block_since  = 0U;      /* 整车堵转开始时间            */
static uint32_t car_detect_since = 0U;      /* 车级堵转检测计时            */
static uint32_t car_ok_since     = 0U;      /* 上次检测到轮速正常的时间（0=从未正常）*/
static int      prev_t1 = 0, prev_t2 = 0, prev_t3 = 0, prev_t4 = 0;  /* 上次四轮指令 */

/* 车级堵转判定：目标非 0 且实际转速远低于目标（≤15RPM 且 < 目标一半） */
static bool wheel_stuck(int way, int target)
{
    if (target == 0) return false;
    float actual = fabsf((float)motor_get_speed_rpm(way));
    float asked  = fabsf((float)target);
    return (actual < MOTOR_STALL_RPM_THRESHOLD) && (actual < asked * 0.5f);
}

void control_car(float f_speed, float l_speed, float rotate_speed)
{
    /* 麦克纳姆轮逆运动学（线速度叠加，单位 mm/s） */
    float wheel1 = f_speed + l_speed + rotate_speed;  //左前轮
    float wheel2 = f_speed - l_speed - rotate_speed;  //右前轮
    float wheel3 = f_speed - l_speed + rotate_speed;  //左后轮
    float wheel4 = f_speed + l_speed - rotate_speed;  //右后轮

    wheel1 = wheel1 * 60.0f / (MOTOR_WHEEL_DIAMETER_MM * PI);  //左前轮
    wheel2 = wheel2 * 60.0f / (MOTOR_WHEEL_DIAMETER_MM * PI);  //右前轮
    wheel3 = wheel3 * 60.0f / (MOTOR_WHEEL_DIAMETER_MM * PI);  //左后轮
    wheel4 = wheel4 * 60.0f / (MOTOR_WHEEL_DIAMETER_MM * PI);  //右后轮
    int t1 = (int)wheel1, t2 = (int)wheel2, t3 = (int)wheel3, t4 = (int)wheel4;

    /* ---- 指令变化检测：换向/变速后给轮子加速时间 ----
     * 模式切换瞬间部分轮子要反向，轮速会短暂穿过 0。此时若按"行驶中突然
     * 堵转"的快速判定（300ms）就会误判堵转 → 整车刹停 → 看起来"切不进
     * 新模式、只会前进"。指令一变就重置检测、改用慢速判定，等轮速稳定
     * 后再恢复"行驶中堵转"的快速保护 */
    if (t1 != prev_t1 || t2 != prev_t2 || t3 != prev_t3 || t4 != prev_t4)
    {
        prev_t1 = t1; prev_t2 = t2; prev_t3 = t3; prev_t4 = t4;
        car_ok_since     = 0U;   /* 强制按"起步/重试"慢速判定 */
        car_detect_since = 0U;
    }

    /* ---- ① 整车停止：清除车级堵转状态，四轮制动 ---- */
    if (t1 == 0 && t2 == 0 && t3 == 0 && t4 == 0)
    {
        car_blocked      = false;
        car_block_since  = 0U;
        car_detect_since = 0U;
        car_ok_since     = 0U;   /* 重新起步按"慢速判定"给足加速时间 */
        motor_speed_control(1, 0);
        motor_speed_control(2, 0);
        motor_speed_control(3, 0);
        motor_speed_control(4, 0);
        return;
    }

    /* ---- ② 整车堵转制动保持期 ----
     * 判定堵转后保持制动 CAR_STALL_HOLD_MS，期间四轮全制动
     * （顺带经 motor_speed_control 清空各轮 PID/堵转状态），
     * 到点后允许重新试探；障碍移除即可正常起步 */
    if (car_blocked)
    {
        if (HAL_GetTick() - car_block_since < CAR_STALL_HOLD_MS)
        {
            motor_speed_control(1, 0);
            motor_speed_control(2, 0);
            motor_speed_control(3, 0);
            motor_speed_control(4, 0);
            return;
        }
        car_blocked = false;   /* 保持结束，允许重新试探驱动 */
    }

    /* ---- ③ 轮级堵转保持（后备用）----
     * 任一车轮进入轮级制动保持 → 整车制动。control_car 正常路径下
     * ④ 的车级检测（300ms）比轮级检测（约 1.6s）更快，③ 主要兜底
     * 单独使用 motor_speed_control 时留下的保持状态 */
    if (motor_any_stalled())
    {
        motor_speed_control(1, 0);
        motor_speed_control(2, 0);
        motor_speed_control(3, 0);
        motor_speed_control(4, 0);
        return;
    }

    /* ---- ④ 车级堵转检测 ----
     * 任一目标非 0 的轮子实际转速远低于目标，持续 CAR_STALL_DETECT_MS
     * 判定整车被卡 → 立即四轮全制动进入保持。只依赖轮速、不等 PID
     * 输出冲上来，避免卡住期间健康轮长时间全速空转推着车身乱窜 */
    bool stuck = wheel_stuck(1, t1) || wheel_stuck(2, t2)
              || wheel_stuck(3, t3) || wheel_stuck(4, t4);
    if (stuck)
    {
        if (car_detect_since == 0U) car_detect_since = HAL_GetTick();
        /* 分级判定：最近轮速正常过（行驶中突然堵转）→ 快速窗口；
         * 起步/换向/保持后重试（一直没动过）→ 慢速窗口，避免把
         * PID 扭矩尚未建立时的正常慢速误判成堵转 */
        uint32_t detect_ms = (car_ok_since != 0U
                              && HAL_GetTick() - car_ok_since <= CAR_STALL_GRACE_MS)
                             ? CAR_STALL_DETECT_MS : CAR_STALL_DETECT_SLOW_MS;
        if (HAL_GetTick() - car_detect_since >= detect_ms)
        {
            car_blocked      = true;
            car_block_since  = HAL_GetTick();
            car_detect_since = 0U;
            car_ok_since     = 0U;   /* 保持期后重新按"起步"慢速判定 */
            /* 立即制动全部轮（并经 motor_speed_control 清空各轮堵转状态） */
            motor_speed_control(1, 0);
            motor_speed_control(2, 0);
            motor_speed_control(3, 0);
            motor_speed_control(4, 0);
            return;
        }
    }
    else
    {
        car_ok_since     = HAL_GetTick();   /* 轮速正常：刷新"行驶中"标志 */
        car_detect_since = 0U;              /* 转速正常，取消车级检测计时 */
    }

    /* ---- ⑤ 正常驱动：进入恒速闭环 ---- */
    motor_speed_control(1, t1);
    motor_speed_control(2, t2);
    motor_speed_control(3, t3);
    motor_speed_control(4, t4);
}

/* ====================================================================
 * 编码器恒速闭环（单电机）
 *
 * 用法（主循环，~10ms 调用一次）：
 *   motor_init();            // 启动 PWM
 *   motor_encoder_init();    // 启动编码器定时器
 *   ...
 *   while (1) {
 *       motor_speed_control(1, 300);   // motor 1 恒速 300 RPM
 *       HAL_Delay(10);
 *   }
 *
 * target_rpm > 0 正转，< 0 反转，= 0 制动停止。
 * 返回值为当前实际转速 (RPM)。
 * ==================================================================== */

/* 启动编码器定时器（TIM2/3/4/5 编码器模式），在 motor_init 后调用一次 */
void motor_encoder_init(void)
{
    /* 逐个启动 4 个编码器定时器，并清零计数值与 PID 状态 */
    for (int i = 0; i < MOTOR_NUM; i++) {
        /* 启动编码器计数：定时器开始随 CH1/CH2 边沿自动增减计数（方向由相位判断） */
        if (HAL_TIM_Encoder_Start(motor_enc_tim[i], TIM_CHANNEL_ALL) != HAL_OK) {
            Error_Handler();
        }
        __HAL_TIM_SET_COUNTER(motor_enc_tim[i], 0);   /* 计数从 0 开始 */
        motor_ctrl[i].previous    = 0;
        motor_ctrl[i].last_rpm    = 0.0f;
        motor_ctrl[i].integral    = 0.0f;
        motor_ctrl[i].last_error  = 0.0f;
        motor_ctrl[i].pid_init    = false;
        motor_ctrl[i].last_target = 0;
        motor_ctrl[i].last_tick       = HAL_GetTick();
        motor_ctrl[i].stall_begin     = 0;
        motor_ctrl[i].stall_hold_tick = 0;
        motor_ctrl[i].kick_until      = 0;
    }
    g_encoder_started = true;
}

/* 编码器方向自动标定：逐个电机开环正转 500ms，看原始计数方向。
 * 正转（IN1=H/IN2=L）时计数应 >0；若 <0 说明该编码器 A/B 相位与物理转向
 * 相反 → 该项 sign 置 -1。在 motor_encoder_init 之后、闭环使用前调用一次。
 * 注意：会依次短转四个电机（共约 2s），仅用于自检/首次调试；
 * 标定结果即真实符号，确认后可直接硬编码回 motor_enc_sign。 */
void motor_encoder_sign_autocal(void)
{
    for (int i = 0; i < MOTOR_NUM; i++)
    {
        TIM_HandleTypeDef *tim = motor_enc_tim[i];
        __HAL_TIM_SET_COUNTER(tim, 0);
        control_motor(i + 1, 400, false);        /* 开环正转，40% 占空比 */
        uint32_t t0 = HAL_GetTick();
        while (HAL_GetTick() - t0 < 500U) {}     /* 保持 500ms */
        control_motor(i + 1, 0, true);           /* 制动停止 */
        int32_t cnt = (int32_t)(int16_t)((uint16_t)__HAL_TIM_GET_COUNTER(tim));
        motor_enc_sign[i] = (cnt < 0) ? (int8_t)-1 : (int8_t)1;
    }
}

/* 读取指定电机最近一次计算的实际转速 (RPM) */
int motor_get_speed_rpm(int way)
{
    if (way < 1 || way > MOTOR_NUM) return 0;
    return (int)(motor_ctrl[way - 1].last_rpm + 0.5f);
}

/* 车级堵转查询：任一电机正处于堵转制动保持期返回 true。
 * control_car 用它做整车保护（堵转 → 四轮全制动，避免车身乱窜/冲刺）。 */
bool motor_any_stalled(void)
{
    for (int i = 0; i < MOTOR_NUM; i++)
    {
        if (motor_ctrl[i].stall_hold_tick != 0U) return true;
    }
    return false;
}

/* ====================================================================
 * 单电机恒速闭环（核心函数）
 *
 * 每个控制周期调用一次（建议 10ms）。内部流程：
 *   ① 计算本次调用的实际时间间隔 dt（HAL_GetTick 差值）
 *   ② 读取编码器计数值，用 16 位回绕安全算法算出 Δ计数
 *   ③ 由 Δ计数换算实际转速：RPM = Δ计数 × 60000 / (CPR × dt_ms)
 *      （Δ计数/CPR = 转数，÷dt 得转/秒，×60 得转/分）
 *   ④ 目标 0 → 制动；否则跑 PID 得到 PWM 输出
 *   ⑤ 输出经方向+PWM 写入电机，返回实际转速
 *
 * @param way        电机编号 1~4（对应 TIM1_CH1~CH4）
 * @param target_rpm 目标转速：>0 正转 / <0 反转 / =0 制动
 * @return 当前实际转速 (RPM)
 * ==================================================================== */
int motor_speed_control(int way, int target_rpm)
{
    if (way < 1 || way > MOTOR_NUM) return 0;      /* 非法电机号 */
    if (!g_encoder_started) return 0;              /* 未初始化编码器，不驱动电机 */

    MotorCtrl *c = &motor_ctrl[way - 1];
    TIM_HandleTypeDef *tim = motor_enc_tim[way - 1];

    /* ---- ① 计算 dt ---- */
    uint32_t now_tick = HAL_GetTick();
    uint32_t dt_ms = now_tick - c->last_tick;      /* 距上次调用的毫秒数 */
    c->last_tick = now_tick;
    /* 首拍或主循环被阻塞过久时 dt 不可信，回退到默认控制周期 */
    if (dt_ms < 1U || dt_ms > 200U) dt_ms = MOTOR_CONTROL_PERIOD_MS;
    float dt_s = (float)dt_ms / 1000.0f;           /* 换算成秒，供 PID 用 */

    /* ---- ② 读编码器增量（16-bit 回绕安全）---- */
    uint32_t current = __HAL_TIM_GET_COUNTER(tim);
    /* 定时器计数在 0~65535 间循环，直接做 16 位减法可自动处理回绕：
     * 例如 65535→5 得差 6，而不是 -65530 */
    int32_t delta = (int32_t)(int16_t)((uint16_t)current - (uint16_t)c->previous);
    c->previous = current;
    delta *= motor_enc_sign[way - 1];              /* 应用接线方向符号 */

    /* ---- ③ 换算实际转速 (RPM) ---- */
    float rpm = ((float)delta * 60000.0f) / (MOTOR_ENCODER_CPR * (float)dt_ms);
    c->last_rpm = rpm;                             /* 保存，供 motor_get_speed_rpm 读取 */

    /* ---- ④ 堵转制动保持 ----
     * 上次判定堵转后进入保持期：期间只制动不驱动，防止"障碍移开后满功率猛冲"。
     * 轮子重新转动（障碍移除/被拖转）或保持超时后解除，允许 PID 重新试探驱动 */
    if (c->stall_hold_tick != 0U)
    {
        if (target_rpm == 0)
        {
            /* 明确要求停止：解除保持（保持期本身也是制动，交给⑤统一处理） */
            c->stall_hold_tick = 0U;
            c->stall_begin     = 0U;
        }
        else if (fabsf(rpm) >= MOTOR_STALL_RPM_THRESHOLD)
        {
            c->stall_hold_tick = 0U;   /* 轮子已能转动：提前解除保持 */
            c->stall_begin     = 0U;
            c->integral        = 0.0f;
            c->last_error      = 0.0f;
        }
        else if (HAL_GetTick() - c->stall_hold_tick >= MOTOR_STALL_HOLD_MS)
        {
            c->stall_hold_tick = 0U;   /* 保持超时：本轮允许重新试探驱动 */
            c->stall_begin     = 0U;
        }
        else
        {
            control_motor(way, 0, true);           /* 仍在保持期：短接制动 */
            return (int)(rpm + 0.5f);
        }
    }

    /* ---- ⑤ 目标为 0：制动并重置 PID，避免残留积分 ---- */
    if (target_rpm == 0) {
        c->integral    = 0.0f;
        c->last_error  = 0.0f;
        c->pid_init    = false;
        c->last_target = 0;
        c->stall_begin     = 0U;   /* 已停止驱动，取消堵转检测 */
        c->stall_hold_tick  = 0U;  /* 已停止驱动，解除堵转保持 */
        c->kick_until       = 0U;  /* 已停止，取消换向踢一脚 */
        control_motor(way, 0, true);               /* 短接制动 */
        return (int)(rpm + 0.5f);
    }

    /* ---- ⑥ 正反转切换时清空积分 ----
     * 换向瞬间误差会突变（实际速度还没跟上新方向），
     * 不清积分的话旧方向的积分会"顶"着新方向输出，导致反转初段卡顿 */
    if (c->pid_init && (target_rpm > 0) != (c->last_target > 0)) {
        c->integral    = 0.0f;
        c->last_error  = 0.0f;
        c->pid_init    = false;
        c->kick_until  = HAL_GetTick() + MOTOR_KICK_MS;   /* 换向踢一脚 */
    }
    c->last_target = target_rpm;

    /* ---- ⑦ PID 计算（位置式）----
     *   error = 目标 − 实际
     *   P 项 = KP × error               ：立即响应偏差
     *   I 项 = KI × ∫error·dt           ：消除稳态误差（速度上不去就靠它加力）
     *   D 项 = KD × Δerror/Δt           ：抑制超调（默认关闭）              */
    float error   = (float)target_rpm - rpm;
    float p_term  = g_pid_kp * error;
    float i_term  = g_pid_ki * c->integral;        /* integral 存的是 ∫error·dt */
    float d_term  = 0.0f;
    if (g_pid_kd > 0.0f && c->pid_init) {
        d_term = g_pid_kd * (error - c->last_error) / dt_s;
    }
    c->pid_init   = true;                          /* 首拍只记录误差，不取微分 */
    c->last_error = error;

    float output = p_term + i_term + d_term;

    /* ---- ⑧ 抗饱和条件积分 ----
     * 输出已到上限（饱和）且误差同向（仍想往饱和方向加）时暂停积分，
     * 防止"输出早已拉满、积分还在继续累积"导致的过冲和难退饱和。
     * （原实现以 output ≤ 上限 判断，而限幅后 output 恒 ≤ 上限，
     *   条件几乎恒真 = 无条件积分，是堵转猛冲的诱因之一——已修复） */
    if (!((output >=  MOTOR_PID_OUTPUT_LIMIT) && (error > 0.0f))
        && !((output <= -MOTOR_PID_OUTPUT_LIMIT) && (error < 0.0f))) {
        c->integral += error * dt_s;               /* 时间积分：∫error·dt */
        if (c->integral >  MOTOR_PID_INTEGRAL_LIMIT) c->integral =  MOTOR_PID_INTEGRAL_LIMIT;
        if (c->integral < -MOTOR_PID_INTEGRAL_LIMIT) c->integral = -MOTOR_PID_INTEGRAL_LIMIT;
    }
    /* 输出限幅，保护电机和驱动器 */
    if (output >  MOTOR_PID_OUTPUT_LIMIT) output =  MOTOR_PID_OUTPUT_LIMIT;
    if (output < -MOTOR_PID_OUTPUT_LIMIT) output = -MOTOR_PID_OUTPUT_LIMIT;

    /* ---- ⑨ 最小 PWM 提升（可选）----
     * 电机有静摩擦，小 PWM 不转。开启后把非零小输出抬升到 MOTOR_MIN_PWM，
     * 但闭环下可能导致小幅振荡，一般保持 0 */
    if (MOTOR_MIN_PWM > 0 && output != 0.0f
        && output > -(float)MOTOR_MIN_PWM && output < (float)MOTOR_MIN_PWM) {
        output = (output > 0.0f) ? (float)MOTOR_MIN_PWM : -(float)MOTOR_MIN_PWM;
    }

    /* ---- ⑨.5 堵转检测 ----
     * 到达此分支时 target_rpm 必非 0：若实际转速远低于目标（基本没转）、
     * 且输出已到"尽力驱动"水平，说明被障碍物卡住。持续超时后：
     * 清积分 + 短接制动 + 进入保持期（见 ④），防止积分饱和，
     * 障碍移开后电机以满功率猛冲（"疯狂冲刺"） */
    if (fabsf(rpm) < MOTOR_STALL_RPM_THRESHOLD
        && fabsf(rpm) < fabsf((float)target_rpm) * MOTOR_STALL_RATIO
        && fabsf(output) >= MOTOR_STALL_PWM_THRESHOLD)
    {
        if (c->stall_begin == 0U) c->stall_begin = HAL_GetTick();
        if (HAL_GetTick() - c->stall_begin >= MOTOR_STALL_TIMEOUT_MS)
        {
            c->integral         = 0.0f;         /* 清掉饱和积分，避免猛冲 */
            c->last_error       = 0.0f;
            c->stall_begin      = 0U;
            c->stall_hold_tick  = HAL_GetTick();/* 进入制动保持期 */
            c->kick_until       = 0U;           /* 已制动，取消换向踢一脚 */
            control_motor(way, 0, true);        /* 短接制动，不再猛冲 */
            return (int)(rpm + 0.5f);
        }
    }
    else
    {
        c->stall_begin = 0U;                    /* 转速/输出正常，取消堵转计时 */
    }

    /* ---- ⑨.6 换向踢一脚：方向翻转后 200ms 内强制 ±50% PWM，
     * 帮电机快速越过 0 速完成反向。放在堵转检测（⑨.5）之后覆盖输出，
     * 因此 kick 不会被误判成堵转；且 200ms < 堵转超时 300ms，安全 */
    if (HAL_GetTick() < c->kick_until) {
        output = (target_rpm > 0) ? MOTOR_KICK_PWM : -MOTOR_KICK_PWM;
    }

    /* ---- ⑩ 写入电机（内部按符号设方向，输出 |PWM|）---- */
    control_motor(way, (int)output, false);
    return (int)(rpm + 0.5f);                      /* 四舍五入返回实际转速 */
}

/* 在线调整 PID 参数（传 0 或负值表示不修改对应项）
 * 例：motor_pid_tune(1.0f, 2.0f, 0.0f);  // 只改 KP */
void motor_pid_tune(float kp, float ki, float kd)
{
    if (kp > 0.0f) g_pid_kp = kp;
    if (ki > 0.0f) g_pid_ki = ki;
    if (kd > 0.0f) g_pid_kd = kd;
}

/* 电机初始化 */
void motor_init(void)
{
    motor_init_pwm();        /* 启动 TIM1 四通道 PWM */
    motor_encoder_init();    /* 启动 TIM2/3/4/5 编码器定时器 */
    HAL_GPIO_WritePin(FORWARD_A, GPIO_PIN_RESET);
    HAL_GPIO_WritePin(BACKWARD_A, GPIO_PIN_RESET);
    HAL_GPIO_WritePin(FORWARD_B, GPIO_PIN_RESET);
    HAL_GPIO_WritePin(BACKWARD_B, GPIO_PIN_RESET);
    HAL_GPIO_WritePin(FORWARD_C, GPIO_PIN_RESET);
    HAL_GPIO_WritePin(BACKWARD_C, GPIO_PIN_RESET);
    HAL_GPIO_WritePin(FORWARD_D, GPIO_PIN_RESET);
    HAL_GPIO_WritePin(BACKWARD_D, GPIO_PIN_RESET);
}