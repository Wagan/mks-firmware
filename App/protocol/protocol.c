/***************************************************************
* Реализация бинарного протокола связи с MATLAB                *
*                                                              *
* Версия 1.3.02 (Рефакторинг)                                 *
*                                                              *
* Согласовано со следующими документами:                      *
*   - МКС API v.1.3.pdf                                        *
*   - Бинарный протокол МКС v.1.3.pdf                          *
*                                                              *
* Copyright (C) NCPR LLC                                       *
* https://flexlab.ru                                           *
***************************************************************/

#include "protocol.h"
#include "dw1000_driver.h"
#include "radio_defs.h"
#include "debug_console.h"
#include <string.h>
#include <stdlib.h>

/* ===========================================================================
 * ВНЕШНИЕ ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ (определены в main.c)
 * =========================================================================== */
extern DW1000_Device tx_device;
extern DW1000_Device rx_device;
extern QueueHandle_t xRadioCommandQueue;
extern QueueHandle_t xUSB_TxQueue;
extern SemaphoreHandle_t xTxCompleteSemaphore;
extern SemaphoreHandle_t xRxCompleteSemaphore;
extern volatile bool experiment_running;

/* ===========================================================================
 * КОНСТАНТЫ И МАКРОСЫ ПРОТОКОЛА
 * =========================================================================== */
#define PROTOCOL_SYNC_WORD       0xAA55
#define PROTOCOL_MAX_PAYLOAD      255
#define PROTOCOL_MAX_PACKET       (2 + 1 + 1 + 255 + 1)

#define GET_U16LE(p) ((uint16_t)(p)[0] | ((uint16_t)(p)[1] << 8))
#define PUT_U16LE(p, v) do { (p)[0] = (v) & 0xFF; (p)[1] = ((v) >> 8) & 0xFF; } while(0)

/* ===========================================================================
 * ПАРСЕР ПАКЕТОВ
 * =========================================================================== */
typedef enum {
    STATE_WAIT_SYNC1,
    STATE_WAIT_SYNC2,
    STATE_WAIT_LEN,
    STATE_WAIT_CMD,
    STATE_WAIT_PARAMS,
    STATE_WAIT_CRC
} ParserState;

static struct {
    ParserState state;
    uint8_t buffer[PROTOCOL_MAX_PACKET];
    uint8_t index;
    uint8_t expected_len;
    uint8_t cmd;
    uint8_t params_len;
    uint8_t crc_received;
} parser;

/* Таблица обработчиков команд */
static CommandHandler handlers[256] = {NULL};

/* ===========================================================================
 * ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
 * =========================================================================== */

/**
 * @brief Вычисление CRC8 (полином 0x07, начальное 0x00)
 */
static uint8_t PROTOCOL_CalculateCRC(const uint8_t* data, uint8_t len)
{
    uint8_t crc = 0;
    for (uint8_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (uint8_t j = 0; j < 8; j++) {
            if (crc & 0x80)
                crc = (crc << 1) ^ 0x07;
            else
                crc <<= 1;
        }
    }
    return crc;
}

/**
 * @brief Построить пакет ответа
 */
uint8_t* PROTOCOL_BuildResponsePacket(ResponseStatus status, const uint8_t* data,
                                        uint8_t data_len, uint8_t* packet_len)
{
    static uint8_t packet[PROTOCOL_MAX_PACKET];
    uint8_t len_field = 1 + data_len;
    uint8_t index = 0;

    packet[index++] = PROTOCOL_SYNC_WORD & 0xFF;
    packet[index++] = (PROTOCOL_SYNC_WORD >> 8) & 0xFF;
    packet[index++] = len_field;
    packet[index++] = (uint8_t)status;

    if (data_len > 0 && data != NULL) {
        memcpy(&packet[index], data, data_len);
        index += data_len;
    }

    packet[index++] = PROTOCOL_CalculateCRC(&packet[2], len_field + 1);
    *packet_len = index;
    return packet;
}

/**
 * @brief Отправить ответ через USB
 */
