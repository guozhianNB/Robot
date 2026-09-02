/**
  ******************************************************************************
  * @file    imu.c
  * @brief   IMU UART 驱动库（基于 USART3 + HAL 库）
  *
  * 通信协议:
  *   - 帧头:  0x7E 0x23
  *   - 帧格式: HEAD1(1B) + HEAD2(1B) + LENGTH(1B) + FUNCTION(1B) + DATA(NB) + CHECKSUM(1B)
  *   - 校验和: SUM(HEAD1 + HEAD2 + LENGTH + FUNCTION + DATA...) & 0xFF
  *
  * 硬件接口:
  *   - USART3: PB10(TX), PB11(RX), 115200-8N1
  *   - 由 CubeMX 初始化硬件，本驱动仅启用 NVIC 中断并启动 IT 接收
  ******************************************************************************
  */

#include "imu.h"
#include "usart.h"
#include "stm32f1xx_hal.h"
#include <string.h>

/* ===================== 移植配置 ============================= */
/*
 * 移植到其他串口时，只修改本文件中的这四项配置：
 * - UART HAL 句柄
 * - UART 寄存器实例
 * - UART 中断号
 * - 中断优先级
 */
#define IMU_PORT_UART_HANDLE       huart3
#define IMU_PORT_UART_INSTANCE     USART3
#define IMU_PORT_UART_IRQn         USART3_IRQn
#define IMU_PORT_UART_IRQ_PRIORITY 0u

/* ===================== 环形缓冲区 ========================== */
static volatile uint8_t  s_rx_buffer[IMU_UART_RX_BUF_SIZE];
static volatile uint16_t s_rx_write_index = 0;
static volatile uint16_t s_rx_read_index  = 0;

#define IMU_UART_DEBUG_BUF_SIZE 128u
static volatile uint8_t  s_debug_buffer[IMU_UART_DEBUG_BUF_SIZE];
static volatile uint16_t s_debug_write_index = 0;
static volatile uint16_t s_debug_read_index  = 0;

static inline uint16_t _rxbuf_next(uint16_t index)
{
    return (uint16_t)((index + 1u) % IMU_UART_RX_BUF_SIZE);
}

static inline int _rxbuf_is_empty(void)
{
    return s_rx_write_index == s_rx_read_index;
}

static inline void _rxbuf_push(uint8_t byte_value)
{
    uint16_t next_index = _rxbuf_next(s_rx_write_index);
    if (next_index == s_rx_read_index) {
        /* 缓冲区满时丢弃最旧数据 */
        s_rx_read_index = _rxbuf_next(s_rx_read_index);
    }
    s_rx_buffer[s_rx_write_index] = byte_value;
    s_rx_write_index = next_index;
}

static inline int _rxbuf_pop(uint8_t *out_byte)
{
    if (_rxbuf_is_empty()) {
        return -1;
    }
    *out_byte = s_rx_buffer[s_rx_read_index];
    s_rx_read_index = _rxbuf_next(s_rx_read_index);
    return 0;
}

/* ===================== 字节转换工具 ======================== */

/** 将两个字节（小端序）转换为 int16 */
static int16_t to_int16(const uint8_t *bytes)
{
    return (int16_t)((bytes[1] << 8) + bytes[0]);
}

/** 将四个字节直接解释为 float（IEEE 754） */
static float to_float(const uint8_t *bytes)
{
    float v;
    memcpy(&v, bytes, sizeof(float));
    return v;
}

