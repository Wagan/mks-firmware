# RECON — Сырой мониторинг аккумулятора CIR (без флага RXFCG)

**Автор:** Vagan Sarukhanov
**Дата:** 2026-07-25
**Тип:** РАЗВЕДКА (recon). Правок кода НЕ делалось. Коммит — только этот отчёт.
**Задание:** `docs/TASK_recon_raw_cir_monitor.md`
**Проект:** mks-firmware (МКС, STM32F411 + 2× DWM1000)

> **Классификация не меняется.** Это временная проверка гипотез для будущего
> 5-канального макета-обнаружителя. НЕ фронт МКС и НЕ функция продукта. HANDOFF
> §11 п.20 и §15.3 («задача обнаружителя, НЕ МКС») остаются в силе.

## Источники

- **Код прошивки** (репозиторий): `App/protocol/protocol.c`, `App/platform/deca_port.c`,
  `Core/Src/main.c`, `Drivers/decadriver/{deca_device.c,deca_regs.h,deca_device_api.h}`,
  `Debug/mks-firmware.map`. Ссылки — `файл:строка`.
- **DW1000 User Manual, Version 2.17** — `OneDrive\Common\02_ПАРТНЕРЫ\ПРОГРЕССТЕХ\
  datasheets\DW1000 User Manual DecaWave.pdf`. Ссылки — `§ / стр. PDF N (печ. N−1)`,
  с дословными цитатами. (В документе печатная страница = PDF минус 1.)
- **DecaRanging EVB1000 MP rev 3p11** — `C:\Users\user\STM32CubeIDE\workspace_1.16.0\
  DecaRangingEVB1000_MP_rev3p11_MX\Src\{application,decadriver}`. Эталон Decawave.

---

## Б1. Наполняется ли аккумулятор без детекта преамбулы

**Ответ: НЕТ. Наполнение CIR стартует строго после детекта преамбулы и является
самим процессом накопления преамбулы (preamble accumulation). До детекта преамбулы
осмысленной CIR в аккумуляторе нет.**

- **UM §4.1.2 «Preamble Accumulation», стр. PDF 33 (печ. 32):** «Once the preamble
  sequence is detected, the receiver begins accumulating correlated preamble symbols,
  while looking for the SFD sequence… Accumulation stops when the SFD is detected…».
  => старт накопления жёстко привязан к событию детекта преамбулы.
- **UM §7.2.38 (ACC_MEM 0x25), стр. PDF 129 (печ. 128):** «Register file 0x25 …
  holds the accumulated channel impulse response (CIR) data.»
- **UM §7.2.47.7 (LDE_REPC), стр. PDF 182; §9.3, стр. PDF 210:** «The accumulator
  operates on the preamble sequence to give the channel impulse response … because of
  the perfect periodic auto-correlation property of the … UWB preamble sequences.»
- Связь с накоплением преамбулы — **прямая**: RXPACC = «Preamble Accumulation Count»,
  число накопленных символов преамбулы (UM Reg 0x10, стр. PDF 97).

**Прямого единственного предложения «содержимое аккумулятора валидно только после
события X» в UM — НЕ НАЙДЕНО** (просмотрены §4.1–4.7 стр. PDF 32–47, §7.2.38 стр. PDF
129). Косвенная привязка к событиям: RXPACC «is updated when a good PHR is detected
(when the RXPHD status bit is set)» и служит «as an aid to interpreting the accumulator
data» (UM Reg 0x10, стр. PDF 97); LDE «analysing the accumulator data» запускается при
хорошем PHR (UM §7.2.17 LDEERR, стр. PDF 90). То есть наполнение — по детекту
преамбулы, а интерпретация (RXPACC/LDE) — по RXPHD.

> **Критично для смысла задачи (детект СШП):** валидность CIR в аккумуляторе **НЕ
> требует прохождения CRC (RXFCG)**. Накопление завершается на этапе преамбулы
> (RXPRD→RXSFDD), задолго до RXFCG. Значит сырой съём возможен ПОСЛЕ детекта преамбулы,
> даже если кадр далее не прошёл (RXPHE/RXFCE). НО: «чисто по таймеру без сигнала»
> осмысленной CIR не будет — событие детекта преамбулы **необходимо**.