static void PROTOCOL_SendResponse(ResponseStatus status, const uint8_t* data, uint8_t data_len)
{
    uint8_t packet_len;
    uint8_t* packet = PROTOCOL_BuildResponsePacket(status, data, data_len, &packet_len);

    uint8_t* tx_buffer = malloc(packet_len);
    if (tx_buffer) {
        memcpy(tx_buffer, packet, packet_len);
        if (xQueueSend(xUSB_TxQueue, &tx_buffer, 0) != pdPASS) {
            free(tx_buffer);
        }
    }
}

/* ===========================================================================
 * ПАРСЕР ВХОДЯЩИХ БАЙТОВ
 * =========================================================================== */

void PROTOCOL_Init(void)
{
    parser.state = STATE_WAIT_SYNC1;
    parser.index = 0;
}

void PROTOCOL_ProcessByte(uint8_t byte)
{
    switch (parser.state) {
        case STATE_WAIT_SYNC1:
            if (byte == (PROTOCOL_SYNC_WORD & 0xFF)) parser.state = STATE_WAIT_SYNC2;
            break;

        case STATE_WAIT_SYNC2:
            if (byte == (PROTOCOL_SYNC_WORD >> 8)) {
                parser.state = STATE_WAIT_LEN;
                parser.index = 0;
                parser.buffer[parser.index++] = byte;
            } else {
                parser.state = STATE_WAIT_SYNC1;
            }
            break;

        case STATE_WAIT_LEN:
            parser.buffer[parser.index++] = byte;
            parser.expected_len = byte;
            parser.state = STATE_WAIT_CMD;
            break;

        case STATE_WAIT_CMD:
            parser.buffer[parser.index++] = byte;
            parser.cmd = byte;
            parser.params_len = parser.expected_len - 1;
            parser.state = (parser.params_len == 0) ? STATE_WAIT_CRC : STATE_WAIT_PARAMS;
            break;

        case STATE_WAIT_PARAMS:
            parser.buffer[parser.index++] = byte;
            if (--parser.params_len == 0) parser.state = STATE_WAIT_CRC;
            break;

        case STATE_WAIT_CRC:
            parser.buffer[parser.index++] = byte;
            parser.crc_received = byte;

            uint8_t crc_len = parser.expected_len + 1;
            uint8_t calc_crc = PROTOCOL_CalculateCRC(&parser.buffer[0], crc_len);

            if (calc_crc == parser.crc_received) {
                CommandPacket cmd_pkt;
                cmd_pkt.cmd_id = parser.cmd;
                cmd_pkt.params_len = parser.expected_len - 1;
                if (cmd_pkt.params_len > 0) {
                    memcpy(cmd_pkt.params, &parser.buffer[1], cmd_pkt.params_len);
                }

                ResponseStatus status = STATUS_UNKNOWN_CMD;
                uint8_t response_data[PROTOCOL_MAX_PAYLOAD];
                uint8_t response_len = 0;
                uint8_t* out_data_ptr = response_data;

                if (handlers[parser.cmd] != NULL) {
                    status = handlers[parser.cmd](cmd_pkt.params, cmd_pkt.params_len,
                                                   &out_data_ptr, &response_len);
                } else {
                    status = STATUS_UNKNOWN_CMD;
                }

                PROTOCOL_SendResponse(status, out_data_ptr, response_len);
            } else {
                PROTOCOL_SendResponse(STATUS_INTERNAL_ERROR, NULL, 0);
            }
            parser.state = STATE_WAIT_SYNC1;
            break;

        default:
            parser.state = STATE_WAIT_SYNC1;
            break;
    }
}

/* ===========================================================================
 * РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ
 * =========================================================================== */
void PROTOCOL_RegisterHandler(CommandID cmd, CommandHandler handler)
{
    handlers[cmd] = handler;
}

/* ===========================================================================
 * ОБРАБОТЧИКИ КОМАНД API
 * =========================================================================== */

static ResponseStatus HandlePING(const uint8_t* params, uint8_t params_len,
                                  uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len;
    *out_len = 0;
    return STATUS_OK;
}

static ResponseStatus HandleINIT(const uint8_t* params, uint8_t params_len,uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len;
    DEBUG_Println("[PROTOCOL] INIT command");
    if (DW1000_Init(&tx_device) != DW1000_OK) return STATUS_RADIO_ERROR;

    /* Копируем состояние в rx_device (один и тот же модуль) */
    rx_device.initialized = tx_device.initialized;
    rx_device.channel = tx_device.channel;
    rx_device.data_rate = tx_device.data_rate;
    rx_device.preamble_len = tx_device.preamble_len;
    rx_device.prf = tx_device.prf;

    *out_len = 0;
    return STATUS_OK;
}