/* ===================== 内部缓存变量 ======================== */
static volatile float s_ax = 0.0f, s_ay = 0.0f, s_az = 0.0f;
static volatile float s_gx = 0.0f, s_gy = 0.0f, s_gz = 0.0f;
static volatile float s_mx = 0.0f, s_my = 0.0f, s_mz = 0.0f;
static volatile float s_roll = 0.0f, s_pitch = 0.0f, s_yaw = 0.0f;
static volatile float s_q0 = 0.0f, s_q1 = 0.0f, s_q2 = 0.0f, s_q3 = 0.0f;
static volatile float s_height = 0.0f, s_temperature = 0.0f;
static volatile float s_pressure = 0.0f, s_pressure_contrast = 0.0f;
static volatile int   s_version_high = -1, s_version_mid = 0, s_version_low = 0;
static volatile uint8_t  s_last_rx_function = 0;
static volatile int16_t  s_last_rx_state = 0;
static volatile uint32_t s_frame_count = 0;      /* 成功解析的帧数 */
static volatile uint32_t s_rx_byte_count = 0;
static volatile uint32_t s_checksum_error_count = 0;
static volatile uint32_t s_overrun_count = 0;

/** @brief 返回已成功解析的帧数 */
uint32_t IMU_UART_GetFrameCount(void)
{
    return s_frame_count;
}

static inline void _debug_push(uint8_t byte_value)
{
    uint16_t next_index = (uint16_t)((s_debug_write_index + 1u) % IMU_UART_DEBUG_BUF_SIZE);
    if (next_index == s_debug_read_index) {
        s_debug_read_index = (uint16_t)((s_debug_read_index + 1u) % IMU_UART_DEBUG_BUF_SIZE);
    }
    s_debug_buffer[s_debug_write_index] = byte_value;
    s_debug_write_index = next_index;
}

/** @brief 获取已接收的字节总数 */
uint32_t IMU_UART_GetRxByteCount(void)
{
    return s_rx_byte_count;
}

/** @brief 获取校验和错误的累计次数 */
uint32_t IMU_UART_GetChecksumErrorCount(void)
{
    return s_checksum_error_count;
}

/** @brief 获取 UART 溢出错误的累计次数 */
uint32_t IMU_UART_GetOverrunCount(void)
{
    return s_overrun_count;
}

/**
  * @brief 读取调试缓冲区中的原始字节
  * @param out      输出缓冲区指针
  * @param max_len  最多读取的字节数
  * @return 实际读取的字节数
  */
uint16_t IMU_UART_DebugRead(uint8_t *out, uint16_t max_len)
{
    uint16_t count = 0;

    if (out == NULL) {
        return 0;
    }

    while (count < max_len && s_debug_read_index != s_debug_write_index) {
        out[count++] = s_debug_buffer[s_debug_read_index];
        s_debug_read_index = (uint16_t)((s_debug_read_index + 1u) % IMU_UART_DEBUG_BUF_SIZE);
    }
    return count;
}

/* ===================== 解析数据帧 ========================== */
static void _parse_frame_data(uint8_t frame_function, const uint8_t *frame_data)
{
    if (frame_function == IMU_FUNC_RAW_ACCEL) {
        float accel_ratio = 16.0f / 32767.0f;
        s_ax = to_int16(&frame_data[0])  * accel_ratio;
        s_ay = to_int16(&frame_data[2])  * accel_ratio;
        s_az = to_int16(&frame_data[4])  * accel_ratio;

        float deg_to_rad = 3.14159265358979323846f / 180.0f;
        float gyro_ratio = (2000.0f / 32767.0f) * deg_to_rad;
        s_gx = to_int16(&frame_data[6])  * gyro_ratio;
        s_gy = to_int16(&frame_data[8])  * gyro_ratio;
        s_gz = to_int16(&frame_data[10]) * gyro_ratio;

        float mag_ratio = 800.0f / 32767.0f;
        s_mx = to_int16(&frame_data[12]) * mag_ratio;
        s_my = to_int16(&frame_data[14]) * mag_ratio;
        s_mz = to_int16(&frame_data[16]) * mag_ratio;
    } else if (frame_function == IMU_FUNC_EULER) {
        s_roll  = to_float(&frame_data[0]);
        s_pitch = to_float(&frame_data[4]);
        s_yaw   = to_float(&frame_data[8]);
    } else if (frame_function == IMU_FUNC_QUAT) {
        s_q0 = to_float(&frame_data[0]);
        s_q1 = to_float(&frame_data[4]);
        s_q2 = to_float(&frame_data[8]);
        s_q3 = to_float(&frame_data[12]);
    } else if (frame_function == IMU_FUNC_BARO) {
        s_height            = to_float(&frame_data[0]);
        s_temperature       = to_float(&frame_data[4]);
        s_pressure          = to_float(&frame_data[8]);
        s_pressure_contrast = to_float(&frame_data[12]);
    } else if (frame_function == IMU_FUNC_VERSION) {
        s_version_high = frame_data[0];
        s_version_mid  = frame_data[1];
        s_version_low  = frame_data[2];
    } else if (frame_function == IMU_FUNC_RETURN_STATE) {
        s_last_rx_function = frame_data[0];
        s_last_rx_state    = (int16_t)frame_data[1];
    }
}

