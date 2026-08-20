#include "usb_proto.h"
#include "motor_control.h"
#include "motor_driver.h"
#include "usbd_cdc_if.h"
#include <string.h>

/* ====================================================================
 * USB 车控协议层实现（usb_proto.c）
 * 依赖：
 *   - motor_control : mc_set_target / mc_get_speed_rpm / mc_pid_tune
 *   - motor_driver  : md_get_encoder_count
 *   - usbd_cdc_if   : CDC_Transmit_FS（USB 发送）
 * 注：本文件须加入顶层 CMakeLists.txt 的 target_sources。
 * ==================================================================== */

/* 心跳状态上报周期 (ms) */
#define UP_STS_PERIOD_MS   100u
/* 接收字节缓冲（原始流缓存，跨包拼接） */
#define UP_RX_BUF_SIZE     64u

/* 待解析的原始字节流缓冲 */
static uint8_t  s_buf[UP_RX_BUF_SIZE];
static uint16_t s_buf_len = 0U;

/* 状态上报序号 */
static uint8_t s_seq = 0U;
/* 上次心跳上报时间戳 */
static uint32_t s_last_sts = 0U;

/* ---------------- 发送 ---------------- */

void up_send(uint8_t cmd, const uint8_t *payload, uint8_t len)
{
    uint8_t f[UP_FRAME_MAX];
    if (len > UP_MAX_PAYLOAD) len = UP_MAX_PAYLOAD;

    f[0] = UP_FRAME_HDR1;
    f[1] = UP_FRAME_HDR2;
    f[2] = len;
    f[3] = cmd;

    uint8_t xorv = f[0] ^ f[1] ^ f[2] ^ f[3];
    for (uint8_t i = 0; i < len; i++)
    {
        f[4 + i] = payload[i];
        xorv    ^= payload[i];
    }
    f[4 + len] = xorv;

    CDC_Transmit_FS(f, 5u + len);
}

/* 封装并发送一帧 STATUS（seq + 4×rpm + 4×enc + flags，小端） */
static void up_send_status(void)
{
    uint8_t p[1u + 8u + 16u + 1u];
    uint16_t idx = 0U;

    p[idx++] = s_seq++;                       /* seq      :uint8 */

    for (int w = 1; w <= 4; w++)              /* 4×rpm    :int16 */
    {
        int16_t r = (int16_t)mc_get_speed_rpm(w);
        p[idx++] = (uint8_t)(r & 0xFFu);
        p[idx++] = (uint8_t)((r >> 8) & 0xFFu);
    }

    for (int w = 1; w <= 4; w++)              /* 4×enc    :int32 */
    {
        int32_t e = md_get_encoder_count(w);
        for (int b = 0; b < 4; b++)
            p[idx++] = (uint8_t)((e >> (8 * b)) & 0xFFu);
    }

    p[idx++] = 0x00u;                         /* flags    :uint8（预留） */

    up_send(UP_CMD_STATUS, p, (uint8_t)idx);
}

/* ---------------- 命令处理 ---------------- */

/* 处理一帧完整、校验通过的数据帧 f（指向帧头） */
static void up_process_frame(const uint8_t *f)
{
    uint8_t len = f[2];
    uint8_t cmd = f[3];
    const uint8_t *pl = f + 4;
    uint8_t ack = UP_ACK_OK;

    switch (cmd)
    {
    case UP_CMD_STOP:
        if (len != 0U) { ack = UP_ACK_BAD_LEN; break; }
        mc_car_set(0, 0, 0);
        break;

    case UP_CMD_SET_CAR_VEL:
        if (len != 6U) { ack = UP_ACK_BAD_LEN; break; }
        {
            int16_t vx = (int16_t)(pl[0] | (pl[1] << 8));  /* 前进 mm/s      */
            int16_t vy = (int16_t)(pl[2] | (pl[3] << 8));  /* 左移 mm/s      */
            int16_t w  = (int16_t)(pl[4] | (pl[5] << 8));  /* 旋转 0.1°/s    */
            mc_car_set(vx, vy, w);
        }
        break;

    case UP_CMD_TUNE_PID:
        if (len != 12U) { ack = UP_ACK_BAD_LEN; break; }
        {
            float kp, ki, kd;
            memcpy(&kp, pl,     4);
            memcpy(&ki, pl + 4, 4);
            memcpy(&kd, pl + 8, 4);
            mc_pid_tune(kp, ki, kd);
        }
        break;

    case UP_CMD_GET_STATUS:
        up_send_status();        /* 立即回一帧状态，不再回 ACK */
        return;

    default:
        ack = UP_ACK_BAD_CMD;
        break;
    }

    /* 回 ACK：[应答的 cmd, 结果码] */
    uint8_t p[2] = { cmd, ack };
    up_send(UP_CMD_ACK, p, 2);
}

/* ---------------- 接收解析 ---------------- */

/* 从缓冲中尽力提取并处理完整帧（处理到没有完整帧为止） */
static void up_parse(void)
{
    while (s_buf_len >= 4U)
    {
        /* ① 丢弃帧头前的垃圾字节 */
        uint16_t h = 0U;
        while ((h + 1U < s_buf_len) &&
               !(s_buf[h] == UP_FRAME_HDR1 && s_buf[h + 1U] == UP_FRAME_HDR2))
            h++;
        if (h > 0U)
        {
            memmove(s_buf, s_buf + h, s_buf_len - h);
            s_buf_len -= h;
        }
        if (s_buf_len < 4U) break;                 /* 连头+len+cmd 都不够 */

        uint8_t  lenf = s_buf[2];
        uint16_t need = 4U + (uint16_t)lenf + 1U;  /* hdr2+cmd+payload+xor */
        if (s_buf_len < need) break;               /* 帧未收全，等待更多 */

        /* ② XOR 校验（除末字节外全部异或） */
        uint8_t x = s_buf[0];
        for (uint16_t i = 1U; i < need - 1U; i++)
            x ^= s_buf[i];

        if (x == s_buf[need - 1U])
            up_process_frame(s_buf);               /* 合法帧 → 分发 */
        /* 校验失败则丢弃本帧（防缓冲卡死） */

        /* ③ 消费掉该帧 */
        memmove(s_buf, s_buf + need, s_buf_len - need);
        s_buf_len -= need;
    }
}

/* ---------------- 对外接口 ---------------- */

void up_init(void)
{
    s_buf_len = 0U;
    s_seq     = 0U;
    s_last_sts = HAL_GetTick();
}

void up_on_rx(const uint8_t *buf, uint32_t len)
{
    for (uint32_t i = 0U; i < len && s_buf_len < sizeof(s_buf); i++)
        s_buf[s_buf_len++] = buf[i];
    up_parse();
}

void up_poll(void)
{
    /* 心跳状态上报 */
    uint32_t now = HAL_GetTick();
    if (now - s_last_sts >= UP_STS_PERIOD_MS)
    {
        s_last_sts = now;
        up_send_status();
    }
}
