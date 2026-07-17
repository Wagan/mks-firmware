# План: периодическая передача — TX_PERIODIC (0x21)

**Дата:** 2026-07-17
**Автор:** Vagan Sarukhanov
**Цель:** периодически (с заданным периодом) слать один и тот же кадр в эфир с
модуля-источника (M1), не блокируя приём команд по USB. Проверяется тем же
loopback M1→M2: при активной периодике `count` на M2 растёт со скоростью,
соответствующей периоду; `TX_STOP` останавливает рост.
**Статус:** план на согласование. Правок нет. Vendor не трогаем.
**Объём этого захода:** только `TX_PERIODIC (0x21)`. Останов — уже существующим
`TX_STOP (0x22)` (расширяем его действие на периодику). Никаких новых команд.
**Опора на код (проверено в присланных исходниках, НЕ по памяти):**
- `HandleTX_FRAME` (protocol.c:638) — готовый TX-паттерн: `deca_port_select_device
  (DW_TX_SOURCE_DEV)` → clear `SYS_STATUS_TXFRS` → `dwt_writetxdata(len+2,…,0)` →
  `dwt_writetxfctrl(len+2,0,0)` → `dwt_starttx(DWT_START_TX_IMMEDIATE)` → busy-wait
  `TXFRS` с `guard=TX_WAIT_GUARD`. Периодика переиспользует ровно это.
- `HandleTX_STOP` (protocol.c:675) — `dwt_forcetrxoff()` на `DW_TX_SOURCE_DEV`.
- `TX_FRAME_MAX=125`, `TX_WAIT_GUARD=100000u`, `GET_U16LE` (protocol.c:628,629,44).
- Main loop: `while(1){ PROTOCOL_PollRx(); PROTOCOL_PollRadio(); }`
  (main.c:120–121) — точка вставки нового `PROTOCOL_PollTx()`.
- `CMD_TX_PERIODIC=0x21` уже в enum (protocol.h:43) — определять не нужно.
- **Таймера в проекте НЕТ:** в main.c нет `htim`/`MX_TIM*_Init`/`HAL_TIM`; в
  stm32f4xx_it.c из таймерных только `SysTick_Handler` (it.c:183). Значит
  `HAL_GetTick()` (мс-тик, работает в thread mode) доступен, аппаратного TIM с
  колбэком — нет.

---

## 1. Референс и как ложится на нас

TX_PERIODIC = «периодически исполнять то, что уже делает TX_FRAME». Отдельного
DecaWave-примера под периодику нет; берём паттерн одиночного TX (`ss_init_main.c`,
на который опирался PLAN_tx §1) и оборачиваем его повтором по времени.

Ключевое отличие от `TX_FRAME`: `TX_FRAME` — разовое синхронное действие в
контексте обработчика команды. Периодика должна:
- **не блокировать** приём USB-команд (иначе `TX_STOP` не пройдёт — обработчик
  TX_PERIODIC не может сам крутить бесконечный цикл);
- поэтому работает **как RX**: команда `TX_PERIODIC` только *взводит режим*
  (сохраняет период+кадр, ставит флаг), а реальная периодическая посылка —
  в main loop через новый `PROTOCOL_PollTx()` (двойник `PROTOCOL_PollRadio`).

## 2. Механизм интервала (РАЗВИЛКА — решение §9.1)

Аппаратного TIM нет. Два пути:

- **Путь A (реком., без CubeMX): программный интервал по `HAL_GetTick()` в main
  loop.** `PollTx()` на каждом проходе сравнивает `HAL_GetTick() - tx_last_ms`
  с `tx_period_ms`; при достижении — шлёт кадр (тем же кодом, что TX_FRAME) и
  обновляет `tx_last_ms`. Плюсы: ноль зависимостей от CubeMX, вся логика в нашем
  App-коде, снимается одной строкой в `TX_STOP`. Минус: точность периода
  ограничена частотой прохода main loop и мс-гранулярностью SysTick (для НИР-задачи
  «периодический тест-сигнал» этого достаточно; суб-мс джиттер несущественен).
- **Путь B (позже, если понадобится точность): аппаратный TIM + колбэк.** Требует
  **CubeMX-шага (за тобой):** завести TIMx (update-прерывание), NVIC, `MX_TIMx_Init`,
  обработчик в it.c → `HAL_TIM_PeriodElapsedCallback` ставит `volatile` флаг, а
  фактическая посылка всё равно в main loop (SPI-транзакция тяжёлая, не в ISR —
  как договорено про «короткий ISR, тяжёлое в фоне», handoff §11.5). Это отдельный
  заход, не сейчас.

