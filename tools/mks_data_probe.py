#!/usr/bin/env python3
"""
*******************************************************************************
  МКС — Модуль коммуникации и сопряжения
  Хостовые инструменты (ПК) для STM32F411 + 2x DWM1000 (DW1000)

  Файл:     mks_data_probe.py
  Описание: минимальный приёмник КАНАЛА ДАННЫХ (SET_STREAM_MODE content=3,
            §15.1 стадия 0). Включает поток content=3, читает потоковые кадры
            «только данные» (тело принятого UWB-кадра без FCS) и печатает/собирает
            принятые байты. Цель — проверка фундамента канала данных на loopback
            M1→M2.

  Copyright (c) 2026 NCPR, Flexlab LLC. Все права защищены.
*******************************************************************************

Потоковый кадр content=3 (СВОЙ формат, отдельный от командного SYNC 0xAA55):
    SMARK(0xDE 0xCA) | LEN16 | SEQ | DROPPED | CONTENT=3 | data_len(u16 LE) | data | CRC8
    data = тело принятого UWB-кадра БЕЗ 2-байтного FCS (frame_len-2 из rx_frame).

Проверка приёмки стадии (0) — self-contained loopback на ОДНОЙ плате МКС (M1 TX → M2 RX):
    python mks_data_probe.py COM3 --txperiodic 50 --payload "DE AD BE EF 01"
Скрипт сам: init → SET_PHY(режим) → rx_start → [TX_PERIODIC payload] → stream content=3.
Ожидаемо: в каждом принятом кадре data == payload (байт-в-байт), SEQ растёт,
DROPPED(fw)=0 и потерь на хосте нет на умеренном темпе.

История изменений (для будущих правщиков: помечать правки в формате
  <Имя>: ГГГГ-ММ-ДД — описание — чтобы различать авторов):
  Wagan: 2026-07-22 — приёмник канала данных (content=3), §15.1 стадия 0.
"""

from __future__ import annotations

import sys
import time

import mks_protocol as mks
from mks_stream import parse_stream_body, StreamReader     # общий парсер/ридер потока

# Пресеты PHY (ключ = номер Mode 1..8; значения идентичны mks_stream_probe.py/mks_gui.py).
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
    """7 байт SET_PHY_CONFIG из пресета (ch, dr, plen u16 LE, code, prf, pac)."""
    return bytes([m["ch"] & 0xFF, m["dr"] & 0xFF,
                  m["plen"] & 0xFF, (m["plen"] >> 8) & 0xFF,
                  m["code"] & 0xFF, m["prf"] & 0xFF, m["pac"] & 0xFF])