/* ===================== 轮询接收（后备） ==================== */
/** 直接轮询 USART3 寄存器，将收到的字节压入环形缓冲区
  *  当 HAL 中断方式失效时作为后备方案 */
/* ===================== 状态机解析 ========================== */
void IMU_UART_Process(void)
{
    /* 先轮询 USART3 寄存器，捕获中断可能漏掉的字节 */
    enum {
        RX_STATE_EXPECT_HEAD1 = 0,
        RX_STATE_EXPECT_HEAD2,
        RX_STATE_EXPECT_LENGTH,
        RX_STATE_EXPECT_FUNCTION,
        RX_STATE_COLLECT_DATA
    };

    static uint8_t  rx_state = RX_STATE_EXPECT_HEAD1;
    static uint8_t  frame_length = 0;
    static uint8_t  frame_function = 0;
    static uint8_t  frame_buffer[64];   /* 数据区 + 校验 */
    static uint16_t frame_index = 0;

    uint8_t current_byte = 0;

    while (_rxbuf_pop(&current_byte) == 0) {
        switch (rx_state) {
        case RX_STATE_EXPECT_HEAD1:
            rx_state = (current_byte == FRAME_HEAD1) ? RX_STATE_EXPECT_HEAD2 : RX_STATE_EXPECT_HEAD1;
            break;

        case RX_STATE_EXPECT_HEAD2:
            rx_state = (current_byte == FRAME_HEAD2) ? RX_STATE_EXPECT_LENGTH : RX_STATE_EXPECT_HEAD1;
            break;

        case RX_STATE_EXPECT_LENGTH:
            frame_length = current_byte;
            rx_state = RX_STATE_EXPECT_FUNCTION;
            break;

        case RX_STATE_EXPECT_FUNCTION:
            frame_function = current_byte;
            frame_index = 0;
            rx_state = RX_STATE_COLLECT_DATA;
            break;

        case RX_STATE_COLLECT_DATA: {
            uint16_t data_length = (frame_length >= 5) ? (uint16_t)(frame_length - 4) : 0;
            if (data_length == 0 || data_length > sizeof(frame_buffer)) {
                rx_state = RX_STATE_EXPECT_HEAD1;
                break;
            }

            frame_buffer[frame_index++] = current_byte;
            if (frame_index >= data_length) {
                uint8_t calculated_checksum = (uint8_t)(FRAME_HEAD1 + FRAME_HEAD2
                                                       + frame_length + frame_function);
                for (uint16_t i = 0; i < data_length - 1; ++i) {
                    calculated_checksum = (uint8_t)(calculated_checksum + frame_buffer[i]);
                }

                uint8_t received_checksum = frame_buffer[data_length - 1];
                if (calculated_checksum == received_checksum) {
                    _parse_frame_data(frame_function, frame_buffer);
                    ++s_frame_count;
                } else {
                    ++s_checksum_error_count;
                }
                rx_state = RX_STATE_EXPECT_HEAD1;
            }
        } break;

        default:
            rx_state = RX_STATE_EXPECT_HEAD1;
            break;
        }
    }
}

