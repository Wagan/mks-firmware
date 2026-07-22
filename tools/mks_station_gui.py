#!/usr/bin/env python3
"""
*******************************************************************************
  МКС — Модуль коммуникации и сопряжения
  Хостовые инструменты (ПК) для STM32F411 + 2x DWM1000 (DW1000)

  Файл:     mks_station_gui.py
  Описание: GUI-РАДИОСТАНЦИЯ (tkinter) поверх ОБЩЕГО ядра Station (mks_station.py).
            Не форк: транспорт content=4 / реассемблер / эхо-фильтр / метрики —
            в Station; GUI лишь подписывается на события и рисует. §15.1 заход 2.
            Сигнал-шкала RX_LEVEL (как индикатор сети смартфона) для замера рабочей
            зоны отходом с ноутбуком; FP_POWER/SNR числами; индикатор связи; адреса;
            лог сообщений (полное/неполное, свои и чужие); ввод с живым счётчиком
            байт/фрагментов; кнопка «Вызов». Прошивку НЕ трогает.

  Copyright (c) 2026 NCPR, Flexlab LLC. Все права защищены.
*******************************************************************************

Запуск:
    python mks_station_gui.py COM3 --src-id 1
    python mks_station_gui.py COM3 --src-id 1 --loopback   # отладка на одной плате

История изменений (для будущих правщиков: помечать правки в формате
  <Имя>: ГГГГ-ММ-ДД — описание — чтобы различать авторов):
  Wagan: 2026-07-22 — GUI-станция (§15.1 заход 2): сигнал-шкала RX_LEVEL, FP/SNR,
                      индикатор связи, лог, счётчик байт/фрагментов, CALL. Поверх
                      общего ядра Station (mks_station.py) — без дублирования протокола.
"""

from __future__ import annotations

import sys
import math
import time
import queue

import tkinter as tk
from tkinter import ttk

import mks_msg as M
import mks_station as ST

APP_TITLE = "МКС-Станция для MATLAB. © 2026 Flexlab | Progresstech"

# Пороги маппинга RX_LEVEL (dBm) → сигнал-шкала. Подобрать на железе (константы).
RSSI_FULL = -75.0   # >= этого → полная шкала
RSSI_EDGE = -95.0   # <= этого → край (0 полосок)
N_BARS    = 5
TICK_MS   = 200     # период обновления UI
CALL_FLASH_S = 4.0  # сколько держать баннер входящего вызова


def dbm_to_bars(dbm, nbars=N_BARS, full=RSSI_FULL, edge=RSSI_EDGE):
    """RX_LEVEL (dBm) → число полосок 0..nbars. None → 0. Линейно между edge и full."""
    if dbm is None:
        return 0
    if dbm >= full:
        return nbars
    if dbm <= edge:
        return 0
    frac = (dbm - edge) / (full - edge)           # 0..1
    return max(0, min(nbars, int(math.ceil(frac * nbars))))


