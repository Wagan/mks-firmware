# RECON — сырой мониторинг CIR-аккумулятора без RXFCG (§11 п.20)

**Тип:** разведка по документам/коду (без железа, правок в прошивке нет).
**Дата:** 2026-07-24 (ред. 2 — сверка по UM PDF).
**Вопрос:** можно ли снять CIR-аккумулятор, когда нормального приёма кадра (RXFCG) не
было — для будущего макета-обнаружителя.
**Источники:** **DW1000 User Manual v2.17** (`docs\reference\DW1000 User Manual DecaWave.pdf`,
далее «UM» + № страницы), `App\protocol\protocol.c`, `Drivers\decadriver\deca_regs.h`,
`Drivers\decadriver\deca_device_api.h`, `docs\reference\decawave-examples\`.

> ✅ **UM PDF теперь в репозитории** (положил Ваган). Пометки «СВЕРИТЬ С UM» из
> ред. 1 закрыты прямыми цитатами первоисточника; оставшиеся «в UM не найдено»
> означают, что соответствующего утверждения в UM v2.17 нет (искал по тексту).

---

## 1. Что UM говорит о содержимом CIR-аккумулятора и когда он наполняется

- **§4.1.2 «Preamble Accumulation» (UM стр. 31):** *«Once the preamble sequence is
  detected, the receiver begins accumulating correlated preamble symbols, while looking
  for the SFD sequence. Accumulation stops when the SFD is detected, but may stop earlier
  if the accumulator grows quickly (…close line-of-sight…).»*
  ⇒ **Аккумулятор наполняется корреляцией ПРЕАМБУЛЫ, между `RXPRD` (preamble detect) и
  `RXSFDD` (SFD detect)** — то есть **ДО** декодирования PHR/данных и ДО проверки CRC.
  Наполнение завершается на SFD, а не на успешном кадре.
- **§7.2.38 Register file 0x25 — ACC_MEM (UM стр. 128):** банк памяти, хранящий
  «accumulated channel impulse response (CIR) data». Span аккумулятора — **одно
  символьное время**: **992** отсчёта (PRF16) / **1016** (PRF64), по паре int16 (real/imag)
  на отсчёт. *«The host system does not need to access the ACC_MEM in normal operation,
  however it may be of interest … to visualise the radio channel for diagnostic purposes.»*
  Первый прочитанный октет — dummy (внутренняя задержка чтения), отбрасывается.
- **§4.6 «Diagnostics» (UM стр. 43)** и **§4.7 (стр. 43–…):** доступ к аккумулятору
  отнесён к **диагностике**; **никакого условия «валиден только при RXFCG» UM не ставит.**
- **Явного утверждения «данные аккумулятора валидны только при RXFCG» в UM v2.17 НЕ
  найдено.** Напротив, по §4.1.2 CIR формируется преамбулой до какого-либо решения о
  валидности кадра.
- **Условие чтения (важно, §7.2.11 PMSC_CTRL0, UM стр. 193):** чтобы прочитать
  аккумулятор, должен присутствовать **приёмный клок** и быть выставлены биты **`FACE`**
  (bit 6) **и `AMCE`** (bit 15) в `0x36:00`: *«if the host system wants to read the
  accumulator data, both this FACE bit and the AMCE bit … need to be set to 1».* Это
  штатно делает `dwt_readaccdata` (порт SPI), см. §5.

## 2. События приёма без RXFCG (SYS_STATUS 0x0F) — что означает «тракт уже отработал»

- **`LDERUNE` (0x36:04 bit 17, UM стр. 194):** *«LDERUNE is 1 by default which means that
  the LDE algorithm will be run **as soon as the SFD is detected** in the receiver.»*
  ⇒ **LDE (поиск first path по аккумулятору) запускается на детекте SFD, а НЕ на RXFCG.**
- **`LDEDONE` (0x0F bit 10, UM стр. 87):** *«LDE processing done … completion of the
  leading edge detection and other adjustments of the receive timestamp.»* Флаг **в
  double-buffered swinging-set**, **автоснимается при RX enable**. ⇒ при `LDEDONE=1`
  **first path посчитан и аккумулятор проанализирован LDE** — независимо от CRC.
- **`RXFCE` (0x0F bit 15, UM стр. 88):** *«CRC check … FAILED … valid at the end of frame
  reception coincidentally with … RXDFR.»* ⇒ кадр **принят целиком**, LDE отработал (см.
  ниже про задержку RXDFR), **аккумулятор полон**, только CRC не сошёлся. `RXFCE` входит
  в `ALL_RX_ERR` (deca_regs.h:304).
- **Ключевой факт про порядок (UM стр. 88, RXDFR):** *«the setting of RXDFR is delayed
  until the LDE adjustments of the timestamp have completed, at which time the LDEDONE
  event status bit will be set».* ⇒ **всегда, когда взведён RXDFR/RXFCG/RXFCE, уже взведён
  и LDEDONE** — first path достоверен и при битом CRC.
- **`RXPHE` (bit 12, стр. 87–88), `RXRFSL` (bit 16, стр. 88):** ошибки PHR / Reed-Solomon
  — возникают **после** SFD (значит преамбула сккоррелирована, аккумулятор наполнен), но
  **обрывают приём кадра** (зависит от DIS_PHE/DIS_RSDE). Для CIR это неважно: span
  аккумулятора = одно символьное время преамбулы, а не тело кадра.
- **`RXSFDTO` (SFD timeout), `RXPTO`/`RXPRD` только:** преамбула могла коррелировать, но
  **SFD не найден** ⇒ по §4.1.2 наполнение не завершилось штатно; достоверность CIR в этом
  случае UM не оговаривает (см. раздел «(в)»).

**Итог по разделу:** ступени `RXPRD → (accumulation) → RXSFDD → LDE run → LDEDONE →
PHR → RXFCG|RXFCE` — аккумулятор наполнен уже к `RXSFDD`, а к `LDEDONE`/`RXFCE` дополнительно
готов результат LDE (first path). **Ни `LDEDONE`, ни `RXFCE` не требуют RXFCG.**

## 3. Затирание (перезапись) аккумулятора

- **Флаги статуса** `LDEDONE/RXDFR/RXFCG/RXFCE` — *«automatically cleared by the RX
  enable»* (UM стр. 87–88), диагностические регистры 0x12 — в double-buffered наборе
  (§4.3, Table 7, стр. 34). Это про **регистры/флаги**, не про саму память 0x25.
- **Момент физической перезаписи памяти ACC_MEM в UM v2.17 ЯВНО НЕ описан** (искал
  «overwrit», «cleared», «reset» рядом с accumulator — единственные попадания относятся к
  RX **frame buffer** двойной буферизации, UM стр. 46, не к аккумулятору).
  Косвенно из §4.1.2: следующий приём **заново наполняет** аккумулятор по мере накопления
  новой преамбулы. ⇒ читать снимок надо **до** повторного `dwt_rxenable`/нового приёма.
- Комментарий в нашем коде (protocol.c около :1379 — «cir_snap … не затёртый перевзводом
  accumulator») — **практическое допущение/эмпирика, прямой цитаты UM под него нет**.
  Точное правило перезаписи — в раздел «(в)» (UM/эксперимент).

## 4. Штатные режимы UM под эту задачу + API

- **SNIFF mode** (Reg 0x1D, §4.5; вводится в §4.1.1, UM стр. 31): приёмник «нюхает» эфир
  периодически для **экономии энергии** на этапе детекта преамбулы. **Это НЕ доступ к
  аккумулятору без кадра.**
- **Continuous frame / TX Power Spectrum Test Mode** (`TX_PSTM`, 0x2F:24, UM стр. 192):
  **передающий** тест под регуляторику/калибровку мощности — не RX-аккумулятор.
- **Отдельного штатного «диагностического RX-режима», отдающего аккумулятор без валидного
  кадра, в UM v2.17 НЕ найдено.** Доступ к 0x25 описан только как диагностическая
  визуализация канала (§4.6/§4.7), с требованием FACE+AMCE (§1).
- **API (`deca_device_api.h`):** `dwt_readaccdata` и `dwt_readdiagnostics` — просто читают
  память/регистры диагностики, **привязки к RXFCG в их doc-комментариях НЕТ**. То есть
  вызвать их можно из любой ветки; **осмысленность** CIR в не-RXFCG-ветке определяется
  разделами 2–3, а не API.

## 5. Что у нас в коде сейчас (точные строки)

- **`PROTOCOL_CaptureCIR`** (`protocol.c:1306`): читает окно аккумулятора
  `dwt_readaccdata` (`:1329`), центр окна — `fp = rx_metrics.diag.firstPath >> 6`
  (`:1313–1314`). Т.е. **привязан к `firstPath`**, а `firstPath` берётся из
  `dwt_readdiagnostics`, вызываемой **ТОЛЬКО в ветке RXFCG** (`:1365`).
- **Вызов `CaptureCIR` — только в ветке RXFCG**, под условием
  `need_cir = (stream_content==1) || (!stream_active)` (`:1381–1384`), **до** `dwt_rxenable`
  (`:1386`).
- **Ветка `ALL_RX_ERR`** (`:1391–1396`): сейчас **снимает флаги
  (`dwt_write32bitreg(SYS_STATUS_ID, SYS_STATUS_ALL_RX_ERR)`), `dwt_rxreset()`,
  `dwt_rxenable()`** — **аккумулятор/диагностику НЕ читает**.
- **`SYS_STATUS_ALL_RX_ERR` включает `RXFCE`** (deca_regs.h:304) ⇒ **сценарий «кадр принят,
  CRC не сошёлся» уже попадает в эту ветку** — готовая точка перехвата, где аккумулятор,
  по разделу 2, полон.
- **Кирпичи, которые уже есть:** `dwt_readaccdata` (порт), `dwt_readdiagnostics`, полный
  опрос `SYS_STATUS` в main loop (`PROTOCOL_PollRadio`), ветка `ALL_RX_ERR` как точка
  «приём был, кадра нет».
  **Чего нет:** чтения аккумулятора/диагностики в НЕ-RXFCG-ветках; захвата CIR без
  валидного `firstPath` (напр. по фиксированному окну/энергии); опроса промежуточных бит
  `LDEDONE`/`RXSFDD` без ожидания кадра.

## 6. Референс EVK/DecaRanging

- `docs\reference\decawave-examples\ss_init_main.c` / `ss_resp_main.c`: опрос
  `SYS_STATUS` на `RXFCG | ALL_RX_TO | ALL_RX_ERR`; ветка `ALL_RX_ERR` — **только снятие
  флагов** (`ss_init_main.c:171`, `ss_resp_main.c:191`), затем повтор. **Ни accumulator,
  ни diagnostics в этих примерах НЕ читаются вообще** (даже при RXFCG — примеры про TWR).
  ⇒ **референса чтения аккумулятора вне RXFCG в наших примерах НЕТ.**

---

## Итог по разделам

### (а) Документировано в UM v2.17
1. **Аккумулятор наполняется преамбулой** (§4.1.2, стр. 31): накопление между preamble
   detect и SFD, завершается на SFD — **до PHR/данных/CRC**. ⇒ CIR формируется **без
   зависимости от RXFCG**.
2. **Утверждения «CIR валиден только при RXFCG» в UM НЕТ.** Доступ к 0x25 — диагностический
   (§4.6/§4.7, §7.2.38), без предусловия RXFCG.
3. **LDE (first path) запускается на детекте SFD** (`LDERUNE`, 0x36:04 bit17, стр. 194), а
   `LDEDONE` (0x0F bit10, стр. 87) отмечает готовность first-path/timestamp — **независимо
   от CRC**.
4. **`RXFCE`** (стр. 88) = кадр принят целиком, CRC не сошёлся; поскольку **RXDFR задержан
   до завершения LDE** (стр. 88), при `RXFCE` **аккумулятор полон и first path посчитан**.
5. **Чтение аккумулятора требует RX-клока + бит `FACE`+`AMCE`** (0x36:00, стр. 193) —
   выполняет `dwt_readaccdata`.
6. **Штатного RX-режима «аккумулятор без кадра» в UM нет**; SNIFF (§4.5) — энергосбережение,
   TX_PSTM (стр. 192) — передающий тест.

### (б) Видно в коде
1. `CaptureCIR` привязан к `firstPath` из `dwt_readdiagnostics`, обе — только в ветке RXFCG
   (`protocol.c:1306/1313/1365/1381`).
2. Ветка `ALL_RX_ERR` существует и **уже ловит `RXFCE`** (deca_regs.h:304), но аккумулятор/
   диагностику **не читает** (`protocol.c:1391–1396`).
3. Кирпичи чтения (`readaccdata`/`readdiagnostics`) готовы; захвата без `firstPath` нет.
4. Референс-примеры аккумулятор не читают ни в одной ветке.

### (в) Осталось неизвестным / требует UM-уточнения или эксперимента на железе
1. **Точный момент физической перезаписи памяти ACC_MEM** (при `rxenable` / при новой
   преамбуле / ином) — **в UM v2.17 явно не описан**; косвенно из §4.1.2 следует
   перенаполнение новой преамбулой. Определяет, «успеем ли прочитать в не-RXFCG-сценарии».
2. **Достоверность/полнота CIR при `RXFCE` и после `LDEDONE` без RXFCG** — по UM ожидается,
   что аккумулятор полон (разделы 1–2), но **числового подтверждения на железе не было**
   (нужен замер: снять окно в ветке `RXFCE`, сверить форму с эталонным RXFCG-кадром).
3. **CIR при обрыве до SFD** (`RXPHE` без RXSFDD, `RXSFDTO`, только `RXPRD`) — достоверность
   аккумулятора без завершённого наполнения UM не оговаривает; требует эксперимента.
4. **Съём осмысленного окна без `firstPath`** (по фиксированному центру/энергии, когда LDE
   не дал first path) — в UM/API готового механизма нет; алгоритм — за архитектором.

**Вывод о реализуемости и архитектуре — за архитектором** (по UM + при необходимости
эксперименту на железе). Разведка фактов завершена; домыслов сверх источников не делаю.
