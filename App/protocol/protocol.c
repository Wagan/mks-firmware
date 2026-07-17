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
#include "usbd_cdc_if.h"     /* CDC_Transmit_FS() — отправка ответа в USB CDC */
#include "deca_port.h"       /* deca_port_select_device/hard_reset/spi_set_slow */
#include "deca_device_api.h" /* dwt_initialise/dwt_configure/dwt_rxenable, dwt_readdiagnostics */
#include "deca_regs.h"       /* SYS_STATUS_ID/RXFCG/ALL_RX_ERR, RX_FINFO_ID, маски */
#include "board_config.h"    /* DW_DEVICE_COUNT, DW_RX_LISTEN_DEV */
#include <string.h>
#include <math.h>            /* log10f, lrintf — строгий RSSI/FP_POWER (UM §4.7) */

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
 * RX-КОЛЬЦЕВОЙ БУФЕР (наш код, App/)
 * ===========================================================================
 * Развязка контекстов: USB-прерывание (CDC_Receive_FS) только КЛАДЁТ байты сюда
 * (PROTOCOL_RxPush), а main loop ВЫГРЕБАЕТ и разбирает (PROTOCOL_PollRx). Это
 * убирает блокирующие HAL_Delay (INIT/reset/TX/RX) из ISR — они исполняются в
 * thread mode, где SysTick тикает. См. docs/REPORT_init_hang_usb_isr_diagnosis.md.
 *
 * SPSC (single-producer / single-consumer): rx_head пишет только ISR, rx_tail —
 * только поток; 16-битные индексы на Cortex-M4 читаются/пишутся атомарно, блокировка
 * не нужна. Размер — степень двойки (маска вместо остатка). Политика переполнения:
 * отбрасывать НОВЫЕ байты (старое принятое не теряем — при переполнении main loop
 * занят долгой командой), счётчик rx_overflow — для диагностики.
 */
#define RX_RING_SIZE  512u
#define RX_RING_MASK  (RX_RING_SIZE - 1u)

static volatile uint8_t  rx_ring[RX_RING_SIZE];
static volatile uint16_t rx_head;      /* индекс записи — пишет ISR */
static volatile uint16_t rx_tail;      /* индекс чтения — читает main loop */
static volatile uint32_t rx_overflow;  /* число отброшенных байт при переполнении */

/* ===========================================================================
 * КЭШ ПРИЁМА (наш код, App/)
 * ===========================================================================
 * rx_active — приём включён командой RX_START (обслуживается в PROTOCOL_PollRadio).
 * rx_metrics — метрики последнего успешно принятого кадра (для GET_SIGNAL_METRICS).
 * Слушаем на модуле DW_RX_LISTEN_DEV (board_config). Триггер приёма на этом этапе —
 * опрос SYS_STATUS в main loop (EXTI добавим позже, обработка та же). */
#define RX_FRAME_MAX  128   /* макс. длина принимаемого кадра, что читаем в буфер */

static volatile uint8_t rx_active;   /* 1 = приём включён (RX_START) */

static struct {
    uint8_t      valid;      /* принят хотя бы один кадр после RX_START */
    uint16_t     count;      /* число принятых хороших кадров (для отладки) */
    uint16_t     frame_len;  /* длина последнего кадра */
    dwt_rxdiag_t diag;       /* сырые метрики последнего кадра (dwt_readdiagnostics) */
    uint16_t     rxpacc_nosat; /* RXPACC_NOSAT последнего кадра (DRX_CONF 0x2C) */
} rx_metrics;

static uint8_t rx_frame[RX_FRAME_MAX];  /* последний принятый кадр */

/* Состояние периодической передачи (TX_PERIODIC). Скаляры — здесь (нужны в
 * PROTOCOL_Init, объявленном ниже); буфер кадра tx_periodic_frame[TX_FRAME_MAX] —
 * рядом с определением TX_FRAME_MAX (перед HandleTX_FRAME), т.к. до него define
 * ещё не виден. Реальная посылка — в PROTOCOL_PollTx (main loop). */
static volatile uint8_t tx_periodic_active;   /* 1 = режим TX_PERIODIC включён */
static uint16_t  tx_period_ms;                /* период посылки, мс */
static uint32_t  tx_last_ms;                  /* HAL_GetTick() последней посылки */
static uint16_t  tx_periodic_len;             /* длина payload */
static uint32_t  tx_periodic_count;           /* послано кадров (диагностика) */

/* Состояние мощности передатчика (SET_TX_POWER, диагностика). */
static uint8_t   tx_power_level;   /* последний применённый power_level (0 = не задан) */
static uint32_t  tx_power_reg;     /* последнее записанное значение регистра power */

/* ===========================================================================
 * ПАРСЕР ВХОДЯЩИХ БАЙТОВ
 * =========================================================================== */

