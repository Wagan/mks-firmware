# TASK: реализация SET_TX_POWER (0x11)

**Дата:** 2026-07-17
**Автор:** Vagan Sarukhanov
**Исполнитель:** СС (Claude Code)

Реализовать команду `SET_TX_POWER (0x11)` — регулятор мощности передатчика
(**вариант A: ручной режим**). Одна ручка `power_level u8` монотонно меняет
выходную мощность DW1000 на TX-модуле. Проверяется готовым loopback M1→M2:
изменение `power_level` сдвигает `RX_LEVEL` в `metrics`.

**Не трогать** vendor (`Drivers/decadriver/`). Не трогать `SET_PHY_CONFIG` и прочие
команды. Все правки — только в `App/protocol/protocol.c` (обработчик, хелпер,
состояние, регистрация). `protocol.h` менять не нужно (`CMD_SET_TX_POWER = 0x11`
уже в enum). `main.c` не трогать (команда синхронная, отдельный Poll не нужен).

## Опора (проверено, НЕ по памяти)

- Образец параметризованной команды — `HandleSET_PHY_CONFIG` (protocol.c): разбор
  params → валидация → `deca_port_select_device` → запись в DW1000 → сохранение в
  `dw_dev_state` → OK.
- API: `void dwt_configuretxrf(dwt_txconfig_t*)`, `dwt_txconfig_t { uint8 PGdly;
  uint32 power; }`, `void dwt_setsmarttxpower(int enable)` — все void.
- `TC_PGDELAY_CH1..CH7` — в `deca_regs.h` (уже включён в protocol.c, строка 20):
  CH1 0xC9, CH2 0xC2, CH3 0xC5, CH4 0x95, CH5 0xC0, CH7 0x93.
- Раскладка `power` (UM §7.2.31): один power-октет = coarse(3dB×7, биты7:5) +
  fine(0.5dB×32, биты4:0). Ручной режим (`dwt_setsmarttxpower(0)` → DIS_STXP=1):
  значимы TXPOWPHR(15:8) и TXPOWSD(23:16); программируем одинаково → дублируем
  октет во все 4 байта.
- `dw_dev_state_t` содержит поле `channel` (заполняется SET_PHY_CONFIG).

## Зафиксированные решения

- `power_level` = число 0.5-dB шагов ослабления от максимума: `octet = 0xFF - level`,
  дубль во все 4 октета `power`.
- `POWER_LEVEL_MAX = 0xDF` — не даём coarse уйти в 000 (нижний октет не ниже 0x20).
- Ручной режим: `dwt_setsmarttxpower(0)` перед `dwt_configuretxrf`.
- PGdly: `map_pgdelay(channel)` из `TC_PGDELAY_CH*` (vendor-константы).
- Область: **только `DW_TX_SOURCE_DEV`**.
- **Требует предварительного `SET_PHY_CONFIG`** (иначе `channel`=0 → `map_pgdelay`
  вернёт false → `STATUS_RADIO_ERROR`).
- Ответ: DATA = применённый `power` (u32 LE, 4 байта).

---

## Шаг 1. `App/protocol/protocol.c` — хелпер `map_pgdelay`

Добавить рядом с прочими `map_*` (после `map_prf`/`map_pac`, секция трансляции):

```c
/* PGdly по каналу из vendor-констант deca_regs.h (TC_PGDELAY_CH*).
 * Значения рекомендованы DecaWave; используются штатным DecaDriver — спектр
 * не портим, регулируем только уровень (power). channel — из dw_dev_state
 * (заполнен SET_PHY_CONFIG). Канал вне {1..5,7} → false. */
static bool map_pgdelay(uint8_t channel, uint8_t* out)
{
    switch (channel) {
        case 1: *out = TC_PGDELAY_CH1; return true;   /* 0xC9 */
        case 2: *out = TC_PGDELAY_CH2; return true;   /* 0xC2  (Mode 3) */
        case 3: *out = TC_PGDELAY_CH3; return true;   /* 0xC5 */
        case 4: *out = TC_PGDELAY_CH4; return true;   /* 0x95 */
        case 5: *out = TC_PGDELAY_CH5; return true;   /* 0xC0 */
        case 7: *out = TC_PGDELAY_CH7; return true;   /* 0x93 */
        default: return false;
    }
}
```

## Шаг 2. `App/protocol/protocol.c` — `#define POWER_LEVEL_MAX`

Рядом с прочими протокольными `#define` (там же, где TX_FRAME_MAX/TX_WAIT_GUARD):

```c
/* Верхняя граница power_level для SET_TX_POWER (0x11). octet = 0xFF - level;
 * ограничиваем так, чтобы coarse-gain (биты 7:5) не опускался до 000 (особый
 * случай DA-off, UM §7.2.31.1): нижний октет не ниже 0x20 → level <= 0xDF. */
#define POWER_LEVEL_MAX  0xDF
```

