# PROJECT HANDOFF — Прошивка МКС / Мини-стенд (DW1000 + STM32F411RE)

**Статус документа:** рабочий черновик v0.4 (OQ-12 закрыт; МКС без физической консоли)
**Назначение:** единая согласованная точка входа в проект. Содержит сверку
требований из всех исходных документов, зафиксированные решения, выявленные
противоречия и открытые вопросы (TBD). Обновляется по мере поступления данных.

---

## 1. Краткое описание проекта

Разработка встроенного ПО (прошивки) для двух конструктивно различных плат на
одном микроконтроллере **STM32F411RE**:

- **МКС** (Модуль коммуникации и сопряжения) — плата расширения ПК, **2× DWM1000**
  (Источник СШП = эталонный TX, Индикатор СШП = эталонный RX).
- **Мини-стенд** — отдельная плата, **1× DWM1000**.

Связь с ПК (MATLAB) — через **USB CDC (Virtual COM Port)**, бинарный протокол с
CRC8. МК принимает команды верхнего уровня (API v1.3), транслирует их в операции
с регистрами DW1000, собирает диагностику (RSSI, SNR, CIR, метки времени).
Задача НИР — обнаружение СШП/UWB-сигналов.

**ОС:** FreeRTOS. **HAL:** STM32 HAL. **Низкий уровень DW1000:** официальный
DecaDriver v4.0.6 (см. раздел 5).

---

## 2. Целевое железо

| Параметр | Значение | Источник / примечание |
|---|---|---|
| МК | **STM32F411RE** (Cortex-M4F) | Указан пользователем. **Заменяет** STM32F405RGT6 из документов |
| RAM / Flash | 128 КБ / 512 КБ | F411RE. Достаточно (буфер CIR 4064 Б + очереди + USB) |
| USB | USB OTG FS → CDC | Требует точные 48 МГц |
| HSE (кварц) | МКС: **24 МГц**, Мини-стенд: **8 МГц** | Подтверждено (схема + Nucleo). Разные настройки PLL для 48 МГц USB |
| DW1000 кварц | 38.4 МГц | На модулях DWM1000 (уже установлен) |
| SPI к радио | МКС: **раздельные** SPI2(M1)+SPI3(M2); Мини-стенд: SPI1 | До 20 МГц (после init); медленный (<3 МГц) на этапе init. **НЕ общая шина** (см. §3 п.7) |
| Питание | 3.3 В (LM1117-3.3) | Развязка по питанию на каждый модуль |
| Watchdog | IWDG обязателен | ТЗ п.4.2 |

### 2.1 Различия плат (вынести в `board_config.h`)

| Аспект | МКС | Мини-стенд (Nucleo-F411RE) |
|---|---|---|
| Число DWM1000 | 2 | 1 |
| `DWT_NUM_DW_DEV` | 2 | 1 |
| Роли | M1 + M2 (Источник/Индикатор назначаются программно) | 1 модуль (для студентов достаточно) |
| Шина(ы) SPI к радио | **раздельные:** M1=SPI2, M2=SPI3 | SPI1 |
| HSE кварц | **24 МГц** (XT1) | 8 МГц |
| Внешняя EEPROM | **M95080 на SPI1** (CS=PA4) | нет |
| Макрос сборки | `BOARD_MKS` | `BOARD_MINISTEND` |

#### Распиновка МКС (из схемы PCI_E_PCB) — ПОДТВЕРЖДЕНО

| Сигнал | STM32F411 | Модуль | Примечание |
|---|---|---|---|
| SPI2 SCK / MISO / MOSI | PB10 / PC2 / PC3 | M1 | шина радио №1 |
| SPI2 CS (NSS) | PB12 | M1 | CS1 |
| SPI3 SCK / MISO / MOSI | PC10 / PC11 / PC12 | M2 | шина радио №2 |
| SPI3 CS (NSS) | PA15 | M2 | CS2 |
| IRQ1 | PB8 | **M2** GPIO8 | внимание: IRQ1↔M2 |
| IRQ2 | PB9 | **M1** GPIO8 | внимание: IRQ2↔M1 |
| RST1 | PB6 → VT1 (BSS138) | M1 RSTn | сброс через N-MOSFET (инверсия уровня) |
| RST2 | PB7 → VT2 (BSS138) | M2 RSTn | сброс через N-MOSFET (инверсия уровня) |
| SPI1 SCK/MISO/MOSI/NSS | PA5/PA6/PA7/PA4 | M95080 EEPROM | НЕ радио |
| USB DM / DP | PA11 / PA12 | XP1 | USB FS (CDC) |
| LED | PB-линии через R3/R5/R6/R8/R9 | — | TX-красный, RX-жёлтый |