static ResponseStatus HandleGET_STATUS(const uint8_t* params, uint8_t params_len,
                                        uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len;
    static uint8_t data[7];
    data[0] = 0; // TX_state
    data[1] = 0; // RX_state
    data[2] = tx_device.channel;
    data[3] = tx_device.data_rate;
    data[4] = tx_device.preamble_len & 0xFF;
    data[5] = (tx_device.preamble_len >> 8) & 0xFF;
    data[6] = tx_device.prf;
    *out_data = data;
    *out_len = 7;
    return STATUS_OK;
}

static ResponseStatus HandleRESET_RADIO(const uint8_t* params, uint8_t params_len,
                                         uint8_t** out_data, uint8_t* out_len)
{
    if (params_len < 1) return STATUS_INVALID_PARAM;
    uint8_t target = params[0];
    DW1000_Status status = DW1000_OK;
    if (target == 0 || target == 1) status = DW1000_SoftReset(&tx_device);
    if (status == DW1000_OK && (target == 0 || target == 2))
        status = DW1000_SoftReset(&rx_device);
    *out_len = 0;
    return (status == DW1000_OK) ? STATUS_OK : STATUS_RADIO_ERROR;
}

static ResponseStatus HandleSET_PHY_CONFIG(const uint8_t* params, uint8_t params_len,
                                            uint8_t** out_data, uint8_t* out_len)
{
    if (params_len < 8) return STATUS_INVALID_PARAM;
    uint8_t target = params[0];
    uint8_t channel = params[1];
    uint8_t data_rate = params[2];
    uint16_t preamble_len = GET_U16LE(&params[3]);
    uint8_t preamble_code = params[5];
    uint8_t prf = params[6];
    uint8_t pac_size = params[7];

    DW1000_Device* dev = (target == 1) ? &tx_device : (target == 2) ? &rx_device : NULL;
    if (dev == NULL) return STATUS_INVALID_PARAM;

    DW1000_SetPhyConfig(dev, channel, data_rate, preamble_len, preamble_code, prf, pac_size);
    *out_len = 0;
    return STATUS_OK;
}

static ResponseStatus HandleSET_TX_POWER(const uint8_t* params, uint8_t params_len,
                                          uint8_t** out_data, uint8_t* out_len)
{
    if (params_len < 2) return STATUS_INVALID_PARAM;
    uint8_t target = params[0];
    uint8_t power_level = params[1];
    DW1000_Device* dev = (target == 1) ? &tx_device : (target == 2) ? &rx_device : NULL;
    if (dev == NULL) return STATUS_INVALID_PARAM;
    DW1000_SetTxPower(dev, power_level);
    *out_len = 0;
    return STATUS_OK;
}

static ResponseStatus HandleTX_FRAME(const uint8_t* params, uint8_t params_len,
                                      uint8_t** out_data, uint8_t* out_len)
{
    if (params_len < 3) return STATUS_INVALID_PARAM;
    uint16_t length = GET_U16LE(params);
    if (length != params_len - 2) return STATUS_INVALID_PARAM;

    RadioCommand_t* cmd = malloc(sizeof(RadioCommand_t) + length);
    if (!cmd) return STATUS_INTERNAL_ERROR;

    cmd->cmd = RADIO_CMD_TX_FRAME;
    cmd->dev = &tx_device;
    cmd->params.tx_frame.len = length;
    memcpy(cmd->params.tx_frame.data, params + 2, length);

    if (xQueueSend(xRadioCommandQueue, &cmd, pdMS_TO_TICKS(100)) != pdPASS) {
        free(cmd);
        return STATUS_RADIO_BUSY;
    }

    if (xSemaphoreTake(xTxCompleteSemaphore, pdMS_TO_TICKS(1000)) == pdTRUE) {
        *out_len = 0;
        return STATUS_OK;
    } else {
        return STATUS_TIMEOUT;
    }
}

