/***************************************************************
* Библиотека функций для работы с DW1000                      *
*                                                              *
* Версия 1.3.04 (Добавлена команда READ_DEV_ID)               *
*                                                              *
* Согласовано со следующими документами:                      *
*   - МКС API v.1.3.pdf                                        *
*   - Бинарный протокол МКС v.1.3.pdf                          *
*   - DW1000 User Manual DecaWave v.2.17                       *
*                                                              *
* Copyright (C) NCPR LLC                                       *
* https://flexlab.ru                                           *
***************************************************************/

#ifndef RADIO_DEFS_H
#define RADIO_DEFS_H

#include <stdint.h>
#include <stdbool.h>
#include "dw1000_driver.h"

/* ===========================================================================
 * ТИПЫ КОМАНД (используются в radio_manager.c и protocol.c)
 * =========================================================================== */

typedef enum {
    /* Системные команды */
    RADIO_CMD_NONE = 0x00,
    RADIO_CMD_PING = 0x01,
    RADIO_CMD_INIT = 0x02,
    RADIO_CMD_GET_STATUS = 0x03,
    RADIO_CMD_RESET_RADIO = 0x04,
    
    /* Конфигурация */
    RADIO_CMD_SET_PHY_CONFIG = 0x10,
    RADIO_CMD_SET_TX_POWER = 0x11,
    
    /* Передатчик */
    RADIO_CMD_TX_FRAME = 0x20,
    RADIO_CMD_TX_PERIODIC = 0x21,
    RADIO_CMD_TX_STOP = 0x22,
    
    /* Приемник */
    RADIO_CMD_RX_START = 0x30,
    RADIO_CMD_RX_STOP = 0x31,
    
    /* Диагностика */
    RADIO_CMD_GET_SIGNAL_METRICS = 0x40,
    RADIO_CMD_GET_CIR = 0x41,
    RADIO_CMD_READ_DEV_ID = 0x42,     /* Новая команда для чтения DEV_ID */
    
    /* Эксперименты */
    RADIO_CMD_START_EXPERIMENT = 0x50,
    RADIO_CMD_STOP_EXPERIMENT = 0x51,
    RADIO_CMD_TX_SWEEP = 0x60,
    RADIO_CMD_DETECTOR_TEST = 0x61
} RadioCommandType;

/* ===========================================================================
 * ТИПЫ СОБЫТИЙ (генерируются в прерываниях)
 * =========================================================================== */

typedef enum {
    RADIO_EVT_RX_DONE,
    RADIO_EVT_RX_TIMEOUT,
    RADIO_EVT_RX_PHR_ERROR,
    RADIO_EVT_RX_OVERRUN,
    RADIO_EVT_FRAME_REJECTED,
    RADIO_EVT_TX_DONE,
    RADIO_EVT_TX_BUFFER_ERROR,
    RADIO_EVT_RX_STARTED,
    RADIO_EVT_TX_STARTED,
    RADIO_EVT_ERROR
} RadioEventType;

/* ===========================================================================
 * СТРУКТУРА КОМАНДЫ (передаётся по очереди xRadioCommandQueue)
 * =========================================================================== */

typedef struct {
    RadioCommandType cmd;
    DW1000_Device* dev;           /* указатель на устройство (TX или RX) */
    uint8_t target;               /* 0=TX, 1=RX, 2=Both (для некоторых команд) */
    
    union {
        /* Команды передачи */
        struct {
            uint16_t len;
            uint8_t data[256];    /* payload */
        } tx_frame;
        
        struct {
            uint16_t period_ms;
            uint16_t len;
            uint8_t data[256];
        } tx_periodic;
        
        /* Конфигурация PHY */
        struct {
            uint8_t channel;
            uint8_t data_rate;
            uint16_t preamble_len;
            uint8_t preamble_code;
            uint8_t prf;
            uint8_t pac_size;
        } phy_config;
        
        /* Мощность передачи */
        struct {
            uint8_t power_level;
        } tx_power;
        
        /* Свипирование каналов */
        struct {
            uint8_t channel_start;
            uint8_t channel_end;
            uint8_t power_start;
            uint8_t power_end;
            uint16_t preamble_len;
        } tx_sweep;
        
        /* Тест детектора */
        struct {
            uint16_t num_packets;
            uint8_t power_start;
            uint8_t power_end;
        } detector_test;
        
        /* Чтение CIR */
        struct {
            uint16_t offset;
            uint16_t length;
        } cir_params;
        
        /* Общие параметры */
        struct {
            uint32_t param1;
            uint32_t param2;
            uint32_t param3;
        } generic;
    } params;
    
    /* Флаги */
    struct {
        uint8_t wait_for_completion : 1;  /* Ждать завершения */
        uint8_t generate_response : 1;     /* Генерировать ответ */
        uint8_t reserved : 6;
    } flags;
    
} RadioCommand_t;