> **Внимание (нумерация IRQ):** на схеме IRQ1 идёт к модулю M2, а IRQ2 — к M1.
> Перекрёстное именование. В `board_config.h` именовать по модулю, не по номеру
> цепи, чтобы не путать.

> **Сброс через MOSFET:** RST управляется N-канальным BSS138 → логика
> **инвертирована** относительно прямого подключения. Это нужно учесть в функции
> аппаратного сброса (активный уровень). Проверить на железе.

#### Распиновка Мини-стенда (Nucleo-F411RE) — ПОДТВЕРЖДЕНО

Один DWM1000 на **SPI1**: CS=PA4, SCK=PB3, MISO=PB4, MOSI=PB5, IRQ=PB0, RST=PC0.

---

## 3. Зафиксированные решения

1. **CRC8 — область покрытия:** считается по **LEN + CMD_ID + PARAMS (без SYNC)**,
   как в ТЗ п.5.2.2. Алгоритм: CRC-8, полином `0x07`, init `0x00`, без рефлексии
   и xorout. Ответ: CRC по **LEN + STATUS + DATA**.
   - **РЕШЕНО (OQ-1 закрыт):** делаем «как правильно» — CRC **без SYNC**. Пример
     MATLAB-кода в документе протокола (где CRC считается по всему массиву с
     SYNC) **игнорируем как ошибочный**. Нужно выдать заказчику **исправленный
     пример MATLAB** (см. раздел 12).
   - Область покрытия всё равно оставить параметром/макросом — дёшево и страхует.
2. **Несовпадение CRC →** вернуть код ошибки (`INTERNAL_ERROR` по ТЗ), не «молча
   игнорировать».
3. **Терминология двух модулей:** Источник (TX) / Индикатор (RX). Унифицировать в
   коде (`DEV_SOURCE` / `DEV_INDICATOR` или `DEV_TX` / `DEV_RX`).
4. **PING** добавлен в v1.3 позже → расхождение «16 vs 17 команд» закрыто, это
   версионный артефакт. Канон — таблицы протокола/API v1.3 (CMD_ID 0x00–0x61).
5. **Низкий уровень DW1000:** берём официальный **DecaDriver v4.0.6**, самодельный
   `dw1000_driver.c` **не используем**. Из самодельной библиотеки сохраняем
   верхние уровни (см. раздел 6).
6. **МК:** STM32F411RE для обеих плат. STM32F405RGT6 из документов — устаревшая
   информация.
7. **SPI к радио — РАЗДЕЛЬНЫЕ шины (КОРРЕКЦИЯ).** Схема МКС показывает: M1 на
   **SPI2**, M2 на **SPI3** (а SPI1 занят EEPROM M95080). Это противоречит всем
   текстовым документам, где SPI назывался «общим». Верить схеме.
   - Мьютекс «на общую шину» в прежнем виде не нужен; чипы аппаратно независимы.
   - Но `pdw1000local` в DecaDriver — глобальный. Защищаем пару
     «`dwt_setlocaldataptr(idx)` + транзакция» (мьютекс) ИЛИ держим всю работу с
     радио в одной `Radio_Manager_Task` (тогда гонок нет по построению).
   - `deca_spi.c` выбирает И нужный `SPI_HandleTypeDef` (hspi2/hspi3), И линию CS
     по активному устройству.
8. **Сброс DWM1000 через N-MOSFET (BSS138).** Линия RST инвертирована. Учесть
   активный уровень в функции аппаратного сброса. Проверить на железе.
9. **USB-роли (OQ-6 закрыт):** «внешний USB» (XP1/Host-разъём) — связь с ПК
   (MATLAB, протокол + CDC). Отладочный USB на плате — подключение к дебаггеру
   STM32CubeIDE; **DEBUG Console идёт через него** (а не через отдельный UART, как
   предполагалось в самодельной библиотеке). Уточнить транспорт консоли:
   ST-LINK VCP/UART или отдельный CDC. См. OQ-8.

---

## 4. Сверка требований: команды API (канонический чек-лист)