static ResponseStatus HandleTX_PERIODIC(const uint8_t* params, uint8_t params_len,
                                         uint8_t** out_data, uint8_t* out_len)
{
    if (params_len < 4) return STATUS_INVALID_PARAM;
    uint16_t period_ms = GET_U16LE(params);
    uint16_t length = GET_U16LE(params + 2);
    if (length != params_len - 4) return STATUS_INVALID_PARAM;

    RadioCommand_t* cmd = malloc(sizeof(RadioCommand_t) + length);
    if (!cmd) return STATUS_INTERNAL_ERROR;

    cmd->cmd = RADIO_CMD_TX_PERIODIC;
    cmd->dev = &tx_device;
    cmd->params.tx_periodic.period_ms = period_ms;
    cmd->params.tx_periodic.len = length;
    memcpy(cmd->params.tx_periodic.data, params + 4, length);

    xQueueSend(xRadioCommandQueue, &cmd, pdMS_TO_TICKS(100));
    *out_len = 0;
    return STATUS_OK;
}

static ResponseStatus HandleTX_STOP(const uint8_t* params, uint8_t params_len,
                                     uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len;
    RadioCommand_t* cmd = malloc(sizeof(RadioCommand_t));
    if (!cmd) return STATUS_INTERNAL_ERROR;
    cmd->cmd = RADIO_CMD_TX_STOP;
    cmd->dev = &tx_device;
    xQueueSend(xRadioCommandQueue, &cmd, pdMS_TO_TICKS(100));
    *out_len = 0;
    return STATUS_OK;
}

static ResponseStatus HandleRX_START(const uint8_t* params, uint8_t params_len,
                                      uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len;
    RadioCommand_t* cmd = malloc(sizeof(RadioCommand_t));
    if (!cmd) return STATUS_INTERNAL_ERROR;
    cmd->cmd = RADIO_CMD_RX_START;
    cmd->dev = &rx_device;
    xQueueSend(xRadioCommandQueue, &cmd, pdMS_TO_TICKS(100));
    *out_len = 0;
    return STATUS_OK;
}

static ResponseStatus HandleRX_STOP(const uint8_t* params, uint8_t params_len,
                                     uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len;
    RadioCommand_t* cmd = malloc(sizeof(RadioCommand_t));
    if (!cmd) return STATUS_INTERNAL_ERROR;
    cmd->cmd = RADIO_CMD_RX_STOP;
    cmd->dev = &rx_device;
    xQueueSend(xRadioCommandQueue, &cmd, pdMS_TO_TICKS(100));
    *out_len = 0;
    return STATUS_OK;
}

static ResponseStatus HandleGET_SIGNAL_METRICS(const uint8_t* params, uint8_t params_len,
                                                 uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len;
    static uint8_t data[8];
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

    data[0] = rxpacc & 0xFF; data[1] = (rxpacc >> 8) & 0xFF;
    data[2] = fp_index & 0xFF; data[3] = (fp_index >> 8) & 0xFF;
    data[4] = fp_ampl1 & 0xFF; data[5] = (fp_ampl1 >> 8) & 0xFF;
    data[6] = std_noise & 0xFF; data[7] = (std_noise >> 8) & 0xFF;

    *out_data = data;
    *out_len = 8;
    return STATUS_OK;
}

static ResponseStatus HandleGET_CIR(const uint8_t* params, uint8_t params_len,
                                     uint8_t** out_data, uint8_t* out_len)
{
    if (params_len < 4) return STATUS_INVALID_PARAM;
    uint16_t offset = GET_U16LE(params);
    uint16_t length = GET_U16LE(params + 2);
    if (length > 1016) return STATUS_INVALID_PARAM;

    static uint8_t cir_data[4064];
    uint8_t raw[4064];

    DW1000_ReadRegister(&rx_device, DW1000_ACC_MEM, offset * 4, raw, length * 4);
    memcpy(cir_data, raw, length * 4);

    *out_data = cir_data;
    *out_len = length * 4;
    return STATUS_OK;
}

static ResponseStatus HandleSTART_EXPERIMENT(const uint8_t* params, uint8_t params_len,
                                               uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len;
    experiment_running = true;
    *out_len = 0;
    return STATUS_OK;
}

