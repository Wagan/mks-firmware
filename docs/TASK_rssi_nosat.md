# TASK: строгий RSSI/FP_POWER в прошивке через RXPACC_NOSAT (GET_SIGNAL_METRICS 0x40)

**Дата:** 2026-07-17
**Автор:** Vagan Sarukhanov
**Исполнитель:** СС (Claude Code)

Довести приёмную диагностику из ИНТЕРИМ (сырые поля) в СТРОГИЙ RSSI/FP_POWER
**в прошивке**: добавить чтение `RXPACC_NOSAT`, корректный расчёт N, вычисление
`RX_LEVEL` и `FP_POWER` (dBm) по формулам UM §4.7 на `float`, и **расширить**
ответ `GET_SIGNAL_METRICS` новыми полями поверх текущих 18 байт (совместимость на
переход — старые 18 байт не трогаем, дописываем в конец). SNR в этот заход НЕ
входит.

**Не трогать** vendor (`Drivers/decadriver/`). Все правки — в
`App/protocol/protocol.c`. `protocol.h` — только если понадобится (не должно).
`main.c` не трогать.

## Зафиксированные решения (архитектор)

- dBm считаем **в прошивке** на `float` (`log10f`), результат — `int16 dBm×100`.
- Формат ответа: **расширяем поверх интерим-18-байт** (не ломаем; дописываем поля
  в конец, хост-парсер выбирает ветку по длине DATA).
- `RXPACC_NOSAT` — регистр `DRX_CONF` (0x27), sub-offset `0x2C`, 2 байта, RO
  (UM §7.2.40.12). Читаем `dwt_read16bitoffsetreg(DRX_CONF_ID, RXPACC_NOSAT_OFFSET)`.
- Правило коррекции N (UM стр. 96): **если `RXPACC == RXPACC_NOSAT` → вычесть SFD-
  коррекцию; иначе N = RXPACC** (коррекция не нужна).
- SFD-коррекция для нашего Mode 3 (110 kbps, nsSFD=1 → DecaWave-defined 64-symbol
  SFD) = **−82** (UM Table 18). Для наших замеров ветка не срабатывает (RXPACC≠
  NOSAT), значение защитное, но заложено документально.

## Опора (проверено по коду/UM, НЕ по памяти)

- `dwt_read16bitoffsetreg(int regFileID, int regOffset)` — api.h:1660.
- `DRX_CONF_ID = 0x27` — regs.h:852. Оффсет `0x2C` — из UM (в regs.h не определён).
- `dwt_rxdiag_t`: `maxGrowthCIR`(C), `rxPreamCount`(RXPACC), `firstPathAmp1..3`,
  `firstPath`, `stdNoise`, `maxNoise`. Заполняется `dwt_readdiagnostics` в
  PollRadio (protocol.c:847).
- `HandleGET_SIGNAL_METRICS` (protocol.c:636): сейчас 18 байт, 9×u16 LE через
  `PUT_U16LE`. Порядок: count, maxGrowthCIR, rxPreamCount, stdNoise, firstPathAmp1,
  firstPathAmp2, firstPathAmp3, firstPath, maxNoise.
- `PUT_U16LE`/`GET_U16LE` — protocol.c:44–45.
- Формулы (проверены на хосте, `estimate_power`):
  `RX_LEVEL = 10·log10(C·2^17 / N²) − A`; `FP_POWER = 10·log10((F1²+F2²+F3²)/N²) − A`;
  `A = 121.74` (PRF64) / `113.77` (PRF16); `2^17 = 131072`.
- PRF источник: `dw_dev_state[DW_RX_LISTEN_DEV].prf` (raw wire 16/64).

---

## Шаг 1. `#define` (рядом с другими протокольными #define в protocol.c)