void PROTOCOL_Init(void)
{
    parser.state = STATE_WAIT_SYNC1;
    parser.index = 0;

    rx_head     = 0;
    rx_tail     = 0;
    rx_overflow = 0;

    rx_active         = 0;
    rx_metrics.valid  = 0;
    rx_metrics.count  = 0;
    rx_metrics.rxpacc_nosat = 0;

    tx_periodic_active = 0;
    tx_periodic_count  = 0;

    tx_power_level = 0;
    tx_power_reg   = 0;
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
 * RX-КОЛЬЦО: ЗАПИСЬ (ISR) / ЧТЕНИЕ (main loop)
 * =========================================================================== */

void PROTOCOL_RxPush(uint8_t byte)
{
    uint16_t next = (uint16_t)((rx_head + 1u) & RX_RING_MASK);

    if (next == rx_tail) {
        /* Кольцо полно — отбрасываем НОВЫЙ байт, старые не трогаем. */
        rx_overflow++;
        return;
    }

    rx_ring[rx_head] = byte;
    rx_head = next;
}

void PROTOCOL_PollRx(void)
{
    /* Байты, пришедшие во время разбора, останутся в кольце до следующего
     * вызова — это нормально (main loop может быть занят долгой командой). */
    while (rx_tail != rx_head) {
        uint8_t byte = rx_ring[rx_tail];
        rx_tail = (uint16_t)((rx_tail + 1u) & RX_RING_MASK);
        PROTOCOL_ProcessByte(byte);
    }
}

uint32_t PROTOCOL_RxOverflowCount(void)
{
    return rx_overflow;
}

/* ===========================================================================
 * КЭШ СОСТОЯНИЯ УСТРОЙСТВ (наш код, App/)
 * ===========================================================================
 * Лёгкий кэш конфигурации каждого DW1000. Назначение: отдавать GET_STATUS без
 * обращения к чипу и хранить признак успешной инициализации. Заполняется при
 * INIT (initialized, dev_id) и позже при SET_PHY_CONFIG (channel/data_rate/
 * preamble_len/prf). На этапе bring-up SET_PHY_CONFIG ещё нет, поэтому после
 * INIT PHY-поля = 0 (дефолты появятся, когда добавим конфигуратор PHY).
 * Индексация — по DW_DEV_M1(0)/DW_DEV_M2(1) из board_config.h.
 * Позже, при вводе radio_manager, кэш можно вынести в общий модуль. */
typedef struct {
    uint8_t  initialized;   /* 1 = dwt_initialise() прошёл успешно */
    uint32_t dev_id;        /* прочитанный DEV_ID (ожидаем 0xDECA0130) */
    uint8_t  channel;       /* канал (заполнит SET_PHY_CONFIG) */
    uint8_t  data_rate;     /* скорость данных (raw, wire) */
    uint16_t preamble_len;  /* длина преамбулы (raw, wire) */
    uint8_t  prf;           /* PRF (raw, wire) */
} dw_dev_state_t;

static dw_dev_state_t dw_dev_state[DW_DEVICE_COUNT];

/* ===========================================================================
 * ОБРАБОТЧИКИ КОМАНД (синхронно, bare-metal, исполнение в main loop)
 * =========================================================================== */

/**
 * @brief PING (0x00). Немедленный ответ, без обращения к чипу.
 *        DATA = 1 байт 0x00 (PONG), LEN=2. См. PROTOCOL_SPEC §8 (строгий
 *        вариант «Бинарный протокол §11»: ответ PING — OK uint8).
 */
static ResponseStatus HandlePING(const uint8_t* params, uint8_t params_len,
                                 uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len;
    (*out_data)[0] = 0x00;   /* PONG */
    *out_len = 1;
    return STATUS_OK;
}

/**
 * @brief INIT (0x01). Инициализация всех модулей: аппаратный сброс, медленный
 *        SPI, dwt_initialise() с загрузкой LDE-микрокода. Результат — в кэш.
 *        Возвращает STATUS_OK только если инициализированы ВСЕ модули.
 */
static ResponseStatus HandleINIT(const uint8_t* params, uint8_t params_len,
                                 uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len; (void)out_data;
    *out_len = 0;

    ResponseStatus status = STATUS_OK;

    for (int i = 0; i < DW_DEVICE_COUNT; i++) {
        dw_dev_state[i].initialized = 0;
        dw_dev_state[i].dev_id      = 0;

        if (deca_port_select_device(i) != DWT_SUCCESS) {
            status = STATUS_RADIO_ERROR;
            continue;
        }

        deca_port_hard_reset(i);      /* аппаратный сброс именно этого модуля */
        deca_port_spi_set_slow();     /* init требует SPI < 3 МГц */

        if (dwt_initialise(DWT_LOADUCODE) != DWT_SUCCESS) {
            status = STATUS_RADIO_ERROR;
            continue;
        }

        dw_dev_state[i].initialized = 1;
        dw_dev_state[i].dev_id      = dwt_readdevid();
        /* PHY-поля (channel/data_rate/...) заполнит будущий SET_PHY_CONFIG. */
    }

    return status;
}

/**
 * @brief GET_STATUS (0x02). Отдаёт кэш состояния (без обращения к чипу).
 *        DATA: TX_state u8, RX_state u8, channel u8, data_rate u8,
 *              preamble_length u16 (LE), PRF u8  — итого 7 байт (API v1.3).
 *        Пока рапортуем по модулю M1 (индекс 0); мультимодульный статус — TBD.
 */
static ResponseStatus HandleGET_STATUS(const uint8_t* params, uint8_t params_len,
                                       uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len;
    static uint8_t data[7];
    const dw_dev_state_t* s = &dw_dev_state[DW_DEV_M1];

    data[0] = 0;                              /* TX_state (трекинга пока нет) */
    data[1] = 0;                              /* RX_state (трекинга пока нет) */
    data[2] = s->channel;
    data[3] = s->data_rate;
    data[4] = s->preamble_len & 0xFF;
    data[5] = (s->preamble_len >> 8) & 0xFF;
    data[6] = s->prf;

    *out_data = data;
    *out_len  = 7;
    return STATUS_OK;
}

/* ---------------------------------------------------------------------------
 * Трансляция wire→enum DecaDriver. Значения строго из deca_device_api.h
 * (не хардкодить по памяти). Возврат false → значение вне таблицы → INVALID_PARAM.
 * --------------------------------------------------------------------------- */

/* channel: допустимые {1,2,3,4,5,7} (см. dwt_config_t.chan). Пропуск как есть. */
static bool map_channel(uint8_t raw, uint8_t* out)
{
    switch (raw) {
        case 1: case 2: case 3: case 4: case 5: case 7:
            *out = raw; return true;
        default:
            return false;
    }
}

/* data_rate: КОД {0:110k, 1:850k, 2:6.8M} → DWT_BR_* (не кбит/с — не влезает в u8). */
static bool map_datarate(uint8_t code, uint8_t* out)
{
    switch (code) {
        case 0: *out = DWT_BR_110K; return true;
        case 1: *out = DWT_BR_850K; return true;
        case 2: *out = DWT_BR_6M8;  return true;
        default: return false;
    }
}

/* preamble_length: сырое число символов → DWT_PLEN_*. */
static bool map_plen(uint16_t raw, uint8_t* out)
{
    switch (raw) {
        case 64:   *out = DWT_PLEN_64;   return true;
        case 128:  *out = DWT_PLEN_128;  return true;
        case 256:  *out = DWT_PLEN_256;  return true;
        case 512:  *out = DWT_PLEN_512;  return true;
        case 1024: *out = DWT_PLEN_1024; return true;
        case 1536: *out = DWT_PLEN_1536; return true;
        case 2048: *out = DWT_PLEN_2048; return true;
        case 4096: *out = DWT_PLEN_4096; return true;
        default: return false;
    }
}

/* PRF: число МГц (16/64) → DWT_PRF_*. */
static bool map_prf(uint8_t mhz, uint8_t* out)
{
    switch (mhz) {
        case 16: *out = DWT_PRF_16M; return true;
        case 64: *out = DWT_PRF_64M; return true;
        default: return false;
    }
}

/* PAC_size: число символов (8/16/32/64) → DWT_PAC*. Берём КАК ЕСТЬ из параметра
 * (для точного повтора конфига EVK — Mode 3 вещает с PAC 32, не подменяем на 64). */
static bool map_pac(uint8_t symbols, uint8_t* out)
{
    switch (symbols) {
        case 8:  *out = DWT_PAC8;  return true;
        case 16: *out = DWT_PAC16; return true;
        case 32: *out = DWT_PAC32; return true;
        case 64: *out = DWT_PAC64; return true;
        default: return false;
    }
}

/* PGdly по каналу из vendor-констант deca_regs.h (TC_PGDELAY_CH*).
 * Значения рекомендованы DecaWave; используются штатным DecaDriver — спектр
 * не портим, регулируем только уровень (power). channel — из dw_dev_state
 * (заполнен SET_PHY_CONFIG). Канал вне {1..5,7} → false. */
static bool map_pgdelay(uint8_t channel, uint8_t* out)
{
    switch (channel) {
        case 1: *out = TC_PGDELAY_CH1; return true;   /* 0xC9 */
        case 2: *out = TC_PGDELAY_CH2; return true;   /* 0xC2  (Mode 3) */
        case 3: *out = TC_PGDELAY_CH3; return true;   /* 0xC5 */
        case 4: *out = TC_PGDELAY_CH4; return true;   /* 0x95 */
        case 5: *out = TC_PGDELAY_CH5; return true;   /* 0xC0 */
        case 7: *out = TC_PGDELAY_CH7; return true;   /* 0x93 */
        default: return false;
    }
}

/**
 * @brief SET_PHY_CONFIG (0x10). Настройка PHY DW1000 (dwt_configure).
 *        Параметры (wire, §6, БЕЗ target, 7 байт): channel u8, data_rate u8,
 *        preamble_length u16 LE, preamble_code u8, PRF u8, PAC_size u8.
 *        Трансляция wire→enum с валидацией (невалидное → INVALID_PARAM).
 *        Применяется на ВСЕ модули. dwt_configure содержит deca_sleep → handler
 *        исполняется в main loop (PollRx), SPI держим медленным (LDE-загрузка).
 *        ТРЕБУЕТ ПРЕДВАРИТЕЛЬНОГО INIT: dwt_configure опирается на состояние,
 *        установленное dwt_initialise; если модуль не инициализирован — команда
 *        возвращает RADIO_ERROR. Правильный порядок: INIT → SET_PHY_CONFIG.
 */
static ResponseStatus HandleSET_PHY_CONFIG(const uint8_t* params, uint8_t params_len,
                                           uint8_t** out_data, uint8_t* out_len)
{
    (void)out_data;
    *out_len = 0;

    if (params_len < 7) return STATUS_INVALID_PARAM;

    uint8_t  channel   = params[0];
    uint8_t  data_rate = params[1];
    uint16_t plen_raw  = GET_U16LE(&params[2]);
    uint8_t  pcode     = params[4];
    uint8_t  prf_raw   = params[5];
    uint8_t  pac_raw   = params[6];

    dwt_config_t cfg;
    if (!map_channel(channel, &cfg.chan))         return STATUS_INVALID_PARAM;
    if (!map_datarate(data_rate, &cfg.dataRate))  return STATUS_INVALID_PARAM;
    if (!map_plen(plen_raw, &cfg.txPreambLength)) return STATUS_INVALID_PARAM;
    if (!map_prf(prf_raw, &cfg.prf))              return STATUS_INVALID_PARAM;
    if (!map_pac(pac_raw, &cfg.rxPAC))            return STATUS_INVALID_PARAM;

    /* preamble_code: временно ШИРОКАЯ валидация 1..24 (общий диапазон DW1000).
     * Строгую PRF-зависимую (PRF16:1..8 / PRF64:9..24) сделаем позже. */
    if (pcode < 1 || pcode > 24) return STATUS_INVALID_PARAM;
    cfg.txCode = pcode;
    cfg.rxCode = pcode;

    /* nsSFD по правилу data_rate: 110k → нестандартный SFD (конвенция EVK).
     * Протокол признак SFD не передаёт (согласовано). */
    cfg.nsSFD   = (cfg.dataRate == DWT_BR_110K) ? 1 : 0;
    cfg.phrMode = DWT_PHRMODE_STD;
    cfg.sfdTO   = 0;   /* 0 → драйвер подставит DWT_SFDTOC_DEF (deca_device.c) */

    /* Применяем на все модули. Требуется предварительный INIT (dwt_configure
     * опирается на состояние, установленное dwt_initialise). */
    for (int i = 0; i < DW_DEVICE_COUNT; i++) {
        if (!dw_dev_state[i].initialized) return STATUS_RADIO_ERROR;

        if (deca_port_select_device(i) != DWT_SUCCESS) return STATUS_RADIO_ERROR;
        dwt_configure(&cfg);   /* void: валидация уже сделана ДО вызова */

        dw_dev_state[i].channel      = channel;
        dw_dev_state[i].data_rate    = data_rate;
        dw_dev_state[i].preamble_len = plen_raw;
        dw_dev_state[i].prf          = prf_raw;
    }

    return STATUS_OK;
}

/**
 * @brief RX_START (0x30). Включить непрерывный приём на модуле DW_RX_LISTEN_DEV.
 *        Требует предварительного INIT. Приём обслуживается в PROTOCOL_PollRadio
 *        (main loop). dwt_setrxtimeout(0) — без таймаута (слушаем бесконечно).
 */
static ResponseStatus HandleRX_START(const uint8_t* params, uint8_t params_len,
                                     uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len; (void)out_data;
    *out_len = 0;

    if (!dw_dev_state[DW_RX_LISTEN_DEV].initialized) return STATUS_RADIO_ERROR;
    if (deca_port_select_device(DW_RX_LISTEN_DEV) != DWT_SUCCESS) return STATUS_RADIO_ERROR;

    dwt_setrxtimeout(0);                        /* непрерывный приём (без таймаута) */
    if (dwt_rxenable(DWT_START_RX_IMMEDIATE) != DWT_SUCCESS) return STATUS_RADIO_ERROR;

    rx_metrics.valid = 0;   /* метрики появятся от следующих принятых кадров */
    rx_active = 1;
    return STATUS_OK;
}

/**
 * @brief RX_STOP (0x31). Остановить приём (dwt_forcetrxoff), снять флаг rx_active.
 */
static ResponseStatus HandleRX_STOP(const uint8_t* params, uint8_t params_len,
                                    uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len; (void)out_data;
    *out_len = 0;

    if (deca_port_select_device(DW_RX_LISTEN_DEV) != DWT_SUCCESS) return STATUS_RADIO_ERROR;
    dwt_forcetrxoff();
    rx_active = 0;
    return STATUS_OK;
}

/* RXPACC_NOSAT: DRX_CONF (0x27), sub-offset 0x2C, 2 байта RO (UM §7.2.40.12).
 * Не-насыщаемый счётчик символов преамбулы — для проверки, нужна ли SFD-коррекция
 * RXPACC (UM стр. 96). В vendor deca_regs.h не определён; DRX_CONF_ID=0x27 есть. */
#define RXPACC_NOSAT_OFFSET   0x2C

/* SFD-коррекция RXPACC для нашего Mode 3 (110 kbps, nsSFD=1 → DecaWave-defined
 * 64-symbol SFD): −82 (UM Table 18, стр. 97). Применяется ТОЛЬКО когда
 * RXPACC == RXPACC_NOSAT (UM стр. 96); для наших замеров (RXPACC≠NOSAT) ветка не
 * срабатывает — значение защитное, но заложено документально. */
#define SFD_CORRECTION_MODE3  82

/* delta для total SNR (SNR = RSL + delta), значения из DecaRanging instance_log.c:
 * delta = 87 − 7.5 = 79.5 (каналы 1,2,3,5); для каналов 4/7 delta −= 2.5 = 77.0. */
#define SNR_DELTA_DEFAULT  79.5f
#define SNR_DELTA_CH47     77.0f

/* A-константа по PRF (raw wire). Источник — DecaRanging instance_log.c
 * (alpha: PRF64 = −121.74, PRF16 = −115.72; у нас это −A). UM §4.7. */
static float metrics_a_const(uint8_t prf_wire)
{
    return (prf_wire == 64) ? 121.74f : 115.72f;
}

/* N (число символов преамбулы) с SFD-коррекцией по UM стр. 96. */
static uint16_t metrics_corrected_n(const dwt_rxdiag_t* d, uint16_t nosat)
{
    uint16_t rxpacc = d->rxPreamCount;
    if (rxpacc == nosat) {                       /* коррекция нужна */
        return (rxpacc > SFD_CORRECTION_MODE3)
             ? (uint16_t)(rxpacc - SFD_CORRECTION_MODE3)
             : rxpacc;                           /* защита от ухода в 0/underflow */
    }
    return rxpacc;                               /* коррекция не нужна */
}

/* RSL (RX_LEVEL) в dBm (float) — единая формула UM §4.7 (сверена с хостом Δ=0.00).
 * Используется в metrics_rssi_q И metrics_snr_q (без дублирования). Предполагает
 * валидные входы (N>0, maxGrowthCIR>0); проверку края делают вызывающие. */
static float metrics_rssi_dbm(const dwt_rxdiag_t* d, uint16_t N, float A)
{
    float n2 = (float)N * (float)N;
    return 10.0f * log10f(((float)d->maxGrowthCIR * 131072.0f) / n2) - A;
}

/* RSSI (RX_LEVEL) в dBm×100. Крайние случаи (N=0 или C=0) → INT16_MIN («н/д»). */
static int16_t metrics_rssi_q(const dwt_rxdiag_t* d, uint16_t N, float A)
{
    if (N == 0 || d->maxGrowthCIR == 0) return INT16_MIN;
    return (int16_t)lrintf(metrics_rssi_dbm(d, N, A) * 100.0f);
}

/* FP_POWER в dBm×100. Крайние случаи (N=0 или сумма амплитуд=0) → INT16_MIN. */
static int16_t metrics_fp_q(const dwt_rxdiag_t* d, uint16_t N, float A)
{
    if (N == 0) return INT16_MIN;
    float f1=(float)d->firstPathAmp1, f2=(float)d->firstPathAmp2, f3=(float)d->firstPathAmp3;
    float fp_sum = f1*f1 + f2*f2 + f3*f3;
    if (fp_sum == 0.0f) return INT16_MIN;
    float n2 = (float)N * (float)N;
    float v  = 10.0f * log10f(fp_sum / n2) - A;
    return (int16_t)lrintf(v * 100.0f);
}

/* delta для total SNR по каналу (DecaRanging instance_log.c): {4,7} → 77.0,
 * иначе 79.5. Написано по образцу metrics_a_const. */
static float metrics_delta_for_channel(uint8_t channel)
{
    return (channel == 4 || channel == 7) ? SNR_DELTA_CH47 : SNR_DELTA_DEFAULT;
}

/* total SNR в dB×100 = RSL_dBm + delta (DecaRanging: знак ПЛЮС). Крайние случаи —
 * те же, что дают INT16_MIN у RSL (N=0 или C=0). delta — по каналу RX-устройства. */
static int16_t metrics_snr_q(const dwt_rxdiag_t* d, uint16_t N, float A, uint8_t channel)
{
    if (N == 0 || d->maxGrowthCIR == 0) return INT16_MIN;
    float snr = metrics_rssi_dbm(d, N, A) + metrics_delta_for_channel(channel);
    return (int16_t)lrintf(snr * 100.0f);
}

/**
 * @brief GET_SIGNAL_METRICS (0x40). Метрики последнего принятого кадра.
 *        Формат — 30 байт, u16 LE. Первые 18 байт (совместимость): count,
 *        maxGrowthCIR, rxPreamCount, stdNoise, firstPathAmp1..3, firstPath,
 *        maxNoise. Далее СТРОГИЕ поля (UM §4.7 / DecaRanging): RXPACC_NOSAT,
 *        N_corrected, RSSI (i16 dBm×100), FP_POWER (i16 dBm×100), A_used×100,
 *        SNR (i16 dB×100 = RSL+delta). Считаются в прошивке на float; N/д →
 *        INT16_MIN. Если кадра ещё не было (valid==0) → TIMEOUT.
 */
static ResponseStatus HandleGET_SIGNAL_METRICS(const uint8_t* params, uint8_t params_len,
                                               uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len;

    if (!rx_metrics.valid) return STATUS_TIMEOUT;   /* кадра ещё не принято */

    static uint8_t data[30];
    const dwt_rxdiag_t* d = &rx_metrics.diag;
    uint8_t* p = data;

    /* Первые 18 байт — интерим-совместимость (не менять порядок). */
    PUT_U16LE(p, rx_metrics.count); p += 2;
    PUT_U16LE(p, d->maxGrowthCIR);  p += 2;
    PUT_U16LE(p, d->rxPreamCount);  p += 2;
    PUT_U16LE(p, d->stdNoise);      p += 2;
    PUT_U16LE(p, d->firstPathAmp1); p += 2;
    PUT_U16LE(p, d->firstPathAmp2); p += 2;
    PUT_U16LE(p, d->firstPathAmp3); p += 2;
    PUT_U16LE(p, d->firstPath);     p += 2;
    PUT_U16LE(p, d->maxNoise);      p += 2;

    /* Строгие поля (UM §4.7), дописаны в конец. */
    uint8_t  prf_wire = dw_dev_state[DW_RX_LISTEN_DEV].prf;
    float    A        = metrics_a_const(prf_wire);
    uint16_t N        = metrics_corrected_n(d, rx_metrics.rxpacc_nosat);
    int16_t  rssi_q   = metrics_rssi_q(d, N, A);
    int16_t  fp_q     = metrics_fp_q(d, N, A);
    uint16_t a_q      = (uint16_t)lrintf(A * 100.0f);

    int16_t  snr_q    = metrics_snr_q(d, N, A, dw_dev_state[DW_RX_LISTEN_DEV].channel);

    PUT_U16LE(p, rx_metrics.rxpacc_nosat); p += 2;
    PUT_U16LE(p, N);                       p += 2;
    PUT_U16LE(p, (uint16_t)rssi_q);        p += 2;   /* i16 в u16-контейнер, LE */
    PUT_U16LE(p, (uint16_t)fp_q);          p += 2;
    PUT_U16LE(p, a_q);                     p += 2;
    PUT_U16LE(p, (uint16_t)snr_q);         p += 2;   /* total SNR, i16 dB×100, LE */

    *out_data = data;
    *out_len  = 30;
    return STATUS_OK;
}

/* Лимиты TX (см. PLAN_tx_tract §9.6). TX_FRAME_MAX — макс. payload (127 макс.
 * стандартный кадр − 2 байта авто-FCS). TX_WAIT_GUARD — потолок busy-wait TXFRS
 * (кадр Mode3 ~сотни мкс; счётчик защищает от вечного цикла). */
#define TX_FRAME_MAX    125
#define TX_WAIT_GUARD   100000u

/* Верхняя граница power_level для SET_TX_POWER (0x11). power_level задаёт мощность
 * передатчика: БОЛЬШЕ level → БОЛЬШЕ мощность (0 ≈ минимум, POWER_LEVEL_MAX ≈
 * максимум). Реализация: octet = 0xFF - level, поэтому рост level уменьшает
 * аттенюацию DA/mixer (октет убывает) → мощность растёт. Верхняя граница 0xDF
 * оставлена, чтобы coarse-код (биты 7:5) не опускался ниже 001 (октет не ниже
 * 0x20; DA-off 000 — особый случай UM §7.2.31.1). Проверено на железе (loopback
 * M1→M2): RX_LEVEL монотонно растёт с level. */
#define POWER_LEVEL_MAX  0xDF

/* Нижняя граница периода TX_PERIODIC (мс). Защита от «шторма»: эфирное время
 * кадра Mode3 ~сотни мкс + busy-wait TXFRS; 5 мс — запас, USB не голодает. */
#define TX_PERIOD_MIN_MS  5

/* Копия payload периодического кадра (params диспетчера переиспользуются). */
static uint8_t tx_periodic_frame[TX_FRAME_MAX];

/**
 * @brief TX_FRAME (0x20). Передать одиночный кадр с модуля DW_TX_SOURCE_DEV.
 *        Параметры (wire, §6): length u16 LE, payload[length].
 *        DW1000 сам дописывает 2-байтный FCS → в драйвер передаём length+2
 *        (dwt_writetxdata копирует len−2 = length байт payload). Требует INIT.
 *        Ждём TXFRS (кадр ушёл) с guard-счётчиком. DATA нет; OK при TXFRS.
 */
static ResponseStatus HandleTX_FRAME(const uint8_t* params, uint8_t params_len,
                                     uint8_t** out_data, uint8_t* out_len)
{
    (void)out_data;
    *out_len = 0;

    if (params_len < 2) return STATUS_INVALID_PARAM;          /* нет поля length */
    uint16_t length = GET_U16LE(&params[0]);
    if (params_len < 2 + length) return STATUS_INVALID_PARAM; /* payload короче заявленного */
    if (length > TX_FRAME_MAX)   return STATUS_BUFFER_OVERFLOW;

    if (!dw_dev_state[DW_TX_SOURCE_DEV].initialized) return STATUS_RADIO_ERROR;
    if (deca_port_select_device(DW_TX_SOURCE_DEV) != DWT_SUCCESS) return STATUS_RADIO_ERROR;

    dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_TXFRS);       /* очистить флаг завершения TX */

    /* length+2: место под авто-FCS; dwt_writetxdata запишет length байт payload. */
    if (dwt_writetxdata((uint16_t)(length + 2), (uint8_t*)&params[2], 0) != DWT_SUCCESS)
        return STATUS_RADIO_ERROR;
    dwt_writetxfctrl((uint16_t)(length + 2), 0, 0);           /* ranging=0 (обычный data-кадр) */

    if (dwt_starttx(DWT_START_TX_IMMEDIATE) != DWT_SUCCESS)
        return STATUS_RADIO_ERROR;

    /* Ждём TXFRS (кадр ушёл в эфир) с ограничением — защита от вечного цикла. */
    uint32_t guard = TX_WAIT_GUARD;
    while (!(dwt_read32bitreg(SYS_STATUS_ID) & SYS_STATUS_TXFRS)) {
        if (--guard == 0) return STATUS_TIMEOUT;              /* кадр не ушёл */
    }
    dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_TXFRS);      /* снять флаг */

    return STATUS_OK;
}

