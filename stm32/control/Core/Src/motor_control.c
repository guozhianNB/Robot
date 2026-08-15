#include "motor_control.h"
#include "motor_driver.h"

/* ====================================================================
 * 单电机恒速 PID 闭环层（STM32F103ZETX）
 *
 * 职责：每个控制周期（建议 10ms）读取编码器增量 → 换算 RPM →
 *      位置式 PID → 输出 PWM，使电机转速收敛到目标 RPM。
 * 依赖 motor_driver 提供编码器/PWM/方向原语，本身不含堵转保护。
 *
 * 电机之间参数隔离（核心）：
 *   - 运行状态（积分/上次误差/时间戳/目标等）存于 mc_mot[way-1]，
 *     按电机编号索引，天然隔离，互不串扰；
 *   - PID 参数 Kp/Ki/Kd 全局共享一组，用 mc_pid_tune() 在线修改。
 * ==================================================================== */

#define MC_MOTOR_NUM          4        /* 电机数量                         */

/* 编码器每圈计数（TIM 编码器 TI1 模式，仅统计 CH1 上升沿）
 * 标定方法：让电机空转 N 圈，看编码器累计计数 → CPR = 总计数 / N    */
#define MC_ENCODER_CPR        330.0f

/* 默认控制周期 (ms)：mc_update_all 内部用 HAL_GetTick 实测 dt，
 * 调用间隔异常（<1ms 或 >200ms）时回退到该值                        */
#define MC_CONTROL_PERIOD_MS  10U

/* ---------------- PID 默认参数（全局共享一组） ----------------
 * 调参方向：
 *   KP 太小 → 速度跟不上目标（稳态误差大）；KP 太大 → 振荡/啸叫
 *   KI 消除稳态误差，太大 → 超调/震荡
 *   KD 抑制超调，但会放大编码器量化噪声（低速时建议保持 0）        */
#define MC_PID_KP             0.8f
#define MC_PID_KI             2.0f
#define MC_PID_KD             0.0f
#define MC_PID_INTEGRAL_LIMIT 150.0f  /* 积分项上限：防积分饱和           */
#define MC_PID_OUTPUT_LIMIT   1000.0f /* 输出限幅（±1000 = 满量程）       */

/* 每电机闭环状态（按 way 索引，互不串扰） */
typedef struct {
    float    integral;    /* PID 积分项                  */
    float    last_error;  /* 上一次误差（微分用）        */
    uint32_t last_tick;   /* dt 计算时间戳               */
    int      target;      /* 当前目标转速 (RPM)          */
    int      last_target; /* 上一次目标（方向切换检测）  */
    float    rpm;         /* 最近一次实际转速 (RPM)      */
    bool     inited;      /* 微分首拍标志（首拍不取微分）*/
} McMotor;

static McMotor mc_mot[MC_MOTOR_NUM];

/* PID 参数：全局共享一组，mc_pid_tune() 在线修改 */
static float g_pid_kp = MC_PID_KP;
static float g_pid_ki = MC_PID_KI;
static float g_pid_kd = MC_PID_KD;

/* 校验电机编号，非法返回 false */
static bool mc_way_ok(int way)
{
    return (way >= 1 && way <= MC_MOTOR_NUM);
}

/* ====================================================================
 * 初始化：清零四个电机的闭环状态
 * ==================================================================== */
void mc_init(void)
{
    for (int i = 0; i < MC_MOTOR_NUM; i++)
    {
        mc_mot[i].integral    = 0.0f;
        mc_mot[i].last_error  = 0.0f;
        mc_mot[i].last_tick   = HAL_GetTick();
        mc_mot[i].target      = 0;
        mc_mot[i].last_target = 0;
        mc_mot[i].rpm         = 0.0f;
        mc_mot[i].inited      = false;
    }
}

/* ====================================================================
 * 在线调整全局 PID 参数（传 0 表示不修改对应项）
 * ==================================================================== */
void mc_pid_tune(float kp, float ki, float kd)
{
    if (kp > 0.0f) g_pid_kp = kp;
    if (ki > 0.0f) g_pid_ki = ki;
    if (kd > 0.0f) g_pid_kd = kd;
}