```c
/* RXPACC_NOSAT: DRX_CONF (0x27), sub-offset 0x2C, 2 байта RO (UM §7.2.40.12).
 * Не-насыщаемый счётчик символов преамбулы — для проверки, нужна ли SFD-коррекция
 * RXPACC (UM стр. 96). В vendor deca_regs.h не определён; DRX_CONF_ID=0x27 есть. */
#define RXPACC_NOSAT_OFFSET   0x2C

/* SFD-коррекция RXPACC для нашего Mode 3 (110 kbps, nsSFD=1 → DecaWave-defined
 * 64-symbol SFD): −82 (UM Table 18, стр. 97). Применяется ТОЛЬКО когда
 * RXPACC == RXPACC_NOSAT (UM стр. 96); для наших замеров (RXPACC≠NOSAT) ветка не
 * срабатывает — значение защитное, но заложено документально. */
#define SFD_CORRECTION_MODE3  82
```

## Шаг 2. Кэш `rx_metrics` — новое поле

В структуру `rx_metrics` (там же, где `diag`, `valid`, `count`, `frame_len`)
добавить:
```c
    uint16_t rxpacc_nosat;   /* RXPACC_NOSAT последнего кадра (DRX_CONF 0x2C) */
```
Инициализацию (рядом с `rx_metrics.valid = 0; rx_metrics.count = 0;`) — добавить:
```c
    rx_metrics.rxpacc_nosat = 0;
```

## Шаг 3. Чтение RXPACC_NOSAT в PollRadio

В ветке `SYS_STATUS_RXFCG`, сразу после `dwt_readdiagnostics(&rx_metrics.diag);`
(protocol.c:847), добавить:
```c
        rx_metrics.rxpacc_nosat = dwt_read16bitoffsetreg(DRX_CONF_ID, RXPACC_NOSAT_OFFSET);
```
Активное устройство уже выбрано выше в PollRadio (RX идёт на `DW_RX_LISTEN_DEV`) —
доп. `deca_port_select_device` не нужен. (Проверить при реализации: select стоит
до этого места в PollRadio; если нет — добавить перед чтением.)

## Шаг 4. Хелперы расчёта (静, рядом с прочими static-функциями)

```c
#include <math.h>   /* log10f, lrintf — вверху файла к остальным include */

/* A-константа по PRF (raw wire), UM §4.7. */
static float metrics_a_const(uint8_t prf_wire)
{
    return (prf_wire == 64) ? 121.74f : 113.77f;
}

/* N (число символов преамбулы) с SFD-коррекцией по UM стр. 96. */
static uint16_t metrics_corrected_n(const dwt_rxdiag_t* d, uint16_t nosat)
{
    uint16_t rxpacc = d->rxPreamCount;
    if (rxpacc == nosat) {                       /* коррекция нужна */
        return (rxpacc > SFD_CORRECTION_MODE3)
             ? (uint16_t)(rxpacc - SFD_CORRECTION_MODE3)
             : rxpacc;                           /* защита от ухода в 0/underflow */
    }
    return rxpacc;                               /* коррекция не нужна */
}

/* RSSI (RX_LEVEL) в dBm×100. Крайние случаи (N=0 или C=0) → INT16_MIN («н/д»). */
static int16_t metrics_rssi_q(const dwt_rxdiag_t* d, uint16_t N, float A)
{
    if (N == 0 || d->maxGrowthCIR == 0) return INT16_MIN;
    float n2 = (float)N * (float)N;
    float v  = 10.0f * log10f(((float)d->maxGrowthCIR * 131072.0f) / n2) - A;
    return (int16_t)lrintf(v * 100.0f);
}

/* FP_POWER в dBm×100. Крайние случаи (N=0 или сумма амплитуд=0) → INT16_MIN. */
static int16_t metrics_fp_q(const dwt_rxdiag_t* d, uint16_t N, float A)
{
    if (N == 0) return INT16_MIN;
    float f1=(float)d->firstPathAmp1, f2=(float)d->firstPathAmp2, f3=(float)d->firstPathAmp3;
    float fp_sum = f1*f1 + f2*f2 + f3*f3;
    if (fp_sum == 0.0f) return INT16_MIN;
    float n2 = (float)N * (float)N;
    float v  = 10.0f * log10f(fp_sum / n2) - A;
    return (int16_t)lrintf(v * 100.0f);
}
```
> `INT16_MIN` — из `<stdint.h>` (уже используется в проекте через uintXX_t; при
> необходимости добавить `#include <stdint.h>`). `lrintf`/`log10f` — из `<math.h>`.

