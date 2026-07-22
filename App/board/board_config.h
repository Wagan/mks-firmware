/**
 * @file    board_config.h
 * @brief   Аппаратная конфигурация платы. Различия между платами (МКС /
 *          мини-стенд) вынесены сюда. Выбор платы — через -D BOARD_MKS или
 *          -D BOARD_MINISTEND в настройках компилятора.
 *
 *  Источник распиновки МКС: схема PCI_E_PCB (STM32F411RETx).
 *  ВНИМАНИЕ:
 *    - У каждого DWM1000 СВОЯ шина SPI: M1 -> SPI2, M2 -> SPI3.
 *      (SPI1 занят внешней EEPROM M95080 — это НЕ радио.)
 *    - Перекрёстная нумерация IRQ: IRQ1 -> M2, IRQ2 -> M1.
 *    - RST идёт через N-MOSFET (BSS138) => уровень, вероятно, ИНВЕРТИРОВАН.
 *      Полярность вынесена в DW_RST_ACTIVE_HIGH (проверить на железе, OQ-9).
 */
#ifndef BOARD_CONFIG_H
#define BOARD_CONFIG_H

#include "main.h"   /* для типов GPIO_TypeDef* и хэндлов HAL */

/* По умолчанию собираем под МКС, если плата не задана извне */
#if !defined(BOARD_MKS) && !defined(BOARD_MINISTEND)
#define BOARD_MKS
#endif

/* ======================================================================== */
/*  МКС — STM32F411RETx, 2x DWM1000                                          */
/* ======================================================================== */
#if defined(BOARD_MKS)

#define BOARD_NAME                  "MKS"
#define DW_DEVICE_COUNT             2

/* HSE кварц 24 МГц (для справки; реальная настройка PLL — в CubeMX/.ioc) */
#define BOARD_HSE_HZ                24000000U

/* --- Индексы устройств (соответствие модуль<->роль уточнить на железе) --- */
#define DW_DEV_M1                   0   /* предполагаемо: Источник (TX) */
#define DW_DEV_M2                   1   /* предполагаемо: Индикатор (RX) */

/* Wagan/Dima: 2026-07-22 — ОДНОКАБЕЛЬНАЯ сборка (проверка гипотезы самоглушения M1→M2,
 * §15.1). Раскомментировать строку ниже → станция-полудуплекс на ОДНОМ модуле M2 (оба
 * направления), M1 полностью заглушён (forcetrxoff, не конфигурится). DW_DEVICE_COUNT
 * остаётся 2 (M2 = индекс 1; INIT должен доходить до него; на плате только с M2 —
 * INIT переживает). Двухмодульная (M1 TX / M2 RX, CIR/loopback) — при закомментированном
 * флаге, поведение прежнее. НЕ забыть пересобрать после смены флага.
 * Итог исследования дальности (docs\FINDINGS_range_investigation.md): гипотеза
 * самоглушения НЕ подтвердилась — по умолчанию флаг ЗАКОММЕНТИРОВАН (двухмодульная
 * сборка). Опция сохранена: нужна для третьей платы, где физически только M2. */
/* #define MKS_SIMPLEX */

#ifdef MKS_SIMPLEX
  /* Однокабельный полудуплекс: оба направления на ОДИН чип M2. */
  #define DW_RX_LISTEN_DEV          DW_DEV_M2
  #define DW_TX_SOURCE_DEV          DW_DEV_M2
  #define DW_SINGLE_MODULE          1     /* гейт полудуплекс-связки + заглушения M1 */
#else
  /* Двухмодульная (как сейчас): M2 слушает, M1 передаёт (loopback M1→M2). */
  #define DW_RX_LISTEN_DEV          DW_DEV_M2
  #define DW_TX_SOURCE_DEV          DW_DEV_M1
  #define DW_SINGLE_MODULE          0
#endif

/* --- Полярность сброса (RST через BSS138). OQ-9: проверить на железе. ---
 * Гипотеза: лог.1 на пине МК -> MOSFET открыт -> RSTn к земле -> сброс актив.
 * Если окажется наоборот — поменять на 0 (одно место). */
#define DW_RST_ACTIVE_HIGH          1

/* --- M1: шина SPI2, CS=PB12, IRQ через EXTI (линия PB9), RST=PB6 --- */
/* hspiX объявляются в main.c (CubeMX). Здесь — extern-ссылки. */
#define DW_M1_SPI                   hspi2
#define DW_M1_CS_PORT               GPIOB
#define DW_M1_CS_PIN                GPIO_PIN_12
#define DW_M1_RST_PORT              GPIOB
#define DW_M1_RST_PIN               GPIO_PIN_6      /* RST1 */
#define DW_M1_IRQ_PIN               GPIO_PIN_9      /* IRQ2 -> M1 (перекрёст!) */

/* --- M2: шина SPI3, CS=PA15, IRQ через EXTI (линия PB8), RST=PB7 --- */
#define DW_M2_SPI                   hspi3
#define DW_M2_CS_PORT               GPIOA
#define DW_M2_CS_PIN                GPIO_PIN_15
#define DW_M2_RST_PORT              GPIOB
#define DW_M2_RST_PIN               GPIO_PIN_7      /* RST2 */
#define DW_M2_IRQ_PIN               GPIO_PIN_8      /* IRQ1 -> M2 (перекрёст!) */

/* Внешняя EEPROM M95080 на SPI1 (хранилище настроек) */
#define EEPROM_SPI                  hspi1
#define EEPROM_CS_PORT              GPIOA
#define EEPROM_CS_PIN               GPIO_PIN_4

/* На МКС физической DEBUG-консоли НЕТ (всё через MATLAB/USB CDC) */
/* #define DEBUG_CONSOLE_ENABLED */

/* ======================================================================== */
/*  Мини-стенд — Nucleo-F411RE, 1x DWM1000 (ОТЛОЖЕН, заготовка)              */
/* ======================================================================== */
#elif defined(BOARD_MINISTEND)

#define BOARD_NAME                  "MINISTEND"
#define DW_DEVICE_COUNT             1
#define BOARD_HSE_HZ                8000000U

#define DW_DEV_M1                   0
#define DW_RX_LISTEN_DEV            DW_DEV_M1   /* единственный модуль */
#define DW_TX_SOURCE_DEV            DW_DEV_M1   /* единственный модуль */
#define DW_SINGLE_MODULE            1           /* один чип → полудуплекс-связка (§15.1) */

#define DW_RST_ACTIVE_HIGH          1   /* прямое подключение, уточнить */

/* Один модуль на SPI1: CS-PA4, SCK-PB3, MISO-PB4, MOSI-PB5, IRQ-PB0, RST-PC0 */
#define DW_M1_SPI                   hspi1
#define DW_M1_CS_PORT               GPIOA
#define DW_M1_CS_PIN                GPIO_PIN_4
#define DW_M1_RST_PORT              GPIOC
#define DW_M1_RST_PIN               GPIO_PIN_0
#define DW_M1_IRQ_PIN               GPIO_PIN_0      /* PB0 */

/* DEBUG-консоль через ST-LINK VCP (USART2, PA2/PA3) */
#define DEBUG_CONSOLE_ENABLED
#define DEBUG_CONSOLE_UART          huart2

#else
#error "Не задана плата: определите BOARD_MKS или BOARD_MINISTEND"
#endif

#endif /* BOARD_CONFIG_H */
