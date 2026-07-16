# План: приёмный тракт — RX_START (0x30) + GET_SIGNAL_METRICS (0x40)

**Дата:** 2026-07-16
**Цель:** принять сигнал EVK1000 (Mode 3) из эфира. RX_START включает приём,
GET_SIGNAL_METRICS даёт обратную связь (метрики принятого кадра) — RX_START сам
по себе проверить нечем.
**Статус:** план на согласование. Правок нет. Vendor не трогаем.
**Референс:** `docs/reference/decawave-examples/ss_resp_main.c` (официальный
RX-паттерн DecaWave) + README рядом.
**Опора на API (проверено в deca_device_api.h):** `dwt_rxenable(int)`,
`dwt_setrxtimeout(uint16)`, `dwt_setinterrupt(uint32,uint8)`, `dwt_rxreset()`,
`dwt_readrxdata()`, `dwt_read32bitreg(SYS_STATUS_ID)`,
`dwt_readdiagnostics(dwt_rxdiag_t*)`.

---

## 1. Референс-паттерн (ss_resp_main.c) и как ложится на нас

Пример (polling):
```
dwt_rxenable(DWT_START_RX_IMMEDIATE);
while(!(status = SYS_STATUS) & (RXFCG | ALL_RX_TO | ALL_RX_ERR)) {}
if (RXFCG) { clear RXFCG; frame_len = RX_FINFO & RXFL; dwt_readrxdata(...); }
else       { clear ALL_RX_ERR; dwt_rxreset(); }
```
Набор регистров/флагов у нас тот же. Отличие — **где** обрабатываем: не в
busy-wait, а в main loop (thread mode), как INIT/SET_PHY_CONFIG (тяжёлое/SPI вне
ISR). Событие приёма приходит либо по EXTI-флагу, либо опросом SYS_STATUS в цикле
(см. §3 — EXTI сейчас не доведён).

## 2. Архитектура: обработка RXFCG в main loop

Логика приёма живёт в новой функции `PROTOCOL_PollRadio()`, вызываемой из
`while(1)` (USER CODE 3) рядом с `PROTOCOL_PollRx()`:
```
PROTOCOL_PollRadio():
  if (!rx_active) return;                 // приём не включён
  status = dwt_read32bitreg(SYS_STATUS_ID);
  if (status & SYS_STATUS_RXFCG) {
      dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_RXFCG);   // очистить флаг
      frame_len = dwt_read32bitreg(RX_FINFO_ID) & RX_FINFO_RXFL_MASK_1023;
      if (frame_len <= RX_FRAME_MAX) dwt_readrxdata(rx_frame, frame_len, 0);
      dwt_readdiagnostics(&rx_diag);       // сырые метрики (см. §5)
      rx_metrics.valid = 1; rx_metrics.count++;
      /* кэшируем frame_len, rx_frame, rx_diag */
      dwt_rxenable(DWT_START_RX_IMMEDIATE); // непрерывный приём — снова слушаем
  } else if (status & (SYS_STATUS_ALL_RX_ERR)) {
      dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_ALL_RX_ERR);
      dwt_rxreset();                        // сброс LDE после ошибки
      dwt_rxenable(DWT_START_RX_IMMEDIATE);
  }
  /* RX_TO обрабатываем только если задан таймаут; для непрерывного приёма — нет */
```
`dwt_readrxdata/rxreset/rxenable/readdiagnostics` — только SPI, без HAL_Delay; но
держим их в main loop для единообразия и сериализации доступа к SPI. Активный
модуль перед вызовами — `deca_port_select_device(rx_dev)`.

## 3. Триггер приёма: EXTI vs polling (ВАЖНО — находка)

**EXTI сейчас НЕ доведён:** пины IRQ1/IRQ2 (PB8/PB9) стоят как
`GPIO_MODE_IT_RISING` (main.c), но в `stm32f4xx_it.c` **нет** `EXTI9_5_IRQHandler`,
NVIC-линия EXTI9_5 не включена, `HAL_GPIO_EXTI_Callback` не реализован → прерывание
никуда не приходит. Включение EXTI9_5 (NVIC) — территория **CubeMX** (генерирует
обработчик в it.c).

