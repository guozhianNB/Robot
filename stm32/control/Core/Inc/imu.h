#ifndef __IMU_H
#define __IMU_H

#include <stdint.h>

/* ========================== 配置项 ========================== */
#ifndef IMU_UART_RX_BUF_SIZE
#define IMU_UART_RX_BUF_SIZE 512   /* 覆盖 OLED 刷新间隔内的自动上报数据 */
#endif

/* 帧头定义 */
#define FRAME_HEAD1  0x7E
#define FRAME_HEAD2  0x23

/* ======================== 功能码 ============================ */
#define IMU_FUNC_VERSION        0x01
#define IMU_FUNC_RAW_ACCEL      0x04
#define IMU_FUNC_RAW_GYRO       0x0A
#define IMU_FUNC_RAW_MAG        0x10
#define IMU_FUNC_QUAT           0x16
#define IMU_FUNC_EULER          0x26
#define IMU_FUNC_BARO           0x32
#define IMU_FUNC_CALIB_IMU      0x70
#define IMU_FUNC_CALIB_MAG      0x71
#define IMU_FUNC_CALIB_BARO     0x72
#define IMU_FUNC_CALIB_TEMP     0x73
#define IMU_FUNC_SET_OUTPUT_RATE 0x60
#define IMU_FUNC_SET_ALGORITHM   0x61
#define IMU_FUNC_REQUEST_DATA   0x80
#define IMU_FUNC_RETURN_STATE   0x81
#define IMU_FUNC_RESET_FLASH    0xA0

/* ===================== 数据结构体 =========================== */
/** 一次性获取所有传感器数据 */
typedef struct {
    float accel[3];     /* 加速度计 (g) */
    float gyro[3];      /* 陀螺仪 (rad/s) */
    float mag[3];       /* 磁力计 (uT) */
    float quat[4];      /* 四元数 */
    float euler[3];     /* 欧拉角 (deg) — 通过 GetEuler 获取时已转角度 */
    float baro[4];      /* [高度(m), 温度(℃), 气压(Pa), 气压差(Pa)] */
    char  version[8];   /* 版本号字符串 */
} imu_data_t;

/* 兼容旧代码的类型名。新代码使用 imu_data_t。 */
typedef imu_data_t imu_measurement_t;

/* ===================== 函数声明 ============================ */

/* --------------------- 上层数据接口 ------------------------ */
/**
 * @brief 初始化 IMU 模块。
 * @note UART 资源由 imu.c 内的 IMU_PORT_* 宏配置。
 */
void IMU_Init(void);

/** @brief 解析已接收的 IMU 串口数据，应在主循环或任务中周期调用。 */
void IMU_Process(void);

/** @brief 获取最新的全部 IMU 数据。 */
int IMU_GetData(imu_data_t *out);

/** @brief 分别获取加速度(g)、角速度(rad/s)、磁场(uT)、四元数和欧拉角(deg)。 */
int IMU_GetAccel(float out[3]);
int IMU_GetGyro(float out[3]);
int IMU_GetMag(float out[3]);
int IMU_GetQuaternion(float out[4]);
int IMU_GetEuler(float out[3]);

/** @brief 初始化 IMU 串口（启用 USART3 NVIC 中断 & 启动 RX 中断接收） */
void IMU_UART_Init(void);

/** @brief USART3 中断处理（直接寄存器操作，由 stm32f1xx_it.c 调用） */
void IMU_UART_IRQHandler(void);

/** @brief 将接收到的字节推入环形缓冲区（由 HAL_UART_RxCpltCallback 自动调用） */
void IMU_UART_RxBytes(volatile uint8_t *data, uint16_t len);

/** @brief 解析环形缓冲区中的数据，提取完整帧并更新内部缓存 */
void IMU_UART_Process(void);

/** @brief 发送命令帧到 IMU */
int  IMU_UART_SendCommand(uint8_t function, const uint8_t *params, uint8_t param_len);

/* --------------------- 数据获取接口 ------------------------ */
int  IMU_UART_GetAccelerometer(float out[3]);
int  IMU_UART_GetGyroscope(float out[3]);
int  IMU_UART_GetMagnetometer(float out[3]);
int  IMU_UART_GetQuaternion(float out[4]);
int  IMU_UART_GetEuler(float out[3]);
int  IMU_UART_GetBarometer(float out[4]);
int  IMU_UART_GetAll(imu_measurement_t *out);
void IMU_UART_GetVersion(void);

/** @brief 主动请求 IMU 发送指定类型的数据帧 */
int  IMU_UART_RequestData(uint8_t data_function);
int  IMU_UART_SetAlgorithm(uint8_t algorithm);
int  IMU_UART_SetOutputRate(uint8_t hz);

/** @brief 清除自动上报的缓存数据 */
void IMU_UART_ClearAutoReportData(void);

/** @brief 返回已成功解析的帧数（调试用） */
uint32_t IMU_UART_GetFrameCount(void);
uint32_t IMU_UART_GetRxByteCount(void);
uint32_t IMU_UART_GetChecksumErrorCount(void);
uint32_t IMU_UART_GetOverrunCount(void);
uint16_t IMU_UART_DebugRead(uint8_t *out, uint16_t max_len);

/* --------------------- 标定接口 ---------------------------- */
int  IMU_UART_CalibrationImu(void);
int  IMU_UART_CalibrationMag(void);
int  IMU_UART_CalibrationTemp(float now_temperature);
int  IMU_UART_ResetUserData(void);
int  IMU_UART_RebootDevice(void);
int  IMU_UART_WaitCalibration(uint8_t function, uint32_t timeout_ms);

/* --------------------- 底层发送接口 ------------------------ */
void IMU_UART_SendByte(uint8_t data);
void IMU_UART_SendArray(uint8_t *pData, uint8_t length);

#endif /* __IMU_H */