def frag_count(nbytes, frag_data=M.FRAG_DATA):
    """Число фрагментов сообщения длиной nbytes (Б): ceil(n/119), минимум 1 (пустое)."""
    return max(1, (nbytes + frag_data - 1) // frag_data)


def bars_color(bars, nbars=N_BARS):
    """Цвет шкалы по числу полосок: край→красный, середина→жёлтый, много→зелёный."""
    if bars <= 0:
        return "#777777"
    if bars <= nbars // 3 + 1 and bars <= 2:
        return "#d93025"      # красный
    if bars <= (2 * nbars) // 3:
        return "#f9ab00"      # жёлтый
    return "#1e8e3e"          # зелёный


class StationGui:
    def __init__(self, root, station):
        self.root = root
        self.st = station
        self.evq = queue.Queue()
        self.st.on_event = self.evq.put            # события ядра → tk-очередь (thread-safe)
        self._call_until = 0.0

        root.title(f"Станция src {station.src_id} ({station.name})")
        self._build()
        self.root.after(TICK_MS, self._tick)

    # ------------------------------------------------------------------ UI --
    def _build(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(4, weight=1)

        ttk.Label(self.root, text=APP_TITLE, font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(6, 0))

        # --- Панель связи/сигнала ---
        top = ttk.LabelFrame(self.root, text="Связь")
        top.grid(row=1, column=0, sticky="ew", padx=6, pady=4)
        top.columnconfigure(6, weight=1)

        ttk.Label(top, text="Я:").grid(row=0, column=0, sticky="e", padx=(6, 0), pady=4)
        ttk.Label(top, text=f"src {self.st.src_id} ({self.st.name})",
                  font=("Consolas", 10, "bold")).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(top, text="Партнёр:").grid(row=0, column=2, sticky="e")
        self.partner_var = tk.StringVar(value="—")
        ttk.Label(top, textvariable=self.partner_var, font=("Consolas", 10, "bold")).grid(
            row=0, column=3, sticky="w", padx=4)

        self.link_lbl = tk.Label(top, text="СВЯЗЬ: —", font=("Segoe UI", 10, "bold"),
                                 fg="white", bg="#777777", padx=8, pady=2)
        self.link_lbl.grid(row=0, column=4, padx=8)
        self.age_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.age_var).grid(row=0, column=5, sticky="w")

        # Сигнал-шкала RX_LEVEL (полоски) + числа
        sig = ttk.Frame(top)
        sig.grid(row=1, column=0, columnspan=7, sticky="w", padx=6, pady=(2, 6))
        ttk.Label(sig, text="RX_LEVEL:").pack(side="left")
        self.bars = tk.Canvas(sig, width=N_BARS * 16 + 6, height=40, highlightthickness=0)
        self.bars.pack(side="left", padx=6)
        self.rssi_var = tk.StringVar(value="RSSI: н/д")
        self.fp_var = tk.StringVar(value="FP_POWER: н/д")
        self.snr_var = tk.StringVar(value="SNR: н/д")
        for v in (self.rssi_var, self.fp_var, self.snr_var):
            ttk.Label(sig, textvariable=v, font=("Consolas", 10), width=20,
                      anchor="w").pack(side="left", padx=6)

        # --- Панель мощности TX (свой передатчик) ---
        self._build_power_panel()

        # --- Баннер входящего вызова ---
        self.call_lbl = tk.Label(self.root, text="", font=("Segoe UI", 13, "bold"),
                                 fg="white", bg=self.root.cget("bg"))
        self.call_lbl.grid(row=3, column=0, sticky="ew", padx=6)

        # --- Лог сообщений ---
        logf = ttk.LabelFrame(self.root, text="Сообщения")
        logf.grid(row=4, column=0, sticky="nsew", padx=6, pady=4)
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)
        self.log = tk.Text(logf, height=14, width=70, state="disabled", wrap="word",
                           font=("Consolas", 10))
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)
        self.log.tag_configure("me", foreground="#1a73e8")
        self.log.tag_configure("them", foreground="#188038")
        self.log.tag_configure("bad", foreground="#d93025")
        self.log.tag_configure("sys", foreground="#777777")

        # --- Ввод + счётчик + кнопки ---
        bottom = ttk.Frame(self.root)
        bottom.grid(row=5, column=0, sticky="ew", padx=6, pady=(0, 6))
        bottom.columnconfigure(0, weight=1)
        self.entry = ttk.Entry(bottom, font=("Consolas", 11))
        self.entry.grid(row=0, column=0, sticky="ew")
        self.entry.bind("<Return>", lambda e: self._send())
        self.entry.bind("<KeyRelease>", lambda e: self._update_counter())
        self.count_var = tk.StringVar(value="0 Б → 1 фрагм.")
        ttk.Label(bottom, textvariable=self.count_var, width=18, anchor="center").grid(
            row=0, column=1, padx=6)
        ttk.Button(bottom, text="Отправить", command=self._send).grid(row=0, column=2, padx=2)
        ttk.Button(bottom, text="Вызов", command=self._call).grid(row=0, column=3, padx=2)

        self._draw_bars(0, fresh=False)
        self._log("система", "Станция запущена (content=4). Введите текст и Enter.", "sys")

    def _build_power_panel(self):
        # канал/PRF станции -> «стандарт под маску» (Table 20) и стартовый уровень
        phy = ST.PHY_MODES.get(self.st.mode, {})
        self._ch = phy.get("ch")
        self._prf = phy.get("prf")
        self._std_level = ST.std_tx_level(self._ch, self._prf)
        start = self._std_level if self._std_level is not None else (ST.POWER_LEVEL_MAX // 2)

        pf = ttk.LabelFrame(self.root, text="Мощность TX (свой передатчик M1)")
        pf.grid(row=2, column=0, sticky="ew", padx=6, pady=2)
        pf.columnconfigure(1, weight=1)

        ttk.Label(pf, text="слабее ↔ мощнее:").grid(row=0, column=0, sticky="w", padx=6)
        self.level_var = tk.IntVar(value=start)
        self.power_scale = tk.Scale(pf, from_=0, to=ST.POWER_LEVEL_MAX, orient="horizontal",
                                    variable=self.level_var, showvalue=False,
                                    command=lambda v: self._on_power_move())
        self.power_scale.grid(row=0, column=1, sticky="ew", padx=6)
        # применяем ТОЛЬКО по отпусканию (не флудить командами на каждый пиксель)
        self.power_scale.bind("<ButtonRelease-1>", lambda e: self._apply_power())

        self.power_lbl = tk.Label(pf, text="", font=("Consolas", 10), width=42, anchor="w")
        self.power_lbl.grid(row=0, column=2, sticky="w", padx=6)

        btns = ttk.Frame(pf)
        btns.grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 4))
        std_txt = "Стандарт (под маску) = дефолт set_phy"
        ttk.Button(btns, text=std_txt,
                   command=lambda: self._preset(self._std_level)).pack(side="left", padx=2)
        ttk.Button(btns, text="Железный макс (лаб., ~9 dB выше маски)",
                   command=lambda: self._preset(ST.POWER_LEVEL_MAX)).pack(side="left", padx=2)
        self.power_applied = tk.Label(btns, text="применено: — (дефалт set_phy)",
                                      foreground="#777777")
        self.power_applied.pack(side="left", padx=10)

        self._on_power_move()          # заполнить подпись (без отправки)

    # -------------------------------------------------------------- действия --
    def _send(self):
        text = self.entry.get()
        if not text:
            return
        self.st.send_text(text)
        self.entry.delete(0, "end")
        self._update_counter()

    def _call(self):
        self.st.send_call()
        self._log("система", "→ ВЫЗОВ отправлен", "sys")

    # -------------------------------------------------------------- мощность --
    def _power_zone(self, level):
        if self._std_level is None:
            return ""
        if level <= self._std_level:
            return "под/на маске"
        return "ВЫШЕ маски (стенд)"

    def _on_power_move(self):
        lvl = int(self.level_var.get())
        reg = ST.level_to_reg(lvl)
        self.power_lbl.configure(text=f"level=0x{lvl:02X}  рег=0x{reg:08X}  [{self._power_zone(lvl)}]")

    def _apply_power(self):
        lvl = int(self.level_var.get())
        self.st.request_tx_power(lvl)          # исполнит engine-тред, вернёт событие txpower

    def _preset(self, level):
        if level is None:
            return
        self.level_var.set(int(level))
        self._on_power_move()
        self._apply_power()

    def _update_counter(self):
        nbytes = len(self.entry.get().encode("utf-8"))
        self.count_var.set(f"{nbytes} Б → {frag_count(nbytes)} фрагм.")

    # ------------------------------------------------------- отрисовка/тик --
    def _draw_bars(self, bars, fresh):
        c = self.bars
        c.delete("all")
        col = bars_color(bars) if fresh else "#777777"
        for i in range(N_BARS):
            x = 3 + i * 16
            h = 8 + i * 6                 # растущие полоски
            y1 = 38 - h
            on = (i < bars) and fresh
            c.create_rectangle(x, y1, x + 12, 38, outline="#444",
                               fill=(col if on else "#e0e0e0"))

    def _log(self, who, text, tag):
        self.log.configure(state="normal")
        self.log.insert("end", f"{who}: ", tag)
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _tick(self):
        # 1) события ядра
        try:
            while True:
                ev = self.evq.get_nowait()
                self._handle(ev)
        except queue.Empty:
            pass
        # 2) живые метрики/индикатор
        now = time.monotonic()
        snap = self.st.metrics_snapshot(now)
        fresh = snap["fresh"]
        self.partner_var.set(str(snap["src"]) if snap["src"] is not None else "—")

        if snap["src"] is None:
            self.link_lbl.configure(text="СВЯЗЬ: —", bg="#777777")
            self.age_var.set("партнёр не слышен")
        elif fresh:
            self.link_lbl.configure(text="СВЯЗЬ ЕСТЬ", bg="#1e8e3e")
            self.age_var.set(f"{snap['age']:.1f} с назад")
        else:
            self.link_lbl.configure(text="СВЯЗЬ ПОТЕРЯНА", bg="#d93025")
            self.age_var.set(f"{snap['age']:.1f} с без партнёра")

        def q(v, unit, name):
            if v is None:
                return f"{name}: н/д"
            return f"{name}: {v:.1f}{unit}" + ("" if fresh else " (устар.)")
        self.rssi_var.set(q(snap["rssi"], " dBm", "RSSI"))
        self.fp_var.set(q(snap["fp"], " dBm", "FP_POWER"))
        self.snr_var.set(q(snap["snr"], " dB", "SNR"))
        self._draw_bars(dbm_to_bars(snap["rssi"]), fresh)

        # 3) гашение баннера вызова
        if self._call_until and now >= self._call_until:
            self._call_until = 0.0
            self.call_lbl.configure(text="", bg=self.root.cget("bg"))

        self.root.after(TICK_MS, self._tick)

    def _handle(self, ev):
        t = ev["type"]
        if t == "text":
            if ev["complete"]:
                self._log(f"[{ev['src']}]", ev["text"], "them")
            else:
                miss = ",".join(map(str, ev["missing"]))
                self._log(f"[{ev['src']}]",
                          f"{ev['text']}   ⟨НЕПОЛНОЕ: потерян фрагмент {miss} из {ev['cnt']}⟩", "bad")
        elif t == "sent":
            self._log("→ я", ev["text"], "me")
        elif t == "call":
            self.root.bell()
            self.call_lbl.configure(text=f"◀◀  ВЫЗОВ от станции {ev['src']}  ▶▶", bg="#d93025")
            self._call_until = time.monotonic() + CALL_FLASH_S
            self._log("система", f"ВЫЗОВ от станции {ev['src']}", "bad")
        elif t == "link":
            pass                          # индикатор обновляется в _tick по snapshot
        elif t == "tx_warn":
            self._log("система", f"TX: {ev['msg']}", "sys")
        elif t == "txpower":
            reg = f"0x{ev['reg']:08X}" if ev.get("reg") is not None else "—"
            if ev.get("ok"):
                self.power_applied.configure(
                    text=f"применено: level=0x{ev['level']:02X} рег={reg}", foreground="#188038")
                self._log("система", f"мощность TX применена: level=0x{ev['level']:02X} рег={reg}", "sys")
            else:
                self.power_applied.configure(text=f"ошибка: {ev.get('status')}", foreground="#d93025")
                self._log("система", f"мощность TX: ошибка {ev.get('status')}", "bad")
        elif t == "error":
            self._log("система", f"поток прерван: {ev['msg']}", "bad")
        # "status" — для консоли; в GUI индикатор живой, игнорируем


def main():
    import argparse
    ap = argparse.ArgumentParser(description="МКС: GUI-радиостанция канала данных (§15.1)")
    ap.add_argument("port")
    ap.add_argument("--src-id", type=int, required=True, metavar="1..255")
    ap.add_argument("--mode", type=int, choices=range(1, 9), default=3, metavar="1..8")
    ap.add_argument("--name", default=None)
    ap.add_argument("--loopback", action="store_true")
    args = ap.parse_args()
    if not (1 <= args.src_id <= 255):
        print("  --src-id вне 1..255 (0 зарезервирован)")
        return 2
    name = args.name or f"ST{args.src_id}"

    print(f"Открываю {args.port} (GUI-станция src {args.src_id}, Mode {args.mode}"
          f"{', LOOPBACK' if args.loopback else ''}) ...")
    dev = ST.open_and_setup(args.port, args.mode)        # init/set_phy/rx_start/stream 4
    station = ST.Station(dev, args.src_id, name, args.mode, args.loopback)

    root = tk.Tk()
    gui = StationGui(root, station)
    station.start()
    try:
        root.mainloop()
    finally:
        station.stop()
        try:
            dev.set_stream_mode(0); dev.flush_input(); dev.rx_stop()
        except Exception:
            pass
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
