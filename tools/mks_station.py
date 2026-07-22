#!/usr/bin/env python3
"""
*******************************************************************************
  МКС — Модуль коммуникации и сопряжения
  Хостовые инструменты (ПК) для STM32F411 + 2x DWM1000 (DW1000)

  Файл:     mks_station.py
  Описание: РАДИОСТАНЦИЯ поверх канала данных UWB (§15.1). Ядро `Station`
            (транспорт content=4 + реассемблер + эхо-фильтр + beacon + метрики
            партнёра) — ОБЩЕЕ для консольной станции и GUI (tools\\mks_station_gui.py):
            станция не печатает сама, а ЭМИТИТ события через callback on_event, а
            состояние партнёра (RSSI/FP/SNR/связь) отдаёт атрибутами/снимком.
            Прошивку НЕ трогает.

  Copyright (c) 2026 NCPR, Flexlab LLC. Все права защищены.
*******************************************************************************

Транспорт: поток content=4 (данные+метрики) — тело принятого кадра БЕЗ FCS + блок
метрик 30 Б ТОГО ЖЕ кадра (прошивка, заход 1). Прикладной слой (заголовок 6 Б /
фрагментация / реассемблер / эхо-фильтр) — mks_msg.py (не дублировать в GUI).

RSSI партнёра: метрики берутся ТОЛЬКО из партнёрских кадров — эхо-фильтр по src_id
(М2 слышит свой М1) отсекает свои, поэтому RSSI/FP_POWER/SNR привязаны к партнёру.
При потере связи (таймаут) метрики помечаются устаревшими (не «замороженными»).

Один владелец COM-порта: приём (StreamReader) и передача (TX_FRAME) — в ОДНОМ
engine-треде (pyserial не потокобезопасен). UI кладёт исходящее в очередь, а
принятое получает через on_event (в GUI — перекладывается в tk-очередь и рисуется
из главного треда).

Запуск консоли (две машины/две платы в поле):
    станция A:  python mks_station.py COM3 --src-id 1 --name A
    станция B:  python mks_station.py COM3 --src-id 2 --name B
Loopback-отладка на одной плате (слушаем себя):
    python mks_station.py COM3 --src-id 1 --loopback
Команды: <текст> отправить | /call вызов | /status | /quit.

История изменений (для будущих правщиков: помечать правки в формате
  <Имя>: ГГГГ-ММ-ДД — описание — чтобы различать авторов):
  Wagan: 2026-07-22 — симметричная радиостанция (§15.1 стадия 1): чат+фрагментация,
                      эхо-фильтр по src_id, beacon/индикатор связи, CALL x3, loopback.
  Wagan: 2026-07-22 — заход 2 RSSI: поток content=4, метрики партнёра (last_rssi/fp/snr),
                      Station эмитит события через on_event (общее ядро для консоли и GUI).
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

STREAM_CONTENT = 4      # заход 2: станция слушает поток «данные+метрики»


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
    """Ядро станции — ОБЩЕЕ для консоли и GUI. Не печатает: эмитит события через
    on_event(dict) и отдаёт состояние партнёра атрибутами / metrics_snapshot().

    Типы событий on_event:
      {"type":"text",  "src", "text", "complete":bool, "missing":[...], "cnt"}
      {"type":"call",  "src"}
      {"type":"link",  "up":bool, "src"}
      {"type":"sent",  "text", "cnt"}        # своё исходящее (для лога GUI)
      {"type":"status","line"}               # периодическая строка (для консоли)
      {"type":"tx_warn","msg"} / {"type":"error","msg"}
    """

    def __init__(self, dev, src_id, name, mode=3, loopback=False,
                 beacon_sec=1.0, link_timeout=3.0, call_repeat=3,
                 content=STREAM_CONTENT, on_event=None):
        self.dev = dev
        self.src_id = src_id
        self.name = name
        self.mode = mode
        self.loopback = loopback
        self.beacon_sec = beacon_sec
        self.link_timeout = link_timeout
        self.call_repeat = call_repeat
        self.content = content
        self.on_event = on_event or (lambda ev: None)

        self.msgid = MsgId()
        self.tx_q = queue.Queue()
        self.reasm = M.Reassembler(timeout=max(3.0, link_timeout))
        self.running = False

        # состояние партнёра (владелец — engine-тред)
        self.last_heard = 0.0
        self.last_heard_src = None
        self.last_rssi = None            # dBm (RX_LEVEL) партнёра
        self.last_fp = None              # dBm (FP_POWER)
        self.last_snr = None             # dB
        self.link_up = False
        self._last_beacon = 0.0
        self._last_status = 0.0

        self.rx_frames = 0
        self.rx_echo = 0
        self.rx_unrec = 0

    def _emit(self, **ev):
        self.on_event(ev)

    # ------------------------------------------------------------- отправка --
    def send_text(self, text: str):
        mid = self.msgid.next()
        frames = M.fragment_text(text, self.src_id, mid)
        for fr in frames:
            self.tx_q.put(fr)
        self._emit(type="sent", text=text, cnt=len(frames))

    def send_call(self):
        mid = self.msgid.next()
        fr = M.fragment(b"", self.src_id, mid, M.MSG_CALL)[0]
        for _ in range(self.call_repeat):
            self.tx_q.put(fr)            # один и тот же (src,msg_id) x N — dedup на приёме
        self._emit(type="sent", text="[ВЫЗОВ]", cnt=1)

    def _send_beacon(self):
        mid = self.msgid.next()
        fr = M.fragment(b"", self.src_id, mid, M.MSG_BEACON)[0]   # данные пусты (RSSI — из content=4)
        try:
            self.dev.tx_frame(fr)
        except Exception:
            pass

    # ------------------------------------------------- разбор одного кадра --
    def _ingest(self, sf, now):
        """Обработать один потоковый кадр (dict из parse_stream_body). Эхо-фильтр,
        метрики партнёра, реассемблер. Вынесено для юнит-тестов (без serial)."""
        if sf["content"] != self.content or not sf["data"]:
            return
        self.rx_frames += 1
        f = M.parse_frame(sf["data"])
        if not f["recognized"]:
            self.rx_unrec += 1
            return
        if M.is_own_echo(f["src_id"], self.src_id, self.loopback):
            self.rx_echo += 1
            return                       # своё эхо — НЕ обновляет ни связь, ни RSSI
        # партнёрский кадр
        self.last_heard = now
        self.last_heard_src = f["src_id"]
        self._update_metrics(sf.get("metrics"))
        for ev in self.reasm.add(f, now):
            self._handle_reasm(ev)

    def _update_metrics(self, m):
        if not m or m.get("format") != "final":
            return
        if m.get("rssi_valid"):
            self.last_rssi = m["rssi_dbm"]
        if m.get("fp_valid"):
            self.last_fp = m["fp_power_dbm"]
        if m.get("snr_valid"):
            self.last_snr = m["snr_db"]

    def _handle_reasm(self, ev):
        kind = ev[0]
        if kind == "complete":
            _, src, mid, mtype, data = ev
            if mtype == M.MSG_TEXT:
                self._emit(type="text", src=src, text=M.decode_text(data),
                           complete=True, missing=[], cnt=1)
            elif mtype == M.MSG_CALL:
                self._emit(type="call", src=src)
            # BEACON complete — не логируем (обновил связь/метрики в _ingest)
        elif kind == "incomplete":
            _, src, mid, mtype, partial, missing, cnt = ev
            if mtype == M.MSG_TEXT:
                self._emit(type="text", src=src, text=M.decode_text(partial),
                           complete=False, missing=missing, cnt=cnt)

    # --------------------------------------------------------- engine (порт) --
    def engine(self):
        reader = StreamReader(self.dev.ser)
        while self.running:
            now = time.monotonic()

            if now - self._last_beacon >= self.beacon_sec:
                self._last_beacon = now
                self._send_beacon()

            sent_any = False
            try:
                while True:
                    fr = self.tx_q.get_nowait()
                    try:
                        st, _ = self.dev.tx_frame(fr)
                        if st != 0x00:
                            self._emit(type="tx_warn", msg=mks.status_name(st))
                    except Exception as e:
                        self._emit(type="tx_warn", msg=str(e))
                    sent_any = True
            except queue.Empty:
                pass
            if sent_any:
                reader.buf.clear()

            try:
                frames = reader.poll()
            except Exception as e:
                self._emit(type="error", msg=str(e))
                break
            for body, crc_ok in frames:
                if not crc_ok:
                    continue
                try:
                    sf = parse_stream_body(body)
                except mks.ProtocolError:
                    continue
                self._ingest(sf, now)

            for ev in self.reasm.sweep(now):
                self._handle_reasm(ev)

            self._update_link(now)

    def _update_link(self, now):
        up = (self.last_heard > 0.0) and ((now - self.last_heard) < self.link_timeout)
        if up != self.link_up:
            self.link_up = up
            self._emit(type="link", up=up, src=self.last_heard_src)
        if now - self._last_status >= 5.0:
            self._last_status = now
            self._emit(type="status", line=self.status_line(now))

    # ----------------------------------------------------- состояние наружу --
    def metrics_snapshot(self, now=None):
        """Снимок для UI: RSSI/FP/SNR партнёра + свежесть (fresh=связь активна)."""
        now = now or time.monotonic()
        fresh = (self.last_heard > 0.0) and ((now - self.last_heard) < self.link_timeout)
        return {"rssi": self.last_rssi, "fp": self.last_fp, "snr": self.last_snr,
                "fresh": fresh, "src": self.last_heard_src,
                "age": (now - self.last_heard) if self.last_heard > 0.0 else None}

    def status_line(self, now=None):
        now = now or time.monotonic()
        snap = self.metrics_snapshot(now)
        if self.last_heard <= 0.0:
            link = "СВЯЗЬ: — (партнёр не слышен)"
        else:
            link = (f"СВЯЗЬ ЕСТЬ (src {snap['src']}, {snap['age']:.1f}s назад)"
                    if snap["fresh"] else f"СВЯЗЬ ПОТЕРЯНА ({snap['age']:.1f}s без партнёра)")

        def q(v, unit):
            if v is None:
                return "н/д"
            return f"{v:.1f}{unit}" + ("" if snap["fresh"] else " (устар.)")

        met = (f"RSSI={q(snap['rssi'], ' dBm')} FP={q(snap['fp'], ' dBm')} "
               f"SNR={q(snap['snr'], ' dB')}")
        return (f"[status {self.name}/src{self.src_id}] {link} | {met} | "
                f"rx={self.rx_frames} эхо={self.rx_echo} нераспозн={self.rx_unrec}")

    # ---------------------------------------------------- жизненный цикл --
    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self.engine, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        t = getattr(self, "_thread", None)
        if t is not None:
            t.join(timeout=1.5)


def open_and_setup(port, mode, content=STREAM_CONTENT):
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
    st, _ = dev.set_stream_mode(content)
    print(f"  SET_STREAM_MODE {content} (данные+метрики): {mks.status_name(st)}")
    if st != 0x00:
        raise RuntimeError(f"плата не приняла content={content} — вероятно, старая прошивка")
    return dev


def _console_printer(ev):
    """on_event для консоли: печатает принятое; своё исходящее ('sent') не дублирует."""
    t = ev["type"]
    if t == "text":
        if ev["complete"]:
            print(f"\n[{ev['src']}] {ev['text']}", flush=True)
        else:
            miss = ",".join(map(str, ev["missing"]))
            print(f"\n[{ev['src']}] {ev['text']}  «НЕПОЛНОЕ: потерян фрагмент {miss} из {ev['cnt']}»",
                  flush=True)
    elif t == "call":
        print(f"\n\a*** ВЫЗОВ от станции {ev['src']} *** (/status)", flush=True)
    elif t == "link":
        s = f"СВЯЗЬ ЕСТЬ (партнёр src {ev['src']})" if ev["up"] else "СВЯЗЬ ПОТЕРЯНА"
        print(f"\n*** {s} ***", flush=True)
    elif t == "status":
        print("\n" + ev["line"], flush=True)
    elif t == "tx_warn":
        print(f"\n[TX] {ev['msg']}", flush=True)
    elif t == "error":
        print(f"\n[RX] поток прерван: {ev['msg']}", flush=True)
    # "sent" — пользователь сам ввёл в консоль, не дублируем


def main():
    import argparse
    ap = argparse.ArgumentParser(description="МКС: радиостанция канала данных (§15.1, content=4)")
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
                 args.beacon_sec, args.link_timeout, args.call_repeat,
                 on_event=_console_printer)
    st.start()

    print(f"\nСтанция {name} на связи (content=4). Команды: <текст> отправить | /call вызов | "
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
        st.stop()
        try:
            dev.set_stream_mode(0); dev.flush_input(); dev.rx_stop()
        except Exception:
            pass
        dev.close()
        print("\nСтанция остановлена.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
