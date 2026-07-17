# TASK_cir_step1 — GET_CIR (0x41): консольное усечённое чтение окна вокруг FP

**Проект:** МКС / прошивка STM32F411 + 2× DWM1000 (плата МКС)
**Автор задания:** Vagan Sarukhanov
**Исполнитель:** Claude Code (CC)
**Дата:** 2026-07-17
**Тип:** реализация (прошивка + хост) + правка PROTOCOL_SPEC.md
**Основание:** NOTES_cir_plan.md §7 шаг CIR-1; PROJECT_HANDOFF.md §4 (0x41), §11 п.13
**Развязанный риск:** accumulator затирается при перевзводе приёма (NOTES §5) —
чтение CIR встроить в ветку RXFCG ДО `dwt_rxenable`.

---

## 0. Роли и границы (ОБЯЗАТЕЛЬНО прочитать перед началом)

- CC: правит файлы репозитория (прошивка + хост), делает `git commit` + `git push`.
  CC **НЕ** деплоит, **НЕ** прошивает плату, **НЕ** запускает железо.
- Ваган: сам собирает/прошивает/проверяет на железе по процедуре из §7.
- **Правило no-guessing:** если по ходу всплывает значение/шаг, которого нет в этом
  TASK или в коде репозитория — **СТОП, спросить Вагана**, не додумывать. Не менять
  порядок полей уже существующих ответов, не трогать работающие RSSI/FP_POWER/SNR.
- Шапки файлов — по CODE_STYLE.md (только `App/**`, версия НЕ в шапку). Драйвер
  `Drivers/decadriver/` **не трогать**.
- **radio_defs.h — НЕ источник истины.** Это остаток старой самодельной библиотеки
  (`#include "dw1000_driver.h"`, коды команд другие: PING=0x01). Его `CIR_Config_t` /
  `RadioCommand_CIR_t` / коды `RADIO_CMD_CIR_*` **игнорировать**. Боевой enum — в
  `App/protocol/protocol.h` (`CMD_GET_CIR = 0x41`).

---

## 1. Цель шага (минимальный рабочий срез)

Получить на экране хоста **форму CIR (окно отсчётов вокруг first path)** для быстрой
диагностики. Полный CIR/водопад/потоковый режим — НЕ здесь (шаги CIR-2/3).

Что делаем:
1. Прошивка: снимок окна CIR вокруг FP в ветке приёма (RXFCG), до перевзвода RX.
2. Прошивка: обработчик `GET_CIR (0x41)` — отдаёт окно из снимка.
3. Хост: команда `cir` в консоли — печатает окно + ASCII-псевдографику амплитуды.
4. Обновить `docs/PROTOCOL_SPEC.md` под фактическую реализацию.

**Осознанные проектные решения (приняты архитектором, реализовать как есть):**
- **Центрирование — в прошивке по FP_INDEX.** Клиент шлёт только полуширину окна;
  прошивка сама берёт индекс first path и вырезает `[FP-half .. FP+half]`. Это
  отход от старой записи в SPEC («offset u16, length u16») — SPEC привести к коду.
- **Снимок при приёме, отдача по запросу.** CIR читается из accumulator ОДИН раз в
  ветке RXFCG (до `rxenable`) в статический буфер-снимок; `GET_CIR` отдаёт из
  снимка. Это развязывает грабль затирания и не держит приёмник во время USB-ответа.
- **Лимит ответа 255 байт** (LEN — 1 байт, см. protocol.c парсер). Окно =
  `(2*half+1)` отсчётов × 4 байта. При `half=31` → 63 отсчёта × 4 = 252 байта (влезает
  с запасом под возможный заголовок). `HALF_MAX = 31`. Дефолт `half = 16`.

---

## 2. Факты из первоисточников (используй дословно, НЕ по памяти)

Формат/сигнатуры сверены по vendor DecaDriver v4.0.6 и коду DecaRanging — привожу,
чтобы не искать заново:

- **Регистр accumulator:** `ACC_MEM_ID = 0x25`, `ACC_MEM_LEN = 4064` (deca_regs.h).
- **Отсчёт CIR:** комплексный, `int16 real` затем `int16 imag`, little-endian —
  `[b0 b1]` = real, `[b2 b3]` = imag. **4 байта на отсчёт.** (DecaRanging
  `instance.h` `complex16{int16 real; int16 imag}`, распаковка `instance_log.c`.)