/* ===================== 初始化 ============================== */
void IMU_UART_Init(void)
{
    /* 使能 USART3 NVIC 中断 */
    HAL_NVIC_SetPriority(IMU_PORT_UART_IRQn, IMU_PORT_UART_IRQ_PRIORITY, 0);
    HAL_NVIC_EnableIRQ(IMU_PORT_UART_IRQn);

    /* 直接使能 USART3 RXNE 中断（不依赖 HAL 状态机） */
    IMU_PORT_UART_INSTANCE->CR1 |= USART_CR1_RXNEIE;
}

/* ===================== 直接中断接收 ======================== */
/**
  * @brief USART3 中断处理（直接寄存器操作，绕过 HAL）
  *        由 stm32f1xx_it.c 中的 USART3_IRQHandler 调用
  */
void IMU_UART_IRQHandler(void)
{
    uint32_t sr = IMU_PORT_UART_INSTANCE->SR;
    uint32_t cr1 = IMU_PORT_UART_INSTANCE->CR1;

    /* RXNE 可读且 RXNEIE 已使能 */
    if ((sr & USART_SR_RXNE) && (cr1 & USART_CR1_RXNEIE)) {
        uint8_t byte = (uint8_t)(IMU_PORT_UART_INSTANCE->DR & 0xFF);
        ++s_rx_byte_count;
        _rxbuf_push(byte);
        _debug_push(byte);
    }

    /* 处理溢出错误 — 读 DR 清 ORE */
    if (sr & USART_SR_ORE) {
        ++s_overrun_count;
        (void)IMU_PORT_UART_INSTANCE->DR;   /* 先读 SR（已在上面通过 sr 读取），再读 DR 清 ORE */
    }
}

/** HAL 兼容回调（保留但不依赖，防止链接冲突） */
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
    /* 不再使用 */
}

/* ===================== 命令发送 ============================ */
/**
  * @brief 向 IMU 发送一帧命令
  * @param function  功能码
  * @param params    参数缓冲区（可为 NULL）
  * @param param_len 参数长度（0~3）
  * @return 0 成功，-1 参数非法
  */
int IMU_UART_SendCommand(uint8_t function, const uint8_t *params, uint8_t param_len)
{
    if (param_len > 3 || (param_len > 0 && params == NULL)) {
        return -1;
    }

    uint8_t frame[8] = {FRAME_HEAD1, FRAME_HEAD2, 0, function, 0, 0, 0, 0};

    for (uint8_t i = 0; i < param_len; ++i) {
        frame[4 + i] = params[i];
    }

    uint8_t frame_len = (uint8_t)(4 + param_len + 1);
    frame[2] = frame_len;

    uint8_t checksum = 0;
    for (uint8_t i = 0; i < frame_len - 1; ++i) {
        checksum = (uint8_t)(checksum + frame[i]);
    }
    frame[frame_len - 1] = checksum;

    IMU_UART_SendArray(frame, frame_len);
    return 0;
}

/** @brief 主动请求 IMU 发送指定类型的数据帧 */
int IMU_UART_RequestData(uint8_t data_function)
{
    uint8_t payload[2] = {data_function, 0x00};
    return IMU_UART_SendCommand(IMU_FUNC_REQUEST_DATA, payload, sizeof(payload));
}

/**
  * @brief 设置 IMU 解算算法
  * @param algorithm  算法类型（6=六轴, 9=九轴）
  * @return 0 成功，-1 参数错误
  */
int IMU_UART_SetAlgorithm(uint8_t algorithm)
{
    if (algorithm != 6u && algorithm != 9u) {
        return -1;
    }

    uint8_t payload[2] = {algorithm, 0x5Fu};
    return IMU_UART_SendCommand(IMU_FUNC_SET_ALGORITHM, payload, sizeof(payload));
}

/**
  * @brief 设置模块自动上报频率
  * @param hz  频率（10~100 Hz，默认 25Hz）
  * @return 0 成功，-1 参数错误
  */