Источники: «Бинарный протокол МКС v1.3», «МКС API v1.3», ТЗ п.5.3, DW1000 UM.
Колонка «Параметры» — формат на проводе (little-endian). Колонка «Регистры/действия»
— что прошивка делает с DW1000 (через DecaDriver).

| CMD_ID | Команда | Параметры (wire) | Ответ DATA | Регистры/действия DW1000 | Примечание |
|---|---|---|---|---|---|
| 0x00 | PING | — | OK uint8 (PONG=0x00) | нет | Немедленный ответ, без обращения к чипу |
| 0x01 | INIT | — | — | DEV_ID, PMSC_CTRL0, OTP_CTRL, SYS_CFG, AGC_TUNE1/2/3, DRX_TUNE2 | `dwt_initialise()` + загрузка LDE + дефолтная конфигурация + фикс suboptimal (UM §2.5.5) |
| 0x02 | GET_STATUS | — | TX_state u8, RX_state u8, channel u8, data_rate u8, preamble_length u16, PRF u8 | SYS_STATE, SYS_CFG (или из кэша конфигурации) | Брать из кэша прошивки |
| 0x03 | RESET_RADIO | — | — | PMSC_CTRL0 (SOFTRESET) | `dwt_softreset()`, затем восстановить конфигурацию |
| 0x10 | SET_PHY_CONFIG | channel u8, data_rate u8, preamble_length u16, preamble_code u8, PRF u8, PAC_size u8 | — | CHAN_CTRL, TX_FCTRL, DRX_TUNE0b/1a/2, AGC_TUNE1 | Заполняет `dwt_config_t` → `dwt_configure()`. **Нужна трансляция** (см. 4.1) |
| 0x11 | SET_TX_POWER | power_level u8 | — | TX_POWER | Маппинг u8 → значение TX_POWER (UM рис.26). Валидация диапазона |
| 0x20 | TX_FRAME | length u16, payload[length] | — | TX_BUFFER, TX_FCTRL, SYS_CTRL, SYS_STATUS | `dwt_writetxdata()`+`dwt_writetxfctrl()`+`dwt_starttx()`; ждать TXFRS |
| 0x21 | TX_PERIODIC | period_ms u16, length u16, payload[length] | — | TX_BUFFER, SYS_CTRL | Аппаратный таймер МК → периодический TX_FRAME |
| 0x22 | TX_STOP | — | — | SYS_CTRL (TRXOFF) | Стоп таймера + `dwt_forcetrxoff()` |
| 0x30 | RX_START | — | — | SYS_CTRL (RXENAB), DRX_PRETOC, RX_FWTO | `dwt_rxenable()` |
| 0x31 | RX_STOP | — | — | SYS_CTRL (TRXOFF) | `dwt_forcetrxoff()` |
| 0x40 | GET_SIGNAL_METRICS | — | RSSI i16 (dBm×100), SNR i16 (dB×100), RXPACC u16, FP_INDEX u16 | RX_FQUAL, RX_FINFO, AGC_STAT1 | Формулы RSSI/SNR — UM §4.7.1/4.7.2 |
| 0x41 | GET_CIR | offset u16, length u16 | I/Q пары int16 (N=length) | ACC_MEM, PMSC_CTRL0 | При чтении ACC_MEM отбросить 1-й dummy-байт. Макс. 1016 отсчётов |
| 0x50 | START_EXPERIMENT | — | поток: timestamp, RSSI, SNR, RXPACC | RX_FQUAL, RX_FINFO | Потоковая телеметрия по приёму пакетов |
| 0x51 | STOP_EXPERIMENT | — | — | — | Снять флаг потокового режима |
| 0x60 | TX_SWEEP | channel_start u8, channel_end u8, power_start u8, power_end u8, preamble_length u16 | результаты измерений RX | CHAN_CTRL, TX_POWER, TX_FCTRL | Перебор параметров + передача |
| 0x61 | DETECTOR_TEST | number_of_packets u16, tx_power_start u8, tx_power_end u8 | P_detect, P_false, RSSI, SNR | RX_FQUAL, RX_FINFO, ACC_MEM | Серия передач + статистика, ROC |

### 4.1 Важная трансляция параметров (wire → DecaDriver)

Протокол передаёт `preamble_length` как «сырое» число (u16, напр. 128), а
`dwt_config_t` ожидает **enum** `DWT_PLEN_64..DWT_PLEN_4096`. Аналогично
`data_rate`, `PRF`, `PAC` — на проводе это малые числа/коды, в драйвере —
именованные константы (`DWT_BR_110K/850K/6M8`, `DWT_PRF_16M/64M`,
`DWT_PAC8/16/32/64`). **Конфигуратор PHY обязан содержать таблицы соответствия и
валидацию** (UM Таблицы 16, 61). Невалидное значение → `INVALID_PARAM`.

