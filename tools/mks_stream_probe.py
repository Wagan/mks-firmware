#!/usr/bin/env python3
"""
*******************************************************************************
  МКС — Модуль коммуникации и сопряжения
  Хостовые инструменты (ПК) для STM32F411 + 2x DWM1000 (DW1000)

  Файл:     mks_stream_probe.py
  Описание: минимальный приёмник потокового режима (SET_STREAM_MODE 0x42, CIR-2a).
            Включает поток, читает потоковые кадры (метрики + окно CIR), считает
            FPS и потери (прошивочные DROPPED / хостовые дырки SEQ). Цель — ЗАМЕР.

  Copyright (c) 2026 NCPR, Flexlab LLC. Все права защищены.
*******************************************************************************

Отдельный диагностический скрипт (не GUI, не консоль). Задача — понять, тянет ли
USB CDC поток кадров после каждого принятого UWB-кадра.

Потоковый кадр (СВОЙ формат, отдельный от командного SYNC 0xAA55):
    SMARK(0xDE 0xCA) | LEN16(u16 LE) | SEQ(u16) | DROPPED(u16) | CONTENT(u8) | PAYLOAD | CRC8
    LEN16   = число байт после LEN16 и до CRC (SEQ+DROPPED+CONTENT+PAYLOAD).
    CONTENT = 1 → PAYLOAD = метрики(30) + окно CIR; 2 → PAYLOAD = метрики(30).
    метрики = формат GET_SIGNAL_METRICS (30 байт); окно CIR = заголовок 6 байт
              (fp_index/start_index/count) + count пар I/Q int16 LE (как DATA GET_CIR).
    CRC8    = poly 0x07 по [LEN16 .. конец PAYLOAD) (SMARK не входит).
    SEQ     инкрементируется только на реально отправленный кадр.
    DROPPED растёт при CDC BUSY (кадр дропнут прошивкой).

Запуск (нагрузочный замер потолка USB — киты EVK выключены, loopback M1->M2):
    python mks_stream_probe.py COM3
    python mks_stream_probe.py COM3 --mode 4 --content 2 --txperiodic 10 --seconds 25
  --mode 1..8     : PHY-пресет (деф. 3). ПРИЁМНИК должен совпадать с передатчиком
                    (иначе 0 кадров). Значения = mks_gui.py PHY_MODES.
  --phy "..."     : ручной PHY (6 чисел: ch dr plen code prf pac) вместо --mode.
  --content 1|2   : 1=метрики+CIR (деф.), 2=только метрики (лёгкий поток).
  --txperiodic ms : гнать TX_PERIODIC(период) для нагрузки M1->M2; без него — пассив.
  --seconds N     : авто-стоп через N c (иначе до Ctrl+C).
Скрипт сам: init -> SET_PHY(выбранный режим) -> rx_start -> [TX_PERIODIC] -> stream on.

История изменений (для будущих правщиков: помечать правки в формате
  <Имя>: ГГГГ-ММ-ДД — описание — чтобы различать авторов):
  Wagan: 2026-07-17 — приёмник потока SET_STREAM_MODE 0x42 (CIR-2a): замер FPS/потерь.
  Wagan: 2026-07-17 — лёгкий режим (--content 2) + loopback-нагрузка (--txperiodic) для
                      нащупывания потолка USB.
  Wagan: 2026-07-20 — выбор PHY-режима (--mode 1..8) для замера на разных режимах.
  Wagan: 2026-07-20 — средний FPS считается по времени 1-го..последнего кадра (совпадает
                      с мгновенным); чистый замер потолка снят на двух раздельных платах.
  Wagan: 2026-07-20 — общий парсер потока вынесен в mks_stream.py (импорт вместо дубля).
"""

from __future__ import annotations

import sys
import time

import mks_protocol as mks
from mks_stream import SMARK, parse_stream_body, StreamReader  # общий парсер потока

