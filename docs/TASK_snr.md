# TASK: SNR (total SNR) в метрики + правка A(PRF16) — GET_SIGNAL_METRICS (0x40)

**Дата:** 2026-07-17
**Автор:** Vagan Sarukhanov
**Исполнитель:** СС (Claude Code)

Добавить в приёмные метрики **total SNR** и попутно исправить константу A для PRF16
под первоисточник DecaWave. Формат ответа расширяется с 28 до 30 байт (новое поле
SNR дописывается в конец, первые 28 байт не трогаем).

**Формат задачи:** это СПЕЦИФИКАЦИЯ (формулы, значения, места вставки, раскладка).
Тела функций пишет СС по образцу существующих `metrics_*`-хелперов. Готовый код
здесь не приводится намеренно.

**Не трогать** vendor (`Drivers/decadriver/`). Все правки — в
`App/protocol/protocol.c`. `main.c`/`protocol.h` не трогать (если не потребуется
прототип — не должно, хелперы static).

## Первоисточник (DecaRanging `instance_log.c`, функции instancecalculatepower /
## instancegetaccumulatordata — предоставлены Вагану, сверено дословно)

- `alpha = (rxCode > 8) ? (-115.72 - 6.02) : (-115.72)` → PRF64: **−121.74**;
  PRF16: **−115.72**. (В нашем коде это `−A`; т.е. A(PRF64)=121.74 ✓,
  A(PRF16) должно стать **115.72** вместо текущего 113.77.)
- `RSL = 10·log10(maxGrowthCIR / N) + alpha + 51.175`, где `N = rxPreamCount²`.
  (Эквивалентно нашей форме `10·log10(C·2^17/N²) − A`, т.к. 10·log10(2^17)=51.175;
  наш RSL уже сверен с хостом Δ=0.00 — формулу RSL НЕ меняем, только A(PRF16).)
- `delta = 87 − 7.5 = 79.5`; если `chan == 4 || chan == 7`: `delta −= 2.5` → 77.0.
- `totalSNR = RSL + delta`  (ЗНАК ПЛЮС — подтверждено кодом; форумное «минус»
  ошибочно).

## Зафиксированные решения

- SNR считаем в прошивке на float: `SNR_dB = RSL_dBm + delta`, где RSL — уже
  вычисленный строгий RSL (тот же, что идёт в поле RSSI), delta по каналу.
- delta по каналу: 79.5 для каналов {1,2,3,5}; 77.0 для {4,7}. Канал — из
  `dw_dev_state[DW_RX_LISTEN_DEV].channel`.
- A(PRF16): исправить 113.77 → **115.72** (в `metrics_a_const`). A(PRF64)=121.74
  без изменений.
- Формат ответа: 28 → **30 байт**. Новое поле SNR — i16 (dB×100, знаковое),
  дописывается ПОСЛЕ существующего поля A_used×100. Первые 28 байт без изменений.
- Крайний случай: если RSL недоступен (RSSI-поле = INT16_MIN, т.е. N=0/C=0) →
  SNR тоже INT16_MIN («н/д»).

## Что сделать (спецификация)

1. **Правка `metrics_a_const`:** значение для PRF16 сменить с `113.77f` на
   `115.72f`. Комментарий обновить: источник — DecaRanging `instance_log.c`
   (`alpha` PRF16 = −115.72). PRF64 (121.74f) не трогать.

2. **`#define` delta:** добавить рядом с прочими metrics-#define две константы
   (значения из DecaRanging): `SNR_DELTA_DEFAULT = 79.5f` (каналы 1,2,3,5) и
   `SNR_DELTA_CH47 = 77.0f` (каналы 4,7). Комментарий — ссылка на первоисточник
   (`delta = 87 − 7.5`, для ch4/7 `−2.5`).

3. **Хелпер `metrics_delta_for_channel(uint8_t channel)`** (static, float):
   вернуть `SNR_DELTA_CH47` для channel ∈ {4,7}, иначе `SNR_DELTA_DEFAULT`.
   Написать по образцу `metrics_a_const`.

4. **Хелпер `metrics_snr_q(...)`** (static, int16_t): вычислить total SNR в dB×100.
   Логика: если RSL недоступен (те же условия, что дают INT16_MIN в
   `metrics_rssi_q`: N==0 или maxGrowthCIR==0) → вернуть INT16_MIN. Иначе:
   `SNR_dB = RSL_dBm + delta`, вернуть `(int16_t)lrintf(SNR_dB * 100.0f)`.
   - RSL_dBm нужен как float (не квантованный). Варианты реализации на выбор СС
     (эквивалентны): (а) вынести расчёт RSL_dBm в отдельный static-хелпер
     `metrics_rssi_dbm(d, N, A)` (float), и вызвать его И из `metrics_rssi_q`, И
     из `metrics_snr_q` (убирает дублирование формулы); либо (б) пересчитать
     RSL_dBm внутри `metrics_snr_q` тем же выражением. Предпочтителен (а) —
     единая формула RSL в одном месте. Решение за СС, лишь бы формула RSL
     оставалась идентичной уже проверенной (Δ=0.00).
   - `delta` берётся из `metrics_delta_for_channel(dw_dev_state[DW_RX_LISTEN_DEV].channel)`.

5. **Расширить `HandleGET_SIGNAL_METRICS`:** буфер `data[28]` → `data[30]`,
   `*out_len = 28` → `30`. После существующего `PUT_U16LE(p, a_q); p += 2;`
   (поле A_used) дописать вычисление и запись SNR:
   - посчитать `int16_t snr_q = metrics_snr_q(d, N, A, dw_dev_state[DW_RX_LISTEN_DEV].channel);`
     (сигнатуру согласовать с тем, что СС заложит в хелпер — важно, чтобы delta
     бралась по каналу RX-устройства);
   - `PUT_U16LE(p, (uint16_t)snr_q); p += 2;` (i16 в u16-контейнер, LE).
   Итоговая раскладка (LE), смещения от начала DATA:
     0..17 — существующие 9×u16 (интерим);
     18..19 RXPACC_NOSAT; 20..21 N_corrected; 22..23 RSSI(i16);
     24..25 FP_POWER(i16); 26..27 A_used×100(u16); **28..29 SNR(i16 dB×100)** ← новое.

## Проверка и коммит

1. Syntax-check (`-fsyntax-only -Wall -Wextra`, EXIT 0, ноль предупреждений).
2. git commit + push. Сообщение:
   `feat(protocol): total SNR (RSL+delta, DecaRanging) in GET_SIGNAL_METRICS 28->30B; fix A(PRF16)=115.72`
3. Отчитаться: изменённые строки, новый размер ответа (30 байт), значение
   A(PRF16), результат syntax-check.

## Границы

- Формулу RSL НЕ менять (только A(PRF16)-константа). RSL уже сверен с хостом.
- Не трогать vendor, `main.c`, `protocol.h`, хост (`tools/` — обновит архитектор
  под 30 байт), первые 28 байт формата.
- Не собирать/деплоить — правки protocol.c + syntax-check + commit/push.
