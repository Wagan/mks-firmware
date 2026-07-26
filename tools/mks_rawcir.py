#!/usr/bin/env python3
"""
*******************************************************************************
  МКС — Модуль коммуникации и сопряжения
  Хостовые инструменты (ПК) для STM32F411 + 2x DWM1000 (DW1000)

  Файл:     mks_rawcir.py
  Описание: съём ПОЛНОГО сырого аккумулятора CIR (CMD_CIR_RAW_ARM 0x43 /
            потоковый кадр CONTENT=5) + дамп в CSV и проверки для замера.

  Copyright (c) 2026 NCPR, Flexlab LLC. Все права защищены.
*******************************************************************************

⚙️ ЭКСПЕРИМЕНТ. Работает только с прошивкой, собранной с раскомментированным
`#define RAW_CIR_MONITOR` (App/board/board_config.h). Без него команда 0x43
вернёт UNKNOWN_CMD. Основание: docs/RECON_raw_cir_monitor.md,
docs/TASK_raw_cir_monitor.md, docs/REPORT_raw_cir_monitor.md, PROTOCOL_SPEC v1.6.

ЗАКРЫТО ЗАМЕРОМ 2026-07-26:
  №2 AMCE на 1016 отсчётах НЕ нужен — нули только в начальной мёртвой зоне
     (56-58 отсчётов подряд от нуля), хвост живой шум, залипаний нет.
  №4 read_ms = 1-3 мс, а НЕ расчётные ~29 мс. Значит вывод разведки Б6 про
     рабочий SPI 1.125 МГц неверен (2 мс на 4066 Б ≈ 16 Мбит/с). Причина не
     установлена — отдельная разведка по deca_port.c / MX_SPI3_Init.
  Съём по RXSFDD даёт настоящую CIR: пик/шум ~40 дБ на столе, фронт 2 отсчёта.

ОСТАЛОСЬ №1: годна ли CIR на кадрах, ПРОВАЛИВШИХСЯ на PHR/CRC (RXPHE/RXFCE).
Замер на китах EVK с 4 м и одним поворотом дал 20/20 RXSFDD — линк всё ещё
слишком хороший, провалов нет. Геометрия оказалась плохой ручкой: шумовая полка
за одну серию уехала на 4 дБ, повторяемости нет.
ПОЭТОМУ: ослаблять линк надо мощностью своего передатчика (--tx-power / --tx-sweep
на loopback M1→M2), а не расстоянием. 0..223 (0xDF), шаг ≈0.5 дБ, монотонно и
повторяемо. Ищем зону, где преамбула ещё детектируется, а кадр уже не проходит.

ГДЕ ВЗЯТЬ СИГНАЛ:
  --txperiodic MS : loopback M1→M2 на одной плате. Нужен для --tx-power/--tx-sweep,
                    потому что регулируем мощность СВОЕГО передатчика.
  внешний источник (киты EVK / вторая плата), без --txperiodic — мощностью не
                    управляем, ослабление только геометрией.

ЗАПУСК:
    python mks_rawcir.py --port COM4 --txperiodic 50 --count 3
    # поиск зоны провалов свипом мощности (главный сценарий для №1):
    python mks_rawcir.py --port COM4 --txperiodic 20 --count 15 \
           --tx-sweep "223,180,140,110,90,70,50,30,15,0" --out dumps/sweep
    python mks_rawcir.py --port COM4 --mode 7 --count 20 --out dumps/ch5

ЗАВИСИМОСТИ: mks_protocol.py, mks_stream.py (версия с разбором CONTENT=5),
mks_phy.py (единая таблица PHY-пресетов).

История изменений (для будущих правщиков: помечать правки в формате
  <Имя>: ГГГГ-ММ-ДД — описание — чтобы различать авторов):
  Wagan: 2026-07-26 — новый скрипт: одиночный/серийный съём полного сырого CIR
                      (0x43 → CONTENT=5), дамп CSV, проверка хвоста на признак AMCE.
  Wagan: 2026-07-26 — скрипт сам ставит PHY (--mode/--phy через mks_phy.py) и делает
                      полную последовательность init → setphy → rxstart.
  Wagan: 2026-07-26 — --txperiodic/--payload: loopback M1→M2 как источник сигнала.
  Wagan: 2026-07-26 — --tx-power/--tx-sweep: регулировка мощности своего передатчика
                      и авто-свип по уровням с таблицей состава событий. Замер на
                      китах показал, что расстоянием зону провалов не нащупать
                      (среда нестационарна), мощностью — можно.
  Wagan: 2026-07-26 — SNR считается по ЧИСТОЙ предимпульсной области (от конца мёртвой
                      зоны до пика−50), а не по участку после пика: в тот участок
                      попадает спад многолучёвки и он занижает оценку.
                      Таймаут больше не обрывает серию — в свипе «кадров нет» это
                      такой же результат, как событие, и он попадает в таблицу.
  Wagan: 2026-07-26 — ПОРЯДОК смены мощности: tx_stop → set_tx_power → tx_periodic.
                      Прежний порядок (менять мощность на уже взведённом TX_PERIODIC)
                      дал регистр от 0x20202020 до 0xFFFFFFFF при полностью неизменном
                      принятом пике — включая уровень 0, максимальное ослабление.
                      Плюс в каждый съём добавлен GET_SIGNAL_METRICS: RSSI считает
                      прошивка по UM §4.7 с нормировкой на RXPACC, и это независимая
                      мера принятой мощности — сырая амплитуда пика ею НЕ является.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import time

import mks_protocol as mks
import mks_stream as mstream
import mks_phy as mphy

# ⚙️ Команда есть в прошивке ТОЛЬКО под #define RAW_CIR_MONITOR (PROTOCOL_SPEC v1.6).
CMD_CIR_RAW_ARM = 0x43

TAIL_SAMPLES = 64        # «хвост» для проверки №2 (закрыта, оставлено как регресс)
STUCK_SAMPLES = 16       # сколько одинаковых значений подряд считать «залипанием»
TX_POWER_MAX = 0xDF      # прошивка ограничивает уровень сверху (mks_protocol.set_tx_power)

DEFAULT_TX_PAYLOAD = "DE AD BE EF 01"


def parse_hex(s: str) -> bytes:
    """Разобрать payload из hex-токенов (пробелы/запятые), напр. 'DE AD BE EF 01'."""
    toks = s.replace(",", " ").split()
    return bytes(int(t, 16) for t in toks)


def parse_levels(s: str) -> list:
    """Разобрать список уровней мощности для свипа: "223,180,140,...". Порядок
    сохраняется как задан — обычно от сильного к слабому."""
    out = []
    for tok in s.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        v = int(tok, 0)                      # допускает и 0xDF
        if not (0 <= v <= 0xFF):
            raise ValueError(f"уровень {v} вне диапазона u8")
        out.append(v)
    if not out:
        raise ValueError("список уровней пуст")
    return out


# --- съём ---------------------------------------------------------------------

def arm_and_capture(m: mks.MKS, reader: mstream.StreamReader, timeout: float):
    """Взвести одиночный сырой съём (0x43) и дождаться кадра CONTENT=5.

    Возвращает разобранный кадр, либо None если кадр не пришёл за timeout
    (в свипе «кадров нет» — это результат, а не ошибка).
    Исключение бросается только на отказ самой команды.
    """
    reader.buf.clear()                       # command() чистит порт, но не накопитель

    status, _ = m.command(CMD_CIR_RAW_ARM, timeout=timeout)
    if status != 0x00:
        name = mks.status_name(status)
        if status == 0x01:
            raise mks.ProtocolError(
                f"0x43 → {name}: прошивка собрана БЕЗ #define RAW_CIR_MONITOR")
        if status == 0x04:
            raise mks.ProtocolError(
                f"0x43 → {name}: нет активного приёма — нужен RX_START до взвода")
        raise mks.ProtocolError(f"0x43 → {name}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        for body, crc_ok in reader.poll():
            if not crc_ok:
                continue
            frame = mstream.parse_stream_body(body)
            if frame["content"] == 5:
                return frame
    return None


# --- разбор -------------------------------------------------------------------

def amplitudes(iq) -> list:
    return [math.sqrt(float(i) * i + float(q) * q) for i, q in iq]


def analyse(raw: dict) -> dict:
    """Метрики формы CIR.

    SNR считается по ЧИСТОЙ предимпульсной области: от конца начальной мёртвой
    зоны (+20 запаса) до пика−50. Замер 2026-07-26 показал, что импульс стоит
    примерно на 79% буфера, то есть перед ним лежит 700+ отсчётов чистого шума —
    это и есть корректная опора. Область ПОСЛЕ пика для опоры не годится: туда
    попадает спад многолучёвки (30-60 отсчётов) и занижает оценку SNR.
    """
    iq = raw["iq"]
    amps = amplitudes(iq)
    n = len(amps)

    peak_idx = max(range(n), key=lambda k: amps[k])
    peak = amps[peak_idx]

    # конец начальной мёртвой зоны (подряд идущие нули от 0)
    dead = 0
    while dead < n and amps[dead] == 0.0:
        dead += 1

    lo, hi = dead + 20, peak_idx - 50
    pre = amps[lo:hi] if hi - lo >= 100 else []
    pre_mean = (sum(pre) / len(pre)) if pre else float("nan")
    snr_db = 20.0 * math.log10(peak / pre_mean) if pre and pre_mean > 0 else float("nan")

    tail = amps[-TAIL_SAMPLES:] if n >= TAIL_SAMPLES else amps[:]
    tail_mean = sum(tail) / len(tail) if tail else float("nan")

    # фронт: первый отсчёт перед пиком, превысивший 5x опорного шума
    rise = None
    if pre:
        for k in range(max(dead, peak_idx - 200), peak_idx + 1):
            if amps[k] > 5.0 * pre_mean:
                rise = k
                break

    return {
        "n": n,
        "dead": dead,
        "peak_idx": peak_idx,
        "peak": peak,
        "pre_mean": pre_mean,
        "snr_db": snr_db,
        "rise_len": (peak_idx - rise) if rise is not None else None,
        "tail_mean": tail_mean,
        "tail_max": max(tail) if tail else float("nan"),
        "zeros_total": sum(1 for a in amps if a == 0.0),
        "zeros_tail": sum(1 for a in tail if a == 0.0),
        "stuck_tail": (n >= STUCK_SAMPLES and len(set(iq[-STUCK_SAMPLES:])) == 1),
    }


def report(raw: dict, a: dict) -> None:
    rl = a["rise_len"]
    print(f"  event={raw['event_name']}  prf={raw['prf']}  n={raw['sample_count']}  "
          f"read_ms={raw['read_ms']}")
    print(f"  пик #{a['peak_idx']} |A|={a['peak']:.0f}   шум(предимп.)={a['pre_mean']:.1f}   "
          f"SNR={a['snr_db']:.1f} дБ   фронт={rl if rl is not None else '-'} отсч.")
    print(f"  мёртвая зона={a['dead']}  хвост ср={a['tail_mean']:.1f} макс={a['tail_max']:.0f}  "
          f"нулей всего={a['zeros_total']} в хвосте={a['zeros_tail']}")

    if a["stuck_tail"]:
        print(f"  ! хвост залип ({STUCK_SAMPLES} одинаковых) — проверка №2")
    if a["zeros_tail"] and a["zeros_tail"] == len(raw["iq"][-TAIL_SAMPLES:]):
        print("  ! хвост целиком нулевой — проверка №2")


# --- вывод в файл -------------------------------------------------------------

def dump_csv(path: str, raw: dict, phy_label: str, tx_level) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["# phy", phy_label])
        w.writerow(["# tx_level", tx_level if tx_level is not None else "n/a"])
        w.writerow(["# event", raw["event"], raw["event_name"]])
        w.writerow(["# prf", raw["prf"]])
        w.writerow(["# read_ms", raw["read_ms"]])
        w.writerow(["# sample_count", raw["sample_count"]])
        w.writerow(["index", "I", "Q", "amp"])
        for k, (i, q) in enumerate(raw["iq"]):
            w.writerow([k, i, q, f"{math.sqrt(float(i) * i + float(q) * q):.3f}"])


# --- серия на одном уровне мощности -------------------------------------------

def run_series(m, reader, args, phy_label, tx_level, verbose: bool) -> dict:
    """Сделать args.count съёмов и собрать состав событий. Возвращает сводку."""
    tally = {"RXSFDD": 0, "RXPHE": 0, "RXFCE": 0, "прочее": 0, "нет кадра": 0}
    peaks, noises, snrs, reads, rssis = [], [], [], [], []

    for k in range(args.count):
        frame = arm_and_capture(m, reader, args.timeout)
        if frame is None:
            tally["нет кадра"] += 1
            if verbose:
                print(f"  [{k + 1}/{args.count}] кадра нет за {args.timeout:.0f} с")
            continue

        raw = frame["raw_cir"]
        a = analyse(raw)
        name = raw["event_name"]
        if name in ("RXSFDD", "RXPHE", "RXFCE"):
            tally[name] += 1
        else:
            tally["прочее"] += 1

        peaks.append(a["peak"])
        if a["pre_mean"] == a["pre_mean"]:
            noises.append(a["pre_mean"])
        if a["snr_db"] == a["snr_db"]:
            snrs.append(a["snr_db"])
        reads.append(raw["read_ms"])

        # НЕЗАВИСИМЫЙ измеритель принятой мощности: RSSI считает прошивка по UM §4.7
        # с нормировкой на RXPACC_NOSAT. Нужен потому, что сырая амплитуда пика
        # аккумулятора мощности не отражает (замер 2026-07-26: пик не следует за
        # уровнем TX вообще). ВНИМАНИЕ: метрики относятся к последнему кадру,
        # прошедшему RXFCG, а НЕ к тому, с которого снят сырой CIR.
        rssi = None
        if not args.no_metrics:
            try:
                st, data = m.get_signal_metrics()
                if st == 0x00:
                    mt = mks.parse_signal_metrics(data)
                    if mt.get("format") == "final" and mt.get("rssi_valid"):
                        rssi = mt["rssi_dbm"]
                        rssis.append(rssi)
            except mks.ProtocolError:
                pass

        if verbose:
            print(f"  [{k + 1}/{args.count}] seq={frame['seq']} dropped={frame['dropped']}"
                  + (f"  RSSI={rssi:.2f} dBm" if rssi is not None else ""))
            report(raw, a)

        if args.out:
            tag = "" if tx_level is None else f"_tx{tx_level:03d}"
            path = f"{args.out}{tag}_{k + 1:02d}_{name}.csv"
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            dump_csv(path, raw, phy_label, tx_level)

    def avg(xs):
        return (sum(xs) / len(xs)) if xs else float("nan")

    return {"tally": tally, "peak": avg(peaks), "noise": avg(noises),
            "snr": avg(snrs), "read_ms": avg(reads), "rssi": avg(rssis),
            "frames": len(peaks)}


# --- main ---------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Съём полного сырого CIR (0x43 / CONTENT=5). "
                    "Требует прошивку с #define RAW_CIR_MONITOR.")
    p.add_argument("--port", required=True, help="COM-порт платы (напр. COM4)")
    p.add_argument("--mode", type=int, choices=range(1, 9), default=mphy.DEFAULT_MODE,
                   metavar="1..8", help=f"PHY-пресет Mode 1..8 (деф. {mphy.DEFAULT_MODE})")
    p.add_argument("--phy", default=None, metavar='"ch dr plen code prf pac"',
                   help='ручной PHY вместо --mode, напр. "2 0 1024 9 64 32"')
    p.add_argument("--txperiodic", type=int, default=None, metavar="MS",
                   help="период мс для TX_PERIODIC (loopback M1→M2) — источник сигнала; "
                        "обязателен для --tx-power/--tx-sweep")
    p.add_argument("--payload", default=DEFAULT_TX_PAYLOAD, metavar='"hex..."',
                   help=f"payload для TX_PERIODIC (деф. \"{DEFAULT_TX_PAYLOAD}\")")
    p.add_argument("--tx-power", type=lambda s: int(s, 0), default=None, metavar="N",
                   help=f"уровень мощности своего TX, 0..{TX_POWER_MAX} (больше = мощнее, "
                        f"шаг ≈0.5 дБ). Требует SET_PHY_CONFIG — скрипт делает его сам")
    p.add_argument("--tx-sweep", default=None, metavar='"223,180,140,..."',
                   help="свип по уровням мощности: на каждом делается --count съёмов, "
                        "в конце таблица состава событий. Так ищется зона провалов PHR/CRC")
    p.add_argument("--count", type=int, default=1, help="съёмов на уровень (default 1)")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="ожидание кадра на один съём, с (default 10)")
    p.add_argument("--out", default=None, help="префикс пути для CSV-дампов")
    p.add_argument("--quiet", action="store_true",
                   help="не печатать каждый съём (в длинном свипе удобнее)")
    p.add_argument("--no-metrics", action="store_true",
                   help="не запрашивать GET_SIGNAL_METRICS после каждого съёма "
                        "(по умолчанию запрашивается: RSSI — независимая мера мощности)")
    p.add_argument("--skip-init", action="store_true", help="не звать INIT")
    p.add_argument("--skip-phy", action="store_true", help="не выставлять PHY")
    p.add_argument("--skip-rx-start", action="store_true", help="не звать RX_START")
    args = p.parse_args(argv)

    try:
        if args.phy is not None:
            phy = mphy.parse_phy_string(args.phy)
            phy_label = f"Ручной ({args.phy})"
        else:
            phy = mphy.by_mode(args.mode)
            phy_label = mphy.MODE_LABELS[args.mode]
        tx_payload = parse_hex(args.payload)
        levels = parse_levels(args.tx_sweep) if args.tx_sweep else None
    except ValueError as e:
        print(f"  {e}")
        return 2

    if args.tx_power is not None and levels is not None:
        print("  --tx-power и --tx-sweep вместе не имеют смысла — выбери одно")
        return 2
    if (args.tx_power is not None or levels is not None) and args.txperiodic is None:
        print("  --tx-power/--tx-sweep регулируют мощность СВОЕГО передатчика, "
              "поэтому нужен --txperiodic (loopback M1→M2)")
        return 2

    print(f"Открываю {args.port} ...")
    with mks.MKS(args.port) as m:
        if not args.skip_init:
            st, _ = m.init(timeout=20.0)
            print(f"  INIT: {mks.status_name(st)}")
            if st != 0x00:
                return 2

        if not args.skip_phy:
            st, _ = m.command(mks.CMD_SET_PHY_CONFIG, mphy.phy_params(phy))
            print(f"  SET_PHY_CONFIG: {mks.status_name(st)}  [{mphy.describe(phy, phy_label)}]")
            if st != 0x00:
                return 2

        if not args.skip_rx_start:
            st, _ = m.rx_start()
            print(f"  RX_START: {mks.status_name(st)}")
            if st != 0x00:
                return 2

        tx_on = False

        def tx_start() -> bool:
            nonlocal tx_on
            st, _ = m.tx_periodic(args.txperiodic, tx_payload)
            tx_on = (st == 0x00)
            return tx_on

        def tx_halt():
            nonlocal tx_on
            if tx_on:
                try:
                    m.tx_stop()
                except Exception:
                    pass
                tx_on = False

        # Передатчик поднимаем ПОСЛЕ установки мощности (см. ниже), поэтому здесь
        # запускаем его только если мощностью не управляем вовсе.
        if args.txperiodic is not None and args.tx_power is None and levels is None:
            if not tx_start():
                print("  TX_PERIODIC: отказ")
                return 2
            print(f"  TX_PERIODIC({args.txperiodic} мс, "
                  f"payload={tx_payload.hex(' ').upper()}): OK")

        reader = mstream.StreamReader(m.ser)
        results = []
        rc = 0

        try:
            for level in (levels if levels is not None else [args.tx_power]):
                if level is not None:
                    # ПОРЯДОК КРИТИЧЕН. Замер 2026-07-26: при смене мощности на уже
                    # взведённом TX_PERIODIC регистр писался правильно (0x20202020 →
                    # 0xFFFFFFFF), но принятый пик не менялся ВООБЩЕ, включая уровень 0
                    # (максимальное ослабление). Похоже, периодическая передача
                    # переприменяет свою конфигурацию и затирает ручную мощность.
                    # Поэтому: останов TX → установка мощности → повторный пуск TX.
                    tx_halt()
                    if level > TX_POWER_MAX:
                        print(f"  уровень {level} > {TX_POWER_MAX}: прошивка ограничит сверху")
                    st, data = m.set_tx_power(level)
                    reg = int.from_bytes(data[:4], "little") if len(data) >= 4 else None
                    print(f"\n--- TX level {level} → {mks.status_name(st)}"
                          + (f"  (регистр 0x{reg:08X})" if reg is not None else ""))
                    if st != 0x00:
                        rc = 3
                        break
                    if args.txperiodic is not None and not tx_start():
                        print("  TX_PERIODIC после смены мощности: отказ")
                        rc = 3
                        break
                    time.sleep(0.3)          # дать передатчику и АРУ осесть
                else:
                    print()

                r = run_series(m, reader, args, phy_label, level, verbose=not args.quiet)
                r["level"] = level
                results.append(r)
        except mks.ProtocolError as e:
            print(f"  ОШИБКА: {e}")
            rc = 3
        finally:
            tx_halt()
            try:
                m.rx_stop()
            except Exception:
                pass

        if results:
            print("\n===== ИТОГ =====")
            print(f"  {phy_label}, txperiodic={args.txperiodic}, {args.count} съёмов на уровень")
            print("  level  SFDD  PHE  FCE  нет   ср.пик  ср.шум  ср.SNR  ср.RSSI")
            for r in results:
                t = r["tally"]
                lv = "n/a" if r["level"] is None else f"{r['level']:3d}"
                rs = r["rssi"]
                print(f"  {lv:>5}  {t['RXSFDD']:4d}  {t['RXPHE']:3d}  {t['RXFCE']:3d}  "
                      f"{t['нет кадра']:3d}   {r['peak']:6.0f}  {r['noise']:6.1f}  "
                      f"{r['snr']:6.1f}  " + ("   н/д" if rs != rs else f"{rs:7.2f}"))

            # Работает ли регулировка мощности вообще: смотрим на РАЗМАХ RSSI по
            # уровням. Сырой пик для этого не годится (замер 2026-07-26).
            rr = [r["rssi"] for r in results if r["rssi"] == r["rssi"]]
            if len(rr) >= 2:
                span = max(rr) - min(rr)
                print(f"\n  Размах RSSI по уровням: {span:.1f} дБ "
                      f"({min(rr):.2f} … {max(rr):.2f} dBm)")
                if span < 3.0:
                    print("  → RSSI практически не двигается: регулировка мощности НЕ "
                          "действует на эфир, несмотря на верно записанный регистр. "
                          "Ослаблять линк этой ручкой нельзя.")
                else:
                    print("  → RSSI следует за уровнем: регулировка действует. Если при "
                          "этом сырой пик стоит на месте — это компенсация в приёмнике, "
                          "и сырая амплитуда мерой мощности не является.")

            got = sum(r["tally"]["RXPHE"] + r["tally"]["RXFCE"] for r in results)
            if got:
                print(f"\n  Провалившихся кадров получено: {got} — это материал "
                      f"для неизвестного №1.")
            else:
                print("\n  Провалившихся кадров НЕ получено. Если на нижних уровнях "
                      "кадры просто исчезают (столбец «нет»), зона провалов между "
                      "соседними уровнями — сузь шаг свипа вокруг границы.")

    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nпрервано")
        sys.exit(130)
