/***************************************************************
* Отладочная консоль для управления через UART                 *
*                                                              *
* Версия 1.3.05 (Команда devid через очередь)                 *
*                                                              *
* Copyright (C) NCPR LLC                                       *
* https://flexlab.ru                                           *
***************************************************************/

#include "debug_console.h"
#include "dw1000_driver.h"
#include "FreeRTOS.h"
#include "cmsis_os.h"
#include "queue.h"
#include "semphr.h"
#include "radio_defs.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include <stdlib.h>

/* ===========================================================================
 * ВНЕШНИЕ ПЕРЕМЕННЫЕ (определены в main.c)
 * =========================================================================== */
extern DW1000_Device tx_device;
extern DW1000_Device rx_device;
extern QueueHandle_t xUSB_TxQueue;
extern QueueHandle_t xRadioCommandQueue;
extern volatile bool experiment_running;

/* ===========================================================================
 * ЛОКАЛЬНЫЕ ПЕРЕМЕННЫЕ
 * =========================================================================== */
static UART_HandleTypeDef* console_uart = NULL;
static DebugLevel current_level = DEBUG_LEVEL_INFO;
static char print_buffer[256];

/* Очередь для команд из консоли (определена здесь, extern в .h) */
QueueHandle_t xConsoleCommandQueue;

/* ===========================================================================
 * UART ПРЕРЫВАНИЯ И КОЛЬЦЕВОЙ БУФЕР
 * =========================================================================== */
#define UART_RX_BUFFER_SIZE     128
static uint8_t uart_rx_buffer[UART_RX_BUFFER_SIZE];
static volatile uint16_t uart_rx_head = 0;
static volatile uint16_t uart_rx_tail = 0;
static uint8_t uart_rx_char;

/* ===========================================================================
 * БАЗОВЫЕ ФУНКЦИИ ВЫВОДА
 * =========================================================================== */

static void uart_send(const char* str)
{
    if (console_uart != NULL) {
        HAL_UART_Transmit(console_uart, (uint8_t*)str, strlen(str), pdMS_TO_TICKS(100));
    }
}

void DEBUG_Print(const char* str)
{
    uart_send(str);
}

void DEBUG_Println(const char* str)
{
    uart_send(str);
    uart_send("\r\n");
}

void DEBUG_Printf(const char* format, ...)
{
    va_list args;
    va_start(args, format);
    vsnprintf(print_buffer, sizeof(print_buffer), format, args);
    va_end(args);
    uart_send(print_buffer);
}

void DEBUG_PrintHex(const uint8_t* data, uint16_t len)
{
    char hex[5];
    for (uint16_t i = 0; i < len; i++) {
        snprintf(hex, sizeof(hex), "%02X ", data[i]);
        uart_send(hex);
    }
    uart_send("\r\n");
}

/* ===========================================================================
 * UART CALLBACK (вызывается из прерывания)
 * =========================================================================== */
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance == USART2) {
        uint16_t next_head = (uart_rx_head + 1) % UART_RX_BUFFER_SIZE;

        if (next_head != uart_rx_tail) {
            uart_rx_buffer[uart_rx_head] = uart_rx_char;
            uart_rx_head = next_head;
        }

        /* Продолжаем приём */
        HAL_UART_Receive_IT(console_uart, &uart_rx_char, 1);
    }
}

/* ===========================================================================
 * Функция чтения символа из буфера (неблокирующая)
 * =========================================================================== */
static int uart_getchar(void)
{
    if (uart_rx_head != uart_rx_tail) {
        uint8_t c = uart_rx_buffer[uart_rx_tail];
        uart_rx_tail = (uart_rx_tail + 1) % UART_RX_BUFFER_SIZE;
        return c;
    }
    return -1;
}

/* ===========================================================================
 * ИНИЦИАЛИЗАЦИЯ
 * =========================================================================== */

void DEBUG_CONSOLE_Init(UART_HandleTypeDef* huart)
{
    console_uart = huart;
    xConsoleCommandQueue = xQueueCreate(10, sizeof(char[64]));

    /* Очищаем буфер */
    uart_rx_head = 0;
    uart_rx_tail = 0;

    /* Запускаем приём в режиме прерываний */
    HAL_UART_Receive_IT(console_uart, &uart_rx_char, 1);

    if (xConsoleCommandQueue == NULL) {
        DEBUG_Printf("[DEBUG] Failed to create console queue\r\n");
    } else {
        DEBUG_Printf("[DEBUG] Console initialized\r\n");
    }
}

void DEBUG_SetUART(UART_HandleTypeDef* huart)
{
    console_uart = huart;
}

void DEBUG_SetLevel(DebugLevel level)
{
    current_level = level;
}

