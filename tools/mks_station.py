#!/usr/bin/env python3
"""
*******************************************************************************
  МКС — Модуль коммуникации и сопряжения
  Хостовые инструменты (ПК) для STM32F411 + 2x DWM1000 (DW1000)

  Файл:     mks_station.py
  Описание: РАДИОСТАНЦИЯ поверх канала данных UWB (§15.1 стадия 1). Симметричная:
            один и тот же скрипт на обоих концах, отличие — COM-порт и src_id.
            ОДНОВРЕМЕННО принимает поток content=3 (М2) и шлёт кадры TX_FRAME (М1).
            Текстовый чат с фрагментацией (>119 Б), индикатор связи (beacon 1 с /
            порог 3 с), кнопка «вызов» (CALL x3). Прошивку НЕ трогает.

  Copyright (c) 2026 NCPR, Flexlab LLC. Все права защищены.
*******************************************************************************

Транспорт: content=3 (data-кадр) — тело принятого UWB-кадра без FCS (прошивка).
Прикладной слой (заголовок/фрагментация/реассемблер/эхо-фильтр) — в mks_msg.py.

КЛЮЧЕВОЕ (одна плата слышит себя): М2 слышит собственный М1, поэтому каждый кадр
несёт src_id станции-источника; приёмник ОТБРАСЫВАЕТ кадры со своим src_id (эхо),
кроме loopback-режима. Индикатор «слышу партнёра» = слышали кадр с ЧУЖИМ src_id за
последние link-timeout секунд. Свои (отфильтрованные) кадры таймер не обновляют.

Один владелец COM-порта: приём (StreamReader) и передача (TX_FRAME — командный кадр)
идут в ОДНОМ фоновом треде (pyserial не потокобезопасен для конкурентного чтения).
UI-тред только кладёт исходящие кадры в очередь.

Запуск (две машины/две платы в поле — проверка дальности):
    станция A:  python mks_station.py COM3 --src-id 1 --name A
    станция B:  python mks_station.py COM3 --src-id 2 --name B
Loopback-отладка на ОДНОЙ плате (слушаем себя, эхо-фильтр выкл):
    python mks_station.py COM3 --src-id 1 --loopback

Команды в консоли:  <текст> — отправить; /call — вызов; /status — статус; /quit — выход.

История изменений (для будущих правщиков: помечать правки в формате
  <Имя>: ГГГГ-ММ-ДД — описание — чтобы различать авторов):
  Wagan: 2026-07-22 — симметричная радиостанция (§15.1 стадия 1): чат+фрагментация,
                      эхо-фильтр по src_id, beacon/индикатор связи, CALL x3, loopback.
"""

from __future__ import annotations

import sys
import time
import queue
import threading

import mks_protocol as mks
import mks_msg as M
from mks_stream import StreamReader, parse_stream_body

# Пресеты PHY (ключ = Mode 1..8; значения идентичны прочим tools).
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
    return bytes([m["ch"] & 0xFF, m["dr"] & 0xFF, m["plen"] & 0xFF, (m["plen"] >> 8) & 0xFF,
                  m["code"] & 0xFF, m["prf"] & 0xFF, m["pac"] & 0xFF])


class MsgId:
    """Потокобезопасный счётчик msg_id (u8, оборачивается) — общий на станцию."""
    def __init__(self):
        self._v = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            v = self._v
            self._v = (self._v + 1) & 0xFF
            return v


