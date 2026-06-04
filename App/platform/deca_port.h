/**
 * @file    deca_port.h
 * @brief   Платформенный слой DecaDriver под STM32F411 + HAL.
 *          Реализует функции, которые DecaDriver ожидает извне:
 *          writetospi(), readfromspi(), deca_sleep(), decamutexon/off().
 *          Плюс вспомогательные функции уровня платы (reset, выбор чипа).
 */
#ifndef DECA_PORT_H
#define DECA_PORT_H

#include "deca_device_api.h"
#include "board_config.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Выбрать активный DW1000 для последующих операций.
 *        Делает ДВЕ вещи: переключает локальные данные драйвера
 *        (dwt_setlocaldataptr) и запоминает, какую SPI-шину/CS использовать
 *        в writetospi/readfromspi.
 * @param dev_index  DW_DEV_M1 (0) или DW_DEV_M2 (1)
 * @return DWT_SUCCESS / DWT_ERROR
 */
int deca_port_select_device(int dev_index);

/**
 * @brief Аппаратный сброс выбранного модуля по линии RST (с учётом полярности).
 * @param dev_index  DW_DEV_M1 / DW_DEV_M2
 */
void deca_port_hard_reset(int dev_index);

/**
 * @brief Переключить скорость SPI активной шины.
 *        До инициализации DW1000 — медленно (<3 МГц), после — быстро (до 20 МГц).
 *        Реализация зависит от того, как заданы прескалеры в CubeMX (см. .c).
 */
void deca_port_spi_set_slow(void);
void deca_port_spi_set_fast(void);

#ifdef __cplusplus
}
#endif

#endif /* DECA_PORT_H */
