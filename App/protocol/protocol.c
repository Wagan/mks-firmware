/***************************************************************
* Реализация бинарного протокола связи с MATLAB                *
*                                                              *
* Согласовано со следующими документами:                      *
*   - МКС API v.1.3.pdf                                        *
*   - Бинарный протокол МКС v.1.3.pdf                          *
*                                                              *
* Этап bring-up (bare-metal, без FreeRTOS): ядро протокола     *
* развязано от DecaDriver и RTOS. Обработчики команд               *
* (PING/INIT/GET_STATUS и пр.) подключаются отдельным шагом.   *
*                                                              *
* Copyright (C) NCPR LLC                                       *
* https://flexlab.ru                                           *
***************************************************************/

#include "protocol.h"
#include "usbd_cdc_if.h"   /* CDC_Transmit_FS() — отправка ответа в USB CDC */
#include <string.h>

/* ===========================================================================
 * КОНСТАНТЫ И МАКРОСЫ ПРОТОКОЛА
 * ===========================================================================
 * Кадр на проводе: [SYNC0 SYNC1] [LEN] [CMD_ID] [PARAMS...] [CRC]
 *   SYNC   = 0xAA, 0x55  (старший байт SYNC_WORD первым)
 *   LEN    = длина CMD_ID+PARAMS (т.е. 1 + params_len)
 *   CRC8   = полином 0x07, init 0x00; область — LEN+CMD_ID+PARAMS (БЕЗ SYNC)
 * Ответ: [SYNC0 SYNC1] [LEN] [STATUS] [DATA...] [CRC]; CRC по LEN+STATUS+DATA.
 */
#define PROTOCOL_MAX_PACKET   (2 + 1 + 1 + PROTOCOL_MAX_PAYLOAD + 1)

/* Байты SYNC в порядке следования на проводе (эталон MATLAB: [0xAA 0x55]). */
#define PROTOCOL_SYNC_B0      ((uint8_t)((PROTOCOL_SYNC_WORD >> 8) & 0xFF))  /* 0xAA */
#define PROTOCOL_SYNC_B1      ((uint8_t)(PROTOCOL_SYNC_WORD & 0xFF))         /* 0x55 */

/* Спорный момент протокола (§3.1 handoff): CRC считается по LEN+CMD_ID+PARAMS
 * БЕЗ SYNC. Оставлено макросом как страховка — если эталон заказчика окажется
 * «с SYNC», переключить в 1 в одном месте. 0 = без SYNC (принятое решение). */
#define PROTOCOL_CRC_COVERS_SYNC   0

#define GET_U16LE(p) ((uint16_t)(p)[0] | ((uint16_t)(p)[1] << 8))
#define PUT_U16LE(p, v) do { (p)[0] = (v) & 0xFF; (p)[1] = ((v) >> 8) & 0xFF; } while(0)

/* ===========================================================================
 * ПАРСЕР ПАКЕТОВ
 * ===========================================================================
 * buffer накапливает кадр начиная с SYNC0: [SYNC0 SYNC1 LEN CMD PARAMS...].
 * CRC-байт в buffer не пишется (держим в crc_received). Область CRC выбирается
 * смещением crc_start (0 = с SYNC, 2 = с LEN), см. PROTOCOL_CRC_COVERS_SYNC.
 */
typedef enum {
    STATE_WAIT_SYNC1,
    STATE_WAIT_SYNC2,
    STATE_WAIT_LEN,
    STATE_WAIT_CMD,
    STATE_WAIT_PARAMS,
    STATE_WAIT_CRC
} ParserState;

/* Смещение начала кадра в buffer для полей LEN/CMD/PARAMS (после 2 байт SYNC). */
#define FRAME_OFFSET   2

static struct {
    ParserState state;
    uint8_t buffer[PROTOCOL_MAX_PACKET];
    uint8_t index;
    uint8_t expected_len;   /* LEN = CMD_ID + PARAMS */
    uint8_t cmd;
    uint8_t params_len;     /* оставшиеся к приёму байты PARAMS (счётчик) */
    uint8_t crc_received;
} parser;

/* Таблица обработчиков команд (256 возможных CMD_ID). */
static CommandHandler handlers[256] = {NULL};

/* ===========================================================================
 * ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
 * =========================================================================== */

/**
 * @brief Вычисление CRC8 (полином 0x07, начальное 0x00, без рефлексии/xorout).
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
 * @brief Построить пакет ответа: [SYNC0 SYNC1][LEN][STATUS][DATA...][CRC].
 *        CRC считается по LEN+STATUS+DATA (без SYNC).
 * @return указатель на статический буфер пакета; длина — в *packet_len.
 */