Рекомендация: **Путь A сейчас.** Он самодостаточен и проверяем на железе без
изменений в CubeMX. Если по результатам понадобится точный период — Путь B.

> Тонкость Пути A: сама посылка кадра содержит busy-wait TXFRS (до
> `TX_WAIT_GUARD` итераций). На один период это тот же микро-блок, что и в
> TX_FRAME (десятки-сотни мкс) — приём USB между посылками не страдает, т.к.
> PollRx вызывается в том же цикле между тиками. Разумный минимум периода
> ограничим снизу (см. §9.4).

## 3. Кто передаёт / принимает

Без изменений против TX_FRAME/loopback: TX = `DW_TX_SOURCE_DEV` (M1), приём ловит
`DW_RX_LISTEN_DEV` (M2) в `PROTOCOL_PollRadio`. Раздельные SPI (M1→SPI2, M2→SPI3),
гонок нет: `PollTx` и `PollRadio` — оба в main loop, последовательно; каждый перед
своими вызовами делает `deca_port_select_device` на свой модуль.

> Порядок в while(1): `PollRx()` (команды) → `PollRadio()` (приём) → `PollTx()`
> (периодика). Каждый самодостаточен; переключение активного устройства — внутри
> каждого. Добавляем **одну** строку вызова `PROTOCOL_PollTx()` (решение §9.2 —
> куда именно; реком. сразу после PollRadio).

## 4. Формат параметров (SPEC §6, из кода не трогаем)

`TX_PERIODIC` params: `period_ms u16 (LE)`, `length u16 (LE)`, `payload[length]`.
- `period_ms` — период между началами посылок, мс.
- `length` — длина payload (как в TX_FRAME).
- `payload` — байты кадра; **сохраняем копию** в статический буфер (params
  диспетчера переиспользуются следующей командой — нельзя держать указатель).

Валидация (по образцу TX_FRAME):
- `params_len < 4` → `INVALID_PARAM` (нет даже period+length).
- `length > TX_FRAME_MAX` → `BUFFER_OVERFLOW`.
- `params_len < 4 + length` → `INVALID_PARAM` (payload короче заявленного).
- `period_ms < TX_PERIOD_MIN_MS` → `INVALID_PARAM` (защита от «шторма», §9.4).
- `DW_TX_SOURCE_DEV` не initialized → `RADIO_ERROR`.

## 5. Состояние периодики (наш код, App/) — по образцу rx_metrics/rx_active

```c
static volatile uint8_t tx_periodic_active;   /* 1 = режим включён */
static uint16_t  tx_period_ms;                /* период посылки, мс */
static uint32_t  tx_last_ms;                  /* HAL_GetTick() последней посылки */
static uint16_t  tx_periodic_len;             /* длина payload */
static uint8_t   tx_periodic_frame[TX_FRAME_MAX];  /* копия payload */
static uint32_t  tx_periodic_count;           /* послано кадров (диагностика) */
```
Инициализация в `PROTOCOL_Init()` (рядом с rx_active=0): всё в 0.

## 6. PollTx (main loop) — двойник PollRadio

```c
void PROTOCOL_PollTx(void) {
    if (!tx_periodic_active) return;
    uint32_t now = HAL_GetTick();
    if ((uint32_t)(now - tx_last_ms) < tx_period_ms) return;   /* ещё рано */
    tx_last_ms = now;

    if (deca_port_select_device(DW_TX_SOURCE_DEV) != DWT_SUCCESS) return;
    dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_TXFRS);
    if (dwt_writetxdata((uint16_t)(tx_periodic_len + 2), tx_periodic_frame, 0) != DWT_SUCCESS) return;
    dwt_writetxfctrl((uint16_t)(tx_periodic_len + 2), 0, 0);
    if (dwt_starttx(DWT_START_TX_IMMEDIATE) != DWT_SUCCESS) return;
    uint32_t guard = TX_WAIT_GUARD;
    while (!(dwt_read32bitreg(SYS_STATUS_ID) & SYS_STATUS_TXFRS)) {
        if (--guard == 0) break;          /* кадр не ушёл — молча пропускаем период */
    }
    dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_TXFRS);
    tx_periodic_count++;
}
```
> Ошибку в PollTx **не** возвращаем наружу (ответа уже нет — команда завершилась
> при взведении режима). Пропущенная посылка = пропущенный период; при
> систематическом сбое это видно по `tx_periodic_count`/loopback-`count`.
> `HAL_GetTick()` — из `main.h`/HAL, `#include "main.h"` в protocol.c добавить,
> если ещё не включён (проверить при реализации; сейчас protocol.c его не
> включает — решение §9.5).

