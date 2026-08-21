#include "motor_driver.h"

/* ====================================================================
 * 基础电机驱动（STM32F103ZETX）
 *
 * 硬件映射（集中在一个表里，改线只动这里）：
 *   PWM     ：TIM1 四通道（PE9/PE11/PE13/PE14），满量程 1000 = 100%
 *   方向引脚：电机 A/B = PB12~15，电机 C/D = PD8~11
 *   编码器  ：TIM2/3/4/5 编码器模式（TI1），计数 16 位（Period = 65535）
 *
 * 模块职责（只三件事）：
 *   ① 编码器读取 ：md_get_encoder_delta / md_get_encoder_count
 *   ② PWM 输出   ：md_set_pwm
 *   ③ 正反转控制 ：md_set_dir / md_set_motor
 * 不含 PID、堵转保护、速度换算 —— 上层按需实现。
 * ==================================================================== */

#define MD_MOTOR_NUM      4      /* 电机数量                    */
#define MD_PWM_FULL_SCALE 1000   /* PWM 满量程（1000 = 100%）   */

/* 方向引脚映射（way = 1~4 → IN1/IN2 引脚） */
typedef struct {
    GPIO_TypeDef *port1;         /* IN1 端口 */
    uint16_t      pin1;          /* IN1 引脚 */
    GPIO_TypeDef *port2;         /* IN2 端口 */
    uint16_t      pin2;          /* IN2 引脚 */
} MdDirPin;

/* 电机安装方向：左前(way1)、右后(way4) 为对角反放（电机为安装摆放而反装，
 * 同样的 IN1/IN2 电平会让这两台电机相对车架反向转动），故在映射表里交换其
 * IN1/IN2 引脚，使 md_set_motor 的"正转"对四轮统一为车架前进方向。
 * 编码器方向仍由上电 md_enc_sign_autocal() 自动标定。 */
static const MdDirPin md_dir_pin[MD_MOTOR_NUM] = {
    { AIN2_GPIO_Port, AIN2_Pin, AIN1_GPIO_Port, AIN1_Pin },  /* way 1 左前：反放，IN1/IN2 交换 */
    { BIN1_GPIO_Port, BIN1_Pin, BIN2_GPIO_Port, BIN2_Pin },  /* way 2 右前 */
    { CIN1_GPIO_Port, CIN1_Pin, CIN2_GPIO_Port, CIN2_Pin },  /* way 3 左后 */
    { DIN2_GPIO_Port, DIN2_Pin, DIN1_GPIO_Port, DIN1_Pin },  /* way 4 右后：反放，IN1/IN2 交换 */
};

/* PWM 通道映射 */
static const uint32_t md_pwm_ch[MD_MOTOR_NUM] = {
    TIM_CHANNEL_1, TIM_CHANNEL_2, TIM_CHANNEL_3, TIM_CHANNEL_4,
};

/* 编码器定时器映射 */
static TIM_HandleTypeDef *const md_enc_tim[MD_MOTOR_NUM] = {
    &htim2, &htim3, &htim4, &htim5,
};

/* 每电机编码器状态 */
typedef struct {
    uint32_t prev_raw;   /* 上一次原始计数（16 位）            */
    int32_t  count;      /* 累计计数（32 位，带方向符号）      */
    int8_t   sign;       /* 方向修正符号（±1，默认 +1）        */
    bool     inited;     /* 是否已初始化                       */
} MdEnc;

static MdEnc md_enc[MD_MOTOR_NUM];

/* 校验电机编号，非法返回 false */
static bool md_way_ok(int way)
{
    return (way >= 1 && way <= MD_MOTOR_NUM);
}

/* ====================================================================
 * 初始化：启动 TIM1 四通道 PWM + TIM2/3/4/5 编码器，清零方向与计数
 * ==================================================================== */
void md_init(void)
{
    /* ① 启动 PWM 四通道（初始占空比 0，电机不转） */
    HAL_TIM_PWM_Start(&htim1, TIM_CHANNEL_1);
    HAL_TIM_PWM_Start(&htim1, TIM_CHANNEL_2);
    HAL_TIM_PWM_Start(&htim1, TIM_CHANNEL_3);
    HAL_TIM_PWM_Start(&htim1, TIM_CHANNEL_4);

    /* ② 启动编码器定时器并清零计数状态 */
    for (int i = 0; i < MD_MOTOR_NUM; i++)
    {
        if (HAL_TIM_Encoder_Start(md_enc_tim[i], TIM_CHANNEL_ALL) != HAL_OK)
        {
            Error_Handler();
        }
        __HAL_TIM_SET_COUNTER(md_enc_tim[i], 0);
        md_enc[i].prev_raw = 0;
        md_enc[i].count    = 0;
        md_enc[i].sign     = 1;
        md_enc[i].inited   = true;
    }

    /* ③ 方向引脚全部复位（IN1=IN2=LOW，驱动器无输出） */
    for (int i = 0; i < MD_MOTOR_NUM; i++)
    {
        HAL_GPIO_WritePin(md_dir_pin[i].port1, md_dir_pin[i].pin1, GPIO_PIN_RESET);
        HAL_GPIO_WritePin(md_dir_pin[i].port2, md_dir_pin[i].pin2, GPIO_PIN_RESET);
    }
}

/* ====================================================================
 * PWM 输出：仅写占空比，不改方向。duty 0~1000，越界钳位
 * 占空比 → TIM1 比较值按 ARR 满量程映射：compare = duty × (ARR+1) / 1000
 * ==================================================================== */