- **Число отсчётов:** 992 (PRF16) / **1016 (PRF64)**. Наш Mode 3 = PRF64 → 1016.
- **Чтение:** `void dwt_readaccdata(uint8 *buffer, uint16 length, uint16 accOffset)`.
  `length` — в БАЙТАХ. **Первый байт — dummy** (внутренняя задержка памяти),
  отбросить. `accOffset` — в БАЙТАХ от начала accumulator.
  Эталон вызова (DecaRanging instance.c, идентично в STM32-версии EVK):
  ```c
  len = accumLength;                 // 992 или 1016 (полный)
  len = len*4 + 1;                   // +1 dummy
  dwt_readaccdata(&buf.dummy, len, 0);
  ```
  Для ОКНА: читаем не весь буфер, а срез — `accOffset = start_index*4`,
  `length = count*4 + 1` (тот же +1 dummy в начале среза).
- **First path index:** `dwt_rxdiag_t.firstPath` — тип **uint16**, сырой регистр
  FP_INDEX, формат **10.6 fixed-point** (6 дробных бит; `deca_device_api.h:230`,
  `deca_device.c:775` — `dwt_read16bitoffsetreg(RX_TIME_ID, RX_TIME_FP_INDEX_OFFSET)`).
  Целочисленный индекс отсчёта = `firstPath >> 6` (÷64). (Прежняя формулировка про
  `double`/`raw*(1/64)` относилась к PC-DecaRanging — у него отдельная структура, к
  нашему драйверу неприменима.) Уже читается в PollRadio через `dwt_readdiagnostics`
  и лежит в `rx_metrics.diag.firstPath`.
- **Длина принятого кадра / маска:** `RX_FINFO_RXFL_MASK_1023 = 0x3FF` (уже
  используется в PollRadio).
- **Доступ к устройству:** `deca_port_select_device(DW_RX_LISTEN_DEV)` (как в
  PollRadio/Handle*). Состояние: `dw_dev_state[DW_RX_LISTEN_DEV].prf/channel/initialized`.

---

## 3. Прошивка — файл App/protocol/protocol.c

### 3.1. Снимок окна CIR (новый статический буфер + захват в RXFCG)

Рядом с кэшем приёма (там, где `rx_metrics`, `rx_frame`), добавить снимок CIR:

```c
/* Снимок окна CIR вокруг first path. Захватывается в ветке RXFCG ДО rxenable
 * (accumulator валиден только пока приёмник не перевзведён — NOTES §5).
 * Хранит сырые байты среза БЕЗ dummy: cir_snap_count отсчётов × 4 байта.
 * Порядок на проводе — как в accumulator: I(int16 LE), Q(int16 LE) на отсчёт. */
#define CIR_HALF_MAX   31u                      /* макс. полуширина окна */
#define CIR_SNAP_MAXCNT (2u*CIR_HALF_MAX + 1u)  /* 63 отсчёта */
#define CIR_SNAP_BYTES  (CIR_SNAP_MAXCNT * 4u)  /* 252 байта данных окна */

static struct {
    uint8_t  valid;                 /* снимок сделан после последнего RX_START */
    uint16_t fp_index;              /* индекс first path (floor(firstPath)) */
    uint16_t start_index;           /* индекс первого отсчёта окна в accumulator */
    uint16_t count;                 /* число отсчётов в снимке */
    uint8_t  data[CIR_SNAP_BYTES];  /* I/Q без dummy: count*4 байт */
} cir_snap;
```

В `PROTOCOL_Init()` добавить: `cir_snap.valid = 0;`

**Захват в PollRadio, ветка RXFCG.** Сейчас там (protocol.c ~строки 939–955):
```c
if (status & SYS_STATUS_RXFCG) {
    dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_RXFCG);
    ... frame_len ...
    dwt_readdiagnostics(&rx_metrics.diag);
    rx_metrics.rxpacc_nosat = dwt_read16bitoffsetreg(DRX_CONF_ID, RXPACC_NOSAT_OFFSET);
    rx_metrics.valid = 1;
    rx_metrics.count++;
    dwt_rxenable(DWT_START_RX_IMMEDIATE);   /* <-- перевзвод: ПОСЛЕ него acc затёрт */
}
```

