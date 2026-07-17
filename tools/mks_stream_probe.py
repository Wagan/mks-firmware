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
    python mks_stream_probe.py COM3 --content 2 --txperiodic 10 --seconds 25
  --content 1|2   : 1=метрики+CIR (деф.), 2=только метрики (лёгкий поток).
  --txperiodic ms : гнать TX_PERIODIC(период) для нагрузки M1->M2; без него — пассив.
  --seconds N     : авто-стоп через N c (иначе до Ctrl+C).
Скрипт сам: init -> Mode 3 -> rx_start -> [TX_PERIODIC] -> stream on. Итог в конце.
"""

from __future__ import annotations

import sys
import time
import struct

import mks_protocol as mks

SMARK = b"\xDE\xCA"

# Mode 3 (как в консоли/GUI): ch2, 110k(код0), plen1024, code9, PRF64, PAC32.
MODE3_PARAMS = bytes([2, 0, 1024 & 0xFF, (1024 >> 8) & 0xFF, 9, 64, 32])


def parse_stream_body(body: bytes) -> dict:
    """Разобрать тело потокового кадра (SEQ+DROPPED+CONTENT+PAYLOAD, без SMARK/LEN16/
    CRC). Возвращает dict: seq, dropped, content, metrics (parse_signal_metrics),
    cir (parse_cir или None — только при content==1). Раскладка: SEQ[0:2],
    DROPPED[2:4], CONTENT[4], метрики[5:35], CIR с байта 35."""
    if len(body) < 5 + 30:
        raise mks.ProtocolError(f"поток: тело короче минимума ({len(body)})")
    seq, dropped = struct.unpack_from("<HH", body, 0)
    content = body[4]
    metrics = mks.parse_signal_metrics(body[5:35])
    cir = mks.parse_cir(body[35:]) if content == 1 else None
    return {"seq": seq, "dropped": dropped, "content": content,
            "metrics": metrics, "cir": cir}


class StreamReader:
    """Извлекает потоковые кадры из байтового потока pyserial с ре-синхронизацией."""

    def __init__(self, ser):
        self.ser = ser
        self.buf = bytearray()

    def poll(self):
        """Дочитать доступные байты; вернуть список готовых (body, crc_ok)."""
        n = self.ser.in_waiting
        chunk = self.ser.read(n if n > 0 else 1)   # read(1) блокирует до POLL-таймаута
        if chunk:
            self.buf.extend(chunk)
        return list(self._extract())

    def _extract(self):
        while True:
            i = self.buf.find(SMARK)
            if i < 0:
                # держим только возможный хвост-начало SMARK
                if self.buf and self.buf[-1] == SMARK[0]:
                    del self.buf[:-1]
                else:
                    self.buf.clear()
                return
            if i > 0:
                del self.buf[:i]                    # мусор до SMARK — отбросить
            if len(self.buf) < 4:
                return                              # ждём SMARK+LEN16
            body_len = struct.unpack_from("<H", self.buf, 2)[0]
            total = 2 + 2 + body_len + 1            # SMARK+LEN16+body+CRC
            if len(self.buf) < total:
                return                              # ждём весь кадр
            frame = bytes(self.buf[:total])
            crc_input = frame[2:4 + body_len]       # LEN16 + body
            crc_ok = (mks.crc8(crc_input) == frame[4 + body_len])
            if crc_ok:
                del self.buf[:total]
                yield (frame[4:4 + body_len], True)
            else:
                del self.buf[:1]                    # рассинхрон — сдвиг на 1, ищем след. SMARK


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
    args = ap.parse_args()

    print(f"Открываю {args.port} ...")
    dev = mks.MKS(args.port)

    print("INIT ...")
    st, _ = dev.init(timeout=20.0)
    print(f"  INIT: {mks.status_name(st)}")
    st, _ = dev.command(mks.CMD_SET_PHY_CONFIG, MODE3_PARAMS)
    print(f"  SET_PHY_CONFIG(Mode 3): {mks.status_name(st)}")
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
    avg_fps = received / dur if dur > 0 else 0.0
    print("\n===== ИТОГ =====")
    print(f"  условия:            content={args.content}, txperiodic={args.txperiodic} мс")
    print(f"  длительность:       {dur:.1f} c")
    print(f"  принято кадров:     {received}")
    print(f"  средний FPS:        {avg_fps:.1f}")
    print(f"  DROPPED прошивкой:  пик {peak_dropped} (u16, оборачивается)")
    print(f"  потеряно на хосте:  {host_lost} (дырки в SEQ)")
    print(f"  CRC/ошибок разбора: {crc_errors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