**Рекомендация (малые шаги):**
- **Шаг A (сейчас):** триггер = **опрос SYS_STATUS в main loop** (как в референсе,
  без зависимости от CubeMX). Достаточно, чтобы поймать сигнал EVK и доказать
  приём. Обработка RXFCG — уже в main loop (§2).
- **Шаг B (позже):** довести EXTI в CubeMX (включить EXTI9_5 в NVIC), добавить
  `HAL_GPIO_EXTI_Callback` (USER CODE) → ставит `volatile` флаг по модулю с учётом
  **перекрёстной нумерации** (EXTI8/PB8→M2, EXTI9/PB9→M1); `PollRadio` реагирует
  на флаг вместо постоянного опроса. Обработка та же — меняется только триггер.
- Если хочешь IRQ сразу — тогда сначала CubeMX-шаг (за тобой), потом код.

Для EXTI-пути также нужен `dwt_setinterrupt(DWT_INT_RFCG, 1)` (чтобы DW1000
дёргал линию IRQ на хороший кадр). Для polling-пути — не требуется.

## 4. Какой модуль слушает (вопрос #2)

RX_START/GET_SIGNAL_METRICS параметра target не имеют (§6). Нужен RX/Индикатор,
но соответствие M1/M2↔роль ещё не зафиксировано.

**Рекомендация:** слушать на ОДНОМ модуле, индекс вынести в `board_config.h`:
```
#define DW_RX_LISTEN_DEV  DW_DEV_M2   /* предположительно Индикатор; уточнить на железе */
```
Обоснование: единственный RX-путь проще всего; если EVK не слышно — поменять одну
строку на `DW_DEV_M1`. Слушать на ОБОИХ (два RX-стейта, две линии) — возможное
расширение позже; GET_SIGNAL_METRICS всё равно отдаёт один набор, так что для цели
«поймать EVK» одного модуля достаточно.

## 5. GET_SIGNAL_METRICS: сырые поля (формулы — за архитектором)

Читаем `dwt_readdiagnostics(&rx_diag)` — один вызов даёт все сырые поля
(`dwt_rxdiag_t`, из deca_device_api.h):

| Поле dwt_rxdiag_t | Смысл | Для формул UM §4.7 |
|---|---|---|
| `maxGrowthCIR` | CIR_PWR (макс. рост CIR) | RSSI (мощность приёма), «C» |
| `rxPreamCount` | RXPACC (симв. преамбулы) | RSSI/SNR, «N» |
| `stdNoise` | STD_NOISE (СКО шума) | SNR, шум |
| `firstPathAmp1` | FP_AMPL1 | First-path power / SNR |
| `firstPathAmp2` | FP_AMPL2 | First-path power |
| `firstPathAmp3` | FP_AMPL3 | First-path power |
| `firstPath` | FP_INDEX | индекс первого пути |
| `maxNoise` | LDE max noise | вспом. |

Формулы RSSI/SNR (dBm/dB) — **из DW1000 UM §4.7, предоставит архитектор**; не
выдумываю. **Интерим-ответ GET_SIGNAL_METRICS = сырые поля** (u16 каждое, LE),
чтобы проверить факт приёма и дать архитектору валидные входы. Итоговый формат
(RSSI i16 dBm×100, SNR i16 dB×100, RXPACC u16, FP_INDEX u16 — handoff §4) добавим
после формул. Это решение §9.2.

Если кадр ещё не принят (`rx_metrics.valid==0`) → предлагаю вернуть `TIMEOUT`
(или OK с флагом valid=0) — решение §9.4.

## 6. Кэш состояния приёма (наш код, App/)

```
static volatile uint8_t rx_active;      // приём включён (RX_START)
static struct {
    uint8_t     valid;                  // принят хотя бы один кадр
    uint32_t    count;                  // счётчик принятых кадров
    uint16_t    frame_len;
    dwt_rxdiag_t diag;                  // сырые метрики последнего кадра
} rx_metrics;
static uint8_t rx_frame[RX_FRAME_MAX];  // последний принятый кадр (для отладки)
```

## 7. Контекст, таймаут, сброс (вопрос #4)

- **Контекст:** RX_START быстрый (dwt_rxenable) — исполняется в main loop через
  PollRx, как прочие. Обработка RXFCG — в main loop (PollRadio, §2). Никакого
  HAL_Delay в RX-пути.