Вставить вызов захвата CIR **между** чтением диагностики и `dwt_rxenable`
(диагностика нужна: из неё берём `firstPath`). Т.е.:
```c
    rx_metrics.valid = 1;
    rx_metrics.count++;

    PROTOCOL_CaptureCIR();                  /* NEW: снять окно вокруг FP ДО rxenable */

    dwt_rxenable(DWT_START_RX_IMMEDIATE);
```

Функция захвата (добавить рядом, static; вызывать только из RXFCG, устройство уже
выбрано `deca_port_select_device(DW_RX_LISTEN_DEV)` выше в PollRadio):
```c
/* Снять окно CIR [FP-half .. FP+half] в cir_snap. Активное DW-устройство уже
 * выбрано вызывающим (PollRadio). Полуширина фиксирована CIR_HALF_MAX (окно
 * максимально; GET_CIR при отдаче обрежет до запрошенной клиентом полуширины).
 * Клампится к границам accumulator [0 .. acc_len-1]. */
static void PROTOCOL_CaptureCIR(void)
{
    cir_snap.valid = 0;

    /* Длина accumulator по PRF принимающего устройства. */
    uint16_t acc_len = (dw_dev_state[DW_RX_LISTEN_DEV].prf == 64) ? 1016u : 992u;

    /* Индекс first path: firstPath — 10.6 fixed-point, целый индекс = >>6 (÷64). */
    uint16_t fp = (uint16_t)(rx_metrics.diag.firstPath >> 6);   /* 10.6 fixed-point → индекс отсчёта (÷64) */
    if (fp >= acc_len) return;              /* защита: FP вне буфера — снимка нет */

    /* Окно [fp-HALF .. fp+HALF], кламп к [0 .. acc_len-1]. */
    uint16_t half  = CIR_HALF_MAX;
    uint16_t start = (fp > half) ? (uint16_t)(fp - half) : 0u;
    uint16_t end   = fp + half;
    if (end >= acc_len) end = (uint16_t)(acc_len - 1u);
    uint16_t count = (uint16_t)(end - start + 1u);
    if (count > CIR_SNAP_MAXCNT) count = CIR_SNAP_MAXCNT;   /* страховка */

    /* Чтение среза: dummy(1) + count*4 байт. Читаем во временный буфер с dummy,
     * затем копируем в cir_snap.data БЕЗ первого байта. */
    static uint8_t tmp[1 + CIR_SNAP_BYTES];  /* dummy + данные окна */
    uint16_t rd_len = (uint16_t)(count * 4u + 1u);
    dwt_readaccdata(tmp, rd_len, (uint16_t)(start * 4u));

    memcpy(cir_snap.data, &tmp[1], (size_t)(count * 4u));   /* отбросить dummy */

    cir_snap.fp_index    = fp;
    cir_snap.start_index = start;
    cir_snap.count       = count;
    cir_snap.valid       = 1;
}
```

> Примечание CC: `dwt_readaccdata` уже объявлена в `deca_device_api.h` (включён в
> protocol.c). `DRX_CONF_ID` / `RXPACC_NOSAT_OFFSET` уже используются рядом.

### 3.2. Обработчик GET_CIR (0x41)

Добавить рядом с другими Handle*, по образцу `HandleSET_TX_POWER`/`HandleGET_SIGNAL_METRICS`
(статический буфер ответа, `*out_data`/`*out_len`).

**Параметры (wire):** `half u8` (полуширина запрашиваемого окна; 0 → дефолт).
Это НАМЕРЕННОЕ упрощение относительно старой записи SPEC (offset/length): клиент
задаёт только полуширину, центр (FP) выбирает прошивка.

**Ответ DATA (заголовок 6 байт + тело):**
```
смещение  поле           тип
0..1      fp_index       u16 LE   (индекс first path в accumulator)
2..3      start_index    u16 LE   (индекс первого отсчёта в ответе)
4..5      count          u16 LE   (число отсчётов далее)
6..       I/Q пары       int16 LE × 2 × count  (I1,Q1,I2,Q2,...)
```
Итого `6 + count*4` байт. При `count=63` → 258 байт — **превышает 255!** Поэтому
жёстко ограничить `count` так, чтобы `6 + count*4 <= 255` → `count <= 62`. Значит:
- `HALF_MAX` для ОТДАЧИ = 30 (→ 61 отсчёт → 6+244=250 байт). Оставляю запас.
- Снимок в §3.1 берёт до 63 отсчётов (CIR_HALF_MAX=31), но GET_CIR отдаёт не более
  61 (обрезает симметрично вокруг FP). Это ок: снимок ≥ отдачи.