/**
 * @brief TX_STOP (0x22). Перевести передатчик в IDLE (dwt_forcetrxoff).
 */
static ResponseStatus HandleTX_STOP(const uint8_t* params, uint8_t params_len,
                                    uint8_t** out_data, uint8_t* out_len)
{
    (void)params; (void)params_len; (void)out_data;
    *out_len = 0;

    tx_periodic_active = 0;   /* остановить периодику (если была) — единая точка останова */
    if (deca_port_select_device(DW_TX_SOURCE_DEV) != DWT_SUCCESS) return STATUS_RADIO_ERROR;
    dwt_forcetrxoff();
    return STATUS_OK;
}

/**
 * @brief TX_PERIODIC (0x21). Взвести режим периодической передачи и вернуть OK.
 *        Параметры (wire, §6): period_ms u16 LE, length u16 LE, payload[length].
 *        Реальная посылка — в PROTOCOL_PollTx() (main loop). Требует INIT.
 *        Останавливается командой TX_STOP. payload копируется (params переиспользуются).
 */
static ResponseStatus HandleTX_PERIODIC(const uint8_t* params, uint8_t params_len,
                                        uint8_t** out_data, uint8_t* out_len)
{
    (void)out_data;
    *out_len = 0;

    if (params_len < 4) return STATUS_INVALID_PARAM;          /* нет period+length */
    uint16_t period = GET_U16LE(&params[0]);
    uint16_t length = GET_U16LE(&params[2]);
    if (length > TX_FRAME_MAX)       return STATUS_BUFFER_OVERFLOW;
    if (params_len < 4 + length)     return STATUS_INVALID_PARAM;  /* payload короче заявленного */
    if (period < TX_PERIOD_MIN_MS)   return STATUS_INVALID_PARAM;  /* защита от шторма */
    if (!dw_dev_state[DW_TX_SOURCE_DEV].initialized) return STATUS_RADIO_ERROR;

    memcpy(tx_periodic_frame, &params[4], length);
    tx_periodic_len    = length;
    tx_period_ms       = period;
    tx_periodic_count  = 0;
    tx_last_ms         = HAL_GetTick() - period;   /* первая посылка — сразу */
    tx_periodic_active = 1;
    return STATUS_OK;
}