# Wagan: 2026-07-20 — пресеты PHY 1..8 для --mode (замер на разных режимах).
# Пресеты PHY (значения идентичны mks_gui.py PHY_MODES; ключ = номер Mode 1..8).
# dr — код (0=110k, 1=850k, 2=6M8); prf — число МГц (16/64).
PHY_MODES = {
    1: dict(ch=2, dr=0, plen=1024, code=3, prf=16, pac=32),
    2: dict(ch=2, dr=2, plen=128,  code=3, prf=16, pac=8),
    3: dict(ch=2, dr=0, plen=1024, code=9, prf=64, pac=32),
    4: dict(ch=2, dr=2, plen=128,  code=9, prf=64, pac=8),
    5: dict(ch=5, dr=0, plen=1024, code=3, prf=16, pac=32),
    6: dict(ch=5, dr=2, plen=128,  code=3, prf=16, pac=8),
    7: dict(ch=5, dr=0, plen=1024, code=9, prf=64, pac=32),
    8: dict(ch=5, dr=2, plen=128,  code=9, prf=64, pac=8),
}


def phy_params(m: dict) -> bytes:
    """Собрать 7 байт SET_PHY_CONFIG из пресета (как cmd_setphy в консоли/GUI):
    ch, dr, plen u16 LE, code, prf, pac."""
    return bytes([m["ch"] & 0xFF, m["dr"] & 0xFF,
                  m["plen"] & 0xFF, (m["plen"] >> 8) & 0xFF,
                  m["code"] & 0xFF, m["prf"] & 0xFF, m["pac"] & 0xFF])


