/**
 * @file    deca_port.c
 * @brief   Платформенный слой DecaDriver под STM32F411 + HAL (bare-metal, без RTOS).
 *
 *  Реализует обязательный интерфейс DecaDriver:
 *    - writetospi()  / readfromspi()  — SPI-транзакции
 *    - deca_sleep()                    — задержка в мс
 *    - decamutexon() / decamutexoff()  — критическая секция (без RTOS: маска IRQ)
 *
 *  Особенности МКС:
 *    - Два модуля на РАЗНЫХ шинах: M1=SPI2, M2=SPI3. Активная шина выбирается
 *      через deca_port_select_device().
 *    - RST через N-MOSFET => полярность DW_RST_ACTIVE_HIGH (OQ-9).
 *
 *  ВАЖНО: для двух устройств в проект должен быть передан -D DWT_NUM_DW_DEV=2
 *  (настройка компилятора в CubeIDE), иначе массив локальных данных драйвера
 *  будет рассчитан на один чип.
 */
#include "deca_port.h"
#include "main.h"
#include <string.h>

/* Хэндлы SPI объявлены в main.c (сгенерированы CubeMX). */
extern SPI_HandleTypeDef DW_M1_SPI;   /* hspi2 на МКС */
#if (DW_DEVICE_COUNT > 1)
extern SPI_HandleTypeDef DW_M2_SPI;   /* hspi3 на МКС */
#endif

/* --- Текущий выбранный контекст (какая шина и какой CS активны сейчас) --- */
static SPI_HandleTypeDef *s_spi      = NULL;
static GPIO_TypeDef      *s_cs_port  = NULL;
static uint16_t           s_cs_pin   = 0;

/* Таблица описаний устройств для удобства */
typedef struct {
    SPI_HandleTypeDef *spi;
    GPIO_TypeDef      *cs_port;
    uint16_t           cs_pin;
    GPIO_TypeDef      *rst_port;
    uint16_t           rst_pin;
} dw_hw_t;

static const dw_hw_t s_hw[DW_DEVICE_COUNT] = {
    { &DW_M1_SPI, DW_M1_CS_PORT, DW_M1_CS_PIN, DW_M1_RST_PORT, DW_M1_RST_PIN },
#if (DW_DEVICE_COUNT > 1)
    { &DW_M2_SPI, DW_M2_CS_PORT, DW_M2_CS_PIN, DW_M2_RST_PORT, DW_M2_RST_PIN },
#endif
};

/* ------------------------------------------------------------------ */
/*  Выбор активного устройства                                        */
/* ------------------------------------------------------------------ */
int deca_port_select_device(int dev_index)
{
    if (dev_index < 0 || dev_index >= DW_DEVICE_COUNT) {
        return DWT_ERROR;
    }
    /* 1) переключаем локальные данные самого драйвера */
    if (dwt_setlocaldataptr((unsigned int)dev_index) != DWT_SUCCESS) {
        return DWT_ERROR;
    }
    /* 2) запоминаем, какую шину/CS использовать в writetospi/readfromspi */
    s_spi     = s_hw[dev_index].spi;
    s_cs_port = s_hw[dev_index].cs_port;
    s_cs_pin  = s_hw[dev_index].cs_pin;
    return DWT_SUCCESS;
}

/* ------------------------------------------------------------------ */
/*  Аппаратный сброс по линии RST                                     */
/* ------------------------------------------------------------------ */
void deca_port_hard_reset(int dev_index)
{
    if (dev_index < 0 || dev_index >= DW_DEVICE_COUNT) return;

    GPIO_TypeDef *port = s_hw[dev_index].rst_port;
    uint16_t      pin  = s_hw[dev_index].rst_pin;

#if (DW_RST_ACTIVE_HIGH)
    GPIO_PinState assert_lvl   = GPIO_PIN_SET;
    GPIO_PinState deassert_lvl = GPIO_PIN_RESET;
#else
    GPIO_PinState assert_lvl   = GPIO_PIN_RESET;
    GPIO_PinState deassert_lvl = GPIO_PIN_SET;
#endif

    HAL_GPIO_WritePin(port, pin, assert_lvl);   /* в сброс */
    deca_sleep(2);
    HAL_GPIO_WritePin(port, pin, deassert_lvl); /* из сброса */
    deca_sleep(2);                              /* дать чипу подняться */
}