/**
 * @brief SET_TX_POWER (0x11). Ручная регулировка мощности передатчика (вариант A).
 *        Параметры (wire): power_level u8 — БОЛЬШЕ level → БОЛЬШЕ мощность
 *        (0 ≈ мин, 0xDF ≈ макс), шаг ≈ 0.5 dB. Реализация: octet = 0xFF - level,
 *        дублируется во все 4 октета регистра TX_POWER.
 *        Включает ручной режим (dwt_setsmarttxpower(0), DIS_STXP=1), затем
 *        dwt_configuretxrf. PGdly — по текущему каналу (TC_PGDELAY_CH*).
 *        Применяется на DW_TX_SOURCE_DEV. ТРЕБУЕТ предварительного SET_PHY_CONFIG
 *        (нужен channel; иначе RADIO_ERROR). Ответ DATA: применённый power (u32 LE).
 */
static ResponseStatus HandleSET_TX_POWER(const uint8_t* params, uint8_t params_len,
                                         uint8_t** out_data, uint8_t* out_len)
{
    *out_len = 0;

    if (params_len < 1) return STATUS_INVALID_PARAM;
    uint8_t level = params[0];
    if (level > POWER_LEVEL_MAX) return STATUS_INVALID_PARAM;

    if (!dw_dev_state[DW_TX_SOURCE_DEV].initialized) return STATUS_RADIO_ERROR;

    uint8_t pgdly;
    if (!map_pgdelay(dw_dev_state[DW_TX_SOURCE_DEV].channel, &pgdly))
        return STATUS_RADIO_ERROR;   /* channel не задан (нет SET_PHY_CONFIG) или вне диапазона */

    if (deca_port_select_device(DW_TX_SOURCE_DEV) != DWT_SUCCESS)
        return STATUS_RADIO_ERROR;

    /* Инвертируем: больше level → меньше октет → меньше аттенюация → больше мощность. */
    uint8_t o = (uint8_t)(0xFF - level);
    dwt_txconfig_t cfg;
    cfg.PGdly = pgdly;
    cfg.power = ((uint32_t)o << 24) | ((uint32_t)o << 16) |
                ((uint32_t)o << 8)  |  (uint32_t)o;

    dwt_setsmarttxpower(0);      /* ручной режим (DIS_STXP=1) */
    dwt_configuretxrf(&cfg);     /* void */

    tx_power_level = level;
    tx_power_reg   = cfg.power;

    /* DATA = применённый power (u32 LE) */
    static uint8_t buf[4];
    buf[0] = (uint8_t)(cfg.power        & 0xFF);
    buf[1] = (uint8_t)((cfg.power >> 8)  & 0xFF);
    buf[2] = (uint8_t)((cfg.power >> 16) & 0xFF);
    buf[3] = (uint8_t)((cfg.power >> 24) & 0xFF);
    *out_data = buf;
    *out_len  = 4;
    return STATUS_OK;
}