uint8_t* PROTOCOL_BuildResponsePacket(ResponseStatus status, const uint8_t* data,
                                        uint8_t data_len, uint8_t* packet_len)
{
    static uint8_t packet[PROTOCOL_MAX_PACKET];
    uint8_t len_field = 1 + data_len;   /* STATUS + DATA */
    uint8_t index = 0;

    packet[index++] = PROTOCOL_SYNC_B0;   /* 0xAA */
    packet[index++] = PROTOCOL_SYNC_B1;   /* 0x55 */
    packet[index++] = len_field;
    packet[index++] = (uint8_t)status;

    if (data_len > 0 && data != NULL) {
        memcpy(&packet[index], data, data_len);
        index += data_len;
    }

    /* CRC по LEN+STATUS+DATA = &packet[2], (len_field + 1) байт. */
    packet[index++] = PROTOCOL_CalculateCRC(&packet[2], len_field + 1);
    *packet_len = index;
    return packet;
}

/**
 * @brief Отправить ответ через USB CDC (bare-metal, синхронно).
 */
static void PROTOCOL_SendResponse(ResponseStatus status, const uint8_t* data, uint8_t data_len)
{
    uint8_t packet_len;
    uint8_t* packet = PROTOCOL_BuildResponsePacket(status, data, data_len, &packet_len);

    /* На bring-up достаточно одной попытки. Возможен USBD_BUSY, если предыдущая
     * передача ещё не ушла — обработку ретраев добавим при интеграции с MATLAB. */
    CDC_Transmit_FS(packet, packet_len);
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
            if (byte == PROTOCOL_SYNC_B0) {
                parser.index = 0;
                parser.buffer[parser.index++] = byte;   /* SYNC0 в buffer[0] */
                parser.state = STATE_WAIT_SYNC2;
            }
            break;

        case STATE_WAIT_SYNC2:
            if (byte == PROTOCOL_SYNC_B1) {
                parser.buffer[parser.index++] = byte;   /* SYNC1 в buffer[1] */
                parser.state = STATE_WAIT_LEN;
            } else {
                /* Возможно, это снова SYNC0 — не терять начало нового кадра. */
                parser.state = (byte == PROTOCOL_SYNC_B0)
                                   ? STATE_WAIT_SYNC2 : STATE_WAIT_SYNC1;
                if (byte == PROTOCOL_SYNC_B0) {
                    parser.index = 0;
                    parser.buffer[parser.index++] = byte;
                }
            }
            break;

        case STATE_WAIT_LEN:
            parser.buffer[parser.index++] = byte;   /* LEN в buffer[2] */
            parser.expected_len = byte;
            parser.state = STATE_WAIT_CMD;
            break;

        case STATE_WAIT_CMD:
            parser.buffer[parser.index++] = byte;   /* CMD в buffer[3] */
            parser.cmd = byte;
            parser.params_len = parser.expected_len - 1;   /* LEN - CMD */
            parser.state = (parser.params_len == 0) ? STATE_WAIT_CRC : STATE_WAIT_PARAMS;
            break;

        case STATE_WAIT_PARAMS:
            parser.buffer[parser.index++] = byte;
            if (--parser.params_len == 0) parser.state = STATE_WAIT_CRC;
            break;

        case STATE_WAIT_CRC: {
            parser.crc_received = byte;

            /* Область CRC: по умолчанию LEN+CMD+PARAMS (без SYNC).
             * crc_start=FRAME_OFFSET (пропуск SYNC) либо 0 (с SYNC). */
            uint8_t crc_start = PROTOCOL_CRC_COVERS_SYNC ? 0 : FRAME_OFFSET;
            uint8_t crc_len   = parser.index - crc_start;
            uint8_t calc_crc  = PROTOCOL_CalculateCRC(&parser.buffer[crc_start], crc_len);

            if (calc_crc == parser.crc_received) {
                CommandPacket cmd_pkt;
                cmd_pkt.cmd_id     = (CommandID)parser.cmd;
                cmd_pkt.params_len = parser.expected_len - 1;
                if (cmd_pkt.params_len > 0) {
                    /* PARAMS начинаются сразу за LEN+CMD = buffer[FRAME_OFFSET+2]. */
                    memcpy(cmd_pkt.params, &parser.buffer[FRAME_OFFSET + 2], cmd_pkt.params_len);
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
        }

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

/**
 * @brief Регистрация обработчиков команд API.
 *
 * Шаг 1 (текущий, bare-metal): обработчики не зарегистрированы — на любую
 * команду ядро отвечает STATUS_UNKNOWN_CMD. Это уже проверяет сквозной тракт
 * USB CDC → парсер → CRC → построитель ответа.
 *
 * Шаг 2: здесь регистрируются PING / INIT / GET_STATUS (синхронно, поверх
 * DecaDriver через deca_port). TX/RX/диагностика/эксперименты — позже, вместе
 * с radio_manager и FreeRTOS.
 */
void PROTOCOL_RegisterAllHandlers(void)
{
    /* Шаг 2: PROTOCOL_RegisterHandler(CMD_PING, HandlePING); ... */
}