/* ===========================================================================
 * ФУНКЦИИ СТАТУСА
 * =========================================================================== */

void DEBUG_PrintStatus(void)
{
    DEBUG_Printf("\r\n=== System Status ===\r\n");
    DEBUG_Printf("TX: channel=%d, rate=%d, preamble=%d, PRF=%d\r\n",
                 tx_device.channel, tx_device.data_rate,
                 tx_device.preamble_len, tx_device.prf);
    DEBUG_Printf("RX: channel=%d, rate=%d, preamble=%d, PRF=%d\r\n",
                 rx_device.channel, rx_device.data_rate,
                 rx_device.preamble_len, rx_device.prf);
    DEBUG_Printf("Experiment running: %s\r\n", experiment_running ? "YES" : "NO");
    DEBUG_Printf("=====================\r\n");
}

void DEBUG_PrintEvent(RadioEvent_t* event)
{
    if (event == NULL) return;
    DEBUG_Printf("[EVENT] dev=%p, type=%d, ts=%lu\r\n",
                 event->dev, event->event, event->timestamp);
}

void DEBUG_PrintCommand(RadioCommand_t* cmd)
{
    if (cmd == NULL) return;
    DEBUG_Printf("[CMD] type=%d, dev=%p\r\n", cmd->cmd, cmd->dev);
}

/* ===========================================================================
 * ОБРАБОТЧИКИ КОМАНД
 * =========================================================================== */

static uint8_t parse_hex_byte(const char* s)
{
    uint8_t result = 0;
    if (s[0] >= '0' && s[0] <= '9') result = (s[0] - '0') << 4;
    else if (s[0] >= 'A' && s[0] <= 'F') result = (s[0] - 'A' + 10) << 4;
    else if (s[0] >= 'a' && s[0] <= 'f') result = (s[0] - 'a' + 10) << 4;

    if (s[1] >= '0' && s[1] <= '9') result |= (s[1] - '0');
    else if (s[1] >= 'A' && s[1] <= 'F') result |= (s[1] - 'A' + 10);
    else if (s[1] >= 'a' && s[1] <= 'f') result |= (s[1] - 'a' + 10);

    return result;
}

static void send_radio_command(RadioCommand_t* cmd)
{
    if (xQueueSend(xRadioCommandQueue, &cmd, pdMS_TO_TICKS(100)) != pdPASS) {
        DEBUG_Println("ERROR: Radio command queue full");
        free(cmd);
    }
}

static void cmd_tx(char* param)
{
    uint16_t len;
    uint8_t data[256];
    char* hex_str;

    len = atoi(param);
    if (len == 0 || len > 256) {
        DEBUG_Println("ERROR: Invalid length (1-256)");
        return;
    }

    hex_str = param;
    while (*hex_str >= '0' && *hex_str <= '9') hex_str++;
    while (*hex_str == ' ') hex_str++;

    if (strlen(hex_str) < len * 2) {
        DEBUG_Println("ERROR: Not enough hex data");
        return;
    }

    for (uint16_t i = 0; i < len; i++) {
        data[i] = parse_hex_byte(hex_str + i * 2);
    }

    RadioCommand_t* cmd = malloc(sizeof(RadioCommand_t) + len);
    if (!cmd) {
        DEBUG_Println("ERROR: Out of memory");
        return;
    }

    cmd->cmd = RADIO_CMD_TX_FRAME;
    cmd->dev = &tx_device;
    cmd->params.tx_frame.len = len;
    memcpy(cmd->params.tx_frame.data, data, len);

    DEBUG_Printf("Sending %d bytes...\r\n", len);
    send_radio_command(cmd);
}

static void cmd_rx(char* param)
{
    RadioCommand_t* cmd = malloc(sizeof(RadioCommand_t));
    if (!cmd) {
        DEBUG_Println("ERROR: Out of memory");
        return;
    }

    if (strcmp(param, "on") == 0) {
        cmd->cmd = RADIO_CMD_RX_START;
        cmd->dev = &rx_device;
        DEBUG_Println("Starting receiver...");
    } else if (strcmp(param, "off") == 0) {
        cmd->cmd = RADIO_CMD_RX_STOP;
        cmd->dev = &rx_device;
        DEBUG_Println("Stopping receiver...");
    } else {
        DEBUG_Println("Usage: rx on/off");
        free(cmd);
        return;
    }

    send_radio_command(cmd);
}