## Шаг 5. Расширить `HandleGET_SIGNAL_METRICS` (совместимость поверх 18 байт)

Оставить текущие 18 байт БЕЗ изменений, дописать в конец новые поля. Итоговый
размер — 18 + 10 = **28 байт**. Новые поля (после maxNoise), все LE:

| Смещение | Поле | Тип | Источник |
|---|---|---|---|
| 18..19 | RXPACC_NOSAT | u16 | `rx_metrics.rxpacc_nosat` |
| 20..21 | N_corrected | u16 | `metrics_corrected_n(...)` |
| 22..23 | RSSI | i16 dBm×100 | `metrics_rssi_q(...)` |
| 24..25 | FP_POWER | i16 dBm×100 | `metrics_fp_q(...)` |
| 26..27 | A_used×100 | u16 | (для отладки: A·100, напр. 12174) |

Код (после существующего блока `PUT_U16LE(p, d->maxNoise); p += 2;`, увеличить
буфер `data[18]` → `data[28]` и `*out_len = 18` → `28`):
```c
    uint8_t  prf_wire = dw_dev_state[DW_RX_LISTEN_DEV].prf;
    float    A        = metrics_a_const(prf_wire);
    uint16_t N        = metrics_corrected_n(d, rx_metrics.rxpacc_nosat);
    int16_t  rssi_q   = metrics_rssi_q(d, N, A);
    int16_t  fp_q     = metrics_fp_q(d, N, A);
    uint16_t a_q      = (uint16_t)lrintf(A * 100.0f);

    PUT_U16LE(p, rx_metrics.rxpacc_nosat); p += 2;
    PUT_U16LE(p, N);                       p += 2;
    PUT_U16LE(p, (uint16_t)rssi_q);        p += 2;   /* i16 в u16-контейнер, LE */
    PUT_U16LE(p, (uint16_t)fp_q);          p += 2;
    PUT_U16LE(p, a_q);                     p += 2;
```
Не забыть: буфер `static uint8_t data[28];` и `*out_len = 28;`.

## Шаг 6. Проверка и коммит

1. Syntax-check (`-fsyntax-only -Wall -Wextra`, ждём EXIT 0, ноль предупреждений).
   Убедиться: `log10f`/`lrintf`/`INT16_MIN` резолвятся; при линковке проекта нужен
   `-lm` (это на этапе линк-билда владельца — но syntax-check должен видеть
   прототипы из `<math.h>`). Если `<math.h>`/`<stdint.h>` не включены — добавить.
2. git commit + push. Сообщение:
   `feat(protocol): strict RSSI/FP_POWER via RXPACC_NOSAT (UM 4.7); extend GET_SIGNAL_METRICS to 28 bytes`
3. Отчитаться: изменённые строки, размер ответа (28 байт), результат syntax-check.

## Границы задачи (что НЕ делать)

- Не трогать vendor (`Drivers/decadriver/`), RX/TX-обработчики (кроме добавления
  чтения NOSAT в PollRadio и расширения GET_SIGNAL_METRICS), `main.c`.
- Не считать SNR — отдельный заход.
- Не ломать первые 18 байт формата (дописываем в конец).
- Не менять хост-скрипты (`tools/`) — их обновляет архитектор.
- Не собирать/деплоить — только правки protocol.c + syntax-check + commit/push.
```