static ResponseStatus HandleSTOP_EXPERIMENT(const uint8_t* params, uint8_t params_len,
                                              uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len;
    experiment_running = false;
    *out_len = 0;
    return STATUS_OK;
}

static ResponseStatus HandleTX_SWEEP(const uint8_t* params, uint8_t params_len,
                                       uint8_t** out_data, uint8_t* out_len)
{
    if (params_len < 7) return STATUS_INVALID_PARAM;
    RadioCommand_t* cmd = malloc(sizeof(RadioCommand_t));
    if (!cmd) return STATUS_INTERNAL_ERROR;
    cmd->cmd = RADIO_CMD_TX_SWEEP;
    cmd->dev = &tx_device;
    cmd->params.tx_sweep.channel_start = params[0];
    cmd->params.tx_sweep.channel_end = params[1];
    cmd->params.tx_sweep.power_start = params[2];
    cmd->params.tx_sweep.power_end = params[3];
    cmd->params.tx_sweep.preamble_len = GET_U16LE(&params[4]);
    xQueueSend(xRadioCommandQueue, &cmd, pdMS_TO_TICKS(100));
    *out_len = 0;
    return STATUS_OK;
}

static ResponseStatus HandleDETECTOR_TEST(const uint8_t* params, uint8_t params_len,
                                            uint8_t** out_data, uint8_t* out_len)
{
    if (params_len < 4) return STATUS_INVALID_PARAM;
    uint16_t num_packets = GET_U16LE(params);
    uint8_t power_start = params[2];
    uint8_t power_end = params[3];

    RadioCommand_t* cmd = malloc(sizeof(RadioCommand_t));
    if (!cmd) return STATUS_INTERNAL_ERROR;
    cmd->cmd = RADIO_CMD_DETECTOR_TEST;
    cmd->dev = &tx_device;
    cmd->params.detector_test.num_packets = num_packets;
    cmd->params.detector_test.power_start = power_start;
    cmd->params.detector_test.power_end = power_end;
    xQueueSend(xRadioCommandQueue, &cmd, pdMS_TO_TICKS(100));
    *out_len = 0;
    return STATUS_OK;
}

/* ===========================================================================
 * РЕГИСТРАЦИЯ ВСЕХ ОБРАБОТЧИКОВ
 * =========================================================================== */
void PROTOCOL_RegisterAllHandlers(void)
{
    PROTOCOL_RegisterHandler(CMD_PING, HandlePING);
    PROTOCOL_RegisterHandler(CMD_INIT, HandleINIT);
    PROTOCOL_RegisterHandler(CMD_GET_STATUS, HandleGET_STATUS);
    PROTOCOL_RegisterHandler(CMD_RESET_RADIO, HandleRESET_RADIO);
    PROTOCOL_RegisterHandler(CMD_SET_PHY_CONFIG, HandleSET_PHY_CONFIG);
    PROTOCOL_RegisterHandler(CMD_SET_TX_POWER, HandleSET_TX_POWER);
    PROTOCOL_RegisterHandler(CMD_TX_FRAME, HandleTX_FRAME);
    PROTOCOL_RegisterHandler(CMD_TX_PERIODIC, HandleTX_PERIODIC);
    PROTOCOL_RegisterHandler(CMD_TX_STOP, HandleTX_STOP);
    PROTOCOL_RegisterHandler(CMD_RX_START, HandleRX_START);
    PROTOCOL_RegisterHandler(CMD_RX_STOP, HandleRX_STOP);
    PROTOCOL_RegisterHandler(CMD_GET_SIGNAL_METRICS, HandleGET_SIGNAL_METRICS);
    PROTOCOL_RegisterHandler(CMD_GET_CIR, HandleGET_CIR);
    PROTOCOL_RegisterHandler(CMD_START_EXPERIMENT, HandleSTART_EXPERIMENT);
    PROTOCOL_RegisterHandler(CMD_STOP_EXPERIMENT, HandleSTOP_EXPERIMENT);
    PROTOCOL_RegisterHandler(CMD_TX_SWEEP, HandleTX_SWEEP);
    PROTOCOL_RegisterHandler(CMD_DETECTOR_TEST, HandleDETECTOR_TEST);
}