static void cmd_power(char* param)
{
    char target[4];
    uint8_t power;

    if (sscanf(param, "%3s %hhu", target, &power) != 2) {
        DEBUG_Println("Usage: power <tx/rx> <0-31>");
        return;
    }

    if (power > 31) {
        DEBUG_Println("Power must be 0-31");
        return;
    }

    DW1000_Device* dev = NULL;
    if (strcmp(target, "tx") == 0) dev = &tx_device;
    else if (strcmp(target, "rx") == 0) dev = &rx_device;
    else {
        DEBUG_Println("Target must be 'tx' or 'rx'");
        return;
    }

    uint32_t power_val = (power << 0) | (power << 8) | (power << 16) | (power << 24);
    DW1000_SetTxPower(dev, power_val);
    DEBUG_Printf("Power set to %d for %s\r\n", power, target);
}

static void cmd_channel(char* param)
{
    uint8_t ch = atoi(param);
    if (ch < 1 || ch > 7 || ch == 6) {
        DEBUG_Println("Channel must be 1-5 or 7");
        return;
    }

    DW1000_SetChannel(&tx_device, ch);
    DW1000_SetChannel(&rx_device, ch);
    DEBUG_Printf("Channel set to %d\r\n", ch);
}

static void cmd_reset(char* param)
{
    if (strcmp(param, "tx") == 0) {
        DW1000_SoftReset(&tx_device);
        DEBUG_Println("TX device reset");
    } else if (strcmp(param, "rx") == 0) {
        DW1000_SoftReset(&rx_device);
        DEBUG_Println("RX device reset");
    } else if (strcmp(param, "all") == 0) {
        DW1000_SoftReset(&tx_device);
        DW1000_SoftReset(&rx_device);
        DEBUG_Println("Both devices reset");
    } else {
        DEBUG_Println("Usage: reset <tx/rx/all>");
    }
}

static void cmd_cir(char* param)
{
    uint16_t offset, length;

    if (sscanf(param, "%hu %hu", &offset, &length) != 2) {
        DEBUG_Println("Usage: cir <offset> <length>");
        return;
    }

    if (length > 32) length = 32;

    uint8_t cir_data[128];
    DW1000_ReadRegister(&rx_device, DW1000_ACC_MEM, offset * 4, cir_data, length * 4);

    DEBUG_Printf("CIR offset=%d, length=%d:\r\n", offset, length);
    for (uint16_t i = 0; i < length; i++) {
        int16_t i_val = (int16_t)(cir_data[i*4] | (cir_data[i*4+1] << 8));
        int16_t q_val = (int16_t)(cir_data[i*4+2] | (cir_data[i*4+3] << 8));
        DEBUG_Printf("[%3d] I=%6d Q=%6d\r\n", i, i_val, q_val);
    }
}

static void cmd_metrics(void)
{
    uint32_t reg;
    uint16_t rxpacc, fp_index, std_noise;
    int16_t fp_ampl1;

    DW1000_ReadRegister32(&rx_device, DW1000_RX_FINFO, 0, &reg);
    rxpacc = (reg >> 20) & 0xFFF;

    DW1000_ReadRegister32(&rx_device, DW1000_RX_FQUAL, 0, &reg);
    std_noise = reg & 0xFFFF;

    DW1000_ReadRegister(&rx_device, DW1000_RX_TIME, 4, (uint8_t*)&reg, 4);
    fp_index = reg & 0xFFFF;
    fp_ampl1 = (reg >> 16) & 0xFFFF;

    DEBUG_Printf("Signal Metrics:\r\n");
    DEBUG_Printf("  RXPACC:    %d\r\n", rxpacc);
    DEBUG_Printf("  FP_INDEX:  %d\r\n", fp_index);
    DEBUG_Printf("  FP_AMPL1:  %d\r\n", fp_ampl1);
    DEBUG_Printf("  STD_NOISE: %d\r\n", std_noise);
}

static void cmd_exp(char* param)
{
    if (strcmp(param, "start") == 0) {
        experiment_running = true;
        DEBUG_Println("Experiment started");
    } else if (strcmp(param, "stop") == 0) {
        experiment_running = false;
        DEBUG_Println("Experiment stopped");
    } else {
        DEBUG_Println("Usage: exp start/stop");
    }
}

static void cmd_log(char* param)
{
    DebugLevel new_level;

    if (strcmp(param, "error") == 0) new_level = DEBUG_LEVEL_ERROR;
    else if (strcmp(param, "warning") == 0) new_level = DEBUG_LEVEL_WARNING;
    else if (strcmp(param, "info") == 0) new_level = DEBUG_LEVEL_INFO;
    else if (strcmp(param, "debug") == 0) new_level = DEBUG_LEVEL_DEBUG;
    else if (strcmp(param, "verbose") == 0) new_level = DEBUG_LEVEL_VERBOSE;
    else {
        DEBUG_Println("Levels: error, warning, info, debug, verbose");
        return;
    }

    DEBUG_SetLevel(new_level);
    DEBUG_Printf("Log level set to: %s\r\n", param);
}