/* ===========================================================================
 * ОБСЛУЖИВАНИЕ ПРИЁМА (main loop) — polling SYS_STATUS
 * ===========================================================================
 * Триггер приёма на этом этапе — опрос SYS_STATUS в main loop (EXTI добавим
 * позже, обработка та же). Разбирает хороший кадр (RXFCG) и ошибки (ALL_RX_ERR),
 * кэширует метрики, перевключает непрерывный приём. Набор регистров/флагов — как
 * в референсе docs/reference/decawave-examples/ss_resp_main.c.
 */
void PROTOCOL_PollRadio(void)
{
    if (!rx_active) return;

    if (deca_port_select_device(DW_RX_LISTEN_DEV) != DWT_SUCCESS) return;

    uint32_t status = dwt_read32bitreg(SYS_STATUS_ID);

    if (status & SYS_STATUS_RXFCG) {
        /* Хороший кадр: снять флаг, прочитать длину/данные и метрики. */
        dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_RXFCG);

        uint16_t frame_len = (uint16_t)(dwt_read32bitreg(RX_FINFO_ID) & RX_FINFO_RXFL_MASK_1023);
        rx_metrics.frame_len = frame_len;
        if (frame_len <= RX_FRAME_MAX) {
            dwt_readrxdata(rx_frame, frame_len, 0);
        }

        dwt_readdiagnostics(&rx_metrics.diag);
        /* RXPACC_NOSAT — активное устройство уже выбрано выше (DW_RX_LISTEN_DEV). */
        rx_metrics.rxpacc_nosat = dwt_read16bitoffsetreg(DRX_CONF_ID, RXPACC_NOSAT_OFFSET);
        rx_metrics.valid = 1;
        rx_metrics.count++;

        dwt_rxenable(DWT_START_RX_IMMEDIATE);   /* снова слушаем */
    } else if (status & SYS_STATUS_ALL_RX_ERR) {
        /* Ошибка приёма: снять флаги, сброс приёмника (реинициализация LDE). */
        dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_ALL_RX_ERR);
        dwt_rxreset();
        dwt_rxenable(DWT_START_RX_IMMEDIATE);
    }
    /* RX_TO не обрабатываем: таймаут не задан (непрерывный приём). */
}