int IMU_UART_SetOutputRate(uint8_t hz)
{
    if (hz < 10u || hz > 100u) {
        return -1;
    }

    uint8_t payload[2] = {hz, 0x5Fu};
    return IMU_UART_SendCommand(IMU_FUNC_SET_OUTPUT_RATE, payload, sizeof(payload));
}

/* ===================== 底层串口收发 ======================== */
/**
  * @brief 通过串口发送一个字节
  * @param data  要发送的字节
  */
void IMU_UART_SendByte(uint8_t data)
{
    HAL_UART_Transmit(&IMU_PORT_UART_HANDLE, &data, 1, HAL_MAX_DELAY);
}

/**
  * @brief 通过串口发送一帧数据
  * @param pData   数据缓冲区指针
  * @param length  数据长度（字节）
  */
void IMU_UART_SendArray(uint8_t *pData, uint8_t length)
{
    HAL_UART_Transmit(&IMU_PORT_UART_HANDLE, pData, length, HAL_MAX_DELAY);
}

/* ===================== 上层数据接口 ========================= */
/**
  * @brief 初始化 IMU 模块（使能串口中断）
  * @param  无
  * @return 无
  */
void IMU_Init(void)
{
    IMU_UART_Init();
}

/**
  * @brief IMU 数据轮询处理（需在主循环中周期调用）
  * @param  无
  * @return 无
  */
void IMU_Process(void)
{
    IMU_UART_Process();
}

/**
  * @brief 获取 IMU 全部测量数据
  * @param out  输出结构体指针（imu_measurement_t），含加速度/陀螺仪/磁力计/四元数/欧拉角/气压计
  * @return 0 成功，-1 参数错误
  */
int IMU_GetData(imu_data_t *out)
{
    return IMU_UART_GetAll(out);
}

/**
  * @brief 获取三轴加速度（m/s²）
  * @param out  输出数组 float[3] = {ax, ay, az}
  * @return 0 成功，-1 参数错误
  */
int IMU_GetAccel(float out[3])
{
    return IMU_UART_GetAccelerometer(out);
}

/**
  * @brief 获取三轴角速度（rad/s）
  * @param out  输出数组 float[3] = {gx, gy, gz}
  * @return 0 成功，-1 参数错误
  */
int IMU_GetGyro(float out[3])
{
    return IMU_UART_GetGyroscope(out);
}

/**
  * @brief 获取三轴磁场强度（μT）
  * @param out  输出数组 float[3] = {mx, my, mz}
  * @return 0 成功，-1 参数错误
  */
int IMU_GetMag(float out[3])
{
    return IMU_UART_GetMagnetometer(out);
}

/**
  * @brief 获取四元数
  * @param out  输出数组 float[4] = {q0, q1, q2, q3}
  * @return 0 成功，-1 参数错误
  */
int IMU_GetQuaternion(float out[4])
{
    return IMU_UART_GetQuaternion(out);
}

/**
  * @brief 获取欧拉角（度）
  * @param out  输出数组 float[3] = {roll, pitch, yaw}（单位：度）
  * @return 0 成功，-1 参数错误
  */
int IMU_GetEuler(float out[3])
{
    return IMU_UART_GetEuler(out);
}

/* ===================== 数据获取接口 ======================== */
/**
  * @brief 读取缓存的加速度计数据（m/s²）
  * @param out  输出数组 float[3] = {ax, ay, az}
  * @return 0 成功，-1 参数错误
  */
int IMU_UART_GetAccelerometer(float out[3])
{
    if (!out) return -1;
    out[0] = s_ax; out[1] = s_ay; out[2] = s_az;
    return 0;
}

/**
  * @brief 读取缓存的陀螺仪数据（rad/s）
  * @param out  输出数组 float[3] = {gx, gy, gz}
  * @return 0 成功，-1 参数错误
  */
int IMU_UART_GetGyroscope(float out[3])
{
    if (!out) return -1;
    out[0] = s_gx; out[1] = s_gy; out[2] = s_gz;
    return 0;
}