- **Таймаут:** для непрерывного приёма `dwt_setrxtimeout(0)` — без таймаута,
  слушаем бесконечно. RX_TO отдельно не обрабатываем (не задаём).
- **Сброс:** на RX_ERR → очистить `ALL_RX_ERR` + `dwt_rxreset()` (реинициализация
  LDE) + повторный `dwt_rxenable`. На переполнение (RXOVRR входит в ALL_RX_ERR) —
  тем же путём.
- **SPI:** держим медленным (как после INIT/config). Быстрый SPI для чтения кадра —
  опциональная оптимизация позже.

## 8. Эскизы обработчиков (псевдокод, не финал)

```c
// RX_START (0x30)
static ResponseStatus HandleRX_START(const uint8_t* p, uint8_t len,
                                     uint8_t** od, uint8_t* ol) {
    (void)p; (void)len; (void)od; *ol = 0;
    if (!dw_dev_state[DW_RX_LISTEN_DEV].initialized) return STATUS_RADIO_ERROR;
    if (deca_port_select_device(DW_RX_LISTEN_DEV) != DWT_SUCCESS) return STATUS_RADIO_ERROR;
    dwt_setrxtimeout(0);                       // непрерывный приём
    if (dwt_rxenable(DWT_START_RX_IMMEDIATE) != DWT_SUCCESS) return STATUS_RADIO_ERROR;
    rx_metrics.valid = 0;
    rx_active = 1;
    return STATUS_OK;
}

// RX_STOP (0x31) — заодно, тривиально: dwt_forcetrxoff(); rx_active=0; OK.

// GET_SIGNAL_METRICS (0x40)
static ResponseStatus HandleGET_SIGNAL_METRICS(const uint8_t* p, uint8_t len,
                                               uint8_t** od, uint8_t* ol) {
    (void)p; (void)len;
    if (!rx_metrics.valid) return STATUS_TIMEOUT;   // кадра ещё не было (§9.4)
    // упаковать сырые поля rx_metrics.diag в DATA (u16 LE) — интерим-формат (§5)
    // ... *od = buf; *ol = N;
    return STATUS_OK;
}
```
> `dwt_rxenable` возвращает int (DWT_SUCCESS/ERROR) — проверяем. RX_STOP включаю в
> объём как парный к RX_START (одна строка `dwt_forcetrxoff`), если не против.

## 9. Решения на согласование с архитектором

1. **Триггер:** Шаг A = **polling SYS_STATUS в main loop сейчас** (реком., EXTI не
   доведён), EXTI (Шаг B) — после CubeMX-настройки NVIC EXTI9_5. Или сразу EXTI
   (тогда CubeMX-шаг за тобой первым)?
2. **GET_SIGNAL_METRICS интерим = сырые поля** `dwt_rxdiag_t` (реком.), RSSI/SNR
   добавим по твоим формулам UM §4.7. Согласовать интерим-формат DATA.
3. **Слушающий модуль:** фикс `DW_RX_LISTEN_DEV = DW_DEV_M2` в board_config
   (реком., менять одной строкой) или слушать оба?
4. **Нет кадра:** GET_SIGNAL_METRICS → `TIMEOUT` (реком.) или OK+флаг valid=0?
5. **RX_STOP (0x31)** включить в этот заход как парную команду (реком.)?

## 10. Порядок работ (малые шаги)

1. RX_START + RX_STOP + GET_SIGNAL_METRICS (интерим сырой) + `PROTOCOL_PollRadio`
   + кэш; регистрация; `board_config` DW_RX_LISTEN_DEV.
2. `main.c` USER CODE 3: вызвать `PROTOCOL_PollRadio()` рядом с `PROTOCOL_PollRx()`.
3. syntax-check → твой линк-билд.
4. Хост: методы `rx_start/rx_stop/get_signal_metrics` в mks_protocol.py + тест.
5. Проверка на железе: EVK Mode 3 в эфир → RX_START → GET_SIGNAL_METRICS: valid=1,
   ненулевые CIR_PWR/RXPACC/FP_AMPL. Затем — RSSI/SNR по формулам (Шаг 2 отдельно).
6. Позже: EXTI (CubeMX NVIC) + переключение триггера, оптимизация SPI.