class Station:
    def __init__(self, dev, src_id, name, mode, loopback,
                 beacon_sec, link_timeout, call_repeat):
        self.dev = dev
        self.src_id = src_id
        self.name = name
        self.mode = mode
        self.loopback = loopback
        self.beacon_sec = beacon_sec
        self.link_timeout = link_timeout
        self.call_repeat = call_repeat

        self.msgid = MsgId()
        self.tx_q = queue.Queue()            # исходящие кадры (bytes, <=125)
        self.reasm = M.Reassembler(timeout=max(3.0, link_timeout))
        self.running = False

        # состояние связи (владелец — engine-тред)
        self.last_heard = 0.0                # монотонное время последнего ЧУЖОГО кадра
        self.last_heard_src = None
        self.link_up = False
        self._last_beacon = 0.0
        self._last_status = 0.0

        # счётчики (диагностика)
        self.rx_frames = 0
        self.rx_echo = 0
        self.rx_unrec = 0

    # ------------------------------------------------------------- отправка --
    def send_text(self, text: str):
        mid = self.msgid.next()
        for fr in M.fragment_text(text, self.src_id, mid):
            self.tx_q.put(fr)

    def send_call(self):
        mid = self.msgid.next()
        fr = M.fragment(b"", self.src_id, mid, M.MSG_CALL)[0]
        for _ in range(self.call_repeat):
            self.tx_q.put(fr)                # один и тот же (src,msg_id) x N — dedup на приёме

    def _send_beacon(self):
        mid = self.msgid.next()
        fr = M.fragment(b"", self.src_id, mid, M.MSG_BEACON)[0]   # данные пусты (RSSI — позже)
        try:
            self.dev.tx_frame(fr)
        except Exception:
            pass

    # ------------------------------------------------------- engine (порт) --
    def engine(self):
        reader = StreamReader(self.dev.ser)
        while self.running:
            now = time.monotonic()

            # 1) beacon по таймеру
            if now - self._last_beacon >= self.beacon_sec:
                self._last_beacon = now
                self._send_beacon()

            # 2) исходящие кадры (текст/вызов) — блокирующий TX_FRAME сериализует сам
            sent_any = False
            try:
                while True:
                    fr = self.tx_q.get_nowait()
                    try:
                        st, _ = self.dev.tx_frame(fr)
                        if st != 0x00:
                            self._out(f"[TX] предупреждение: {mks.status_name(st)}")
                    except Exception as e:
                        self._out(f"[TX] ошибка: {e}")
                    sent_any = True
            except queue.Empty:
                pass
            if sent_any:
                reader.buf.clear()           # после команд буфер мог разойтись — ресинк

            # 3) приём потока content=3
            try:
                frames = reader.poll()
            except Exception as e:
                self._out(f"[RX] поток прерван: {e}")
                break
            for body, crc_ok in frames:
                if not crc_ok:
                    continue
                try:
                    sf = parse_stream_body(body)
                except mks.ProtocolError:
                    continue
                if sf["content"] != 3 or not sf["data"]:
                    continue
                self.rx_frames += 1
                f = M.parse_frame(sf["data"])
                if not f["recognized"]:
                    self.rx_unrec += 1
                    continue
                if M.is_own_echo(f["src_id"], self.src_id, self.loopback):
                    self.rx_echo += 1
                    continue
                # чужой (партнёрский) кадр — обновляем индикатор связи
                self.last_heard = now
                self.last_heard_src = f["src_id"]
                for ev in self.reasm.add(f, now):
                    self._handle_event(ev)

            # 4) неполные по таймауту
            for ev in self.reasm.sweep(now):
                self._handle_event(ev)

            # 5) индикатор связи (смена состояния) + периодический статус
            self._update_link(now)

    def _handle_event(self, ev):
        kind = ev[0]
        if kind == "complete":
            _, src, mid, mtype, data = ev
            if mtype == M.MSG_TEXT:
                self._out(f"[{src}] {M.decode_text(data)}")
            elif mtype == M.MSG_CALL:
                self._out(f"\a*** ВЫЗОВ от станции {src} *** (нажмите /status)")
            # BEACON complete — не печатаем (только обновил индикатор выше)
        elif kind == "incomplete":
            _, src, mid, mtype, partial, missing, cnt = ev
            if mtype == M.MSG_TEXT:
                miss = ",".join(map(str, missing))
                self._out(f"[{src}] {M.decode_text(partial)}  «НЕПОЛНОЕ: потерян фрагмент "
                          f"{miss} из {cnt}»")
            # неполный BEACON/CALL молча игнорируем (они однофрагментные — до сюда не дойдут)

    def _update_link(self, now):
        up = (self.last_heard > 0.0) and ((now - self.last_heard) < self.link_timeout)
        if up != self.link_up:
            self.link_up = up
            if up:
                self._out(f"*** СВЯЗЬ ЕСТЬ (партнёр src {self.last_heard_src}) ***")
            else:
                self._out("*** СВЯЗЬ ПОТЕРЯНА ***")
        # периодический короткий статус (индикатор «постоянно виден»)
        if now - self._last_status >= 5.0:
            self._last_status = now
            self._out(self.status_line(now))

    def status_line(self, now=None):
        now = now or time.monotonic()
        if self.last_heard <= 0.0:
            link = "СВЯЗЬ: — (партнёр не слышен)"
        else:
            age = now - self.last_heard
            link = (f"СВЯЗЬ ЕСТЬ (src {self.last_heard_src}, {age:.1f}s назад)"
                    if age < self.link_timeout else f"СВЯЗЬ ПОТЕРЯНА ({age:.1f}s без партнёра)")
        return (f"[status {self.name}/src{self.src_id}] {link} | RSSI: н/д (content=3) | "
                f"rx={self.rx_frames} эхо={self.rx_echo} нераспозн={self.rx_unrec}")

    def _out(self, text):
        # печать из engine-треда; в консоли может перемешаться с приглашением — ок.
        print("\n" + text, flush=True)