### 4.2 Коды STATUS (полный набор, ТЗ п.5.2.4)

`0x00 OK` · `0x01 UNKNOWN_CMD` · `0x02 INVALID_PARAM` · `0x03 RADIO_BUSY` ·
`0x04 RADIO_ERROR` · `0x05 BUFFER_OVERFLOW` · `0x06 TIMEOUT` · `0x07 INTERNAL_ERROR`

### 4.3 Флаги ошибок DW1000 для контроля (SYS_STATUS, UM §7.2.17)

`TXFRS` (TX успех), `TXBERR` (ошибка буфера TX), `RXFCG/RXFCE` (приём ок/ошибка
CRC), `RXPHE` (ошибка PHR), `RXOVRR` (переполнение RX), `AFFREJ` (отклонение
фильтром кадров). При `RADIO_ERROR` → вернуть статус + receiver-only reset
(SOFTRESET в PMSC_CTRL0, UM §4.1.6).

---

## 5. Решение по драйверу (DecaDriver v4.0.6)

**Источник:** `dwm1001-examples-master/deca_driver/` (версия `0x040006`).
Остальные репозитории — `uwb-dw1000`, `uwb-core`, `uwb-apps` — это **другой стек**
(uwb-core поверх Apache Mynewt). Для нашего проекта (FreeRTOS + HAL) **не
используются**, остаются как справочный материал.

**Файлы драйвера (берём как есть):** `deca_device.c`, `deca_device_api.h`,
`deca_regs.h`, `deca_params_init.c`, `deca_param_types.h`, `deca_types.h`,
`deca_range_tables.c`, `deca_version.h`.

### 5.1 Поддержка нескольких чипов — решена «из коробки»

Драйвер хранит состояние в **массиве экземпляров**, а не в одной глобальной
переменной:

```c
static dwt_local_data_t dw1000local[DWT_NUM_DW_DEV];
static dwt_local_data_t *pdw1000local = dw1000local;
int dwt_setlocaldataptr(unsigned int index); // переключает активный экземпляр
```

По умолчанию `#define DWT_NUM_DW_DEV (1)` в `deca_device_api.h`.

**Что делаем:**
- МКС: `DWT_NUM_DW_DEV = 2`; Мини-стенд: `= 1`.
- Перед каждой транзакцией с конкретным чипом: под мьютексом SPI выбрать CS
  нужного модуля **и** вызвать `dwt_setlocaldataptr(idx)`. Паттерн доступа:
  `mutex_lock → set CS + dwt_setlocaldataptr(idx) → транзакция → mutex_unlock`.
- Вся сериализация — в `Radio_Manager_Task` (единственный владелец SPI к радио).

### 5.2 Платформенный слой — пишем сами (под STM32F411 / HAL)

DecaDriver платформенно-независим. Реализуем:
- `deca_spi.c` — `writetospi()` / `readfromspi()` через `HAL_SPI_TransmitReceive`.
  **На МКС выбирает И нужный SPI (hspi2 для M1 / hspi3 для M2), И линию CS** по
  активному устройству. На Мини-стенде — всегда hspi1. Текущее устройство берётся
  из `board_config` по индексу, синхронно с `dwt_setlocaldataptr(idx)`.
- `deca_sleep.c` — `deca_sleep()` (мс-задержка).
- `decamutexon()` / `decamutexoff()` — критическая секция / мьютекс FreeRTOS
  (защищает глобальный `pdw1000local`, см. §3 п.7).
- Аппаратный сброс по линии RST каждого модуля. **На МКС RST через BSS138 →
  инверсия уровня** (§3 п.8).
- EXTI на линиях IRQ → разблокировка `Radio_Manager_Task`
  (`xSemaphoreGiveFromISR` / `xQueueSendFromISR`). На МКС внимание к перекрёстной
  нумерации: IRQ1↔M2, IRQ2↔M1.

> Папка `deca_driver/port/` в примере — порт под Nordic nRF52 (плата DWM1001).
> Используем как **референс**, переписываем под STM32F4.

---

## 6. Что берём из самодельной библиотеки (verbatim/с правками)