/* ------------------------------------------------------------------ */
/*  Скорость SPI                                                      */
/* ------------------------------------------------------------------ */
/* Простейшая реализация: меняем прескалер на текущей активной шине.
 * Значения прескалеров зависят от тактовой APB. Здесь — каркас; конкретные
 * BaudRatePrescaler подобрать под вашу частоту шины (см. CubeMX -> Clock Config).
 * Цель: slow < 3 МГц (для надёжной инициализации), fast <= 20 МГц. */
void deca_port_spi_set_slow(void)
{
    if (!s_spi) return;
    s_spi->Init.BaudRatePrescaler = SPI_BAUDRATEPRESCALER_32; /* подобрать */
    HAL_SPI_Init(s_spi);
}

void deca_port_spi_set_fast(void)
{
    if (!s_spi) return;
    s_spi->Init.BaudRatePrescaler = SPI_BAUDRATEPRESCALER_4;  /* подобрать */
    HAL_SPI_Init(s_spi);
}

/* ================================================================== */
/*  ОБЯЗАТЕЛЬНЫЙ интерфейс DecaDriver                                 */
/* ================================================================== */

#define CS_LOW()   HAL_GPIO_WritePin(s_cs_port, s_cs_pin, GPIO_PIN_RESET)
#define CS_HIGH()  HAL_GPIO_WritePin(s_cs_port, s_cs_pin, GPIO_PIN_SET)
#define SPI_TIMEOUT_MS  5

/**
 * @brief Запись в DW1000: сначала заголовок, затем тело.
 */
int writetospi(uint16 headerLength, const uint8 *headerBuffer,
               uint32 bodyLength,  const uint8 *bodyBuffer)
{
    if (!s_spi) return DWT_ERROR;

    decaIrqStatus_t st = decamutexon();
    CS_LOW();

    HAL_StatusTypeDef ok = HAL_SPI_Transmit(s_spi, (uint8_t *)headerBuffer,
                                            (uint16_t)headerLength, SPI_TIMEOUT_MS);
    if (ok == HAL_OK && bodyLength) {
        ok = HAL_SPI_Transmit(s_spi, (uint8_t *)bodyBuffer,
                              (uint16_t)bodyLength, SPI_TIMEOUT_MS);
    }

    CS_HIGH();
    decamutexoff(st);
    return (ok == HAL_OK) ? DWT_SUCCESS : DWT_ERROR;
}

/**
 * @brief Чтение из DW1000: передаём заголовок, затем принимаем readLength байт.
 */
int readfromspi(uint16 headerLength, const uint8 *headerBuffer,
                uint32 readLength,   uint8 *readBuffer)
{
    if (!s_spi) return DWT_ERROR;

    decaIrqStatus_t st = decamutexon();
    CS_LOW();

    HAL_StatusTypeDef ok = HAL_SPI_Transmit(s_spi, (uint8_t *)headerBuffer,
                                            (uint16_t)headerLength, SPI_TIMEOUT_MS);
    if (ok == HAL_OK && readLength) {
        ok = HAL_SPI_Receive(s_spi, readBuffer,
                            (uint16_t)readLength, SPI_TIMEOUT_MS);
    }

    CS_HIGH();
    decamutexoff(st);
    return (ok == HAL_OK) ? DWT_SUCCESS : DWT_ERROR;
}

/**
 * @brief Задержка в миллисекундах.
 */
void deca_sleep(unsigned int time_ms)
{
    HAL_Delay(time_ms);
}

/**
 * @brief Вход в критическую секцию.
 *        Без RTOS: запрещаем прерывания, возвращаем прежнее состояние PRIMASK.
 *        (Когда добавим FreeRTOS — заменить на taskENTER_CRITICAL/мьютекс.)
 */
decaIrqStatus_t decamutexon(void)
{
    decaIrqStatus_t s = (decaIrqStatus_t)__get_PRIMASK();
    __disable_irq();
    return s;
}

/**
 * @brief Выход из критической секции — восстановить состояние прерываний.
 */
void decamutexoff(decaIrqStatus_t s)
{
    if (!s) {           /* если прерывания были разрешены — снова разрешить */
        __enable_irq();
    }
}