```c
/* Верхняя граница полуширины ОКНА в ответе GET_CIR: 6-байтный заголовок + count*4
 * данных должно уместиться в 255 (LEN — 1 байт). count<=62 → half<=30 (61 отсчёт,
 * 250 байт). Дефолт half=16 (33 отсчёта, 138 байт). */
#define CIR_RESP_HALF_MAX   30u
#define CIR_RESP_HALF_DEF   16u

/**
 * @brief GET_CIR (0x41). Отдать окно CIR вокруг first path из снимка cir_snap.
 *        Параметр (wire): half u8 — полуширина окна (0 → дефолт CIR_RESP_HALF_DEF,
 *        клампится к CIR_RESP_HALF_MAX). Центр окна — first path (выбирает прошивка).
 *        Ответ DATA: fp_index u16 LE, start_index u16 LE, count u16 LE, затем
 *        count пар I/Q (int16 LE каждая). Если снимка нет (не было приёма после
 *        RX_START) → TIMEOUT. Снимок делается в PollRadio (ветка RXFCG) до rxenable.
 */
static ResponseStatus HandleGET_CIR(const uint8_t* params, uint8_t params_len,
                                    uint8_t** out_data, uint8_t* out_len)
{
    *out_len = 0;

    if (!cir_snap.valid) return STATUS_TIMEOUT;   /* кадра/снимка ещё не было */

    /* Полуширина из запроса. */
    uint16_t half = CIR_RESP_HALF_DEF;
    if (params_len >= 1 && params[0] != 0) half = params[0];
    if (half > CIR_RESP_HALF_MAX) half = CIR_RESP_HALF_MAX;

    /* Окно отдачи центрируем на fp_index снимка, но не выходя за границы снимка
     * [start_index .. start_index+count-1]. Работаем в индексах accumulator. */
    uint16_t fp    = cir_snap.fp_index;
    uint16_t s_beg = cir_snap.start_index;
    uint16_t s_end = (uint16_t)(cir_snap.start_index + cir_snap.count - 1u);

    uint16_t out_beg = (fp > half) ? (uint16_t)(fp - half) : 0u;
    uint16_t out_end = (uint16_t)(fp + half);
    if (out_beg < s_beg) out_beg = s_beg;
    if (out_end > s_end) out_end = s_end;
    uint16_t out_cnt = (uint16_t)(out_end - out_beg + 1u);

    /* Смещение начала окна отдачи внутри cir_snap.data (в отсчётах → в байтах). */
    uint16_t off_samples = (uint16_t)(out_beg - s_beg);

    static uint8_t data[6 + CIR_RESP_HALF_MAX*2*4 + 4];  /* заголовок + макс. тело */
    uint8_t* p = data;
    PUT_U16LE(p, fp);       p += 2;
    PUT_U16LE(p, out_beg);  p += 2;
    PUT_U16LE(p, out_cnt);  p += 2;
    memcpy(p, &cir_snap.data[off_samples * 4u], (size_t)(out_cnt * 4u));
    p += out_cnt * 4u;

    *out_data = data;
    *out_len  = (uint8_t)(p - data);   /* <= 250, помещается в u8 LEN */
    return STATUS_OK;
}
```

### 3.3. Регистрация обработчика

В `PROTOCOL_RegisterAllHandlers()` добавить строку (рядом с остальными):
```c
    PROTOCOL_RegisterHandler(CMD_GET_CIR, HandleGET_CIR);
```

### 3.4. Самопроверка компиляции (CC, в своём окружении)

- Сборка проходит (Debug), без новых warnings.
- Проверить, что `PROTOCOL_CaptureCIR` объявлена/определена до первого использования
  в `PollRadio` (или добавить прототип выше). НЕ полагаться на неявные объявления.
- НЕ запускать на железе (нет доступа; это делает Ваган).

---

## 4. Хост — tools/mks_protocol.py

Добавить код команды и метод + парсер. НЕ ломать существующее.

```python
CMD_GET_CIR = 0x41   # рядом с прочими CMD_*
```

