/***************************************************************
* Заголовочный файл бинарного протокола связи с MATLAB         *
*                                                              *
* Согласовано со следующими документами:                      *
*   - МКС API v.1.3.pdf                                        *
*   - Бинарный протокол МКС v.1.3.pdf                          *
*                                                              *
* Транспорт: USB CDC (bare-metal). CRC8 по LEN+CMD_ID+PARAMS   *
* (без SYNC). SYNC на проводе: 0xAA, затем 0x55.               *
*                                                              *
* Copyright (C) NCPR LLC                                       *
* https://flexlab.ru                                           *
***************************************************************/

#ifndef PROTOCOL_H
#define PROTOCOL_H

#include <stdint.h>
#include <stdbool.h>

/* ===========================================================================
 * КОНСТАНТЫ ПРОТОКОЛА
 * =========================================================================== */
#define PROTOCOL_MAX_PAYLOAD     255
#define PROTOCOL_SYNC_WORD       0xAA55

/* ===========================================================================
 * КОДЫ КОМАНД (CMD_ID) ИЗ API v1.3
 * =========================================================================== */
typedef enum {
    /* Системные команды */
    CMD_PING                = 0x00,
    CMD_INIT                = 0x01,
    CMD_GET_STATUS          = 0x02,
    CMD_RESET_RADIO         = 0x03,
    
    /* Конфигурация */
    CMD_SET_PHY_CONFIG      = 0x10,
    CMD_SET_TX_POWER        = 0x11,
    
    /* Передатчик */
    CMD_TX_FRAME            = 0x20,
    CMD_TX_PERIODIC         = 0x21,
    CMD_TX_STOP             = 0x22,
    
    /* Приемник */
    CMD_RX_START            = 0x30,
    CMD_RX_STOP             = 0x31,
    
    /* Диагностика */
    CMD_GET_SIGNAL_METRICS  = 0x40,
    CMD_GET_CIR             = 0x41,
    
    /* Эксперименты */
    CMD_START_EXPERIMENT    = 0x50,
    CMD_STOP_EXPERIMENT     = 0x51,
    CMD_TX_SWEEP            = 0x60,
    CMD_DETECTOR_TEST       = 0x61
} CommandID;

/* ===========================================================================
 * КОДЫ СОСТОЯНИЯ ОТВЕТА (STATUS) ИЗ БИНАРНОГО ПРОТОКОЛА v1.3
 * =========================================================================== */
typedef enum {
    STATUS_OK               = 0x00,
    STATUS_UNKNOWN_CMD      = 0x01,
    STATUS_INVALID_PARAM    = 0x02,
    STATUS_RADIO_BUSY       = 0x03,
    STATUS_RADIO_ERROR      = 0x04,
    STATUS_BUFFER_OVERFLOW  = 0x05,
    STATUS_TIMEOUT          = 0x06,
    STATUS_INTERNAL_ERROR   = 0x07
} ResponseStatus;

/* ===========================================================================
 * СТРУКТУРЫ ДЛЯ ХРАНЕНИЯ ПАРАМЕТРОВ КОМАНД И ОТВЕТОВ
 * =========================================================================== */
typedef struct {
    CommandID cmd_id;
    uint8_t params[PROTOCOL_MAX_PAYLOAD];
    uint8_t params_len;
} CommandPacket;

typedef struct {
    ResponseStatus status;
    uint8_t data[PROTOCOL_MAX_PAYLOAD];
    uint8_t data_len;
} ResponsePacket;

/* Тип для указателя на функцию-обработчик команды */
typedef ResponseStatus (*CommandHandler)(const uint8_t* params, uint8_t params_len,
                                          uint8_t** out_data, uint8_t* out_len);

/* ===========================================================================
 * ПРОТОТИПЫ ФУНКЦИЙ
 * =========================================================================== */

/* Инициализация протокола */
void PROTOCOL_Init(void);

/* Обработка входящего байта из USB */
void PROTOCOL_ProcessByte(uint8_t byte);

/* Построение пакета ответа */
uint8_t* PROTOCOL_BuildResponsePacket(ResponseStatus status, const uint8_t* data,
                                       uint8_t data_len, uint8_t* packet_len);

/* Регистрация обработчика команды */
void PROTOCOL_RegisterHandler(CommandID cmd, CommandHandler handler);

/* Регистрация всех обработчиков команд API */
void PROTOCOL_RegisterAllHandlers(void);

#endif /* PROTOCOL_H */