/* ===========================================================================
 * НОВАЯ КОМАНДА: ЧТЕНИЕ DEV_ID ЧЕРЕЗ ОЧЕРЕДЬ
 * =========================================================================== */

static void cmd_devid(void)
{
    RadioCommand_t* cmd = malloc(sizeof(RadioCommand_t));
    if (!cmd) {
        DEBUG_Println("ERROR: Out of memory");
        return;
    }

    cmd->cmd = RADIO_CMD_READ_DEV_ID;
    cmd->dev = &tx_device;
    cmd->flags.wait_for_completion = 0;
    cmd->flags.generate_response = 1;

    if (xQueueSend(xRadioCommandQueue, &cmd, pdMS_TO_TICKS(100)) != pdPASS) {
        DEBUG_Println("ERROR: Radio command queue full");
        free(cmd);
    } else {
        DEBUG_Println("DEV_ID request sent to radio task");
    }
}

/* ===========================================================================
 * ЗАДАЧА КОНСОЛИ (с использованием кольцевого буфера)
 * =========================================================================== */

void DEBUG_Console_Task(void* pvParameters)
{
    char cmd_line[64];
    char cmd_name[32];
    char cmd_param[32];
    int idx = 0;
    int c;

    DEBUG_Println("\r\n=== MKS Debug Console v1.3.05 ===\r\n");
    DEBUG_Println("Type 'help' for available commands\r\n");
    DEBUG_Print("> ");

    while (1) {
        /* Читаем символ из кольцевого буфера (неблокирующе) */
        c = uart_getchar();

        if (c != -1) {
            char rx_char = (char)c;

            if (rx_char == '\r' || rx_char == '\n') {
                DEBUG_Print("\r\n");
                if (idx > 0) {
                    cmd_line[idx] = '\0';

                    if (sscanf(cmd_line, "%31s %31[^\n]", cmd_name, cmd_param) >= 1) {
                        if (strcmp(cmd_name, "help") == 0) {
                            DEBUG_Println("\r\nAvailable commands:");
                            DEBUG_Println("  help                 - show this help");
                            DEBUG_Println("  status               - show system status");
                            DEBUG_Println("  tx <len> <hex>       - transmit frame");
                            DEBUG_Println("  rx on/off            - start/stop receiver");
                            DEBUG_Println("  power <tx/rx> <0-31> - set TX power");
                            DEBUG_Println("  channel <1-7>        - set channel");
                            DEBUG_Println("  reset <tx/rx/all>    - reset radio");
                            DEBUG_Println("  cir <offset> <len>   - read CIR (max 32)");
                            DEBUG_Println("  metrics              - show signal metrics");
                            DEBUG_Println("  exp start/stop       - experiment control");
                            DEBUG_Println("  log <level>          - set debug level");
                            DEBUG_Println("  devid                - read DWM1000 DEV_ID");
                        } else if (strcmp(cmd_name, "status") == 0) {
                            DEBUG_PrintStatus();
                        } else if (strcmp(cmd_name, "tx") == 0) {
                            cmd_tx(cmd_param);
                        } else if (strcmp(cmd_name, "rx") == 0) {
                            cmd_rx(cmd_param);
                        } else if (strcmp(cmd_name, "power") == 0) {
                            cmd_power(cmd_param);
                        } else if (strcmp(cmd_name, "channel") == 0) {
                            cmd_channel(cmd_param);
                        } else if (strcmp(cmd_name, "reset") == 0) {
                            cmd_reset(cmd_param);
                        } else if (strcmp(cmd_name, "cir") == 0) {
                            cmd_cir(cmd_param);
                        } else if (strcmp(cmd_name, "metrics") == 0) {
                            cmd_metrics();
                        } else if (strcmp(cmd_name, "exp") == 0) {
                            cmd_exp(cmd_param);
                        } else if (strcmp(cmd_name, "log") == 0) {
                            cmd_log(cmd_param);
                        } else if (strcmp(cmd_name, "devid") == 0) {
                            cmd_devid();
                        } else {
                            DEBUG_Printf("Unknown command: %s\r\n", cmd_name);
                            DEBUG_Println("Type 'help' for available commands");
                        }
                    }
                    idx = 0;
                }
                DEBUG_Print("> ");
            } else if (rx_char == '\b' || rx_char == 127) {
                if (idx > 0) {
                    idx--;
                    DEBUG_Print("\b \b");
                }
            } else if (idx < (int)sizeof(cmd_line) - 1) {
                cmd_line[idx++] = rx_char;
                char temp[2] = {rx_char, '\0'};
                DEBUG_Print(temp);
            }
        }

        /* Небольшая задержка для снижения нагрузки */
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