Метод в классе `MKS`:
```python
    def get_cir(self, half: int = 0, timeout=None):
        """GET_CIR (0x41): окно CIR вокруг first path.
        PARAMS = half u8 (полуширина окна; 0 = дефолт прошивки, макс. 30).
        Ответ DATA: fp_index u16 LE, start_index u16 LE, count u16 LE, затем
        count пар I/Q (int16 LE). Снимок делается прошивкой при приёме кадра
        (в ветке RXFCG, до перевзвода RX). Требует, чтобы после RX_START был
        принят хотя бы один кадр (иначе STATUS=TIMEOUT)."""
        if not (0 <= half <= 0xFF):
            raise ValueError("half вне диапазона u8")
        return self.command(CMD_GET_CIR, bytes([half]), timeout=timeout)
```

Парсер (рядом с `parse_signal_metrics`):
```python
def parse_cir(data: bytes) -> dict:
    """Разобрать DATA GET_CIR: заголовок 6 байт (fp_index, start_index, count —
    u16 LE) + count пар I/Q (int16 LE). Возвращает dict с fp_index, start_index,
    count, samples (список (i, q)) и amps (список sqrt(i^2+q^2))."""
    if len(data) < 6:
        raise ProtocolError(f"GET_CIR: заголовок 6 байт не помещается ({len(data)})")
    fp_index, start_index, count = struct.unpack_from("<HHH", data, 0)
    need = 6 + count * 4
    if len(data) < need:
        raise ProtocolError(
            f"GET_CIR: тело короче заявленного (count={count} → надо {need}, "
            f"есть {len(data)})")
    samples = []
    amps = []
    off = 6
    for _ in range(count):
        i, q = struct.unpack_from("<hh", data, off)
        off += 4
        samples.append((i, q))
        amps.append((i * i + q * q) ** 0.5)
    return {
        "fp_index": fp_index,
        "start_index": start_index,
        "count": count,
        "samples": samples,
        "amps": amps,
    }
```

Обновить docstring модуля вверху файла: дописать строку версии
`v6 (2026-07-17): добавлен GET_CIR (0x41) — окно CIR вокруг FP (half u8),
parse_cir(). Консольная псевдографика в mks_console.` и поднять
`HOST_VERSION = "6"`.

---

## 5. Хост — tools/mks_console.py

Добавить команду `cir` (по образцу `cmd_metrics`). ASCII-псевдографика амплитуды.

```python
def cmd_cir(dev, args, show_hex):
    # опциональный аргумент: half (полуширина окна). 0/нет → дефолт прошивки.
    half = 0
    if args:
        try:
            half = int(args[0])
        except ValueError:
            print("  использование: cir [half]   (half = полуширина окна, 0..30)")
            return
    st, data = dev.get_cir(half)
    show_response(st, data, show_hex)
    if st != 0x00:
        if st == 0x06:  # TIMEOUT
            print("    (снимка CIR нет — после rxstart прими хотя бы один кадр, затем cir)")
        return
    try:
        c = mks.parse_cir(data)
    except Exception as e:
        print(f"    (разбор не удался: {e})")
        return

    print(f"    fp_index   = {c['fp_index']}  (first path)")
    print(f"    start_index= {c['start_index']}")
    print(f"    count      = {c['count']}")

    amps = c["amps"]
    if not amps:
        return
    peak = max(amps) or 1.0
    WIDTH = 50
    # Строки: индекс отсчёта, амплитуда, бар. Маркер '<<FP' на first path.
    for k, a in enumerate(amps):
        idx = c["start_index"] + k
        bar = "#" * int(round(a / peak * WIDTH))
        mark = "  <<FP" if idx == c["fp_index"] else ""
        print(f"    [{idx:4}] {a:8.0f} |{bar}{mark}")
```

Зарегистрировать в диспетчере (рядом с `metrics`):
```python
                elif cmd == "cir":
                    cmd_cir(dev, cargs, show_hex)
```

Дописать в `HELP` строку:
```
  cir [half]                             GET_CIR (0x41): окно CIR вокруг first path
                                         half = полуширина (0..30, 0 = дефолт 16).
                                         Нужен принятый кадр после rxstart.
```

---

## 6. Обновить docs/PROTOCOL_SPEC.md (привести к реализации)

SPEC — живой документ, «прав код». Внести:

1. Таблица §5 (статус команд): строку `0x41 | GET_CIR` → статус `✅` (реализовано;
   `🔬` поставит Ваган после проверки на железе — CC ставит только `✅`).