## 7. Обработчики: TX_PERIODIC + правка TX_STOP

```c
// TX_PERIODIC (0x21) — взвести режим, немедленно вернуть OK (посылка — в PollTx)
static ResponseStatus HandleTX_PERIODIC(const uint8_t* params, uint8_t params_len,
                                        uint8_t** out_data, uint8_t* out_len) {
    (void)out_data; *out_len = 0;
    if (params_len < 4) return STATUS_INVALID_PARAM;
    uint16_t period = GET_U16LE(&params[0]);
    uint16_t length = GET_U16LE(&params[2]);
    if (length > TX_FRAME_MAX)          return STATUS_BUFFER_OVERFLOW;
    if (params_len < 4 + length)        return STATUS_INVALID_PARAM;
    if (period < TX_PERIOD_MIN_MS)      return STATUS_INVALID_PARAM;
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
`TX_STOP (0x22)` — добавить снятие периодики ПЕРЕД `dwt_forcetrxoff` (одна строка):
```c
    tx_periodic_active = 0;      // <-- добавляется
    // далее существующий код: select_device + dwt_forcetrxoff
```
> Так `TX_STOP` останавливает и одиночный «хвост», и периодику — единая точка
> останова передатчика (решение §9.3). RX это не трогает.

## 8. Регистрация

В `PROTOCOL_RegisterAllHandlers()` — одна строка (рядом с TX_FRAME/TX_STOP):
```c
    PROTOCOL_RegisterHandler(CMD_TX_PERIODIC, HandleTX_PERIODIC);
```
`main.c` — одна строка в USER CODE 3: `PROTOCOL_PollTx();` после `PROTOCOL_PollRadio();`.
Прототип `void PROTOCOL_PollTx(void);` — в protocol.h (рядом с PollRadio).

## 9. Решения на согласование с архитектором

1. **Механизм интервала:** Путь A = программный по `HAL_GetTick()` в main loop
   (реком., без CubeMX) или сразу Путь B = аппаратный TIM (тогда CubeMX-шаг за
   тобой первым)?
2. **Вызов PollTx** ставим в while(1) сразу после `PROTOCOL_PollRadio()` (реком.)?
3. **TX_STOP** расширяем на останов периодики (реком., единая точка) — подтвердить.
4. **`TX_PERIOD_MIN_MS`** (нижняя граница периода, защита от шторма). Реком.: 5 мс
   (эфирное время кадра Mode3 ~сотни мкс + busy-wait; 5 мс = запас, USB не голодает).
   Значение на согласование.
5. **`#include "main.h"` в protocol.c** ради `HAL_GetTick()` — ок? (Альтернатива:
   объявить `extern uint32_t HAL_GetTick(void);` локально. Реком.: include main.h,
   он уже тянет HAL.)
6. **Ответ TX_PERIODIC:** OK сразу при взведении (DATA нет), реком. Или отдавать
   что-то в DATA? Реком.: пусто, как TX_FRAME.

## 10. Порядок работ (малые шаги)

1. `protocol.h`: прототип `PROTOCOL_PollTx`. (СС)
2. `protocol.c`: состояние периодики (§5) + init в `PROTOCOL_Init` + `HandleTX_PERIODIC`
   + правка `HandleTX_STOP` (одна строка) + `PROTOCOL_PollTx` + `#define
   TX_PERIOD_MIN_MS` + регистрация + `#include "main.h"`. (СС)
3. `main.c`: `PROTOCOL_PollTx();` в USER CODE 3. (СС)
4. syntax-check → твой линк-билд.
5. Хост (я): в `mks_protocol.py` — `tx_periodic(period_ms, payload)`; в
   `mks_console.py` — команда `txperiodic <period_ms> <hex...>` (стоп — существующим
   `txstop`).
6. Проверка на железе (loopback M1→M2): init → mode3 → rxstart →
   `txperiodic 100 DE AD BE EF 01` → несколько раз `metrics` (count растёт ~10/с
   при 100 мс) → `txstop` → `metrics` (count перестал расти). Успех: рост count
   коррелирует с периодом, останавливается по txstop.
7. Позже: аппаратный TIM (Путь B) при потребности в точном периоде; EXTI.