def parse_hex(s: str) -> bytes:
    toks = s.replace(",", " ").split()
    return bytes(int(t, 16) for t in toks)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="МКС: приёмник канала данных (content=3)")
    ap.add_argument("port")
    ap.add_argument("--mode", type=int, choices=range(1, 9), default=3, metavar="1..8",
                    help="PHY-пресет Mode 1..8 (деф. 3). Приёмник должен совпадать с передатчиком")
    ap.add_argument("--txperiodic", type=int, default=None, metavar="MS",
                    help="период мс для TX_PERIODIC (loopback M1->M2); без него — пассив")
    ap.add_argument("--payload", default="DE AD BE EF 01", metavar='"hex..."',
                    help="payload для TX_PERIODIC И эталон сверки принятых данных")
    ap.add_argument("--seconds", type=float, default=None, metavar="N",
                    help="авто-стоп через N секунд (иначе до Ctrl+C)")
    ap.add_argument("--show", type=int, default=8, metavar="N",
                    help="печатать первые N принятых кадров данных подробно (деф. 8)")
    args = ap.parse_args()

    expect = parse_hex(args.payload) if args.payload else None
    phy = PHY_MODES[args.mode]

    print(f"Открываю {args.port} ...")
    dev = mks.MKS(args.port)

    print("INIT ...")
    st, _ = dev.init(timeout=20.0)
    print(f"  INIT: {mks.status_name(st)}")
    st, _ = dev.command(mks.CMD_SET_PHY_CONFIG, phy_params(phy))
    print(f"  SET_PHY_CONFIG(Mode {args.mode}): {mks.status_name(st)}  [{phy}]")
    st, _ = dev.rx_start()
    print(f"  RX_START: {mks.status_name(st)}")

    tx_on = False
    if args.txperiodic is not None:
        st, _ = dev.tx_periodic(args.txperiodic, expect or bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x01]))
        print(f"  TX_PERIODIC({args.txperiodic} мс, payload={ (expect or b'').hex(' ').upper() }): "
              f"{mks.status_name(st)}")
        tx_on = True

    st, _ = dev.set_stream_mode(3)           # канал данных
    print(f"  SET_STREAM_MODE 3 (данные): {mks.status_name(st)}")
    if st != 0x00:
        print("  !! плата не приняла content=3 — вероятно, старая прошивка. Стоп.")
        dev.close()
        return 2

    tail = "Ctrl+C — стоп." if args.seconds is None else f"авто-стоп через {args.seconds:g} c."
    print(f"\nКанал данных включён (content=3, txperiodic={args.txperiodic}). {tail}\n")

    reader = StreamReader(dev.ser)
    t0 = time.time()
    last_print = t0
    received = 0
    shown = 0
    prev_seq = None
    host_lost = 0
    last_dropped = 0
    peak_dropped = 0
    crc_errors = 0
    match_ok = 0
    match_bad = 0
    first_t = last_t = None

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
                if fr["content"] != 3:           # чужой контент — пропустить (не должно быть)
                    continue
                received += 1
                now = time.time()
                first_t = first_t or now
                last_t = now
                data = fr["data"] or b""
                seq = fr["seq"]
                last_dropped = fr["dropped"]
                peak_dropped = max(peak_dropped, last_dropped)
                if prev_seq is not None:
                    gap = (seq - ((prev_seq + 1) & 0xFFFF)) & 0xFFFF
                    if gap:
                        host_lost += gap
                prev_seq = seq

                if expect is not None:
                    if data == expect:
                        match_ok += 1
                    else:
                        match_bad += 1

                if shown < args.show:
                    shown += 1
                    txt = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
                    verdict = ""
                    if expect is not None:
                        verdict = "  == payload OK" if data == expect else "  != payload MISMATCH"
                    print(f"  SEQ={seq:5} len={len(data):3}  {data.hex(' ').upper()}  |{txt}|{verdict}")

            now = time.time()
            if now - last_print >= 1.0:
                fps = received / (now - t0) if now > t0 else 0.0
                print(f"[{now - t0:6.1f}s] rx={received} fps={fps:5.1f} SEQ={prev_seq} "
                      f"DROPPED(fw)={last_dropped} host_lost={host_lost} crcErr={crc_errors} "
                      f"match(ok/bad)={match_ok}/{match_bad}")
                last_print = now

            if args.seconds is not None and (now - t0) >= args.seconds:
                print("\nАвто-стоп по времени.")
                break
    except KeyboardInterrupt:
        print("\nОстанавливаю поток...")
    finally:
        try:
            dev.set_stream_mode(0)
        except Exception:
            pass
        try:
            dev.flush_input()
            if tx_on:
                dev.tx_stop()
            dev.rx_stop()
        except Exception:
            pass
        dev.close()

    dur = time.time() - t0
    avg = (received / (last_t - first_t)) if (received >= 2 and first_t and last_t > first_t) else None
    print("\n===== ИТОГ (канал данных, content=3) =====")
    print(f"  условия:           Mode {args.mode}, txperiodic={args.txperiodic} мс, "
          f"payload={(expect or b'').hex(' ').upper()}")
    print(f"  длительность:      {dur:.1f} c")
    print(f"  принято кадров:    {received}")
    print(f"  средний FPS:       {f'{avg:.1f}' if avg else 'н/д'}")
    print(f"  DROPPED прошивкой: пик {peak_dropped}")
    print(f"  потеряно на хосте: {host_lost} (дырки SEQ)")
    print(f"  CRC/ошибок разбора:{crc_errors}")
    if expect is not None:
        print(f"  сверка с payload:  OK={match_ok}  MISMATCH={match_bad}")
        ok = (received > 0 and match_bad == 0 and host_lost == 0)
        note = "данные совпали байт-в-байт, без потерь" if ok else "см. MISMATCH/потери выше"
        print(f"  ПРИЁМКА СТАДИИ 0:  {'ПРОЙДЕНА' if ok else 'НЕ пройдена'} ({note})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