2. Таблица §6 (параметры): для `0x41 GET_CIR` заменить `offset u16, length u16` на
   **`half u8`** (полуширина окна вокруг first path; 0 = дефолт 16, макс. 30) с
   пометкой, что центрирование по FP делает прошивка.
3. §8 (форматы ответа): добавить раздел **GET_CIR (0x41) — ✅** с описанием
   заголовка (fp_index u16 LE, start_index u16 LE, count u16 LE) + тело (count пар
   I/Q int16 LE). Отметить: окно усечённое (диагностика формы FP), полный CIR /
   потоковый режим — будущие шаги CIR-2/3. Отметить лимит: `6 + count*4 <= 255`.
4. В §8 у старой справочной строки «GET_CIR: N=length; пары I/Q...» — заменить на
   фактический формат выше (со ссылкой, что это окно вокруг FP, а не произвольный
   срез; произвольный offset — на будущее).

Не менять описания уже проверенных команд (PING/INIT/GET_STATUS/RX/TX/metrics).

---

## 7. Проверка на железе (выполняет ВАГАН, не CC) — процедура

CC этот раздел НЕ выполняет; он здесь, чтобы Ваган прошёл шаги после сборки/прошивки.

Предусловие: плата МКС, внешнее питание 3.3В (правило одного источника, HANDOFF §14),
эталонный передатчик — EVK1000 Mode 3, LOS ~0.7 м (как в проверках RX/метрик), либо
внутренний loopback M1→M2 (`txframe`/`txperiodic`).

1. Прошить свежий build. Открыть консоль: `python mks_console.py COM3`.
2. Последовательность:
   ```
   init
   mode3
   rxstart
   ```
   Подать сигнал (EVK Mode 3 в эфир, ИЛИ внутренний: в отдельной сессии/устройстве
   `txframe DE AD BE EF 01`, либо `txperiodic 100 DE AD BE EF 01`).
3. Убедиться, что кадр принят:
   ```
   metrics
   ```
   Ожидаемо: `count` растёт, физичные метрики (как в прошлых проверках). Это значит,
   что снимок CIR сделан (тот же RXFCG).
4. Прочитать CIR:
   ```
   cir
   ```
   Ожидаемо: печатается `fp_index`, окно ~33 отсчёта, ASCII-бары; пик амплитуды —
   около строки с маркером `<<FP` (first path). Форма: резкий рост у FP, затем спад
   (channel impulse response).
5. Проверить полуширину: `cir 30` (шире окно, до 61 отсчёта), `cir 8` (уже).
6. Негатив: сразу после `rxstart` без принятого кадра `cir` → `STATUS=TIMEOUT` и
   подсказка. Это корректно.

Что записать в отчёт (для последующего вылизывания):
- fp_index и типичная форма (пик у FP? спад справа?).
- Совпадает ли пик CIR с first path (маркер `<<FP`) — грубая валидация центрирования.
- Ширина окна ок? (для водопада на шаге 2 нужен полный — здесь только форма FP.)
- Есть ли артефакты/переполнение/битые значения (например, все нули → снимок не
  снялся / accumulator затёрся → сообщить архитектору, вернёмся к грабле NOTES §5).

---

## 8. Границы: чего в этом TASK НЕТ (не делать)

- Полный CIR (1016 отсчётов) одним ответом — НЕ здесь (не влезает в 255-байтный кадр).
- Потоковый режим / `SET_STREAM_MODE` / отдельный кадровый формат — шаг CIR-2.
- Утилита-водопад (Python, псевдографика полного CIR) — шаг CIR-3.
- Композит USB CDC+bulk — не трогаем (шаг CIR-2/3 по замеру).
- EXTI вместо polling — отдельная задача (HANDOFF §11 п.13), не смешивать.
- Абсолютный произвольный срез (offset/length) — на будущее; сейчас только окно по FP.

---

## 9. Коммит

- Одним логическим коммитом: прошивка (protocol.c) + хост (mks_protocol.py,
  mks_console.py) + PROTOCOL_SPEC.md.
- Сообщение коммита (пример): `feat(cir): GET_CIR 0x41 — окно CIR вокруг FP (шаг CIR-1)`.
- `git commit` + `git push` — делает CC. Деплой/прошивка — Ваган.
- Файл этого TASK при необходимости положить в `docs/` под именем `TASK_cir_step1.md`
  (не переиспользовать существующие имена; финальное имя/renaming — за Ваганом).
