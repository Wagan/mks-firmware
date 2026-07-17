# TASK: реализация TX_PERIODIC (0x21) — ред. 2

**Дата:** 2026-07-17
**Автор:** Vagan Sarukhanov
**Исполнитель:** СС (Claude Code)
**План-основание:** `docs/PLAN_tx_periodic.md` (Путь A согласован).

Реализовать команду `TX_PERIODIC (0x21)` — периодическую передачу кадра поверх
готового `TX_FRAME`. Механизм интервала — **программный по `HAL_GetTick()`** в main
loop (без CubeMX / аппаратного TIM). Периодика работает как RX: команда только
взводит режим, реальная посылка — в main loop через новую `PROTOCOL_PollTx()`.
Останов — расширением существующего `TX_STOP`.

**Не трогать** vendor (`Drivers/decadriver/`). Все правки — в трёх файлах:
`App/protocol/protocol.h`, `App/protocol/protocol.c`, `Core/Src/main.c`.

---

## Шаг 1. `App/protocol/protocol.h`

Рядом с прототипом `PROTOCOL_PollRadio` добавить прототип:

```c
/* Обслуживание периодической передачи: если включён режим TX_PERIODIC, по
 * достижении периода шлёт кадр с DW_TX_SOURCE_DEV. Вызывается из main loop
 * (thread mode) рядом с PROTOCOL_PollRadio(). Активно только после TX_PERIODIC. */
void PROTOCOL_PollTx(void);
```

## Шаг 2. `App/protocol/protocol.c`

> **Примечание (доступ к `HAL_GetTick()`):** явный `#include "main.h"` НЕ нужен —
> `main.h` подтягивается транзитивно через `board_config.h` (`#include "main.h"`),
> который `protocol.c` уже включает. `HAL_GetTick()` доступен без правки списка
> включений. (Проверено СС при сверке плана.)


### 2a. Состояние периодики
Добавить набор статических переменных состояния периодической передачи. Флаги и
счётчики — в секции состояния (рядом с `rx_active` / `rx_metrics`). Массив
`tx_periodic_frame` использует существующий `#define TX_FRAME_MAX 125` — **не
дублировать** этот define. Если из-за порядка объявления `TX_FRAME_MAX` ещё не
виден в секции состояния приёма, объявить `tx_periodic_frame` там, где
`TX_FRAME_MAX` уже определён (перед `HandleTX_FRAME`), а флаги/счётчики — в секции
состояния. Итоговый набор:

```c
static volatile uint8_t tx_periodic_active;   /* 1 = режим TX_PERIODIC включён */
static uint16_t  tx_period_ms;                /* период посылки, мс */
static uint32_t  tx_last_ms;                  /* HAL_GetTick() последней посылки */
static uint16_t  tx_periodic_len;             /* длина payload */
static uint32_t  tx_periodic_count;           /* послано кадров (диагностика) */
static uint8_t   tx_periodic_frame[TX_FRAME_MAX];  /* копия payload */
```

### 2b. Нижняя граница периода
Рядом с `#define TX_WAIT_GUARD` добавить:

```c
/* Нижняя граница периода TX_PERIODIC (мс). Защита от «шторма»: эфирное время
 * кадра Mode3 ~сотни мкс + busy-wait TXFRS; 5 мс — запас, USB не голодает. */
#define TX_PERIOD_MIN_MS  5
```

### 2c. Инициализация в `PROTOCOL_Init()`
Рядом с `rx_active = 0; rx_metrics.valid = 0; rx_metrics.count = 0;` добавить:

```c
    tx_periodic_active = 0;
    tx_periodic_count  = 0;
```

### 2d. Обработчик `HandleTX_PERIODIC`
Добавить рядом с `HandleTX_FRAME` / `HandleTX_STOP`:

```c
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
```

### 2e. Правка `HandleTX_STOP` — добавить останов периодики
В существующий `HandleTX_STOP` добавить одну строку `tx_periodic_active = 0;`
**перед** `deca_port_select_device` (единая точка останова передатчика).

Было:
```c
    (void)params; (void)params_len; (void)out_data;
    *out_len = 0;

    if (deca_port_select_device(DW_TX_SOURCE_DEV) != DWT_SUCCESS) return STATUS_RADIO_ERROR;
    dwt_forcetrxoff();
    return STATUS_OK;
```

Стало:
```c
    (void)params; (void)params_len; (void)out_data;
    *out_len = 0;

    tx_periodic_active = 0;   /* остановить периодику (если была) */
    if (deca_port_select_device(DW_TX_SOURCE_DEV) != DWT_SUCCESS) return STATUS_RADIO_ERROR;
    dwt_forcetrxoff();
    return STATUS_OK;
```

### 2f. Функция `PROTOCOL_PollTx`
Добавить рядом с `PROTOCOL_PollRadio`. Ошибки наружу **не** возвращать (ответа уже
нет — команда завершилась при взведении режима); пропущенная посылка = пропущенный
период, видно по `tx_periodic_count`:

```c
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
```

### 2g. Регистрация обработчика
В `PROTOCOL_RegisterAllHandlers()` рядом с регистрацией TX_FRAME/TX_STOP добавить:

```c
    PROTOCOL_RegisterHandler(CMD_TX_PERIODIC,        HandleTX_PERIODIC);
```

## Шаг 3. `Core/Src/main.c`

В USER CODE 3 (тело `while(1)`), сразу **после** `PROTOCOL_PollRadio();`, добавить:

```c
    PROTOCOL_PollTx();      /* обслуживание периодической передачи (TX_PERIODIC) */
```

## Шаг 4. Проверка и коммит

1. Syntax-check компилятором своего окружения (сборку/линк-билд делает владелец —
   НЕ ты). Убедиться, что нет предупреждений о неиспользуемых переменных и что
   `main.h` даёт `HAL_GetTick()`.
2. git commit + push. Сообщение коммита:
   `feat(protocol): TX_PERIODIC (0x21) via HAL_GetTick main-loop interval; TX_STOP halts periodic`
3. Отчитаться: какие строки изменены в каждом из трёх файлов и результат
   syntax-check.

---

## Границы задачи (что НЕ делать)

- Не трогать `Drivers/decadriver/` (vendor).
- Не заводить аппаратный TIM / не менять CubeMX-конфиг (это Путь B, отдельный заход).
- Не менять хост-скрипты (`tools/`) — их готовит архитектор отдельно.
- Не менять обработчики RX / прочие команды.
- Не собирать и не деплоить — только правки в репозитории + syntax-check + commit/push.