| Компонент | Решение |
|---|---|
| Уровень протокола (`protocol.c/.h`): парсер, CRC8, диспетчер, построитель ответов | **Сохранить**, адаптировать под DecaDriver |
| Отладочная консоль (`debug_console.c/.h`) | **Только мини-стенд**, USART2 (huart2, PA2/PA3, ST-LINK VCP). На МКС консоли нет — всё через MATLAB |
| Хранилище настроек (M95080 EEPROM, SPI1) | **Новый модуль** `settings_storage` (antenna delay, PHY-профиль, серийник; сигнатура+CRC). Только МКС |
| USB CDC (`usbd_cdc_if.c/.h`), кольцевой буфер | **Сохранить** |
| Структура задач FreeRTOS, очереди/семафоры | **Сохранить** (см. раздел 8) |
| `dw1000_driver.c/.h` (низкий уровень) | **Заменить** на DecaDriver v4.0.6 |
| `radio_manager.c/.h` | **Переписать** под вызовы DecaDriver |

---

## 7. Открытые вопросы (Open Questions)

**Закрыто:**
- ~~OQ-1 (CRC area)~~ → CRC **без SYNC**, MATLAB-пример из документа считаем
  ошибочным, выдаём исправленный (раздел 12).
- ~~OQ-2 (Pinout МКС)~~ → извлечён из схемы (см. §2.1).
- ~~OQ-3 (Pinout Мини-стенд)~~ → SPI1: CS-PA4 SCK-PB3 MISO-PB4 MOSI-PB5 IRQ-PB0 RST-PC0.
- ~~OQ-4 (Роль модуля Мини-стенда)~~ → один модуль, для учебных задач достаточно.
- ~~OQ-5 (HSE кварц)~~ → МКС 24 МГц, Мини-стенд 8 МГц.
- ~~OQ-6 (USB Host)~~ → внешний USB = MATLAB/протокол; отладочный USB = дебаггер +
  DEBUG Console.

**Закрыто (раунд 2):**
- ~~OQ-7 (EEPROM)~~ → M95080 (SPI1) = хранилище настроек: antenna delay, PHY-профиль,
  серийник. Реализуем модуль `settings_storage` (сигнатура + CRC). Закрывает ТЗ п.5.5.
- ~~OQ-8 (DEBUG Console)~~ → через **ST-LINK VCP = USART2 (PA2/PA3)**, не отдельный
  UART/CDC. Самодельную консоль перецепить на huart2.
- ~~OQ-10 (роли M1/M2)~~ → **фиксированные**: Источник (TX тестовых сигналов) +
  Индикатор (подтверждение наличия сигнала в эфире). Привязка статическая в
  `board_config.h`. Соответствие «M1/M2 ↔ роль» уточнить на железе при bring-up
  (если перепутано — поменять одной строкой). LED: TX-красный, RX-жёлтый.

- ~~OQ-12 (Консоль на МКС)~~ → **МКС без физической DEBUG Console.** Всё
  управление/диагностика — через MATLAB (USB CDC). Отладка МКС — через SWD
  (опц. printf по ITM/SWO). Физическая консоль (USART2 PA2/PA3, ST-LINK VCP) —
  только на мини-стенде.

**Открыто:**
- **OQ-9 (Активный уровень RST через BSS138):** проверить на железе. Гипотеза:
  лог.1 на GPIO МК → MOSFET открыт → RSTn к земле → **сброс активен высоким**.
  В коде активный уровень = параметр `DW_RST_ACTIVE_LEVEL` в `board_config.h`.
- **OQ-11 (MATLAB-скрипт заказчика):** желателен как эталон поведения протокола.

---

## 8. Структура задач FreeRTOS (целевая)

| Задача | Приоритет | Функция |
|---|---|---|
| USB_Command_Task | 2 | Приём байт из USB CDC, сборка пакета, CRC, декод, → очередь команд |
| Radio_Manager_Task | 3 | Владелец SPI к радио. Исполняет команды, обрабатывает IRQ-события обоих чипов |
| Periodic_TX_Task | 2 | TX_PERIODIC по таймеру (`vTaskDelayUntil`) |
| Diagnostic_Stream_Task | 1 | Потоковая телеметрия в режиме START_EXPERIMENT |
| USB_Transmit_Task | 2 | Отправка буферов из `xUSB_TxQueue` в USB CDC |
| DEBUG_Console_Task | 1 | Отладочная консоль UART (115200 8N1) |

