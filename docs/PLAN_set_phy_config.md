# План: SET_PHY_CONFIG (0x10) — настройка PHY под приём EVK1000 Mode 3

**Дата:** 2026-07-16
**Цель:** реализовать `SET_PHY_CONFIG`, настроить приёмный модуль МКС на параметры
EVK1000 **Mode 3**, чтобы затем принять сигнал EVK в эфире.
**Статус:** план на согласование. Правок в коде нет. Vendor не трогаем.
**Опора:** `Drivers/decadriver/deca_device_api.h` (точные enum), `dwt_configure()`
в `deca_device.c`, `docs/PROTOCOL_SPEC.md` §5/§6.

**EVK Mode 3:** channel 2, PRF 64M, preamble length 1024, preamble code 9,
PAC 32, data rate 110 kbps, нестандартный SFD.

---

## 1. Wire-формат (PROTOCOL_SPEC §6) и раскладка

`SET_PHY_CONFIG` params = **7 байт, БЕЗ target** (источник истины — §6):

| Смещение | Поле | Тип | Mode 3 |
|---|---|---|---|
| 0 | channel | u8 | 2 |
| 1 | data_rate | u8 (код) | 0 (110k) |
| 2..3 | preamble_length | u16 LE | 1024 |
| 4 | preamble_code | u8 | 9 |
| 5 | PRF | u8 | 64 |
| 6 | PAC_size | u8 | 32 |

`LEN = 1(CMD) + 7 = 8`. `params_len < 7` → `INVALID_PARAM`.

## 2. Трансляция wire→enum (значения точно из deca_device_api.h)

**preamble_length (u16 симв.) → `txPreambLength`:**

| raw | enum | hex |
|---|---|---|
| 64 | DWT_PLEN_64 | 0x04 |
| 128 | DWT_PLEN_128 | 0x14 |
| 256 | DWT_PLEN_256 | 0x24 |
| 512 | DWT_PLEN_512 | 0x34 |
| 1024 | DWT_PLEN_1024 | 0x08 |
| 1536 | DWT_PLEN_1536 | 0x18 |
| 2048 | DWT_PLEN_2048 | 0x28 |
| 4096 | DWT_PLEN_4096 | 0x0C |

**PRF (u8, МГц) → `prf`:** 16 → DWT_PRF_16M (1), 64 → DWT_PRF_64M (2).

**PAC_size (u8, симв.) → `rxPAC`:** 8→DWT_PAC8(0), 16→DWT_PAC16(1),
32→DWT_PAC32(2), 64→DWT_PAC64(3).

**data_rate (u8, КОД) → `dataRate`:** 0→DWT_BR_110K, 1→DWT_BR_850K, 2→DWT_BR_6M8.

> ⚠️ **Уточнение спеки:** `data_rate` — это **код 0/1/2**, а не значение в кбит/с.
> Причина: 850 и 6800 не помещаются в u8. В §6 сказано «сырые числа», но для
> data_rate это невозможно. Предлагаю зафиксировать в PROTOCOL_SPEC: data_rate —
> код {0:110k, 1:850k, 2:6.8M}. (Совпадает с DWT_BR_*, но таблицу задаём явно,
> не завязываясь на vendor-числа.)

**channel (u8):** валидные {1,2,3,4,5,7} (комментарий к `dwt_config_t.chan`).
Пропускаем как есть после валидации.

**preamble_code (u8):** валидный диапазон 1..24. `txCode = rxCode = preamble_code`.
(Строго: PRF16→коды 1..8, PRF64→9..24; на этом шаге валидируем широко 1..24 +
заметка. Mode 3 = 9, валиден для PRF64.)

Любое значение вне таблиц → **`INVALID_PARAM`** (ничего не пишем в чип).

## 3. ВОПРОС ПО SFD (nsSFD) — рекомендация

Протокол v1.3 не передаёт признак нестандартного SFD, а Mode 3 (110k) требует
`nsSFD=1`. `dwt_configure()` honored поле `nsSFD` (deca_device.c:539).

**Рекомендация (принять): прошивка сама ставит nsSFD по правилу data_rate:**
```
config.nsSFD = (dataRate == DWT_BR_110K) ? 1 : 0;
```
Обоснование: это конвенция DecaWave/EVK — для 110 kbps используется нестандартный
SFD (лучше чувствительность), для 850k/6.8M — стандартный. Совпадает с Mode 3
(110k → nsSFD=1). Протокол НЕ меняем, обратная совместимость сохраняется.

Альтернатива (не рекомендую сейчас): расширить протокол байтом `nsSFD`. Оставить
как задел на будущее, если понадобится ручное управление независимо от data_rate.

## 4. Прочие поля dwt_config_t

- **`sfdTO`** (SFD timeout): ставим **0** → драйвер подставит `DWT_SFDTOC_DEF`
  (0x1041), см. deca_device.c:528-530. Большой таймаут безопасен для первого
  приёма (не даёт преждевременного preamble-timeout). Оптимизацию по формуле
  `preamble + 1 + SFD_len − PAC` оставляем как follow-up после первого приёма.
- **`phrMode`**: `DWT_PHRMODE_STD` (0) — стандартный PHR (Mode 3 не extended).

## 5. Куда применять конфиг + кэш (вопрос #4)

Протокол не содержит target. Для цели «принять EVK» нужен RX/Индикатор-модуль.