void md_set_pwm(int way, int duty)
{
    if (!md_way_ok(way)) return;
    if (duty > MD_PWM_FULL_SCALE) duty = MD_PWM_FULL_SCALE;
    if (duty < 0) duty = 0;
    uint32_t compare = ((uint32_t)duty * (htim1.Init.Period + 1U)) / MD_PWM_FULL_SCALE;
    __HAL_TIM_SET_COMPARE(&htim1, md_pwm_ch[way - 1], compare);
}

/* ====================================================================
 * 方向控制：dir = +1 正转 / -1 反转 / 0 短接制动
 * 制动时 IN1/IN2 同时拉高（线圈短接，快速停转）并把 PWM 清零
 * ==================================================================== */
void md_set_dir(int way, int dir)
{
    if (!md_way_ok(way)) return;
    const MdDirPin *p = &md_dir_pin[way - 1];

    if (dir == 0)
    {
        /* 短接制动：IN1/IN2 同高 */
        HAL_GPIO_WritePin(p->port1, p->pin1, GPIO_PIN_SET);
        HAL_GPIO_WritePin(p->port2, p->pin2, GPIO_PIN_SET);
        md_set_pwm(way, 0);
    }
    else
    {
        /* 正转：IN1=H/IN2=L；反转：IN1=L/IN2=H */
        GPIO_PinState in1 = (dir > 0) ? GPIO_PIN_SET : GPIO_PIN_RESET;
        GPIO_PinState in2 = (dir > 0) ? GPIO_PIN_RESET : GPIO_PIN_SET;
        HAL_GPIO_WritePin(p->port1, p->pin1, in1);
        HAL_GPIO_WritePin(p->port2, p->pin2, in2);
    }
}

/* ====================================================================
 * 方向 + PWM 一体：speed > 0 正转 / < 0 反转 / = 0 短接制动
 * 输出 |speed|（0~1000），越界钳位
 * ==================================================================== */
void md_set_motor(int way, int speed)
{
    if (!md_way_ok(way)) return;
    if (speed >  MD_PWM_FULL_SCALE) speed =  MD_PWM_FULL_SCALE;
    if (speed < -MD_PWM_FULL_SCALE) speed = -MD_PWM_FULL_SCALE;

    if (speed == 0)
    {
        md_set_dir(way, 0);   /* 制动 */
        return;
    }
    md_set_dir(way, (speed > 0) ? 1 : -1);
    md_set_pwm(way, (speed > 0) ? speed : -speed);
}

/* ====================================================================
 * 编码器读取（核心）：自上次调用以来新增的脉冲数（带方向符号）
 *
 * 16 位回绕安全：定时器计数在 0~65535 间循环，直接做 16 位减法
 * 自动处理回绕（例如 65535→5 得差 6，而非 -65530）。计数本身带
 * 方向符号（编码器模式由 A/B 相位自动增减）。
 * ==================================================================== */
int32_t md_get_encoder_delta(int way)
{
    if (!md_way_ok(way) || !md_enc[way - 1].inited) return 0;

    MdEnc *e = &md_enc[way - 1];
    uint32_t raw = __HAL_TIM_GET_COUNTER(md_enc_tim[way - 1]);
    int32_t delta = (int32_t)(int16_t)((uint16_t)raw - (uint16_t)e->prev_raw);
    e->prev_raw = raw;
    e->count += delta * e->sign;      /* 应用方向修正符号 */
    return delta * e->sign;
}

/* 编码器累计计数（带方向符号） */
int32_t md_get_encoder_count(int way)
{
    if (!md_way_ok(way) || !md_enc[way - 1].inited) return 0;
    return md_enc[way - 1].count;
}

/* 清零累计计数：way = 0 表示四个电机全部清零 */
void md_clear_encoder(int way)
{
    if (way == 0)
    {
        for (int i = 0; i < MD_MOTOR_NUM; i++)
        {
            md_enc[i].count    = 0;
            md_enc[i].prev_raw = 0;
            __HAL_TIM_SET_COUNTER(md_enc_tim[i], 0);
        }
        return;
    }
    if (!md_way_ok(way)) return;
    md_enc[way - 1].count    = 0;
    md_enc[way - 1].prev_raw = 0;
    __HAL_TIM_SET_COUNTER(md_enc_tim[way - 1], 0);
}

/* 编码器方向修正符号（±1）：接线方向与物理转向相反时调用 */
void md_set_enc_sign(int way, int sign)
{
    if (!md_way_ok(way)) return;
    md_enc[way - 1].sign = (sign < 0) ? (int8_t)-1 : (int8_t)1;
}

/* ====================================================================
 * 编码器方向自动标定：逐个电机开环正转一段，读原始累计计数判断方向，
 * 为负则把该电机 sign 设为 -1（接线方向与物理转向相反时）。
 * 必须在 md_init 之后、闭环之前调用；会让四个电机依次短转。
 * ⚠️ 不可仅凭"开环正反转正常"推断编码器符号 —— 必须实测标定，
 *    否则 PID 读到反向转速会持续乱颤/停转（历史教训，见仓库记忆）。
 * ==================================================================== */
void md_enc_sign_autocal(void)
{
    const uint32_t calib_ms = 500U;  /* 每电机开环正转时长  */
    const uint32_t calib_gap = 200U; /* 电机间隔，避免电流冲击 */
    const int      calib_pwm = 400;  /* 40% 占空比（不依赖 PID）*/

    for (int way = 1; way <= MD_MOTOR_NUM; way++)
    {
        md_set_motor(way, calib_pwm);   /* 开环正转 */
        HAL_Delay(calib_ms);
        md_set_motor(way, 0);           /* 制动 */

        /* 标定时 sign 均为默认 +1，累计计数即原始方向 */
        int32_t raw = md_get_encoder_count(way);
        md_set_enc_sign(way, (raw < 0) ? -1 : 1);
        md_clear_encoder(way);
        HAL_Delay(calib_gap);
    }
}
