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
 * ⚠️ 实测标定值：手动转轮子 10 圈测得 PPR ≈ 28202（两次复核 28219/28202）。
 *    这是"轮子每圈脉冲数"（已含减速比放大），所以 rpm 语义 = 轮子转速，
 *    与 mc_car_set 的 mm/s→RPM 换算（轮子 RPM）统一。标定方法：
 *    手动转轮子 N 圈，看累计计数 → PPR = 总计数 / N                     */
#define MC_ENCODER_CPR        28202.0f

/* 默认控制周期 (ms)：mc_update_all 内部用 HAL_GetTick 实测 dt，
 * 调用间隔异常（<1ms 或 >200ms）时回退到该值                        */
#define MC_CONTROL_PERIOD_MS  10U

/* ---------------- PID 默认参数（全局共享一组） ----------------
 * 2026-09-11 实测整定（架空 47RPM 阶跃，stm32/control_test 用 0x84 诊断帧量测、可重复）：
 *   最初 KP=13/KI=3    → 超调 19.1%、±5% 稳定 10~12s、末段误差 −8.1%
 *   KP=2/KI=20         → 超调 2.1%、稳定 0.56s
 *   + 静摩擦前馈 KP=2  → 超调 2.1%、稳定 0.12s
 *   + 静摩擦前馈 KP=4  → 超调 0.0%、稳定 0.04s   ← 本次采用（命令下去第 1 拍
 *                        就给 174~265 PWM，轮子立刻起转，不再"慢悠悠"）
 *   机理：维持 47RPM 只需 ~120 PWM，而静摩擦死区就有 ~100 PWM；老代码无前馈
 *   且 KI=3，起步只能靠积分慢慢爬 → 启动慢/顿挫，也是"自转只执行约 35%"之源。
 * 调参方向：
 *   KP 太小 → 速度跟不上目标（稳态误差大）；KP 太大 → 起步饱和/振荡/啸叫
 *   KI 消除稳态误差，太大 → 超调/震荡
 *   KD 抑制超调，但会放大编码器量化噪声（低速时建议保持 0）
 * ⚠️ 积分限幅必须与 KI 解耦：旧值 1000 会让 KI×∫ 最大到 20000 ≫ 输出 1000，
 *    "条件积分"抗饱和彻底失效；统一取 OUTPUT_LIMIT/KI，即积分项最多贡献满量程。 */
#define MC_PID_KP             4.0f
#define MC_PID_KI             20.0f
#define MC_PID_KD             0.0f
#define MC_PID_OUTPUT_LIMIT   1000.0f /* 输出限幅（±1000 = 满量程）        */
#define MC_PID_INTEGRAL_LIMIT 50.0f   /* = OUTPUT_LIMIT/KI：积分项最多贡献满量程 */

/* ---------------- 死区/静摩擦前馈（2026-09-11 实测加装） ----------------
 * 实测：轮子要 ~100 PWM 才开始转（多组参数下测到 93~106），而 KP 首拍输出
 * 往往还不到死区 → 得靠积分爬 ~100ms 才动，这就是"启动慢悠悠"的物理来源。
 * 加前馈后第一个控制周期（11ms）就把 ~100 PWM 打给电机，PID 只做修正
 * （稳态时 ff≈100 已够，积分几乎不出力）。方向按 target 符号给（摩擦永远
 * 阻碍运动，与误差符号无关，避免在 0 附近抖动）；幅值随 |target| 线性引入，
 * 防止极低速命令时一顿一冲。 */
#define MC_FF_DEADZONE   100.0f /* 起步占空比实测值（换电机/调压后可再标定）*/
#define MC_FF_FULL_RPM   5.0f   /* |target| ≥ 5 RPM 时前馈给满 */

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

        /* 死区前馈：克服静摩擦的固定占空比（实测 ~100 PWM），让电机第一个控制
         * 周期就起步，不必等积分爬过死区；放在抗饱和判断之前，让冻结条件看到
         * 含前馈的真实总输出。 */
        float tgt_f = (float)m->target;
        if (tgt_f >=  MC_FF_FULL_RPM) output +=  MC_FF_DEADZONE;
        else if (tgt_f <= -MC_FF_FULL_RPM) output += -MC_FF_DEADZONE;
        else output += MC_FF_DEADZONE * tgt_f / MC_FF_FULL_RPM;  /* 低速线性引入 */

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

/* ====================================================================
 * 整车速度控制（麦克纳姆 X 型）
 * vx：前进速度 (mm/s)   vy：左移速度 (mm/s)   w：旋转 (0.1°/s)
 * 三通道全 0 = 整车制动。内部逆运动学 → 四轮 RPM → mc_set_target。
 * ==================================================================== */
#define MC_WHEEL_DIAMETER_MM   80.0f  /* 麦克纳姆轮直径，按实际轮子修改 */
#define MC_ROTATE_RADIUS_MM   150.0f  /* 旋转半径（半对角线），按车架标定 */
#define MC_PI                  3.14159265f

void mc_car_set(int vx, int vy, int w)
{
    /* 旋转角速度 (0.1°/s → rad/s) → 切向线速度 (mm/s) */
    float w_rad = (float)w * 0.1f * MC_PI / 180.0f;
    float rot   = w_rad * MC_ROTATE_RADIUS_MM;

    float f = (float)vx;
    float l = (float)vy;

    /* 麦克纳姆 X 型逆运动学（线速度叠加，mm/s）：
     *   LF = f + l + rot    RF = f - l - rot
     *   LR = f - l + rot    RR = f + l - rot   */
    float w1 = f + l + rot;
    float w2 = f - l - rot;
    float w3 = f - l + rot;
    float w4 = f + l - rot;

    /* mm/s → RPM：RPM = v · 60 / (π · D) */
    float k = 60.0f / (MC_WHEEL_DIAMETER_MM * MC_PI);
    mc_set_target(1, (int)(w1 * k));   /* 左前 */
    mc_set_target(2, (int)(w2 * k));   /* 右前 */
    mc_set_target(3, (int)(w3 * k));   /* 左后 */
    mc_set_target(4, (int)(w4 * k));   /* 右后 */
}