---

## Б2. Чем заменить RXFCG как событие съёма

Из `deca_regs.h` (биты SYS_STATUS 0x0F) и UM §7.2.17 (стр. PDF 86–92) —
хронологический порядок приёмного автомата (UM §4.1, стр. PDF 32–34):

| Флаг | Бит | `deca_regs.h` | Смысл (UM §7.2.17) | Состояние аккумулятора |
|---|---|---|---|---|
| **RXPRD** | 8 | `:263` `SYS_STATUS_RXPRD 0x0100` | «receiver has detected (and confirmed) the presence of the preamble sequence» (стр. PDF 88) | **накопление CIR НАЧАЛОСЬ** (UM §4.1.2) |
| **RXSFDD** | 9 | `:264` `SYS_STATUS_RXSFDD 0x0200` | «detected the SFD sequence and is moving on to decode the PHR» (стр. PDF 88) | **накопление ЗАВЕРШЕНО** («Accumulation stops when the SFD is detected», §4.1.2) — CIR наполнена |
| **RXPHD** | 11 | `:266` `SYS_STATUS_RXPHD 0x0800` | «completed the decoding of the PHR» (стр. PDF 88) | RXPACC финализирован (Reg 0x10, стр. PDF 97); стартует LDE |
| **LDEDONE** | 10 | `:265` `SYS_STATUS_LDEDONE 0x0400` | «completion of the leading edge detection…» (стр. PDF 88) | LDE проанализировал аккумулятор; FP-индекс готов |
| **RXDFR** | 13 | `:268` `SYS_STATUS_RXDFR 0x2000` | «Data Frame Ready» (после LDE, стр. PDF 89) | CIR валидна |
| **RXFCG** | 14 | `:269` `SYS_STATUS_RXFCG 0x4000` | «FCS Good» (CRC прошёл) — *текущий триггер* | CIR валидна |

Ошибочные/таймаутные события того же этапа (тоже несут наполненную CIR, если детект
преамбулы был): **RXPHE** (bit12, ошибка PHR, `:267`), **RXFCE** (bit15, ошибка CRC,
`:270`), **RXSFDTO** (bit26, «SFD detection timeout starts running as soon as preamble
is detected», UM стр. PDF 92), **RXPTO** (bit21, preamble detection timeout — детекта
НЕ было → CIR НЕ наполнена).

**Съём по таймеру, а не по флагу.** UM: приёмник может быть включён (RXENAB, §7.2.15
стр. PDF 83: «turn on its receiver and begin looking for the configured preamble
sequence»), но **без детекта преамбулы в ACC_MEM осмысленной CIR не будет** (Б1). То
есть «съём по чистому таймеру без сигнала» не даёт валидной CIR.

