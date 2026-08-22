/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"
#include "i2c.h"
#include "tim.h"
#include "usb_device.h"
#include "gpio.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */

#include "motor_driver.h"
#include "motor_control.h"
#include "usb_proto.h"
#include "oled.h"
#include <stdio.h>

/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
/* ===== 单轮测试（KEY1=PE3 按下进入）参数 =====
 * 用于底盘调试：按键进入电机1闭环恒速测试，OLED 显示 rpm/pwm。
 * ⚠️ CPR 实测 28202（轮子每圈脉冲），rpm 语义 = 轮子转速：
 *    48 RPM ≈ 200mm/s（标准行驶）  100 RPM ≈ 420mm/s（测试观察） */
#define TEST_MOTOR       1        /* 测试电机号 1~4 */
#define TEST_LOOP_RPM    100      /* 目标轮子转速 (RPM) */
#define TEST_CALIB_MS    600      /* 方向标定转动时长 (ms) */
#define TEST_CALIB_PWM   300      /* 标定/软启停占空比（0~1000） */

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/

/* USER CODE BEGIN PV */

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
/* USER CODE BEGIN PFP */

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */
/* ===== 软启动/软停止：PWM 斜坡缓冲，避免电流冲击 =====
 * 硬启动（PWM 0→满跳变）近似堵转电流、硬制动（IN1=IN2 短接）产生
 * 反电动势冲击回灌电池，导致电源过载/电池过热/MCU 复位。因此：
 *   启动：PWM 每 20ms 步进 100（约 80ms 从 0 升到目标）
 *   停止：PWM 每 20ms 步降 100，最后为 0 时保持方向引脚（滑行），
 *         不用短接制动 —— 电机自然减速，无冲击 */
static void motor_ramp(int way, int target)
{
  const int step = 100;
  const uint32_t step_ms = 20;
  int cur = 0;

  while (cur != target)
  {
    if (target > cur)
    {
      cur += step;
      if (cur > target) cur = target;
    }
    else
    {
      cur -= step;
      if (cur < target) cur = target;
    }
    md_set_motor(way, cur);
    HAL_Delay(step_ms);
  }
}

/* 软停：从检测 PWM 逐步降到 0，方向引脚保持 → 滑行（非短接制动） */
static void motor_soft_stop(int way)
{
  const int step = 100;
  const uint32_t step_ms = 20;
  int cur = TEST_CALIB_PWM;

  while (cur > 0)
  {
    cur -= step;
    if (cur < 0) cur = 0;
    md_set_pwm(way, cur);      /* 只降 PWM，不改方向引脚 = 滑行 */
    HAL_Delay(step_ms);
  }
}

/* ===== 单电机闭环测试（KEY1 按下进入，死循环） =====
 * 流程：① 软启动正转标定编码器方向 → 软停
 *       ② PID 闭环恒速，OLED 显示 rpm/pwm（单页刷新不阻塞 10ms 周期）
 * 注意：闭环循环内禁止整帧 OLED 刷新（~97ms 阻塞会打碎控制周期，
 *       导致 rpm 乱跳/抖动，见 AGENTS.md 约束 3）。 */
