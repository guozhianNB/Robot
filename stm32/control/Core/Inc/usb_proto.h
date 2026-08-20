#ifndef __USB_PROTO_H__
#define __USB_PROTO_H__

#include <stdint.h>

/* ====================================================================
 * USB 车控协议层（usb_proto）
 *
 * 职责：在地瓜派（USB CDC 虚拟串口）与 STM32 电机控制之间提供
 *       一套二进制帧协议。负责：
 *         - 接收字节流 → 帧缓冲解析（含跨包粘连/坏帧丢弃）
 *         - 命令分发 → 调用 motor_control / motor_driver
 *         - 应答（ACK）与心跳状态上报（STATUS）封装发送
 *
 * 帧格式（沿用既有约定）：
 *   [0xAA][0x55][len][cmd][payload...][xor]
 *     len  = payload 长度
 *     payload 字段小端
 *     xor  = 除末字节外全部字节异或
 *   命令号分区：0x0x 下行（地瓜派→STM32），0x8x 上行（STM32→地瓜派）
 *
 * 用法：
 *   USB 初始化后调用 up_init()；
 *   CDC 收包回调里把收到的字节交给 up_on_rx(buf, len)；
 *   主循环（约 10ms）调用 up_poll()（内部做命令处理 + 心跳上报）。
 * ==================================================================== */

/* ---------------- 帧常量 ---------------- */
#define UP_FRAME_HDR1      0xAAu
#define UP_FRAME_HDR2      0x55u
#define UP_MAX_PAYLOAD     32u
#define UP_FRAME_MAX       (4u + UP_MAX_PAYLOAD + 1u) /* hdr2+len+cmd+payload+xor */

/* ---------------- 下行命令（地瓜派 → STM32） ---------------- */
#define UP_CMD_STOP        0x01u  /* 无 payload：立即四轮制动          */
#define UP_CMD_SET_CAR_VEL 0x03u  /* 6B：vx:int16 vy:int16 w:int16     */
#define UP_CMD_TUNE_PID    0x04u  /* 12B：kp:float ki:float kd:float   */
#define UP_CMD_GET_STATUS  0x05u  /* 无 payload：按需立即回一帧 STATUS */

/* ---------------- 上行命令（STM32 → 地瓜派） ---------------- */
#define UP_CMD_ACK         0x81u  /* 2B：reply:uint8 code:uint8        */
#define UP_CMD_STATUS      0x82u  /* 状态帧：见 up_send_status()       */

/* ---------------- 应答码 ---------------- */
#define UP_ACK_OK          0x00u
#define UP_ACK_BAD_CMD     0x01u  /* 未知命令号                        */
#define UP_ACK_BAD_LEN     0x02u  /* payload 长度与命令不符            */
#define UP_ACK_BAD_CRC     0x03u  /* 帧校验失败（对坏帧不单独回，预留）*/

/* ---------------- 状态 flags 位 ---------------- */
#define UP_FLAG_STALL      0x01u  /* 整车/任一轮堵转（预留，当前恒 0） */

/* 初始化：清零协议/接收状态。USB 初始化后、主循环前调用一次 */
void up_init(void);

/* 主循环周期调用（建议与 mc_update_all 同周期 10ms）：
 * 解析已收帧 + 心跳状态上报（默认 100ms） */
void up_poll(void);

/* CDC 收包回调喂数据：USB 收到一段字节后调用，内部做帧缓冲解析 */
void up_on_rx(const uint8_t *buf, uint32_t len);

/* 手动发送一帧（调试用）：payload 长度不超过 UP_MAX_PAYLOAD */
void up_send(uint8_t cmd, const uint8_t *payload, uint8_t len);

#endif /* __USB_PROTO_H__ */
