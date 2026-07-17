# План: передающий тракт — TX_FRAME (0x20) + TX_STOP (0x22)

**Дата:** 2026-07-17
**Автор:** Vagan Sarukhanov
**Цель:** послать одиночный кадр в эфир с модуля-источника и принять его нашим же
приёмным трактом (loopback M1→M2). RX уже доверенный (проверен против EVK), значит
любой сбой loopback локализуется в TX-пути.
**Статус:** план на согласование. Правок нет. Vendor не трогаем.
**Объём этого захода (согласовано):** только `TX_FRAME (0x20)` + `TX_STOP (0x22)`,
одиночный кадр. `TX_PERIODIC (0x21)` — следующим заходом, после подтверждения
loopback.
**Референс:** `docs/reference/decawave-examples/ss_init_main.c` (официальный
TX-паттерn DecaWave) — парный к `ss_resp_main.c`, на который опирался RX.
**Опора на API (проверено в deca_device_api.h):**
`int dwt_writetxdata(uint16 len, uint8* bytes, uint16 offset)`,
`void dwt_writetxfctrl(uint16 len, uint16 offset, int ranging)`,
`int dwt_starttx(uint8 mode)` (`DWT_START_TX_IMMEDIATE = 0`),
`void dwt_forcetrxoff(void)`, `dwt_read32bitreg/write32bitreg(SYS_STATUS_ID)`.

---

## 1. Референс-паттерн (ss_init_main.c) и как ложится на нас

Пример (передача poll-кадра, строки 92–100):
```
dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_TXFRS);  // очистить флаг завершения TX
dwt_writetxdata(sizeof(msg), msg, 0);                // залить кадр в TX-буфер (offset 0)
dwt_writetxfctrl(sizeof(msg), 0, 1);                 // TX frame control (ranging=1 у примера)
dwt_starttx(DWT_START_TX_IMMEDIATE | DWT_RESPONSE_EXPECTED);
while (!(SYS_STATUS & (RXFCG|RX_TO|RX_ERR))) {}      // пример ЖДЁТ ответ (ranging)
```

**Отличия у нас (важно):**
- **Без `DWT_RESPONSE_EXPECTED`** — это ranging-флаг (автозапуск RX после TX для
  приёма ответа). Нам нужно просто послать кадр; приём идёт независимо на M2.
  Значит `dwt_starttx(DWT_START_TX_IMMEDIATE)` (mode = 0, без доп. флагов).
- **`ranging` в writetxfctrl = 0** — мы не в ranging-обмене, обычный data-кадр.
  (Примечание §5 плана: ranging-бит влияет на служебное поле кадра, не на факт
  передачи; для loopback ставим 0.)
- **Ждём `SYS_STATUS_TXFRS`, а не RXFCG.** Признак «кадр ушёл в эфир» —
  бит TXFRS (TX Frame Sent). Приём ловит уже M2 (наш PollRadio).
- **Где ждём TXFRS** — см. §3 (решение: в обработчике коротким busy-wait или в
  main loop). TX быстрый (SPI + эфирное время кадра ~доли мс), HAL_Delay не нужен.

## 2. Кто передаёт, кто принимает (loopback M1→M2)

- **RX = M2** (`DW_RX_LISTEN_DEV = DW_DEV_M2`, board_config) — уже слушает по
  RX_START, обслуживается в PollRadio.
- **TX = M1** (`DW_DEV_M1`, предполож. Источник по board_config).
- **Раздельные SPI**: M1→SPI2, M2→SPI3 (board_config). Пересечения шин нет —
  TX на M1 не мешает SPI-доступу к M2. Активный модуль перед TX-вызовами —
  `deca_port_select_device(DW_TX_SOURCE_DEV)`.
- **Индекс TX-модуля выносим в board_config** (по образцу DW_RX_LISTEN_DEV):
  ```
  #define DW_TX_SOURCE_DEV  DW_DEV_M1   /* источник для TX_FRAME; loopback -> M2 слушает */
  ```
  Если loopback не пойдёт с M1→M2 — поменять одну строку (напр. M2→M1). Это
  решение §9.3.

## 3. Последовательность работы loopback (на хосте)

Порядок команд для проверки (ключевой момент — M2 должен УЖЕ слушать к моменту TX):
```
init            # оба модуля подняты
mode3           # одинаковый PHY на ОБОИХ (иначе M2 не расслышит M1) — см. §6
rxstart         # M2 начинает непрерывный приём
txframe ...     # M1 передаёт один кадр -> уходит в эфир (TXFRS)
metrics         # M2: принял ли? count++ и ненулевые метрики = loopback есть
```