/* ===========================================================================
 * ОБСЛУЖИВАНИЕ ПЕРИОДИЧЕСКОЙ ПЕРЕДАЧИ (main loop) — интервал по HAL_GetTick()
 * ===========================================================================
 * Если включён режим TX_PERIODIC, по достижении tx_period_ms шлёт кадр с
 * DW_TX_SOURCE_DEV тем же паттерном, что HandleTX_FRAME. Ошибки не возвращаются
 * наружу (команда уже завершилась при взведении режима); сбой = пропуск периода.
 */
void PROTOCOL_PollTx(void)
{
    if (!tx_periodic_active) return;

    uint32_t now = HAL_GetTick();
    if ((uint32_t)(now - tx_last_ms) < tx_period_ms) return;   /* ещё рано */
    tx_last_ms = now;

    if (deca_port_select_device(DW_TX_SOURCE_DEV) != DWT_SUCCESS) return;

    dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_TXFRS);
    if (dwt_writetxdata((uint16_t)(tx_periodic_len + 2), tx_periodic_frame, 0) != DWT_SUCCESS)
        return;
    dwt_writetxfctrl((uint16_t)(tx_periodic_len + 2), 0, 0);
    if (dwt_starttx(DWT_START_TX_IMMEDIATE) != DWT_SUCCESS)
        return;

    uint32_t guard = TX_WAIT_GUARD;
    while (!(dwt_read32bitreg(SYS_STATUS_ID) & SYS_STATUS_TXFRS)) {
        if (--guard == 0) break;          /* кадр не ушёл — пропускаем период */
    }
    dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_TXFRS);
    tx_periodic_count++;
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
 * Зарегистрированы (синхронно, поверх DecaDriver через deca_port, исполнение
 * в main loop): PING / INIT / GET_STATUS / SET_PHY_CONFIG. Остальные команды НЕ
 * регистрируются — диспетчер на них отвечает STATUS_UNKNOWN_CMD (заглушка).
 * TX/RX/диагностика/эксперименты — позже, вместе с radio_manager и FreeRTOS.
 */