def main():
    import argparse
    ap = argparse.ArgumentParser(description="МКС: приёмник потока + нагрузочный замер USB")
    ap.add_argument("port")
    ap.add_argument("--content", type=int, choices=(1, 2), default=1,
                    help="1=метрики+CIR (деф.), 2=только метрики (лёгкий поток)")
    ap.add_argument("--txperiodic", type=int, default=None, metavar="MS",
                    help="период мс для TX_PERIODIC (loopback M1->M2); без него — пассив")
    ap.add_argument("--seconds", type=float, default=None, metavar="N",
                    help="авто-стоп через N секунд (иначе до Ctrl+C)")
    ap.add_argument("--mode", type=int, choices=range(1, 9), default=3, metavar="1..8",
                    help="PHY-пресет Mode 1..8 (деф. 3). Приёмник должен совпадать с передатчиком")
    ap.add_argument("--phy", default=None, metavar='"ch dr plen code prf pac"',
                    help='ручной PHY вместо --mode: 6 чисел в кавычках, напр. "2 2 128 9 64 8"')
    args = ap.parse_args()

    # Разрешить PHY: ручной (--phy) имеет приоритет над пресетом (--mode).
    if args.phy is not None:
        try:
            vals = [int(x) for x in args.phy.split()]
            if len(vals) != 6:
                raise ValueError
        except ValueError:
            print('  --phy: нужно 6 чисел, напр. "2 2 128 9 64 8"')
            return 2
        phy = dict(ch=vals[0], dr=vals[1], plen=vals[2],
                   code=vals[3], prf=vals[4], pac=vals[5])
        phy_label = f"Ручной ({args.phy})"
    else:
        phy = PHY_MODES[args.mode]
        phy_label = f"Mode {args.mode}"
    phy_bytes = phy_params(phy)

    print(f"Открываю {args.port} ...")
    dev = mks.MKS(args.port)

    print("INIT ...")
    st, _ = dev.init(timeout=20.0)
    print(f"  INIT: {mks.status_name(st)}")
    st, _ = dev.command(mks.CMD_SET_PHY_CONFIG, phy_bytes)
    print(f"  SET_PHY_CONFIG({phy_label}): {mks.status_name(st)}  [{phy}]")
    st, _ = dev.rx_start()
    print(f"  RX_START: {mks.status_name(st)}")

    tx_on = False
    if args.txperiodic is not None:
        st, _ = dev.tx_periodic(args.txperiodic, bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x01]))
        print(f"  TX_PERIODIC({args.txperiodic} мс): {mks.status_name(st)}")
        tx_on = True

    st, _ = dev.set_stream_mode(args.content)
    print(f"  SET_STREAM_MODE {args.content}: {mks.status_name(st)}")

    tail = "Ctrl+C — стоп." if args.seconds is None else f"авто-стоп через {args.seconds:g} c."
    print(f"\nПоток включён (content={args.content}, txperiodic={args.txperiodic}). {tail}\n")

    reader = StreamReader(dev.ser)
    t0 = time.time()
    last_print = t0
    received = 0
    prev_seq = None
    host_lost = 0
    last_dropped = 0
    peak_dropped = 0
    crc_errors = 0
    last_metrics = None
    last_cir = None
    first_frame_time = None   # время 1-го принятого кадра (для честного среднего FPS)
    last_frame_time = None    # время последнего принятого кадра

    try:
        while True:
            for body, crc_ok in reader.poll():
                if not crc_ok:
                    crc_errors += 1
                    continue
                try:
                    fr = parse_stream_body(body)
                except mks.ProtocolError:
                    crc_errors += 1
                    continue
                received += 1
                tframe = time.time()
                if first_frame_time is None:
                    first_frame_time = tframe
                last_frame_time = tframe
                seq = fr["seq"]
                last_dropped = fr["dropped"]
                peak_dropped = max(peak_dropped, last_dropped)
                last_metrics = fr["metrics"]
                last_cir = fr["cir"]
                if prev_seq is not None:
                    gap = (seq - ((prev_seq + 1) & 0xFFFF)) & 0xFFFF
                    if gap:
                        host_lost += gap
                prev_seq = seq

            now = time.time()
            if now - last_print >= 1.0:
                fps = received / (now - t0) if now > t0 else 0.0
                m = last_metrics or {}
                fpidx = last_cir.get("fp_index") if last_cir else None
                print(f"[{now - t0:6.1f}s] rx={received} fps={fps:5.1f} "
                      f"SEQ={prev_seq} DROPPED(fw)={last_dropped} host_lost={host_lost} "
                      f"crcErr={crc_errors} | "
                      f"SNR={m.get('snr_db')} RSSI={m.get('rssi_dbm')} "
                      f"FP_POWER={m.get('fp_power_dbm')} fp_index={fpidx}")
                last_print = now

            if args.seconds is not None and (now - t0) >= args.seconds:
                print("\nАвто-стоп по времени.")
                break
    except KeyboardInterrupt:
        print("\nОстанавливаю поток...")
    finally:
        try:
            dev.set_stream_mode(0)      # выключить поток на плате
        except Exception:
            pass
        try:
            dev.flush_input()           # сбросить остаток потоковых байт
            if tx_on:
                dev.tx_stop()
            dev.rx_stop()
        except Exception:
            pass
        dev.close()

    dur = time.time() - t0
    # Wagan: 2026-07-20 — средний FPS по времени 1-го..последнего кадра (совпадает с
    # мгновенным). Средний FPS — по интервалу между ПЕРВЫМ и ПОСЛЕДНИМ кадром (не по
    # всей сессии с init/хвостом), чтобы средний совпадал с мгновенным из секундных строк.
    if received >= 2 and first_frame_time is not None and last_frame_time > first_frame_time:
        avg_str = f"{received / (last_frame_time - first_frame_time):.1f}"
    else:
        avg_str = "н/д"
    print("\n===== ИТОГ =====")
    print(f"  условия:            content={args.content}, txperiodic={args.txperiodic} мс, {phy_label}")
    print(f"  длительность:       {dur:.1f} c")
    print(f"  принято кадров:     {received}")
    print(f"  средний FPS:        {avg_str} (по времени 1-го..последнего кадра)")
    print(f"  DROPPED прошивкой:  пик {peak_dropped} (u16, оборачивается)")
    print(f"  потеряно на хосте:  {host_lost} (дырки в SEQ)")
    print(f"  CRC/ошибок разбора: {crc_errors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
