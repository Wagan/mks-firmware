# RECON — TX-мощность и дальность (§15.1 диагностика)

**Тип:** разведка по коду/регистрам (правки НЕ вносились). **Дата:** 2026-07-22.
**Повод:** плохая дальность (устойчиво 2–3 м, человек рвёт связь), RSSI на близкой
дистанции ~−78 dBm (низко). Антенный тракт исключён (тест со снятыми ВЧ-джамперами не
изменил дальность). Подозрение №1 — TX-мощность. **Источник:**
`App\protocol\protocol.c`, `Drivers\decadriver\deca_device.c`, `deca_regs.h`,
`tools\*.py`. Только факты/регистры. Расхождения с UM/DecaRanging по ch2 — явно.

---

## ГЛАВНЫЙ ВЫВОД (кандидат №1, подтверждён по коду)
**Станция передаёт на РЕГИСТРАХ TX-тракта в состоянии hardware-reset, НЕ
откалиброванных под канал 2:** ни `TX_POWER`, ни `TC_PGDELAY` (PG_DELAY) в наших
рабочих сценариях **никогда не записываются**. Единственное место, где они пишутся —
`dwt_configuretxrf`, а её зовёт только `SET_TX_POWER` (0x11), которую **станция не
вызывает** (см. §6). `dwt_configure` (из `SET_PHY_CONFIG`) их НЕ трогает (§3–4).
Итог: TX_POWER и PG_DELAY = сбросовый дефолт, не ch2-значения DecaRanging.

---

## 1. Что реально ставит SET_TX_POWER (0x11)
`HandleSET_TX_POWER` (`protocol.c:1075–1113`):
- Вход `power_level u8`; **кламп/валидация:** `level > POWER_LEVEL_MAX(0xDF)` →
  `INVALID_PARAM` (`:1082`, `POWER_LEVEL_MAX` = `0xDF`, `:970`); требует INIT + channel
  (иначе `RADIO_ERROR`).
- **Маппинг в регистр (`:1094–1098`):** `o = 0xFF − level`; `cfg.power = o|o<<8|o<<16|
  o<<24` (один октет во все 4 байта регистра `TX_POWER` 0x1E). `cfg.PGdly =
  map_pgdelay(channel)`.
- Пишет: `dwt_setsmarttxpower(0)` (DIS_STXP=1, ручной режим) + `dwt_configuretxrf(&cfg)`
  (`:1100–1101`). Ответ DATA = применённый `power` (u32 LE).
- Диапазон октета: `level=0` → `o=0xFF` (макс. аттенюация = мин. мощность);
  `level=0xDF` → `o=0x20` → `power=0x20202020` (макс. по нашему клампу).
- Раскладка октета (наша заметка `docs\TASK_set_tx_power.md`, UM §7.2.31): coarse
  (3 dB × 7, биты 7:5) + fine (0.5 dB × 32, биты 4:0).

## 2. Дефолтная TX-мощность (что стоит без SET_TX_POWER)
- **`TX_POWER` (0x1E) не пишется НИ в `dwt_initialise`, НИ в `dwt_configure`.** Во всём
  `deca_device.c` запись `TX_POWER_ID` — **единственная**, в `dwt_configuretxrf`
  (`deca_device.c:422`). ⇒ у станции TX_POWER = **hardware-reset дефолт**.
- В `deca_regs.h:534` есть `TX_POWER_MAN_DEFAULT = 0x0E080222` (рекомендуемый vendor
  «ручной дефолт» для DIS_STXP=1), но он **нигде в коде не применяется** (грепом по
  `deca_device.c` не пишется). То есть даже этот дефолт не выставляется — действует
  сбросовое значение чипа.
- **Станция работает на дефолте** (SET_TX_POWER не зовёт, §6).

## 3. Smart TX power vs manual (DIS_STXP)
- `dwt_configure` пишет `SYS_CFG` (`deca_device.c:481`), но бит **`DIS_STXP`
  (0x00040000, `deca_regs.h:88`) НЕ трогает** — только RXM110K и PHR_MODE. `DIS_STXP`
  меняет лишь `dwt_setsmarttxpower` (`:2030–2042`), которую station не зовёт.
- ⇒ у станции `DIS_STXP` = сбросовый дефолт (**0 = smart power включён**). Но на
  **Mode 3 (110k)** smart-boost к преамбуле/данным НЕ применяется (boost только для
  коротких кадров 6.8M) — значит **уровнем реально управляет базовый регистр
  `TX_POWER`**, который у нас на сбросовом дефолте. Т.е. в нашем режиме передаём на
  неоткалиброванном `TX_POWER`.