**Отдельного флага «накопление преамбулы завершено / аккумулятор готов» в UM — НЕ
НАЙДЕНО** (просмотрены оба октета SYS_STATUS: REG:0F:00 стр. PDF 86–91, REG:0F:04
стр. PDF 87). Ближайший эквивалент завершения накопления — **RXSFDD**; готовности к
интерпретации — **RXPHD**/**LDEDONE**.

> **Вывод Б2:** самое раннее событие с гарантированно наполненной CIR — **RXSFDD**
> (накопление окончено). Самое раннее событие вообще — **RXPRD** (накопление идёт, но
> может быть не завершено). Оба — раньше RXFCG и не требуют валидного кадра.

---

## Б3. Чтение ACC_MEM: тактирование, dummy-байт, предусловия

### Что делает `dwt_readaccdata` (`deca_device.c:712-720`)

```c
void dwt_readaccdata(uint8 *buffer, uint16 len, uint16 accOffset) {
    _dwt_enableclocks(READ_ACC_ON);                        // 715
    dwt_readfromdevice(ACC_MEM_ID, accOffset, len, buffer);// 717
    _dwt_enableclocks(READ_ACC_OFF);                       // 719 — вернуть клоки
}
```

Функция **сама** форсирует тактирование аккумулятора вокруг чтения:

- **READ_ACC_ON** (`deca_device.c:2476-2480`): `reg[0] = 0x48 | (reg[0] & 0xb3)` —
  включает **FACE** (Force Accumulator Clock Enable, бит6 `0x40`, `deca_regs.h:1305`) +
  **RXCLKS_125M** (RX-клок от PLL, бит `0x08`, `deca_regs.h:1299`); `reg[1] = 0x80 | reg[1]`.
- **READ_ACC_OFF** (`deca_device.c:2482-2486`): снимает эти биты, возвращает
  секвенсированное тактирование (`ENABLE_ALL_SEQ`).

Соответствие UM §7.2.50.1 (PMSC_CTRL0, стр. PDF 195): для чтения аккумулятора нужны
**FACE (bit6)=1 И AMCE (bit17)=1**, плюс присутствие RX-клока (RXCLKS, стр. PDF 194:
«the receive clock needs to be present to access the accumulator memory»).

> **⚠ Расхождение (наблюдение, не блокер):** UM требует **и FACE, и AMCE** (bit17,
> `0x00020000`, байт 2 регистра). Vendor-`READ_ACC_ON` пишет только байты 0 и 1
> регистра (`deca_device.c:2515-2516` — offset 0 и offset 1), байт 2 (где AMCE) **не
> трогает**. Тем не менее наши CIR-чтения на железе работают (PROTOCOL_SPEC §8:
> корректная форма CIR, фронт у first path). => либо AMCE на данном кремнии не нужен,
> либо взводится иначе. Это **ГИПОТЕЗА (не проверено)** — установить осциллографом /
> сравнением полного съёма (см. ВЫВОД п.3). На текущую задачу не влияет: тактирование
> самодостаточно внутри `dwt_readaccdata`, и оно НЕ зависит от нахождения в ветке RXFCG.

### Dummy-байт

- **UM §7.2.38, стр. PDF 129 (и повтор стр. PDF 130):** «Because of an internal memory
  access delay when reading the accumulator the first octet output is a dummy octet that
  should be discarded. This is true no matter what sub-index the read begins at.»
- **Драйвер dummy-байт НЕ срезает** — только предупреждает в комментарии
  (`deca_device.c:700-701`). Отбрасывает вызывающий: наш код читает `count*4 + 1` байт и
  копирует со смещения `+1` (`protocol.c:1327-1331`).

### Можно ли звать `dwt_readaccdata` вне ветки RXFCG

**Да, кодовых предусловий, привязанных к RXFCG, у функции НЕТ.** Она самостоятельно
включает/выключает клоки аккумулятора. Единственное фактическое требование — чтобы в
ACC_MEM лежала валидная CIR (т.е. был детект преамбулы, Б1) и она ещё не затёрта (Б4).
Активное DW-устройство должно быть выбрано вызывающим (`deca_port_select_device`).

### Наш фактический контекст вызова

`PROTOCOL_CaptureCIR` (`protocol.c:1306-1337`) вызывается из `PROTOCOL_PollRadio`
(`protocol.c:1382-1384`) в ветке RXFCG. Что даёт нам нахождение в этой ветке (а не сама
возможность чтения):

1. **Кадр только что принят** → в ACC_MEM лежит CIR именно этого кадра (наполнена
   на этапе преамбулы, Б1).
2. **FP-индекс доступен** — `dwt_readdiagnostics` вызван выше (`protocol.c:1365`),
   `rx_metrics.diag.firstPath` используется для центрирования окна (`protocol.c:1314`).
3. **Аккумулятор ещё не затёрт** — снимок берётся **ДО** `dwt_rxenable`
   (`protocol.c:1383` перед `:1386`; комментарий-грабля `:1304-1305`).
4. Устройство уже выбрано (`deca_port_select_device(DW_RX_LISTEN_DEV)`, `protocol.c:1351`).

Для сырого съёма без RXFCG все четыре условия надо обеспечить иначе: (1) съём по более
раннему событию (Б2), (2) FP из LDE может быть ещё не готов при RXPRD/RXSFDD, (3)
снимать до перевзвода RX, (4) выбор устройства — как сейчас.

---

## Б4. Затирание аккумулятора

- **Перезапись новым приёмом:** CIR переписывается накоплением при следующем детекте
  преамбулы (UM §4.1.2). RX-флаги статуса «automatically cleared by the next receiver
  enable, including those caused by the RXAUTR auto-re-enable» (UM §7.2.17, стр. PDF
  88–90). => повторный `dwt_rxenable` (`protocol.c:1386,1395`) запускает новое
  накопление, затирающее прежнюю CIR. Прямого предложения «rxenable очищает ACC_MEM» в
  UM НЕ НАЙДЕНО — говорится о сбросе флагов и новом приёме. Наш код это учитывает:
  снимок строго до `dwt_rxenable`.
- **rxreset** (`protocol.c:1394`, ветка RX-ошибки): UM §4.1.6 note (стр. PDF 34–35) —
  receiver-only reset после ошибок/таймаутов «ensures that the next good frame will have
  correctly calculated timestamp»; реализуется через SOFTRESET (Sub-Register 0x36:00
  PMSC_CTRL0, «clear and set bit 28 only», UM стр. PDF 196). Ре-инициализирует приёмник
  (в т.ч. LDE) → прежний аккумулятор для дальнейшего использования недействителен.
- **forcetrxoff** (TRXOFF, UM §7.2.14 Reg 0x0D bit6, стр. PDF 82): «returns to idle mode
  immediately. Any TX or RX activity … will be aborted.» **Явного утверждения, что
  TRXOFF стирает аккумулятор, в UM НЕ НАЙДЕНО** (описан только переход в idle).
- **Разрушающее ли чтение ACC_MEM:** прямого утверждения о порче содержимого при чтении
  в UM **НЕ НАЙДЕНО**. ACC_MEM помечен RO (§7.2.38, стр. PDF 129); единственное
  предостережение — dummy-октет (Б3). UM не запрещает повторное чтение, но и явно
  неразрушаемость не гарантирует. Наш код читает окно один раз на кадр.

**Итог Б4:** затирает — новый приём (детект преамбулы / rxenable) и rxreset. forcetrxoff
и само чтение — по UM не подтверждено как затирающие. Для сырого съёма повторяемость
чтения одного содержимого **не подтверждена и не опровергнута** — устанавливать замером.

---

## Б5. Прецедент у Decawave

### DecaRanging (эталон)

Единственное обращение к аккумулятору во всём прикладном коде — функция
**`instance_readaccumulatordata()`** (`Src\application\instance.c:1108-1122`):

- Гейтирована `#if DECA_SUPPORT_SOUNDING==1` (`instance.c:1110`), а макрос
  **`DECA_SUPPORT_SOUNDING` в проекте нигде не определён** → тело в этой сборке в ноль.
- **Ни одного вызывающего** во всём дереве `Src` (только определение + прототип
  `instance.h:519`). Ни таймер, ни ISR, ни RX-колбэк её не дёргают.
- Когда читает (в sounding-варианте): `len = 992` (PRF16) или `1016` (PRF64),
  `offset = 0`, `len = len*4 + 1` (dummy-байт зарезервирован полем `.dummy` в начале
  буфера) — `instance.c:1112-1121`. Полный аккумулятор по PRF, с начала.
- В штатной ветке успешного приёма (`instance_rxgoodcallback`, `instance_common.c:753`)
  читаются только rx-timestamp (`:829`) и данные кадра (`:836`) — **аккумулятор НЕ
  читается вообще**.

**Режима непрерывного/сырого мониторинга приёмника (sniff/listen-raw/continuous-RX) в
DecaRanging НЕ НАЙДЕНО:** `continuous` = непрерывная **передача** TX-спектра
(`dw_main.c`, `instance_calib.c:115-121`, `dwt_configcontinuousframemode`); `monitor` =
флаг TWR-таймера; `Listener` mode = штатный приём валидных кадров без ответа.

> **Вывод Б5 (прецедент):** готового образца сырого съёма аккумулятора вне RXFCG у
> Decawave **нет**. Наш `PROTOCOL_CaptureCIR` уже является рабочим паттерном чтения
> окна. Правило «не изобретать, что у Decawave готово» здесь не срабатывает — готового
> нет; ближайший ориентир по объёму/offset/dummy — `instance_readaccumulatordata`.

### Функции decadriver для диагностики/непрерывного режима, которые мы НЕ используем

(перечень с сигнатурами, без оценки пригодности; `deca_device_api.h`)

- `void dwt_setsniffmode(int enable, uint8 timeOn, uint8 timeOff);` (`:767`) — SNIFF.
- `void dwt_setdblrxbuffmode(int enable);` (`:829`) — двойной RX-буфер.
- `void dwt_setrxtimeout(uint16 time);` (`:845`).
- `void dwt_setpreambledetecttimeout(uint16 timeout);` (`:860`).
- `void dwt_configeventcounters(int enable);` (`:1341`) — диагностические счётчики.
- `void dwt_readeventcounters(dwt_deviceentcnts_t *counters);` (`:1355`).
- `void dwt_configcwmode(uint8 chan);` (`:1433`) — continuous wave (**TX**).
- `void dwt_configcontinuousframemode(uint32 framerepetitionrate);` (`:1448`) — continuous frame (**TX**).
- `void dwt_setlnapamode(int lna, int pa);` (`:387`); `void dwt_setleds(uint8 mode);` (`:1389`).

Выделенной функции «сырой RX / съём аккумулятора по таймеру» в decadriver **НЕ
НАЙДЕНО**. Чтение аккумулятора — только `dwt_readaccdata` (`:1271`), которую мы уже
используем.

SNIFF (UM §7.2.30, стр. PDF 107; §4.1.1 стр. PDF 33): «the receiver samples ('sniffs')
the air periodically … If preamble is detected … the receiver will remain on … If no
preamble is detected the receiver will be returned to idle mode». => SNIFF — таймерная
выборка **на детект преамбулы**, а не таймерный съём аккумулятора.

---

## Б6. Бюджет полного съёма (1016 отсчётов = 4064 байта)

### Объём

- ACC_MEM: 4064 байта (`deca_regs.h:661` `ACC_MEM_LEN (4064)`). Полный CIR = **1016
  отсчётов × 4 байта (I/Q int16)** при PRF64; **992 отсчёта = 3968 Б** при PRF16
  (`protocol.c:1311`; DecaRanging `instance.c:1112-1115`).
- Чтение по SPI = header + dummy(1) + данные. Полный съём с offset 0 → **header 1 байт**
  (index 0 → без sub-index, `deca_device.c:1022-1024`). Итого по проводу:
  **1 + 1 + 4064 = 4066 байт** (PRF64).

### Частота SPI к радио в рабочем режиме

Тактовая (`Core/Src/main.c:146-163`): HSE 24 МГц → PLLM=12 (→2 МГц) → PLLN=72 (→VCO 144)
→ PLLP=DIV2 → **SYSCLK 72 МГц**; PLLQ=3 → USB 48 МГц ✓. AHB=/1 (72 МГц),
**APB1CLKDivider=DIV2 → APB1 = 36 МГц**. SPI2 (M1) и SPI3 (M2) сидят на **APB1**.

Прескалеры (`deca_port.c`): slow = `SPI_BAUDRATEPRESCALER_32` (`:103`), fast =
`SPI_BAUDRATEPRESCALER_4` (`:110`).

> **⚠ Ключевой факт:** `deca_port_spi_set_fast()` **определён, но НЕ вызывается нигде**
> в App (grep по репозиторию: только определение `deca_port.c:107` и прототип
> `deca_port.h:40`). Реально вызывается только `set_slow` — в INIT (`protocol.c:469`) и
> bringup (`bringup.c:40`). => **рабочий SPI радио идёт на медленном прескалере 32**:
> **36 МГц / 32 = 1.125 МГц** (комментарий `deca_port.c:96-99` помечает значения как
> «подобрать» — каркас). Fast (если включить) = 36/32... = **36/4 = 9 МГц** (не 20:
> APB1 всего 36 МГц).

### Время и темп полного съёма (расчёт)

Теоретически (8 бит/байт, встык, без межбайтовых зазоров и HAL-оверхеда):

| Режим SPI | Частота | Время 4066 Б | Съёмов/с (SPI-only) | Поток 4064 Б × темп |
|---|---|---|---|---|
| **slow (текущий, prescaler 32)** | 1.125 МГц | 4066×8/1.125e6 = **28.9 мс** | ≈ **34.6** | ≈ **140 КБ/с** |
| fast (prescaler 4, НЕ вызывается) | 9 МГц | 4066×8/9e6 = **3.61 мс** | ≈ **277** | ≈ 1.1 МБ/с |

**Оговорка:** это теоретический минимум по битовому клоку. Реально `readfromspi`
использует `HAL_SPI_Receive` в polling-режиме (`deca_port.c:159`) + тоггл CS +
per-byte-оверхед → фактическое время **выше**, и оно **не измерено**. Для полного CIR
его надо мерить отдельно (DWT cycle counter / HAL_GetTick).

### Потолок транспорта

Замер HANDOFF §11 п.16 дал **≥200 fps × 168 Б (~33 КБ/с) без потерь**, но упёрся в темп
передатчика (`txperiodic` минимум 5 мс), **а НЕ в транспорт** — реальный потолок USB CDC
**не достигнут и неизвестен**. 33 КБ/с **не является пределом**. Для полного CIR (при
slow SPI это ~140 КБ/с даже по одному SPI) потолок USB придётся мерить заново.

### RAM (F411, 128 КБ)

Из `Debug/mks-firmware.map`: RAM ORIGIN 0x20000000, LENGTH 0x20000 (**128 КБ**),
`_estack = 0x20020000` (`:4936`). Занято:
- `.data` = 0x150 (**336 Б**, `:5943`);
- `.bss` = 0x2cc0 (**11456 Б**, `:6006`, `_ebss = 0x20002e10` `:6113`);
- heap `_Min_Heap_Size` = 0x200 (512 Б, `:4937`), stack `_Min_Stack_Size` = 0x400
  (1024 Б, `:4938`); конец heap+stack-региона `0x20003410` (`:6123`).

Итого статически занято ≈ **13 КБ из 128 КБ**; свободно ≈ **115 КБ**. Буфер полного
съёма **4065 Б (4064 + dummy) помещается свободно**.

> **Уточнение к HANDOFF §2 («буфер CIR 4064 Б уже учтён как допустимый»):** это
> **проектная** формулировка, в текущем коде **не реализована**. Фактически буфера
> полного CIR НЕТ — есть только `cir_snap.data` = **252 Б** (`protocol.c:230-239`,
> `CIR_SNAP_BYTES = 63 отсчёта × 4`) и временный `tmp[1+252]` (`protocol.c:1327`,
> static). Плюс `rx_frame[128]` (`protocol.c:210`). Полный съём потребует **нового**
> буфера ~4 КБ — RAM позволяет.

### Ограничение формата

- **Командный кадр** (PROTOCOL_SPEC §2): LEN — **1 байт**, макс. **255 Б DATA**. 4064 Б
  **не несёт**. `GET_CIR` уже упёрт в это (≤62 отсчёта, PROTOCOL_SPEC §8). Полный CIR
  одним командным кадром **невозможен** без чанкинга (offset/length, CIR-3).
- **Потоковый кадр** (PROTOCOL_SPEC §8, SET_STREAM_MODE): LEN16 — **u16**, до 65535 Б.
  4064 Б **несёт** без изменения формата.

**Итог Б6:** полный CIR несёт только потоковый формат (LEN16); командный — нет.

---

## ВЫВОД

### 1. Возможен ли сырой съём без RXFCG

**Да, но не «по чистому таймеру» — требуется как минимум детект преамбулы.**

- CIR наполняется на этапе накопления преамбулы (RXPRD→RXSFDD), **до PHR и до RXFCG**,
  и **не требует прохождения CRC** (Б1, UM §4.1.2). Значит съём можно привязать к более
  раннему событию: гарантированно наполненная CIR — с **RXSFDD** (накопление
  завершено); самое раннее — **RXPRD** (накопление идёт).
- `dwt_readaccdata` **не имеет кодовых предусловий на RXFCG** — самодостаточно управляет
  тактированием аккумулятора (Б3). Прецедента у Decawave нет, но и запрета нет (Б5).
- Ограничение: **без детекта преамбулы осмысленной CIR в аккумуляторе не будет** (Б1,
  Б2, SNIFF в UM). «Водопад чистого шума без сигнала» штатным аккумулятором DW1000 не
  снять — это подтверждает классификацию §15.3 (отдельная задача обнаружителя).

### 2. Минимальный набор изменений (без реализации)

1. **Триггер съёма:** в `PROTOCOL_PollRadio` опрашивать более ранние флаги SYS_STATUS
   (**RXSFDD** как основной; опц. RXPRD / RXPHD / LDEDONE / RX-ошибки RXPHE·RXFCE) в
   дополнение/вместо RXFCG — по образцу текущего опроса `dwt_read32bitreg(SYS_STATUS_ID)`.
2. **Полный буфер:** добавить static-буфер ~4065 Б (RAM позволяет, Б6) — отдельно от
   252-байтного `cir_snap`.
3. **Съём:** `dwt_readaccdata(buf, N*4+1, 0)`, отбросить dummy; `N` = 1016/992 по PRF
   (как `instance_readaccumulatordata`).
4. **SPI:** включить `deca_port_spi_set_fast()` (сейчас не вызывается) — иначе полный
   съём ≈29 мс/кадр (~35/с). С fast (9 МГц) ≈3.6 мс (Б6). Требует проверки надёжности
   fast-SPI на железе (прескалер помечен «подобрать»).
5. **Транспорт:** только потоковый LEN16-кадр (несёт 4064 Б) или чанкинг CIR-3
   (offset/length) для командного формата (Б6).
6. **Затирание:** снимать **до** `dwt_rxenable`/`rxreset` (как сейчас в
   `PROTOCOL_CaptureCIR`, Б4).
7. **Изоляция:** compile-time `#define` по образцу `MKS_SIMPLEX`, по умолчанию
   закомментирован; при выключенном — прошивка байт-в-байт как сейчас (требование
   задания).

### 3. Что осталось неизвестным и чем установить

- **Реальный потолок USB CDC для полного CIR-потока** (~140 КБ/с при slow, больше при
  fast) — 33 КБ/с из HANDOFF §11 п.16 не предел. → **замер на железе** (`mks_stream_probe`).
- **Реальное время `dwt_readaccdata` 4066 Б** с учётом HAL-polling/CS-оверхеда — расчёт
  28.9 мс теоретический. → **замер** (DWT cycle counter / HAL_GetTick).
- **Качество/валидность CIR при съёме по RXSFDD/RXPRD и по RX-ошибке** (RXPHE/RXFCE),
  без валидного кадра — UM гарантирует лишь факт наполнения по преамбуле. → **эмпирика
  на железе** (сравнить форму CIR с текущей RXFCG-веткой).
- **Нужен ли AMCE (bit17)** — UM требует FACE+AMCE, vendor `READ_ACC_ON` байт 2 не
  трогает, но CIR-чтения работают (Б3, расхождение помечено ГИПОТЕЗОЙ). → тест полного
  съёма / осциллограф; на окне 63 отсчёта проблем нет, поведение на 1016 не проверено.
- **Разрушающее ли чтение ACC_MEM** (повторный съём того же содержимого) — UM не
  утверждает ни то, ни другое (Б4). → **замер** (два подряд чтения одного кадра).
- **Надёжность fast-SPI (9 МГц)** на обеих шинах M1/M2 — прескалер в `deca_port.c`
  помечен «подобрать», fast никогда не гонялся в работе. → проверка на железе.

---

*Разведка. Реализация (если состоится) — отдельный шаг по образцу `MKS_SIMPLEX`,
compile-time `#define` по умолчанию выключен. Классификация §15.3 (задача обнаружителя,
не МКС) не меняется.*