/* ===========================================================================
 * СТРУКТУРА СОБЫТИЯ (передаётся по очереди xRadioEventQueue)
 * =========================================================================== */

typedef struct {
    DW1000_Device* dev;           /* указатель на устройство */
    RadioEventType event;
    uint32_t timestamp;           /* время события (в тактах) */
    
    union {
        /* Событие приёма пакета */
        struct {
            uint16_t rxpacc;      /* количество принятых символов преамбулы */
            int16_t fp_ampl1;     /* амплитуда первого пика */
            int16_t fp_ampl2;     /* амплитуда второго пика */
            int16_t fp_ampl3;     /* амплитуда третьего пика */
            uint16_t fp_index;    /* индекс первого пика */
            uint16_t std_noise;   /* СКО шума */
            uint16_t cir_pwr;     /* мощность CIR */
            uint16_t packet_len;  /* длина пакета */
            uint8_t rx_buffer[256]; /* данные пакета */
        } rx_done;
        
        /* Событие передачи */
        struct {
            uint16_t tx_counter;  /* счётчик передач */
        } tx_done;
        
        /* Ошибка */
        struct {
            uint32_t error_code;
            uint32_t sys_status;  /* значение регистра SYS_STATUS */
        } error;
        
    } info;
    
} RadioEvent_t;

/* ===========================================================================
 * СТРУКТУРА РЕЗУЛЬТАТА (для синхронных вызовов)
 * =========================================================================== */

typedef struct {
    RadioCommandType cmd_id;
    uint8_t status;               /* 0=успешно, иначе код ошибки */
    
    union {
        /* Статус устройства */
        struct {
            uint8_t tx_state;
            uint8_t rx_state;
            uint8_t channel;
            uint8_t data_rate;
            uint16_t preamble_len;
            uint8_t prf;
        } status_info;
        
        /* Метрики сигнала */
        struct {
            int16_t rssi;         /* в dBm * 10 (например, -850 = -85.0 dBm) */
            int16_t snr;          /* в dB * 10 */
            uint16_t rxpacc;
            uint16_t fp_index;
            uint16_t cir_pwr;
        } metrics;
        
        /* Данные CIR */
        struct {
            uint16_t length;
            int16_t* iq_data;     /* указатель на буфер с комплексными отсчётами */
        } cir;
        
        /* Общие данные */
        struct {
            uint8_t data[256];
            uint16_t length;
        } generic;
    } result;
    
    uint16_t result_len;          /* размер данных в result */
    
} RadioResult_t;

/* ===========================================================================
 * USB КОНСТАНТЫ И СТРУКТУРЫ
 * =========================================================================== */

/* Статусы USB */
#define USB_OK                  0
#define USB_FAIL                1
#define USB_BUSY                2

/* Размеры буферов */
#define USB_TX_BUFFER_SIZE      1024
#define USB_RX_BUFFER_SIZE      1024

/* Максимальные размеры очередей */
#define USB_MAX_TX_QUEUE        16
#define USB_MAX_RX_QUEUE        8

/* Структура для передачи данных через USB */
typedef struct {
    uint8_t* data;      /* Указатель на данные */
    uint16_t len;       /* Длина данных */
    uint32_t timestamp; /* Временная метка */
} USB_TxPacket_t;

/* Структура для приёма данных через USB */
typedef struct {
    uint8_t* data;      /* Указатель на данные */
    uint16_t len;       /* Длина данных */
    uint32_t timestamp; /* Временная метка */
} USB_RxPacket_t;

/* ===========================================================================
 * СТАТИСТИКА И МОНИТОРИНГ
 * =========================================================================== */

/**
 * @brief Статистика радиоустройства
 */
typedef struct {
    uint32_t tx_packets;            /* Отправлено пакетов */
    uint32_t rx_packets;            /* Принято пакетов */
    uint32_t tx_bytes;              /* Отправлено байт */
    uint32_t rx_bytes;              /* Принято байт */
    uint32_t tx_errors;             /* Ошибок передачи */
    uint32_t rx_errors;             /* Ошибок приёма */
    uint32_t crc_errors;            /* Ошибок CRC */
    uint32_t preamble_errors;       /* Ошибок преамбулы */
    uint32_t sync_errors;           /* Ошибок синхронизации */
    uint32_t timeout_errors;        /* Таймаутов */
    uint32_t last_rssi;             /* Последний RSSI */
    uint32_t last_fp_ampl1;         /* Последний FP Ampl1 */
    uint32_t last_fp_ampl2;         /* Последний FP Ampl2 */
    uint32_t last_fp_ampl3;         /* Последний FP Ampl3 */
} RadioStats_t;

/**
 * @brief Статистика системы
 */