Очереди: `xRadioCommandQueue`, `xRadioEventQueue`, `xUSB_TxQueue`,
`xConsoleCommandQueue`. Семафоры: `xTxCompleteSemaphore`, `xRxCompleteSemaphore`.

---

## 9. Целевая архитектура прошивки (слои)

```
МКС/Мини-стенд (прошивка)
├── Protocol Layer        — парсер, CRC8, диспетчер CMD_ID, построитель ответов
├── DW1000 Control Layer  — обёртка над DecaDriver:
│   ├── INIT / RESET_RADIO
│   ├── PHY-конфигуратор (SET_PHY_CONFIG/SET_TX_POWER) + трансляция wire→enum
│   ├── TX (TX_FRAME/TX_PERIODIC/TX_STOP)
│   ├── RX (RX_START/RX_STOP)
│   └── Диагностика (GET_SIGNAL_METRICS/GET_CIR)
├── Experiment Layer      — START_EXPERIMENT, TX_SWEEP, DETECTOR_TEST
├── Settings Storage      — M95080 EEPROM (SPI1): antenna delay, PHY-профиль, серийник [только МКС]
├── DecaDriver v4.0.6     — фирменный низкий уровень (НЕ модифицируем)
├── Platform port         — deca_spi/deca_sleep/mutex/RST/EXTI (наш, под F411)
└── System Layer          — FreeRTOS, таймеры, IWDG, USB CDC, board_config.h
```

---

## 10. Тайминги и требования надёжности (ТЗ)

- Отклик: ≤100 мс (конфигурация), ≤10 мс (команды управления передачей).
- IWDG обязателен; нет «зависаний».
- 24 ч непрерывной работы TX/RX без сбоев (критерий приёмки).
- Загрузка LDE-микрокода при INIT и после пробуждения из SLEEP — до включения RX
  (нужно для точных меток времени, UM Table 4).
- Delayed TX/RX через `DX_TIME` — для точного управления временем.

---

## 11. Следующие шаги

1. ~~Распиновка обеих плат~~ → получена, занести в `board_config.h` (МКС + Nucleo).
2. Каркас платформенного слоя (`deca_spi`/`sleep`/`mutex`/RST/EXTI). Начать с
   **Мини-стенда (Nucleo, 1 чип, SPI1)** — проще для bring-up, чем МКС с двумя
   шинами.
3. Поднять SPI + чтение `DEV_ID` (ожидаем `0xDECA0130`) — первая проверка связи.
4. Слой протокола (PING/INIT/GET_STATUS) → стабильный обмен с MATLAB.
5. Перенести на МКС (2 чипа, SPI2/SPI3, инверсный RST, перекрёстные IRQ).
6. Конфигурация и передача → приём и диагностика → автоматизация (по этапам ТЗ).

---

## 12. Исправленный пример CRC8 для MATLAB (выдать заказчику)

Документ протокола содержит **ошибочный** пример: там `crc8()` вызывается от
массива, включающего SYNC (`0xAA 0x55`). По принятому решению (§3 п.1) CRC
считается **только по LEN + CMD_ID + PARAMS**. Корректный вариант:

```matlab
function crc = crc8(data)
% CRC-8, полином 0x07, init 0x00, без рефлексии, без xorout
    crc = uint8(0);
    for k = 1:numel(data)
        crc = bitxor(crc, uint8(data(k)));
        for b = 1:8
            if bitand(crc, 0x80)
                crc = bitand(bitshift(crc,1), 0xFF);
                crc = bitxor(crc, 0x07);
            else
                crc = bitand(bitshift(crc,1), 0xFF);
            end
        end
    end
end

% Пример: SET_TX_POWER(15)
SYNC   = uint8([0xAA 0x55]);
LEN    = uint8(0x02);            % CMD_ID + PARAMS = 2 байта
CMD_ID = uint8(0x11);
PARAMS = uint8(0x0F);

crc_input = [LEN CMD_ID PARAMS]; % <-- БЕЗ SYNC
crc = crc8(crc_input);

packet = [SYNC LEN CMD_ID PARAMS crc];
s = serialport("COM5", 115200);
write(s, packet, "uint8");
```

Прошивка проверяет CRC по тем же байтам (LEN..конец PARAMS). Ответ МКС: CRC по
LEN + STATUS + DATA.

---

*Документ будет дополняться по мере поступления данных (особенно распиновки и
MATLAB-скрипта заказчика).*
