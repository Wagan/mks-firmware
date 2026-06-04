#ifndef DEBUG_CONSOLE_H
#define DEBUG_CONSOLE_H

#include "stm32f4xx_hal.h"
#include "FreeRTOS.h"
#include "queue.h"
#include "semphr.h"
#include "radio_defs.h"
#include <stdint.h>
#include <stdarg.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ===========================================================================
 * КОНСТАНТЫ
 * =========================================================================== */
#define CONSOLE_BUFFER_SIZE     256

/* ===========================================================================
 * УРОВНИ ОТЛАДКИ (ДОБАВИТЬ ЭТОТ БЛОК)
 * =========================================================================== */
typedef enum {
    DEBUG_LEVEL_ERROR = 0,
    DEBUG_LEVEL_WARNING,
    DEBUG_LEVEL_INFO,
    DEBUG_LEVEL_DEBUG,
    DEBUG_LEVEL_VERBOSE
} DebugLevel;

/* ===========================================================================
 * ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
 * =========================================================================== */
extern DW1000_Device tx_device;
extern DW1000_Device rx_device;

extern QueueHandle_t xRadioCommandQueue;
extern QueueHandle_t xRadioEventQueue;
extern QueueHandle_t xUSB_TxQueue;
extern QueueHandle_t xConsoleCommandQueue;
extern SemaphoreHandle_t xTxCompleteSemaphore;
extern SemaphoreHandle_t xRxCompleteSemaphore;
extern volatile bool experiment_running;

/* ===========================================================================
 * ПРОТОТИПЫ ФУНКЦИЙ
 * =========================================================================== */
void DEBUG_CONSOLE_Init(UART_HandleTypeDef* huart);
void DEBUG_Console_Task(void* pvParameters);
void DEBUG_Print(const char* str);
void DEBUG_Println(const char* str);
void DEBUG_Printf(const char* format, ...);
void DEBUG_PrintHex(const uint8_t* data, uint16_t len);
void DEBUG_PrintStatus(void);
void DEBUG_SetLevel(DebugLevel level);
void DEBUG_SetUART(UART_HandleTypeDef* huart);
void DEBUG_PrintEvent(RadioEvent_t* event);
void DEBUG_PrintCommand(RadioCommand_t* cmd);

#ifdef __cplusplus
}
#endif

#endif /* DEBUG_CONSOLE_H */