**Рекомендация: применять конфиг ко ВСЕМ модулям** (цикл по `DW_DEVICE_COUNT`,
как INIT). Обоснование:
- гарантированно настраивает RX-модуль независимо от того, какой физический
  модуль сейчас Индикатор (соответствие M1/M2↔роль ещё уточняется на железе);
- конфигурирование второго (TX) модуля тем же профилем безвредно (TX пока не
  используется);
- протокол не меняем.

**Кэш `dw_dev_state[i]`** после успешного `dwt_configure` на модуле i: пишем
`channel`, `data_rate` (код), `preamble_len` (raw u16), `prf` (raw). GET_STATUS
уже читает эти поля по M1 → сразу отразит применённый профиль. (PAC/preamble_code
GET_STATUS не возвращает; при желании добавим в структуру позже.)

**Будущее:** когда понадобятся РАЗНЫЕ профили на модулях (TX Mode X / RX Mode Y),
расширим протокол полем target — отдельным согласованным шагом.

## 6. Контекст исполнения (тот же паттерн, что INIT)

`dwt_configure()` вызывает `_dwt_configlde()` → `deca_sleep(1)` (HAL_Delay). Значит
handler исполняется в **main loop** (через `PROTOCOL_PollRx`), не в ISR — это уже
обеспечено архитектурой Варианта B. SPI держим **медленным** (LDE-загрузка требует
<3 МГц; после INIT он и так slow). Порядок в handler'е:
`deca_port_select_device(i)` → `dwt_configure(&cfg)` → запись в кэш.

## 7. Эскиз обработчика (псевдокод, не финал)

```c
static ResponseStatus HandleSET_PHY_CONFIG(const uint8_t* p, uint8_t len,
                                           uint8_t** out_data, uint8_t* out_len) {
    (void)out_data; *out_len = 0;
    if (len < 7) return STATUS_INVALID_PARAM;

    uint8_t  channel   = p[0];
    uint8_t  data_rate = p[1];
    uint16_t plen_raw  = p[2] | (p[3] << 8);
    uint8_t  pcode     = p[4];
    uint8_t  prf_raw   = p[5];
    uint8_t  pac_raw   = p[6];

    dwt_config_t cfg;
    if (!map_channel(channel, &cfg.chan))         return STATUS_INVALID_PARAM;
    if (!map_datarate(data_rate, &cfg.dataRate))  return STATUS_INVALID_PARAM;
    if (!map_plen(plen_raw, &cfg.txPreambLength)) return STATUS_INVALID_PARAM;
    if (!map_prf(prf_raw, &cfg.prf))              return STATUS_INVALID_PARAM;
    if (!map_pac(pac_raw, &cfg.rxPAC))            return STATUS_INVALID_PARAM;
    if (pcode < 1 || pcode > 24)                  return STATUS_INVALID_PARAM;
    cfg.txCode = cfg.rxCode = pcode;
    cfg.nsSFD   = (cfg.dataRate == DWT_BR_110K) ? 1 : 0;   // авто-SFD (§3)
    cfg.phrMode = DWT_PHRMODE_STD;
    cfg.sfdTO   = 0;                                        // → DWT_SFDTOC_DEF

    for (int i = 0; i < DW_DEVICE_COUNT; i++) {
        if (deca_port_select_device(i) != DWT_SUCCESS) return STATUS_RADIO_ERROR;
        dwt_configure(&cfg);                               // void, без кода возврата
        dw_dev_state[i].channel      = channel;
        dw_dev_state[i].data_rate    = data_rate;
        dw_dev_state[i].preamble_len = plen_raw;
        dw_dev_state[i].prf          = prf_raw;
    }
    return STATUS_OK;
}
```
> Примечание: `dwt_configure()` — `void` (не возвращает статус). Валидация — на
> нашей стороне ДО вызова; поэтому «плохие» значения не доходят до чипа. Таблицы
> `map_*` — маленькие switch/справочники в protocol.c.

## 8. Порядок работ (малые шаги)

1. Реализовать `map_*` + `HandleSET_PHY_CONFIG`, зарегистрировать `CMD_SET_PHY_CONFIG`.
2. syntax-check → твой линк-билд в CubeIDE.
3. Хост: добавить в `tools/mks_protocol.py` метод `set_phy_config(...)` + тест
   `mks_setphy_test.py` (шлёт Mode 3, ждёт OK; затем GET_STATUS → проверяет кэш).
4. Проверка на железе: SET_PHY_CONFIG(Mode3) → OK, GET_STATUS отражает профиль.
5. Обновить PROTOCOL_SPEC (data_rate-код, таблицы трансляции, авто-nsSFD, статус
   0x10 → ✅) и PROJECT_HANDOFF.
6. Следующий этап (отдельно): `RX_START` + реальный приём сигнала EVK.

## 9. Решения на согласование с архитектором

1. **nsSFD:** принять авто-правило `110k→nsSFD=1` (рекоменд.) или расширять протокол?
2. **data_rate = код 0/1/2** (не кбит/с) — согласовать и зафиксировать в спеке.
3. **Применять на ВСЕ модули** (рекоменд.) или только на фиксированный RX-индекс?
4. **sfdTO=0 → драйверный дефолт** на первом шаге (рекоменд.), оптимизация позже?
5. **preamble_code**: широкая валидация 1..24 сейчас, или сразу PRF-зависимая
   (PRF16:1..8 / PRF64:9..24)?