**Требование одновременности:** TX на M1 и активный RX на M2 сосуществуют. Это
безопасно: разные SPI-шины, разные DW1000. Единственная тонкость —
`deca_port_select_device` переключает «активный» модуль для драйвера; TX-обработчик
выбирает M1, делает передачу, а PollRadio на следующем проходе снова выберет M2.
Оба — в main loop, последовательно, гонки нет (не ISR). Это решение §9.1.

## 4. Контекст исполнения, буфер, ответ

- **Контекст:** TX_FRAME исполняется синхронно в main loop через диспетчер
  (`PROTOCOL_PollRx` → handler), как все прочие. TX быстрый — busy-wait по TXFRS
  короткий (эфирное время кадра десятки-сотни мкс), HAL_Delay в TX-пути нет.
- **Буфер кадра:** payload приходит в `params` обработчика (уже скопирован
  диспетчером в `cmd_pkt.params`). Формат params — см. §6.
- **Ответ TX_FRAME:** DATA нет; STATUS_OK если кадр ушёл (TXFRS поймали),
  иначе STATUS_RADIO_ERROR (starttx вернул ошибку или TXFRS не дождались).
  Это решение §9.4.

## 5. Формат кадра в эфир (что именно шлём)

`TX_FRAME` по SPEC §6: params = `length u16 (LE), payload[length]`.
- **length** — число байт полезной нагрузки, что кладём в TX-буфер.
- **payload** — сами байты кадра.
- **CRC кадра (2 байта) НЕ включаем в payload** — DW1000 добавляет FCS сам
  (референс NOTE 3: `dwt_writetxdata` берёт полную длину, но шлёт len; FCS
  дописывается автоматически). Значит в `dwt_writetxfctrl` длина = length + 2
  (payload + авто-FCS), как в примере (`sizeof(msg)` включает 2 нулевых хвоста).
  **Это тонкость — уточнить в §9.5:** передаём `length` или `length+2` в
  writetxdata/writetxfctrl. По референсу: writetxdata(full_len,...) где full_len
  включает 2 байта под FCS; writetxfctrl(full_len,...). Склоняюсь: наш `length`
  из params = длина payload БЕЗ FCS, а в драйвер передаём `length + 2`.
- **Минимальный тестовый payload:** для loopback содержимое кадра не важно —
  M2 в PollRadio принимает ЛЮБОЙ валидный кадр (не проверяет содержимое, см.
  RX-тракт: читает frame_len + диагностику, не сверяет байты). Значит для первой
  проверки достаточно послать несколько произвольных байт (напр. 5 байт
  `DE AD BE EF 01`) и смотреть, вырос ли count на M2.

## 6. PHY-согласование TX и RX (критично)

M2 расслышит M1, ТОЛЬКО если PHY-конфиг совпадает (канал, преамбула, код, PRF,
data rate) — тот же принцип, что был с EVK. `mode3` применяет SET_PHY_CONFIG на
ВСЕ модули (HandleSET_PHY_CONFIG: цикл `for i in DW_DEVICE_COUNT`), значит M1 и M2
после `mode3` уже согласованы. Отдельной настройки TX-канала не требуется.
> Проверить: SET_PHY_CONFIG действительно конфигурирует txCode/rxCode на обоих —
> по коду HandleSET_PHY_CONFIG применяется ко всем dw_dev_state[i]. OK.

## 7. Эскизы обработчиков (псевдокод, не финал)