## Шаг 3. `App/protocol/protocol.c` — состояние (диагностика)

В секции состояния (рядом с `tx_periodic_*` / `dw_dev_state`) добавить:

```c
static uint8_t  tx_power_level;   /* последний применённый power_level (0 = не задан) */
static uint32_t tx_power_reg;     /* последнее записанное значение регистра power */
```

В `PROTOCOL_Init()` (рядом с прочими инициализациями) добавить:

```c
    tx_power_level = 0;
    tx_power_reg   = 0;
```

## Шаг 4. `App/protocol/protocol.c` — обработчик `HandleSET_TX_POWER`

Добавить рядом с прочими обработчиками (например, после `HandleSET_PHY_CONFIG`):

```c
/**
 * @brief SET_TX_POWER (0x11). Ручная регулировка мощности передатчика (вариант A).
 *        Параметры (wire): power_level u8 — число 0.5-dB шагов ослабления от макс.
 *        octet = 0xFF - level, дублируется во все 4 октета регистра TX_POWER.
 *        Включает ручной режим (dwt_setsmarttxpower(0), DIS_STXP=1), затем
 *        dwt_configuretxrf. PGdly — по текущему каналу (TC_PGDELAY_CH*).
 *        Применяется на DW_TX_SOURCE_DEV. ТРЕБУЕТ предварительного SET_PHY_CONFIG
 *        (нужен channel; иначе RADIO_ERROR). Ответ DATA: применённый power (u32 LE).
 */
static ResponseStatus HandleSET_TX_POWER(const uint8_t* params, uint8_t params_len,
                                         uint8_t** out_data, uint8_t* out_len)
{
    *out_len = 0;

    if (params_len < 1) return STATUS_INVALID_PARAM;
    uint8_t level = params[0];
    if (level > POWER_LEVEL_MAX) return STATUS_INVALID_PARAM;

    if (!dw_dev_state[DW_TX_SOURCE_DEV].initialized) return STATUS_RADIO_ERROR;

    uint8_t pgdly;
    if (!map_pgdelay(dw_dev_state[DW_TX_SOURCE_DEV].channel, &pgdly))
        return STATUS_RADIO_ERROR;   /* channel не задан (нет SET_PHY_CONFIG) или вне диапазона */

    if (deca_port_select_device(DW_TX_SOURCE_DEV) != DWT_SUCCESS)
        return STATUS_RADIO_ERROR;

    uint8_t o = (uint8_t)(0xFF - level);
    dwt_txconfig_t cfg;
    cfg.PGdly = pgdly;
    cfg.power = ((uint32_t)o << 24) | ((uint32_t)o << 16) |
                ((uint32_t)o << 8)  |  (uint32_t)o;

    dwt_setsmarttxpower(0);      /* ручной режим (DIS_STXP=1) */
    dwt_configuretxrf(&cfg);     /* void */

    tx_power_level = level;
    tx_power_reg   = cfg.power;

    /* DATA = применённый power (u32 LE) */
    static uint8_t buf[4];
    buf[0] = (uint8_t)(cfg.power        & 0xFF);
    buf[1] = (uint8_t)((cfg.power >> 8)  & 0xFF);
    buf[2] = (uint8_t)((cfg.power >> 16) & 0xFF);
    buf[3] = (uint8_t)((cfg.power >> 24) & 0xFF);
    *out_data = buf;
    *out_len  = 4;
    return STATUS_OK;
}
```

## Шаг 5. `App/protocol/protocol.c` — регистрация

В `PROTOCOL_RegisterAllHandlers()` добавить (рядом с SET_PHY_CONFIG):

```c
    PROTOCOL_RegisterHandler(CMD_SET_TX_POWER,       HandleSET_TX_POWER);
```

## Шаг 6. Проверка и коммит

1. Syntax-check компилятором своего окружения (сборку/линк-билд делает владелец —
   НЕ ты). Убедиться: нет предупреждений о неиспользуемых переменных; `bool`,
   `dwt_txconfig_t`, `dwt_configuretxrf`, `dwt_setsmarttxpower`, `TC_PGDELAY_CH*`
   резолвятся (bool — как в существующих map_*; остальное — deca_device_api.h /
   deca_regs.h, уже включены).
2. git commit + push. Сообщение:
   `feat(protocol): SET_TX_POWER (0x11) manual TX power (variant A) via dwt_configuretxrf`
3. Отчитаться: какие строки изменены в protocol.c и результат syntax-check.

## Границы задачи (что НЕ делать)

- Не трогать `Drivers/decadriver/` (vendor), `SET_PHY_CONFIG`, RX/TX-обработчики,
  `main.c`, `protocol.h`.
- Не менять хост-скрипты (`tools/`) — их готовит архитектор отдельно.
- Не собирать и не деплоить — только правки protocol.c + syntax-check + commit/push.
- Не реализовывать пресеты Table 19/20 (вариант B) и smart-boost (вариант C) —
  это отдельные будущие заходы.
