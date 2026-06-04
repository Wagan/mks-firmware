/**
 * @file    bringup.c
 * @brief   Первичная проверка связи с DW1000: чтение DEV_ID.
 *          Ожидаемое значение для DW1000 (MP) = 0xDECA0130.
 *
 *  Как использовать (bare-metal, без RTOS):
 *    1) В main.c после MX_*_Init() вызвать bringup_read_devids();
 *    2) Поставить точку останова или смотреть переменные в отладчике,
 *       либо вывести через SWO/ITM (printf), если настроен.
 *
 *  Порядок для одного чипа:
 *    deca_port_select_device(idx) -> deca_port_hard_reset(idx)
 *    -> spi slow -> dwt_readdevid() -> сверка с 0xDECA0130.
 */
#include "deca_port.h"
#include "deca_device_api.h"
#include "board_config.h"

/* Результаты для отладчика */
volatile uint32_t g_devid[DW_DEVICE_COUNT];
volatile int      g_devid_ok[DW_DEVICE_COUNT];

/**
 * @brief Прочитать DEV_ID со всех модулей платы. Заполняет g_devid[].
 * @return число модулей, ответивших корректным 0xDECA0130.
 */
int bringup_read_devids(void)
{
    int ok_count = 0;

    for (int i = 0; i < DW_DEVICE_COUNT; ++i) {
        g_devid[i]    = 0;
        g_devid_ok[i] = 0;

        if (deca_port_select_device(i) != DWT_SUCCESS) {
            continue;
        }

        deca_port_hard_reset(i);     /* аппаратный сброс именно этого модуля */
        deca_port_spi_set_slow();    /* медленный SPI для надёжного чтения */

        uint32_t id = dwt_readdevid();
        g_devid[i] = id;

        if (id == DWT_DEVICE_ID) {   /* 0xDECA0130 */
            g_devid_ok[i] = 1;
            ok_count++;
        }
    }

    return ok_count;
}