```c
// TX_FRAME (0x20)
static ResponseStatus HandleTX_FRAME(const uint8_t* params, uint8_t params_len,
                                     uint8_t** out_data, uint8_t* out_len) {
    (void)out_data; *out_len = 0;

    if (params_len < 2) return STATUS_INVALID_PARAM;         // нет даже length
    uint16_t length = GET_U16LE(&params[0]);
    if (params_len < 2 + length) return STATUS_INVALID_PARAM; // payload короче заявл.
    if (length + 2 > TX_FRAME_MAX) return STATUS_BUFFER_OVERFLOW; // +FCS

    if (!dw_dev_state[DW_TX_SOURCE_DEV].initialized) return STATUS_RADIO_ERROR;
    if (deca_port_select_device(DW_TX_SOURCE_DEV) != DWT_SUCCESS) return STATUS_RADIO_ERROR;

    dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_TXFRS);       // очистить флаг
    if (dwt_writetxdata(length + 2, (uint8_t*)&params[2], 0) != DWT_SUCCESS)
        return STATUS_RADIO_ERROR;                            // (payload + место под FCS)
    dwt_writetxfctrl(length + 2, 0, 0);                       // ranging=0
    if (dwt_starttx(DWT_START_TX_IMMEDIATE) != DWT_SUCCESS)
        return STATUS_RADIO_ERROR;

    // ждать TXFRS с ограничением (защита от вечного цикла)
    uint32_t guard = TX_WAIT_GUARD;                          // напр. цикловый счётчик
    while (!(dwt_read32bitreg(SYS_STATUS_ID) & SYS_STATUS_TXFRS)) {
        if (--guard == 0) return STATUS_TIMEOUT;             // кадр не ушёл
    }
    dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_TXFRS);      // снять флаг
    return STATUS_OK;
}

// TX_STOP (0x22) — тривиально, парный к TX_FRAME
static ResponseStatus HandleTX_STOP(const uint8_t* params, uint8_t params_len,
                                    uint8_t** out_data, uint8_t* out_len) {
    (void)params; (void)params_len; (void)out_data; *out_len = 0;
    if (deca_port_select_device(DW_TX_SOURCE_DEV) != DWT_SUCCESS) return STATUS_RADIO_ERROR;
    dwt_forcetrxoff();
    return STATUS_OK;
}
```
> `GET_U16LE` уже используется в protocol.c (HandleSET_PHY_CONFIG) — тот же макрос.
> `TX_FRAME_MAX` / `TX_WAIT_GUARD` — новые #define в protocol.c (значения на
> согласование, §9.6).

## 8. Регистрация

В `PROTOCOL_RegisterAllHandlers()` добавить две строки (по образцу RX):
```
PROTOCOL_RegisterHandler(CMD_TX_FRAME, HandleTX_FRAME);
PROTOCOL_RegisterHandler(CMD_TX_STOP,  HandleTX_STOP);
```
Константы `CMD_TX_FRAME=0x20`, `CMD_TX_STOP=0x22` уже есть в protocol.h (enum) —
определять не нужно. `main.c` не трогаем (диспетчер уже вызывает handler'ы;
TX не требует нового вызова в while(1), в отличие от PollRadio для RX).

## 9. Решения на согласование с архитектором

1. **Ожидание TXFRS** — короткий busy-wait в обработчике с guard-счётчиком
   (реком., TX быстрый) или вынести в main loop как async? Реком.: busy-wait,
   проще и детерминированнее для одиночного кадра.
2. **ranging-бит в writetxfctrl** = 0 (обычный data-кадр, реком.) — подтвердить.
3. **DW_TX_SOURCE_DEV = DW_DEV_M1** в board_config (реком., менять одной строкой).
4. **Ответ TX_FRAME:** STATUS_OK при TXFRS, RADIO_ERROR/TIMEOUT иначе; DATA нет
   (реком.). Или возвращать что-то в DATA (напр. TX timestamp)?
5. **Длина в драйвер:** `length` (payload) из params, в writetxdata/fctrl
   передаём `length + 2` под авто-FCS (реком. по референсу NOTE 3). Подтвердить.
6. **TX_FRAME_MAX** (лимит payload) и **TX_WAIT_GUARD** (потолок busy-wait) —
   значения. Реком.: TX_FRAME_MAX = 125 (127 макс. кадр − 2 FCS), TX_WAIT_GUARD —
   эмпирически (напр. 100000 итераций; кадр Mode3 ~сотни мкс).

## 10. Порядок работ (малые шаги)

1. `board_config.h`: `DW_TX_SOURCE_DEV = DW_DEV_M1`. (СС)
2. `protocol.c`: `HandleTX_FRAME` + `HandleTX_STOP` + #define TX_FRAME_MAX/
   TX_WAIT_GUARD; регистрация обеих в RegisterAllHandlers. (СС)
3. syntax-check → твой линк-билд.
4. Хост (я): в `mks_protocol.py` — `tx_frame(payload)`/`tx_stop()`; в
   `mks_console.py` — команды `txframe <hex...>` / `txstop`.
5. Проверка на железе (loopback M1→M2): антенны разнесены, обе горизонтально
   параллельно (поляризация согласована). Порядок: init → mode3 → rxstart →
   txframe DE AD BE EF 01 → metrics. Успех: count на M2 вырос после txframe.
6. Позже: TX_PERIODIC (0x21), RSSI строгий (RXPACC_NOSAT), EXTI.