/**
  * @brief 读取缓存的磁力计数据（μT）
  * @param out  输出数组 float[3] = {mx, my, mz}
  * @return 0 成功，-1 参数错误
  */
int IMU_UART_GetMagnetometer(float out[3])
{
    if (!out) return -1;
    out[0] = s_mx; out[1] = s_my; out[2] = s_mz;
    return 0;
}

/**
  * @brief 读取缓存的四元数
  * @param out  输出数组 float[4] = {q0, q1, q2, q3}
  * @return 0 成功，-1 参数错误
  */
int IMU_UART_GetQuaternion(float out[4])
{
    if (!out) return -1;
    out[0] = s_q0; out[1] = s_q1; out[2] = s_q2; out[3] = s_q3;
    return 0;
}

/**
  * @brief 读取缓存的欧拉角（度）
  * @param out  输出数组 float[3] = {roll, pitch, yaw}（单位：度）
  * @return 0 成功，-1 参数错误
  */
int IMU_UART_GetEuler(float out[3])
{
    if (!out) return -1;
    const float RAD2DEG = 57.2957795f;
    out[0] = s_roll  * RAD2DEG;
    out[1] = s_pitch * RAD2DEG;
    out[2] = s_yaw   * RAD2DEG;
    return 0;
}

/**
  * @brief 读取缓存的气压计数据
  * @param out  输出数组 float[4] = {高度(m), 温度(℃), 气压(Pa), 气压对比值}
  * @return 0 成功，-1 参数错误
  */
int IMU_UART_GetBarometer(float out[4])
{
    if (!out) return -1;
    out[0] = s_height;
    out[1] = s_temperature;
    out[2] = s_pressure;
    out[3] = s_pressure_contrast;
    return 0;
}

/**
  * @brief 获取 IMU 固件版本（若未获取则主动查询）
  * @param  无
  * @return 无（版本信息通过 IMU_UART_GetAll 读取）
  */
void IMU_UART_GetVersion(void)
{
    if (s_version_high < 0) {
        uint8_t payload[2] = {IMU_FUNC_VERSION, 0x00};
        IMU_UART_SendCommand(IMU_FUNC_REQUEST_DATA, payload, (uint8_t)sizeof(payload));

        for (int i = 0; i < 20; ++i) {
            IMU_UART_Process();
            if (s_version_high >= 0) {
                /* version 已通过 _parse_frame_data 更新 */
                return;
            }
            HAL_Delay(5);
        }
    }
}

/**
  * @brief 获取 IMU 所有测量数据
  * @param out  输出结构体指针（imu_measurement_t）
  * @return 0 成功，-1 参数错误
  */
int IMU_UART_GetAll(imu_measurement_t *out)
{
    if (!out) return -1;
    IMU_UART_GetAccelerometer(out->accel);
    IMU_UART_GetGyroscope(out->gyro);
    IMU_UART_GetMagnetometer(out->mag);
    IMU_UART_GetQuaternion(out->quat);
    IMU_UART_GetEuler(out->euler);
    IMU_UART_GetBarometer(out->baro);

    /* 组装版本号字符串 */
    if (s_version_high >= 0) {
        out->version[0] = (char)('0' + s_version_high);
        out->version[1] = '.';
        out->version[2] = (char)('0' + s_version_mid);
        out->version[3] = '.';
        out->version[4] = (char)('0' + s_version_low);
        out->version[5] = '\0';
    } else {
        out->version[0] = '-';
        out->version[1] = '1';
        out->version[2] = '\0';
    }

    return 0;
}

/** @brief 清空所有自动上报的缓存数据（全部置零） */
void IMU_UART_ClearAutoReportData(void)
{
    s_ax = s_ay = s_az = 0.0f;
    s_gx = s_gy = s_gz = 0.0f;
    s_mx = s_my = s_mz = 0.0f;
    s_roll = s_pitch = s_yaw = 0.0f;
    s_q0 = s_q1 = s_q2 = s_q3 = 0.0f;
    s_height = s_temperature = s_pressure = s_pressure_contrast = 0.0f;
}

