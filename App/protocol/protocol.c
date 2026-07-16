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
#include "deca_device_api.h" /* dwt_initialise/dwt_readdevid, DWT_LOADUCODE, DWT_SUCCESS */
#include "board_config.h"    /* DW_DEVICE_COUNT */
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
 * ПАРСЕР ВХОДЯЩИХ БАЙТОВ
 * =========================================================================== */

void PROTOCOL_Init(void)
{
    parser.state = STATE_WAIT_SYNC1;
    parser.index = 0;

    rx_head     = 0;
    rx_tail     = 0;
    rx_overflow = 0;
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
 * ОБРАБОТЧИКИ КОМАНД (Шаг 2: PING / INIT / GET_STATUS, синхронно, bare-metal)
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
    PROTOCOL_RegisterHandler(CMD_PING,           HandlePING);
    PROTOCOL_RegisterHandler(CMD_INIT,           HandleINIT);
    PROTOCOL_RegisterHandler(CMD_GET_STATUS,     HandleGET_STATUS);
    PROTOCOL_RegisterHandler(CMD_SET_PHY_CONFIG, HandleSET_PHY_CONFIG);
    /* Остальные CMD_ID остаются NULL → STATUS_UNKNOWN_CMD. */
}