typedef struct {
    uint32_t uptime_seconds;        /* Время работы системы */
    uint32_t total_tx_packets;      /* Всего отправлено пакетов */
    uint32_t total_rx_packets;      /* Всего принято пакетов */
    uint32_t total_tx_bytes;        /* Всего отправлено байт */
    uint32_t total_rx_bytes;        /* Всего принято байт */
    uint32_t total_errors;          /* Всего ошибок */
    uint8_t  system_state;          /* Состояние системы */
    uint8_t  tx_device_state;       /* Состояние TX устройства */
    uint8_t  rx_device_state;       /* Состояние RX устройства */
    uint8_t  usb_state;             /* Состояние USB */
} SystemStats_t;

/* ===========================================================================
 * КОНСТАНТЫ И СТРУКТУРЫ ДЛЯ CIR
 * =========================================================================== */

/**
 * @brief Конфигурация CIR
 */
typedef struct {
    uint8_t  enable;                /* Включение CIR */
    uint16_t start_index;           /* Начальный индекс CIR */
    uint16_t end_index;             /* Конечный индекс CIR */
    uint8_t  decimation_factor;     /* Коэффициент децимации */
    uint8_t  averaging_enable;      /* Включение усреднения */
    uint8_t  averaging_count;       /* Количество усреднений */
} CIR_Config_t;

/**
 * @brief Данные CIR
 */
typedef struct {
    uint32_t timestamp;             /* Метка времени */
    uint16_t length;                /* Длина данных CIR */
    int16_t* data;                  /* Данные CIR (I/Q или амплитуда) */
    uint32_t fp_ampl1;              /* FP Amplitude 1 */
    uint32_t fp_ampl2;              /* FP Amplitude 2 */
    uint32_t fp_ampl3;              /* FP Amplitude 3 */
    uint32_t rssi;                  /* RSSI */
    uint8_t  channel;               /* Канал */
    uint8_t  prf;                   /* PRF */
    uint8_t  preamble_length;       /* Длина преамбулы */
} CIR_Data_t;

/**
 * @brief Команда для работы с CIR
 */
typedef struct {
    uint8_t       command_type;     /* Тип команды */
    CIR_Config_t  config;           /* Конфигурация CIR */
    uint8_t       device_id;        /* ID устройства */
    uint8_t       mode;             /* Режим работы */
} RadioCommand_CIR_t;

/**
 * @brief Событие CIR
 */
typedef struct {
    uint8_t       event_type;       /* Тип события */
    CIR_Data_t    data;             /* Данные CIR */
    uint8_t       device_id;        /* ID устройства */
    uint8_t       status;           /* Статус */
} RadioEvent_CIR_t;

/* ===========================================================================
 * КОМАНДЫ И СОБЫТИЯ CIR
 * =========================================================================== */

/* Команды для работы с CIR */
#define RADIO_CMD_CIR_START         0x50  /* Запуск сбора CIR */
#define RADIO_CMD_CIR_STOP          0x51  /* Остановка сбора CIR */
#define RADIO_CMD_CIR_CONFIG        0x52  /* Конфигурация CIR */
#define RADIO_CMD_CIR_READ          0x53  /* Чтение CIR данных */
#define RADIO_CMD_CIR_CALIBRATE     0x54  /* Калибровка CIR */

/* События CIR */
#define RADIO_EVT_CIR_DATA_READY    0x60  /* Данные CIR готовы */
#define RADIO_EVT_CIR_COMPLETE      0x61  /* Сбор CIR завершен */
#define RADIO_EVT_CIR_ERROR         0x62  /* Ошибка CIR */
#define RADIO_EVT_CIR_CALIBRATED    0x63  /* CIR откалиброван */

/* ===========================================================================
 * КОНСТАНТЫ CIR
 * =========================================================================== */

#define CIR_MAX_LENGTH              1024  /* Максимальная длина CIR */
#define CIR_DEFAULT_START_INDEX     0     /* Начальный индекс по умолчанию */
#define CIR_DEFAULT_END_INDEX       511   /* Конечный индекс по умолчанию */
#define CIR_DECIMATION_FACTOR_1     0     /* Без децимации */
#define CIR_DECIMATION_FACTOR_2     1     /* Децимация 2 */
#define CIR_DECIMATION_FACTOR_4     2     /* Децимация 4 */
#define CIR_DECIMATION_FACTOR_8     3     /* Децимация 8 */

/* ===========================================================================
 * ПРОТОТИПЫ ФУНКЦИЙ
 * =========================================================================== */

/* Создание команды (выделяет память) */
RadioCommand_t* RADIO_CreateCommand(RadioCommandType cmd, DW1000_Device* dev);

/* Освобождение команды */
void RADIO_FreeCommand(RadioCommand_t* cmd);

/* Создание события (для использования в прерываниях) */
RadioEvent_t* RADIO_CreateEvent(RadioEventType event, DW1000_Device* dev);

/* Освобождение события */
void RADIO_FreeEvent(RadioEvent_t* event);

#endif /* RADIO_DEFS_H */