/* ===================== 等待标定结果 ======================== */
/**
  * @brief 等待标定完成并返回结果
  * @param function    预期返回的功能码
  * @param timeout_ms  超时时间（ms），0 表示无限等待
  * @return 标定状态（≥0 成功，-1 超时或失败）
  */
int IMU_UART_WaitCalibration(uint8_t function, uint32_t timeout_ms)
{
    uint32_t elapsed_ms = 0;
    while (1) {
        IMU_UART_Process();
        if (s_last_rx_function == function) return s_last_rx_state;
        if (timeout_ms != 0 && elapsed_ms >= timeout_ms) return -1;
        HAL_Delay(1);
        if (timeout_ms != 0) ++elapsed_ms;
    }
}

/* ===================== 标定接口 ============================ */

/** 内部辅助：发送标定命令并等待结果 */
static int _calibration_with_wait(uint8_t function, const uint8_t *payload,
                                   uint8_t payload_len, uint32_t timeout_ms)
{
    s_last_rx_function = 0;
    s_last_rx_state = -1;

    int rc = IMU_UART_SendCommand(function, payload, payload_len);
    if (rc != 0) return rc;

    return IMU_UART_WaitCalibration(function, timeout_ms);
}

/**
  * @brief IMU 标定（加速度计 + 陀螺仪）
  *         标定时需将模块静止放置
  * @param  无
  * @return 标定状态（0 成功，-1 失败/超时）
  */
int IMU_UART_CalibrationImu(void)
{
    uint8_t payload[2] = {0x01, 0x5F};
    return _calibration_with_wait(IMU_FUNC_CALIB_IMU, payload, sizeof(payload), 7000);
}

/**
  * @brief 磁力计标定
  *         标定时需将模块在空间中旋转
  * @param  无
  * @return 标定状态（0 成功，-1 失败）
  */
int IMU_UART_CalibrationMag(void)
{
    uint8_t payload[2] = {0x01, 0x5F};
    return _calibration_with_wait(IMU_FUNC_CALIB_MAG, payload, sizeof(payload), 0);
}

/**
  * @brief 温度偏置标定
  * @param now_temperature  当前环境温度（℃），范围 -50~50
  * @return 标定状态（0 成功，-1 失败/超时/参数越界）
  */
int IMU_UART_CalibrationTemp(float now_temperature)
{
    if (now_temperature > 50.0f || now_temperature < -50.0f) return -1;
    int16_t temperature_raw = (int16_t)(now_temperature * 100.0f);
    uint8_t param_low  = (uint8_t)(temperature_raw & 0xFF);
    uint8_t param_high = (uint8_t)((temperature_raw >> 8) & 0xFF);
    uint8_t payload[3] = {param_low, param_high, 0x5F};
    return _calibration_with_wait(IMU_FUNC_CALIB_TEMP, payload, sizeof(payload), 2000);
}

/**
  * @brief 恢复用户数据到出厂默认值
  * @param  无
  * @return 0 成功，-1 发送失败
  */
int IMU_UART_ResetUserData(void)
{
    uint8_t payload[2] = {0x01, 0x5F};
    return IMU_UART_SendCommand(IMU_FUNC_RESET_FLASH, payload, sizeof(payload));
}

/**
  * @brief 远程重启 IMU 模块
  * @note  注意：协议文档未定义独立的重启功能字，当前实现发送的是
  *        IMU_FUNC_RESET_FLASH（与 IMU_UART_ResetUserData 相同，
  *        会恢复出厂默认）。使用前请核实模块实际协议，勿当重启用。
  * @param  无
  * @return 0 成功，-1 发送失败
  */
int IMU_UART_RebootDevice(void)
{
    uint8_t payload[2] = {0x01, 0x5F};
    return IMU_UART_SendCommand(IMU_FUNC_RESET_FLASH, payload, sizeof(payload));
}