## 4. PG_DELAY / TC_PGDELAY и RF_TXCTRL — калибровка под ch2
- **RF_TXCTRL (0x28): выставляется ПРАВИЛЬНО по каналу** — `dwt_configure` пишет
  `RF_CONF_ID/RF_TXCTRL_OFFSET = tx_config[chan_idx[chan]]` (`deca_device.c:496`). Для
  ch2 берётся корректная константа. **Расхождения нет.**
- **TC_PGDELAY (PG_DELAY, в TX_CAL 0x2A): НЕ выставляется под канал в рабочем пути.**
  `dwt_configure` его не пишет; единственная запись — `dwt_configuretxrf`
  (`deca_device.c:419`) → только через `SET_TX_POWER`. Наш `map_pgdelay` знает верное
  значение **ch2 = `TC_PGDELAY_CH2` = 0xC2** (`protocol.c:618`; совпадает с
  DecaRanging/UM), НО оно применяется ТОЛЬКО в `SET_TX_POWER`. ⇒ у станции PG_DELAY =
  **сбросовый дефолт, НЕ 0xC2**. **Неверный PG_DELAY реально роняет излучаемую
  мощность/сужает спектр** — это второй фактор к TX_POWER.

## 5. Максимум и headroom (грубо, по коду/UM — без точного числа)
- Наш кламп: `POWER_LEVEL_MAX=0xDF` → октет `0x20` (не даём coarse уйти в 000).
- Один октет: fine до 0.5 dB × 32 ≈ **16 dB**, coarse 3 dB × 7 ≈ **21 dB** диапазона
  (UM §7.2.31 / `TASK_set_tx_power.md`). Т.е. между «слабым дефолтом» и «максимумом»
  потенциально **несколько..~10+ dB** headroom.
- RSSI ~−78 dBm на близкой дистанции при регуляторном потолке ch2 (~−41 dBm/МГц EIRP)
  — очень низко, косвенно подтверждает недокрут. **Точную прибавку в dB — только
  замером** (после выставления ch2-калибровки TX_POWER+PGdelay).

## 6. Зовёт ли станция SET_TX_POWER — НЕТ
- `set_tx_power`/`SET_TX_POWER` в `tools\` вызывается **только** в `mks_console.py`
  (`cmd_txpower`, `:235–248`, ручная команда `txpower <level>`).
- **`mks_station.py`, `mks_station_gui.py`, `mks_data_probe.py`, `mks_stream_probe.py`
  — SET_TX_POWER НЕ вызывают** (грепом). ⇒ станция и все потоковые сценарии передают
  на **дефолтной (сбросовой) TX-мощности с неоткалиброванным PG_DELAY**. Это
  **кандидат №1** плохой дальности.

---

## 7. Расхождения с UM/DecaRanging по ch2-калибровке (ключевое)
| Регистр | Наш код (станция) | UM/DecaRanging для ch2 | Статус |
|---|---|---|---|
| RF_TXCTRL (0x28) | по каналу в `dwt_configure` (`:496`) | по каналу | ✅ совпадает |
| TC_PGDELAY (PG_DELAY) | **сбросовый дефолт** (не пишется без SET_TX_POWER) | 0xC2 (у нас есть в `map_pgdelay`, но не применяется) | ❌ не выставлен |
| TX_POWER (0x1E) | **сбросовый дефолт** (не пишется ни init, ни configure) | ch2-калиброванное значение через `dwt_configuretxrf` | ❌ не выставлен |
| DIS_STXP | дефолт=0 (smart вкл); на 110k boost не действует | manual (DIS_STXP=1) с ch2 power | ⚠ уровнем правит неоткалиброванный TX_POWER |

**Итог:** штатный поток DecaWave — `dwt_configure()` **ЗАТЕМ** `dwt_configuretxrf()` с
ch2-значениями (PGdelay 0xC2 + ch2 power). Мы делаем только первый шаг; второй
(`dwt_configuretxrf`) выполняется лишь при ручном `SET_TX_POWER`, которого в станции
нет. Поэтому TX-тракт (мощность + PG_DELAY) — не под ch2. Решение (звать SET_TX_POWER/
`dwt_configuretxrf` в старте станции или в INIT/SET_PHY, с каким level) — за
архитектором; это firmware/хост-правка, здесь не делалась.