def open_and_setup(port, mode):
    dev = mks.MKS(port)
    try:
        dev.flush_input(); dev.set_stream_mode(0); dev.flush_input()   # страховка
    except Exception:
        pass
    st, _ = dev.init(timeout=20.0)
    print(f"  INIT: {mks.status_name(st)}")
    st, _ = dev.command(mks.CMD_SET_PHY_CONFIG, phy_params(PHY_MODES[mode]))
    print(f"  SET_PHY_CONFIG(Mode {mode}): {mks.status_name(st)}")
    st, _ = dev.rx_start()
    print(f"  RX_START: {mks.status_name(st)}")
    st, _ = dev.set_stream_mode(3)
    print(f"  SET_STREAM_MODE 3 (данные): {mks.status_name(st)}")
    if st != 0x00:
        raise RuntimeError("плата не приняла content=3 — вероятно, старая прошивка")
    return dev


def main():
    import argparse
    ap = argparse.ArgumentParser(description="МКС: радиостанция канала данных (§15.1 стадия 1)")
    ap.add_argument("port")
    ap.add_argument("--src-id", type=int, required=True, metavar="1..255",
                    help="id станции-источника (1..255; 0 зарезервирован)")
    ap.add_argument("--mode", type=int, choices=range(1, 9), default=3, metavar="1..8",
                    help="PHY-пресет Mode 1..8 (деф. 3). Обе станции — один режим")
    ap.add_argument("--name", default=None, help="метка станции для вывода (деф. ST<src>)")
    ap.add_argument("--loopback", action="store_true",
                    help="одна плата: слушать СВОИ кадры (эхо-фильтр выкл) — для отладки")
    ap.add_argument("--beacon-sec", type=float, default=1.0, help="период beacon, с (деф. 1)")
    ap.add_argument("--link-timeout", type=float, default=3.0, help="порог «связь потеряна», с (деф. 3)")
    ap.add_argument("--call-repeat", type=int, default=3, help="повторов CALL (деф. 3)")
    args = ap.parse_args()

    if not (1 <= args.src_id <= 255):
        print("  --src-id вне 1..255 (0 зарезервирован под broadcast)")
        return 2
    name = args.name or f"ST{args.src_id}"

    print(f"Открываю {args.port} (станция {name}, src_id={args.src_id}, "
          f"Mode {args.mode}{', LOOPBACK' if args.loopback else ''}) ...")
    dev = open_and_setup(args.port, args.mode)

    st = Station(dev, args.src_id, name, args.mode, args.loopback,
                 args.beacon_sec, args.link_timeout, args.call_repeat)
    st.running = True
    eng = threading.Thread(target=st.engine, daemon=True)
    eng.start()

    print(f"\nСтанция {name} на связи. Команды: <текст> отправить | /call вызов | "
          f"/status | /quit. Ctrl+C — выход.\n")
    try:
        while True:
            line = input()
            if line == "":
                continue
            if line in ("/quit", "/exit", "/q"):
                break
            elif line == "/call":
                st.send_call()
                print("  → ВЫЗОВ отправлен")
            elif line == "/status":
                print("  " + st.status_line())
            elif line.startswith("/"):
                print("  команды: /call /status /quit  (или просто текст для отправки)")
            else:
                st.send_text(line)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        st.running = False
        eng.join(timeout=1.5)
        try:
            dev.set_stream_mode(0); dev.flush_input(); dev.rx_stop()
        except Exception:
            pass
        dev.close()
        print("\nСтанция остановлена.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
