# PLAN — минимизация окна выключенного RX в RXFCG-ветке (§15.1 дальность)

**Дата:** 2026-07-22. **Основание:** RECON_rx_tuning.md (`8d64f4d`) — причина
нестабильности: приёмник выключен на каждом кадре на время `CaptureCIR` (~250 Б SPI,
безусловно) + USB-стрима, `dwt_rxenable` последним → кадры в этом окне теряются на
любом уровне сигнала. EVK-киты на том же столе бьют 5–8 м. **Статус: PLAN на
утверждение ORDER, код НЕ трогаем до согласования.**

## Рамка архитектора
- Цель — минимизировать окно выключенного RX в `PROTOCOL_PollRadio` (ветка RXFCG). Две
  дешёвые правки, без структурных (двойной буфер/EXTI — отдельно, позже, если не хватит).
- **Правка 1:** `CaptureCIR` вызывать только когда CIR нужен (content==1), для
  content=2/3/4 и когда стрим выключен — пропускать.
- **Правка 2:** `dwt_rxenable` перенести вверх — после чтения кадра/диагностики в RAM,
  но ДО `SendStreamFrame` (USB). При content==1 — CaptureCIR до rxenable (CIR требует
  старого порядка); при content!=1 — rxenable сразу.
- Регистровый тюнинг RX и антенную задержку не трогаем.

## 1. Текущее тело RXFCG (`protocol.c:1311–1333`)
```c
if (status & SYS_STATUS_RXFCG) {
    dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_RXFCG);         // :1313 снять флаг
    frame_len = RX_FINFO & RXFL; rx_metrics.frame_len = ...;    // :1315-1316
    if (frame_len <= RX_FRAME_MAX) dwt_readrxdata(rx_frame,...);// :1317-1319  тело -> RAM
    dwt_readdiagnostics(&rx_metrics.diag);                      // :1321        диаг -> RAM
    rx_metrics.rxpacc_nosat = ...; rx_metrics.valid=1; count++; // :1323-1325   -> RAM
    PROTOCOL_CaptureCIR();                                      // :1327  ~250Б SPI, БЕЗУСЛОВНО
    if (stream_active) PROTOCOL_SendStreamFrame();             // :1330  USB (может блокировать)
    dwt_rxenable(DWT_START_RX_IMMEDIATE);                       // :1333  RX включается ПОСЛЕДНИМ
}
```
Окно выключенного RX = `CaptureCIR` + `SendStreamFrame` (для станции content=4 — оба лишние в этом окне).

## 2. Диагностика/метрики — читаются ДО ветвления, не теряются
`readrxdata`/`readdiagnostics`/`rxpacc_nosat` (`:1315–1325`) кладут кадр и метрики в
**наши RAM-буферы** (`rx_frame`, `rx_metrics`) ДО ветвления и ДО `dwt_rxenable`. Ранний
перевзвод RX их не затирает (следующий кадр перезапишет RAM только на следующем
`PollRadio`). Безопасно.

## 3. Предлагаемая структура (обе правки, единый `dwt_rxenable`)
`cir_snap` — это **КОПИЯ** окна (CaptureCIR копирует accumulator в `cir_snap.data`);
`SendStreamFrame` читает копию, а не живой accumulator. Значит `dwt_rxenable` можно
ставить сразу после (условного) `CaptureCIR`, **до** `SendStreamFrame` — даже при content=1:
```c
    /* Wagan/Andrey: 2026-07-22 — минимизировать окно выключенного RX. */
    if (need_cir) {
        PROTOCOL_CaptureCIR();                 /* снимок (копия) ДО rxenable */
    }
    dwt_rxenable(DWT_START_RX_IMMEDIATE);        /* RX включаем РАНЬШЕ тяжёлой работы */
    if (stream_active) {
        PROTOCOL_SendStreamFrame();             /* USB — приёмник уже слушает */
    }
```
- **content=1** (нужен CIR): `need_cir=1` → CaptureCIR(копия) → **rxenable** →
  SendStreamFrame(читает копию). Корректно (rxenable затирает *живой* accumulator, копия снята).
- **content=2/3/4** (станция): `need_cir=0` → **rxenable сразу** → SendStreamFrame.
  CaptureCIR исключён из горячего пути. Цель достигнута.

Это ровно две ветки из рамки, но с **одним** `dwt_rxenable` и условным `CaptureCIR` —
меньше дублирования.

## 4. `dwt_rxenable` — где встаёт
Единственный (для RXFCG-ветки), **после условного `CaptureCIR`, до `SendStreamFrame`**.
Ветку ошибок (`ALL_RX_ERR`, `:1338`) не трогаем.

## 5. Риски

**РИСК A (ключевой) — `GET_CIR` команда в командном режиме (стрим ВЫКЛ).** `cir_snap`
заполняется ТОЛЬКО в `PROTOCOL_CaptureCIR`, а он — ТОЛЬКО здесь. `HandleGET_CIR` отдаёт
последний `cir_snap` (иначе TIMEOUT). Если `need_cir` = «только content==1», то при
ВЫКЛЮЧЕННОМ стриме `CaptureCIR` не вызовется → **`GET_CIR` (консольная `cir`) сломается**
(всегда TIMEOUT). Буквальная формулировка «когда стрим выключен — пропускать» это и вызовет.
→ **Рекомендую `need_cir = (stream_content == 1) || (!stream_active)`:** снимать CIR при
потоке-с-CIR ИЛИ в командном режиме (для GET_CIR); пропускать ТОЛЬКО при активном потоке
content=2/3/4 — это ровно станция, где проблема. GET_CIR остаётся рабочим, горячий путь
станции чистый.
Альтернатива (буквальная): `need_cir = (stream_content==1)` — GET_CIR без потока
перестаёт работать; принять как «GET_CIR только со стримом content=1» либо позже добавить
on-demand захват в `HandleGET_CIR` (отдельная правка).

**РИСК B — content=1 CIR-путь.** Сохранён: CaptureCIR (копия) до rxenable,
SendStreamFrame читает `cir_snap`. `dwt_rxenable` между ними трогает *живой* accumulator
(регистр), не `cir_snap` (RAM). Регресса нет.

**РИСК C — ранний rxenable + одиночный буфер.** Новый кадр во время `SendStreamFrame`
буферизуется железом (перезапишет HW RX-буфер), но наши RAM-копии (`rx_frame`/
`rx_metrics`) уже сняты → текущий стрим-кадр корректен; новый разберётся на следующем
`PollRadio`. Порчи нет.

**Не трогаем:** `HandleGET_CIR` (путь команды), `ALL_RX_ERR`-ветку, регистровый тюнинг
RX, антенную задержку.

## 6. Авторство (по указанию архитектора)
- Ошибку настройки **TX** решал **Dima** — пометить у TX-калибровки/дефолта.
- Избыточное чтение **CIR** нашли и тестировали **Wagan и Andrey** — пометить
  `Wagan/Andrey: 2026-07-22` у правки RXFCG.

## На утверждение (ORDER)
- **(A)** Условие `need_cir`: **`stream_content==1 || !stream_active`** (рекомендую —
  сохраняет GET_CIR в командном режиме) — vs буквальное **`stream_content==1`** (ломает
  GET_CIR без потока)?
- **(B)** Структура: единый `dwt_rxenable` + условный `CaptureCIR` (§3) — ок, или явные
  две ветки content==1/!=1 с отдельными `rxenable`?

Приёмка после реализации — замер дальности на железе (ожидание: скачок к EVK-подобной 5–8 м).