/* ====================================================================
 * 设置目标转速：way = 1~4，target_rpm > 0 正转 / < 0 反转 / 0 制动。
 * 目标 0 时立即制动并清积分；方向翻转时清积分避免旧方向过冲
 * ==================================================================== */
void mc_set_target(int way, int target_rpm)
{
    if (!mc_way_ok(way)) return;
    McMotor *m = &mc_mot[way - 1];

    if (target_rpm == 0)
    {
        /* 目标 0：立即制动并清空闭环状态 */
        md_set_motor(way, 0);
        m->integral    = 0.0f;
        m->last_error  = 0.0f;
        m->inited      = false;
        m->last_target = 0;
        m->target      = 0;
        return;
    }

    /* 方向翻转：清积分与微分历史，避免旧方向残余造成过冲 */
    if (m->last_target != 0 && ((target_rpm > 0) != (m->last_target > 0)))
    {
        m->integral   = 0.0f;
        m->last_error = 0.0f;
        m->inited     = false;
    }
    m->target      = target_rpm;
    m->last_target = target_rpm;
}

/* 读取指定电机最近一次计算的实际转速 (RPM) */
int mc_get_speed_rpm(int way)
{
    if (!mc_way_ok(way)) return 0;
    return (int)mc_mot[way - 1].rpm;
}

/* ====================================================================
 * 每个控制周期调用一次：对四个电机执行完整闭环。
 * 位置式 PID + 条件积分抗饱和：
 *   output = Kp·err + Ki·∫err·dt + Kd·(derr/dt)
 *   输出触及 ±1000 限幅且误差同向时冻结积分累积，防积分饱和
 * ==================================================================== */
void mc_update_all(void)
{
    for (int way = 1; way <= MC_MOTOR_NUM; way++)
    {
        McMotor *m = &mc_mot[way - 1];
        uint32_t now = HAL_GetTick();

        /* ① 读编码器增量（驱动层已处理 16 位回绕 + 方向符号） */
        int32_t delta = md_get_encoder_delta(way);

        /* ② dt：实测，异常（<1ms 或 >200ms）回退默认周期 */
        uint32_t dt_ms = now - m->last_tick;
        if (dt_ms < 1U || dt_ms > 200U) dt_ms = MC_CONTROL_PERIOD_MS;
        float dt = (float)dt_ms / 1000.0f;
        m->last_tick = now;

        /* ③ 换算 RPM：delta 脉冲 / (CPR × dt 秒) × 60 */
        m->rpm = (float)delta * 60.0f / (MC_ENCODER_CPR * dt);

        /* ④ 目标 0：制动并清状态 */
        if (m->target == 0)
        {
            md_set_motor(way, 0);
            m->integral   = 0.0f;
            m->last_error = 0.0f;
            m->inited     = false;
            continue;
        }

        /* ⑤ 位置式 PID */
        float err = (float)m->target - m->rpm;
        float der;
        if (!m->inited)
        {
            der = 0.0f;              /* 首拍不取微分 */
            m->inited = true;
        }
        else
        {
            der = (err - m->last_error) / dt;
        }
        m->last_error = err;

        float output = g_pid_kp * err + g_pid_ki * m->integral + g_pid_kd * der;

        /* 条件积分（抗饱和）：输出已到限幅且误差同向时冻结积分 */
        if (!(output >=  MC_PID_OUTPUT_LIMIT && err > 0.0f) &&
            !(output <= -MC_PID_OUTPUT_LIMIT && err < 0.0f))
        {
            m->integral += err * dt;
        }
        if (m->integral >  MC_PID_INTEGRAL_LIMIT) m->integral =  MC_PID_INTEGRAL_LIMIT;
        if (m->integral < -MC_PID_INTEGRAL_LIMIT) m->integral = -MC_PID_INTEGRAL_LIMIT;

        /* 输出限幅 ±1000 后写 PWM */
        if (output >  MC_PID_OUTPUT_LIMIT) output =  MC_PID_OUTPUT_LIMIT;
        if (output < -MC_PID_OUTPUT_LIMIT) output = -MC_PID_OUTPUT_LIMIT;
        md_set_motor(way, (int)output);
    }
}
