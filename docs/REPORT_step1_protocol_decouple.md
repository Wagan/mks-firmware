# Отчёт: Шаг 1 — развязка ядра протокола (bare-metal)

**Дата:** 2026-07-16
**Этап:** рефакторинг слоя протокола под DecaDriver, bare-metal (без FreeRTOS).
**Итог:** ядро протокола развязано от удалённого драйвера и RTOS; сборка чистая.
**Статус:** ожидает сверки архитектором + включения `App/protocol` в сборку CubeIDE.

---

## Что изменено (4 файла)

| Файл | Что |
|---|---|
| `App/protocol/protocol.h` | Убран номер версии из шапки (CODE_STYLE), добавлено примечание о транспорте/CRC. Enum'ы и прототипы без изменений. |
| `App/protocol/protocol.c` | **Полностью переписан:** убраны `dw1000_driver.h`, `radio_defs.h`, `debug_console.h`, все FreeRTOS-глобалы, `malloc`. Отправка через `CDC_Transmit_FS`. Исправлены 2 бага. Все `Handle*` удалены, `PROTOCOL_RegisterAllHandlers()` пуст (обработчики — Шаг 2). |
| `Core/Src/main.c` | USER CODE-хуки: include + `PROTOCOL_Init()`/`PROTOCOL_RegisterAllHandlers()`. |
| `USB_DEVICE/App/usbd_cdc_if.c` | USER CODE-хуки: include + подача байт в парсер. |

## Исправленные баги

1. **Порядок SYNC** приведён к эталону MATLAB: на проводе `0xAA` затем `0x55`
   (было наоборот — парсер не поймал бы пакеты MATLAB). Исправлено и в парсере,
   и в построителе ответа.
2. **Область CRC запроса**: теперь строго `LEN+CMD_ID+PARAMS` без SYNC (был
   off-by-one + захват байта SYNC). Покрытие вынесено в макрос
   `PROTOCOL_CRC_COVERS_SYNC = 0` (страховка, §3.1 handoff).

## Проверка сборки

Компилятор `arm-none-eabi-gcc`, флаги/инклюды взяты из `Debug/**/subdir.mk`:

```
protocol.c      -fsyntax-only  ->  EXIT 0, 0 warnings
usbd_cdc_if.c   -fsyntax-only  ->  EXIT 0
main.c          -fsyntax-only  ->  EXIT 0
```

Ссылок на удалённый `dw1000_driver` и на FreeRTOS больше нет;
`CDC_Transmit_FS` / `PROTOCOL_ProcessByte` резолвятся, include-пути
(`-I../App/protocol`) присутствуют в обоих генерённых модулях.

> Примечание: это точечная проверка синтаксиса (`-fsyntax-only`) старым
> `arm-none-eabi-gcc` из PATH. Полноценный линк-билд — в CubeIDE штатным
> тулчейном (GNU Tools for STM32 12.3) после включения `App/protocol` в сборку.

## Точные вставки в USER CODE (что и куда)

**`Core/Src/main.c` → `USER CODE BEGIN Includes`:**
```c
#include "protocol.h"
```

**`Core/Src/main.c` → `USER CODE BEGIN 2`** (после `bringup_read_devids()`):
```c
  PROTOCOL_Init();
  PROTOCOL_RegisterAllHandlers();
```

**`USB_DEVICE/App/usbd_cdc_if.c` → `USER CODE BEGIN INCLUDE`:**
```c
#include "protocol.h"
```

**`USB_DEVICE/App/usbd_cdc_if.c` → `CDC_Receive_FS`, `USER CODE BEGIN 6`**
(перед `USBD_CDC_SetRxBuffer`):
```c
  for (uint32_t i = 0; i < *Len; i++) {
    PROTOCOL_ProcessByte(Buf[i]);
  }
```

Всё — внутри BEGIN/END. Вне USER CODE-секций ничего не изменено.

## ⚠️ Действие вне USER CODE (агент сам не делает — по правилу проекта)

Чтобы проектный билд в CubeIDE подхватил новый код, надо **вернуть
`App/protocol` в сборку**. Сейчас в `.cproject`: `excluding="console|protocol"`.
Это управляется CubeIDE (не CubeMX, не USER CODE), поэтому — инструкцией:

> В CubeIDE: правый клик на папке **`App/protocol`** →
> **Resource Configurations → Exclude from Build…** → снять галку для Debug
> (и Release, если есть) → Apply.
> `App/console` оставить исключённым (`debug_console` тянет старый драйвер и на
> МКС не нужен).

Альтернатива: одноразовая правка `.cproject` (`"console|protocol"` → `"console"`)
— только с явного разрешения (файл вне USER CODE).

## Найденные зависимости старого кода (справка для Шага 2+)

Старый `protocol.c` ссылался на удалённый `dw1000_driver`:
- тип `DW1000_Device` (абстракция чипа) → заменяется на выбор через
  `deca_port_select_device(idx)` + `dwt_*`;
- функции `DW1000_Init/SoftReset/SetPhyConfig/SetTxPower/ReadRegister[32]` →
  `dwt_initialise/dwt_softreset/dwt_configure/dwt_settxpower/dwt_read*`;
- константы регистров `DW1000_RX_FINFO/RX_FQUAL/RX_TIME/ACC_MEM` → `deca_regs.h`;
- FreeRTOS: очереди/семафоры/`malloc` — вся модель `radio_manager` отложена до
  ввода FreeRTOS (Шаг 4).

`radio_defs.h` остаётся исключённым из сборки протокола — это RTOS-модель для
будущего `radio_manager`.

## Что дальше — Шаг 2

Обработчики `PING` / `INIT` / `GET_STATUS` синхронно поверх `deca_port` +
DecaDriver, с лёгким кэшем состояния устройств (`channel/data_rate/preamble_len/
prf/initialized`). Остальные 14 команд — заглушка `STATUS_UNKNOWN_CMD`.

## Открытые вопросы к архитектору

1. Разрешить одноразовую правку `.cproject` для включения `App/protocol`, или
   сделаешь через CubeIDE GUI сам?
2. Подтверждаешь переход к Шагу 2 в описанном объёме (только PING/INIT/GET_STATUS)?