static void single_motor_test_run(void)
{
  char line[24];
  uint32_t last_ms = 0;
  int way = TEST_MOTOR;

  OLED_Init();
  HAL_Delay(20);

  OLED_NewFrame();
  snprintf(line, sizeof(line), "W%d CLOSE t=%d", way, TEST_LOOP_RPM);
  OLED_PrintASCIIString(0, 0, line, &afont8x6, OLED_COLOR_NORMAL);
  OLED_ShowFrame();
  HAL_Delay(500);

  /* ① 开环预转 600ms：让轮子转一下，OLED 显示实际转速方向
   *    （正值 = 编码器计数与驱动方向一致；负值 = A/B 接反）
   *    ⚠️ 不再自动设 sign（已取消自动标定）——接线正反由用户手动决定：
   *    在 motor_driver.c 用 md_set_enc_sign(way, -1) 修正，或对调 A/B 线 */
  md_clear_encoder(0);
  motor_ramp(way, TEST_CALIB_PWM);
  HAL_Delay(TEST_CALIB_MS);
  md_get_encoder_delta(way);                       /* 关键：同步硬件 */
  int32_t raw_dir = md_get_encoder_count(way);     /* 开环转向检测值 */
  motor_soft_stop(way);
  md_clear_encoder(0);
  HAL_Delay(300);

  /* ② PID 闭环恒速：实测整定 Kp=6.5/Ki=2.0（快速响应）
   *    Kp 决定起步推力（err=100 时 6.5×100=650 PWM，立即冲上去）
   *    Ki 决定积分爬升速度（2.0/秒补足稳态误差）
   *    积分限幅已放宽到 1000（与输出限幅一致，等效不限幅）
   *    ⚠️ 与 motor_control.c 的 MC_PID_KP/KI 默认值保持一致 */
  mc_init();
  mc_pid_tune(6.5f, 2.0f, 0.0f);
  mc_set_target(way, TEST_LOOP_RPM);

  last_ms = HAL_GetTick();
  while (1)
  {
    mc_update_all();                 /* 10ms 周期闭环（硬约束） */

    if (HAL_GetTick() - last_ms >= 200)
    {
      int rpm = mc_get_speed_rpm(way);
      uint32_t pwm = (way == 1) ? __HAL_TIM_GET_COMPARE(&htim1, TIM_CHANNEL_1) :
                     (way == 2) ? __HAL_TIM_GET_COMPARE(&htim1, TIM_CHANNEL_2) :
                     (way == 3) ? __HAL_TIM_GET_COMPARE(&htim1, TIM_CHANNEL_3) :
                                  __HAL_TIM_GET_COMPARE(&htim1, TIM_CHANNEL_4);
      last_ms = HAL_GetTick();

      /* 页1 = rpm，页2 = pwm，页3 = raw（afont8x6 = 8px = 一页，单页刷新 ≈3ms）
       * 判读：
       *   pwm 顶到 1000 → 目标超电机能力 / 负载过大（需降目标或查机械）
       *   rpm 上不去且 pwm 小 → PID 参数弱（升 Kp/Ki）
       *   raw 为负（rpm 负）→ 编码器 A/B 接反，需 md_set_enc_sign(way,-1) 手动修正 */
      snprintf(line, sizeof(line), "rpm=%d", rpm);
      OLED_NewFrame();
      OLED_PrintASCIIString(0, 8, line, &afont8x6, OLED_COLOR_NORMAL);
      OLED_ShowPage(1);

      snprintf(line, sizeof(line), "pwm=%lu", (unsigned long)pwm);
      OLED_NewFrame();
      OLED_PrintASCIIString(0, 16, line, &afont8x6, OLED_COLOR_NORMAL);
      OLED_ShowPage(2);

      snprintf(line, sizeof(line), "raw=%ld", (long)raw_dir);
      OLED_NewFrame();
      OLED_PrintASCIIString(0, 24, line, &afont8x6, OLED_COLOR_NORMAL);
      OLED_ShowPage(3);
    }

    HAL_Delay(10);                   /* 10ms 周期      */
  }
}

/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_TIM1_Init();
  MX_TIM2_Init();
  MX_TIM3_Init();
  MX_TIM4_Init();
  MX_TIM5_Init();
  MX_TIM8_Init();
  MX_USB_DEVICE_Init();
  MX_I2C1_Init();
  /* USER CODE BEGIN 2 */
  md_init();               /* 启动 PWM + 编码器，清零方向引脚 */
  /* ⚠️ 已取消 md_enc_sign_autocal() 上电自动标定：
   *   自动标定需四轮依次短转，且结果不可控；接线正反改为手动决定——
   *   编码器 A/B 接反时，在 motor_driver.c 的 md_enc[].sign 或
   *   初始化处手动 md_set_enc_sign(way, -1)。默认全 +1。 */
  mc_init();               /* 清零 PID 闭环状态 */
  up_init();               /* 清零 USB 车控协议状态 */

  /* ===== 单轮测试通道：KEY1(PE3) 按下 → 电机闭环测试 =====
   * 按住 KEY1 上电即进入单电机闭环测试，用于底盘调试；
   * 否则走正常 USB 车控主循环。 */
  if (HAL_GPIO_ReadPin(KEY1_GPIO_Port, KEY1_Pin) == GPIO_PIN_RESET)
  {
    HAL_Delay(100);                  /* 消抖 */
    if (HAL_GPIO_ReadPin(KEY1_GPIO_Port, KEY1_Pin) == GPIO_PIN_RESET)
    {
      single_motor_test_run();       /* 死循环：电机1闭环 + OLED 显示 */
    }
  }
  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  /* ===== USB 车控主循环 =====
   * 上电先自动标定编码器方向（四轮依次短转约 2.8s），
   * 之后每 10ms：up_poll()（解析 USB 命令 + 心跳状态上报）
   *           + mc_update_all()（四电机闭环）。
   * 底盘运动由地瓜派通过 USB 下发，见 docs/2.pre/USB车控接口.md。 */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
    up_poll();                                 /* USB 协议：命令分发 + 心跳 */
    mc_update_all();                           /* 10ms 周期闭环  */
    HAL_Delay(10);                             /* 10ms 周期      */
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};
  RCC_PeriphCLKInitTypeDef PeriphClkInit = {0};

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_ON;
  RCC_OscInitStruct.HSEPredivValue = RCC_HSE_PREDIV_DIV1;
  RCC_OscInitStruct.HSIState = RCC_HSI_ON;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLMUL = RCC_PLL_MUL9;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV2;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_2) != HAL_OK)
  {
    Error_Handler();
  }
  PeriphClkInit.PeriphClockSelection = RCC_PERIPHCLK_USB;
  PeriphClkInit.UsbClockSelection = RCC_USBCLKSOURCE_PLL_DIV1_5;
  if (HAL_RCCEx_PeriphCLKConfig(&PeriphClkInit) != HAL_OK)
  {
    Error_Handler();
  }
}

/* USER CODE BEGIN 4 */

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