void PROTOCOL_RegisterAllHandlers(void)
{
    PROTOCOL_RegisterHandler(CMD_PING,               HandlePING);
    PROTOCOL_RegisterHandler(CMD_INIT,               HandleINIT);
    PROTOCOL_RegisterHandler(CMD_GET_STATUS,         HandleGET_STATUS);
    PROTOCOL_RegisterHandler(CMD_SET_PHY_CONFIG,     HandleSET_PHY_CONFIG);
    PROTOCOL_RegisterHandler(CMD_SET_TX_POWER,       HandleSET_TX_POWER);
    PROTOCOL_RegisterHandler(CMD_RX_START,           HandleRX_START);
    PROTOCOL_RegisterHandler(CMD_RX_STOP,            HandleRX_STOP);
    PROTOCOL_RegisterHandler(CMD_GET_SIGNAL_METRICS, HandleGET_SIGNAL_METRICS);
    PROTOCOL_RegisterHandler(CMD_TX_FRAME,           HandleTX_FRAME);
    PROTOCOL_RegisterHandler(CMD_TX_STOP,            HandleTX_STOP);
    PROTOCOL_RegisterHandler(CMD_TX_PERIODIC,        HandleTX_PERIODIC);
    /* Остальные CMD_ID остаются NULL → STATUS_UNKNOWN_CMD. */
}
