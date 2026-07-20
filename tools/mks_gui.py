#!/usr/bin/env python3
"""
*******************************************************************************
  МКС — Модуль коммуникации и сопряжения
  Хостовые инструменты (ПК) для STM32F411 + 2x DWM1000 (DW1000)

  Файл:     mks_gui.py
  Описание: исследовательский GUI «МКС для MATLAB» (tkinter + matplotlib).
            Вкладки Монитор/Настройки/Водопад, потоковый приём, сохраняемые
            сценарии старта, запись из потока в CSV. Поверх mks_protocol.py.

  Copyright (c) 2026 NCPR, Flexlab LLC. Все права защищены.
*******************************************************************************

mks_gui.py — GUI поверх готовой библиотеки. Шаг 2: вкладки + сценарии старта.

Вкладки:
  Монитор  — график CIR, кружок-индикатор, телеметрия, кнопки Старт/Стоп, запись.
  Настройки— подключение, выбор PHY-режима, content, сценарий старта, настройки записи.
  Водопад  — заглушка (рендер — Шаг 3; буфер CIR уже копится).

Движок Шага 1 сохранён: потоковый приём (SET_STREAM_MODE), фоновое чтение
(mks_stream.StreamReader), кружок/гашение по таймауту, прорежённый рендер при записи
ВСЕХ кадров, буфер водопада (deque), страховки от залипшего потока.

Запуск:
    python mks_gui.py            # порт вводится в окне
    python mks_gui.py COM3       # порт аргументом (автозаполнить поле)

Требования (Windows, Python 3.14): pyserial, tkinter (встроен), matplotlib.
"""

from __future__ import annotations

import os
import sys
import csv
import time
import queue
import threading
import datetime
import collections

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import mks_protocol as mks
from mks_stream import StreamReader, parse_stream_body

GUI_VERSION = "2"
APP_TITLE = "МКС для MATLAB. © 2026 Flexlab | Progresstech"

PHY_MODES = {
    "Mode 1 (ch2, 110k, PRF16, code3)":  dict(ch=2, dr=0, plen=1024, code=3, prf=16, pac=32),
    "Mode 2 (ch2, 6M8, PRF16, code3)":   dict(ch=2, dr=2, plen=128,  code=3, prf=16, pac=8),
    "Mode 3 (ch2, 110k, PRF64, code9)":  dict(ch=2, dr=0, plen=1024, code=9, prf=64, pac=32),
    "Mode 4 (ch2, 6M8, PRF64, code9)":   dict(ch=2, dr=2, plen=128,  code=9, prf=64, pac=8),
    "Mode 5 (ch5, 110k, PRF16, code3)":  dict(ch=5, dr=0, plen=1024, code=3, prf=16, pac=32),
    "Mode 6 (ch5, 6M8, PRF16, code3)":   dict(ch=5, dr=2, plen=128,  code=3, prf=16, pac=8),
    "Mode 7 (ch5, 110k, PRF64, code9)":  dict(ch=5, dr=0, plen=1024, code=9, prf=64, pac=32),
    "Mode 8 (ch5, 6M8, PRF64, code9)":   dict(ch=5, dr=2, plen=128,  code=9, prf=64, pac=8),
}
MANUAL_LABEL = "Ручной (6 полей)"
DEFAULT_MODE = "Mode 4 (ch2, 6M8, PRF64, code9)"

PRESENCE_WINDOW_S = 1.0
PUMP_MS           = 60
REC_FLUSH_EVERY   = 50
WATERFALL_MAXLEN  = 500
WF_PERIOD_S       = 0.15    # перерисовка водопада (~6/с; imshow тяжелее линии), только на активной вкладке
WF_CMAP           = "turbo"

METRICS_HEADER = ["frame_id", "timestamp", "count", "frames_per_sec", "RXPACC",
                  "RXPACC_NOSAT", "N_corrected", "CIR_PWR", "STD_NOISE", "FP_INDEX",
                  "RSSI_dBm", "FP_POWER_dBm", "SNR_dB", "mode"]
CIR_HEADER = ["frame_id", "sample_index", "I", "Q", "amplitude"]

# Минимальный безопасный набор команд сценария старта: имя -> число аргументов.
SCENARIO_CMDS = {"init": 0, "setphy": 6, "mode": 1, "rxstart": 0, "rxstop": 0,
                 "stream": 1, "txstop": 0}


def phy_by_mode_num(n: int) -> dict:
    """Пресет PHY по номеру Mode 1..8 (из PHY_MODES)."""
    for label, p in PHY_MODES.items():
        if label.startswith(f"Mode {n} "):
            return p
    raise ValueError(f"Mode {n} не найден")


def parse_scenario(text: str) -> list:
    """Разобрать текст сценария старта → список (cmd, [int-аргументы]).
    Формат: одна команда на строку, '#' — комментарий, пустые строки игнорируются.
    Поддержаны только команды SCENARIO_CMDS; иначе / при неверных аргументах —
    ValueError с номером строки. Пустой сценарий → ValueError."""
    steps = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd not in SCENARIO_CMDS:
            raise ValueError(f"строка {lineno}: неизвестная команда '{cmd}'")
        need = SCENARIO_CMDS[cmd]
        if len(args) != need:
            raise ValueError(f"строка {lineno}: '{cmd}' ожидает {need} арг., получено {len(args)}")
        try:
            iargs = [int(a) for a in args]
        except ValueError:
            raise ValueError(f"строка {lineno}: аргументы '{cmd}' должны быть целыми")
        if cmd == "mode" and not (1 <= iargs[0] <= 8):
            raise ValueError(f"строка {lineno}: mode вне 1..8")
        if cmd == "stream" and iargs[0] not in (0, 1, 2):
            raise ValueError(f"строка {lineno}: stream вне 0..2")
        if cmd == "setphy" and not (1 <= iargs[2] <= 4096):
            raise ValueError(f"строка {lineno}: setphy plen подозрителен")
        steps.append((cmd, iargs))
    if not steps:
        raise ValueError("пустой сценарий")
    return steps


def default_scenario(phy: dict, mode_label: str, content: int) -> str:
    """Сгенерировать дефолтный сценарий из выбранного режима и content."""
    return (
        f"# авто-сценарий: {mode_label}, content={content}\n"
        f"init\n"
        f"setphy {phy['ch']} {phy['dr']} {phy['plen']} {phy['code']} "
        f"{phy['prf']} {phy['pac']}   # {mode_label}\n"
        f"rxstart\n"
        f"stream {content}\n"
    )


def build_waterfall_matrix(frames):
    """Собрать матрицу водопада из CIR-кадров (вариант А — по АБСОЛЮТНОМУ
    sample_index; пустые ячейки = NaN, честно отражает дрожание FP по X).

    frames — последовательность cir-dict (start_index, amps), новейший ПОСЛЕДНИМ.
    Возвращает (matrix, x_min, x_max): row 0 = НОВЕЙШИЙ кадр (для origin='upper',
    водопад течёт вниз). Если данных нет — (None, 0, 0)."""
    frames = [f for f in frames if f and f.get("amps")]
    if not frames:
        return None, 0, 0
    x_min = min(f["start_index"] for f in frames)
    x_max = max(f["start_index"] + len(f["amps"]) - 1 for f in frames)
    width = x_max - x_min + 1
    rows = list(reversed(frames))                 # новейший — сверху (row 0)
    mat = np.full((len(rows), width), np.nan, dtype=float)
    for r, f in enumerate(rows):
        s = f["start_index"] - x_min
        amps = f["amps"]
        mat[r, s:s + len(amps)] = amps
    return mat, x_min, x_max


class MKSGui:
    def __init__(self, root: tk.Tk, default_port: str = ""):
        self.root = root
        self.dev = None
        self.detecting = False
        self.busy = False
        self.stream_thread = None
        self.q = queue.Queue()

        self._fps_count = None
        self._fps_time = None
        self._fps = 0.0
        self._last_frame_time = 0.0
        self._current_mode = "—"
        self._plot_blanked = False
        self._stream_content = 1          # что в потоке: 1=метрики+CIR, 2=метрики

        self._prev_seq = None
        self._stream_host_lost = 0
        self._stream_dropped = 0

        self.waterfall = collections.deque(maxlen=WATERFALL_MAXLEN)

        self.rec_lock = threading.Lock()
        self.recording = False
        self._rec = None
        self.rec_dir = ""
        self._rec_written = 0
        self._rec_info = ""
        self.scenario_path = None

        root.title(f"МКС для MATLAB  (GUI v{GUI_VERSION})")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui(default_port)
        self._reset_scenario()            # заполнить сценарий по умолчанию
        self._refresh_controls()
        self.root.after(PUMP_MS, self._pump_queue)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self, default_port: str):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        ttk.Label(self.root, text=APP_TITLE, font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(6, 0))

        self.nb = ttk.Notebook(self.root)
        self.nb.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)

        self.tab_mon = ttk.Frame(self.nb)
        self.tab_wf = ttk.Frame(self.nb)
        self.tab_set = ttk.Frame(self.nb)
        self.nb.add(self.tab_mon, text="Монитор")
        self.nb.add(self.tab_wf, text="Водопад")
        self.nb.add(self.tab_set, text="Настройки")

        self._build_monitor(self.tab_mon)
        self._build_waterfall(self.tab_wf)
        self._build_settings(self.tab_set, default_port)
        # bind ПОСЛЕ построения вкладок — иначе ранний TabChanged дёрнет пустой водопад
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.status = tk.StringVar(value="Готово. Настройки → Подключить.")
        ttk.Label(self.root, textvariable=self.status, relief="sunken",
                  anchor="w").grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 6))

    def _build_monitor(self, tab):
        pad = dict(padx=4, pady=3)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        top = ttk.Frame(tab)
        top.grid(row=0, column=0, sticky="ew", padx=4, pady=2)
        top.columnconfigure(0, weight=1)
        self.btn_start = ttk.Button(top, text="Старт", command=self.on_start)
        self.btn_start.grid(row=0, column=0, sticky="w", **pad)
        self.btn_stop = ttk.Button(top, text="Стоп", command=self.on_stop)
        self.btn_stop.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(top, text="приём:").grid(row=0, column=2, sticky="e")
        self.circle = tk.Canvas(top, width=26, height=26, highlightthickness=0)
        self._circle_id = self.circle.create_oval(3, 3, 23, 23, fill="#3a3a3a", outline="")
        self.circle.grid(row=0, column=3, sticky="e", padx=4)

        tel = ttk.LabelFrame(tab, text="Телеметрия (из потока)")
        tel.grid(row=1, column=0, sticky="ew", padx=4, pady=2)
        self.tel_vars = {}
        rows = [("fps", "Кадров/с"), ("count", "Принято кадров"),
                ("snr", "SNR, dB"), ("rssi", "RSSI, dBm"),
                ("fp_power", "FP_POWER, dBm"), ("fp_index", "FP_INDEX"),
                ("dropped", "DROPPED (fw)"), ("host_lost", "Потери хоста"),
                ("mode", "Режим")]
        for i, (key, label) in enumerate(rows):
            ttk.Label(tel, text=label + ":").grid(row=i // 3, column=(i % 3) * 2,
                                                  sticky="e", **pad)
            v = tk.StringVar(value="—")
            self.tel_vars[key] = v
            ttk.Label(tel, textvariable=v, width=16, anchor="w",
                      font=("Consolas", 10)).grid(row=i // 3, column=(i % 3) * 2 + 1,
                                                  sticky="w", **pad)

        plot = ttk.LabelFrame(tab, text="CIR (окно вокруг first path)")
        plot.grid(row=2, column=0, sticky="nsew", padx=4, pady=2)
        self.fig = Figure(figsize=(6, 3), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self._reset_axes()
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        pbot = ttk.Frame(plot)
        pbot.pack(fill="x")
        ttk.Button(pbot, text="Сохранить PNG", command=self.on_save_png).pack(side="right", padx=4)

        rec = ttk.LabelFrame(tab, text="Запись")
        rec.grid(row=3, column=0, sticky="ew", padx=4, pady=2)
        self.btn_rec = ttk.Button(rec, text="Запись", command=self.on_start_record)
        self.btn_rec.grid(row=0, column=0, **pad)
        self.btn_rec_stop = ttk.Button(rec, text="Стоп записи", command=self.on_stop_record)
        self.btn_rec_stop.grid(row=0, column=1, **pad)
        self.rec_status = ttk.Label(rec, text="не пишется", foreground="gray")
        self.rec_status.grid(row=0, column=2, sticky="w", **pad)

    def _build_settings(self, tab, default_port):
        pad = dict(padx=4, pady=3)

        conn = ttk.LabelFrame(tab, text="Подключение")
        conn.grid(row=0, column=0, sticky="ew", padx=6, pady=4)
        ttk.Label(conn, text="COM-порт:").grid(row=0, column=0, **pad)
        self.port_var = tk.StringVar(value=default_port or "COM3")
        ttk.Entry(conn, textvariable=self.port_var, width=12).grid(row=0, column=1, **pad)
        self.btn_connect = ttk.Button(conn, text="Подключить", command=self.on_connect)
        self.btn_connect.grid(row=0, column=2, **pad)
        self.conn_status = ttk.Label(conn, text="не подключено", foreground="gray")
        self.conn_status.grid(row=0, column=3, **pad)

        phy = ttk.LabelFrame(tab, text="PHY-режим и поток")
        phy.grid(row=1, column=0, sticky="ew", padx=6, pady=4)
        ttk.Label(phy, text="Режим:").grid(row=0, column=0, **pad)
        self.mode_var = tk.StringVar(value=DEFAULT_MODE)
        self.mode_cb = ttk.Combobox(phy, textvariable=self.mode_var, width=34,
                                    state="readonly",
                                    values=list(PHY_MODES.keys()) + [MANUAL_LABEL])
        self.mode_cb.grid(row=0, column=1, columnspan=6, sticky="w", **pad)
        self.mode_cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_controls())

        self.manual_vars = {}
        man = ttk.Frame(phy)
        man.grid(row=1, column=0, columnspan=7, sticky="w")
        for i, (key, dflt) in enumerate(
                [("ch", 2), ("dr", 2), ("plen", 128), ("code", 9), ("prf", 64), ("pac", 8)]):
            ttk.Label(man, text=key).grid(row=0, column=2 * i, padx=2)
            v = tk.StringVar(value=str(dflt))
            self.manual_vars[key] = v
            ttk.Entry(man, textvariable=v, width=6).grid(row=0, column=2 * i + 1, padx=2)

        ttk.Label(phy, text="content:").grid(row=2, column=0, **pad)
        self.content_var = tk.IntVar(value=1)
        self.rb_c1 = ttk.Radiobutton(phy, text="1 = метрики+CIR", value=1,
                                     variable=self.content_var)
        self.rb_c2 = ttk.Radiobutton(phy, text="2 = только метрики", value=2,
                                     variable=self.content_var)
        self.rb_c1.grid(row=2, column=1, sticky="w", **pad)
        self.rb_c2.grid(row=2, column=2, sticky="w", **pad)

        scen = ttk.LabelFrame(tab, text="Сценарий старта (команды: init, setphy, mode N, rxstart, rxstop, stream N, txstop)")
        scen.grid(row=2, column=0, sticky="nsew", padx=6, pady=4)
        tab.rowconfigure(2, weight=1)
        tab.columnconfigure(0, weight=1)
        btns = ttk.Frame(scen)
        btns.grid(row=0, column=0, sticky="ew")
        self.btn_scen_load = ttk.Button(btns, text="Загрузить…", command=self.on_scenario_load)
        self.btn_scen_save = ttk.Button(btns, text="Сохранить", command=self.on_scenario_save)
        self.btn_scen_saveas = ttk.Button(btns, text="Сохранить как…", command=self.on_scenario_saveas)
        self.btn_scen_reset = ttk.Button(btns, text="Сбросить к умолчанию", command=self._reset_scenario)
        for b in (self.btn_scen_load, self.btn_scen_save, self.btn_scen_saveas, self.btn_scen_reset):
            b.pack(side="left", padx=3, pady=2)
        self.scen_path_lbl = ttk.Label(scen, text="(дефолтный, не сохранён)", foreground="gray")
        self.scen_path_lbl.grid(row=1, column=0, sticky="w", padx=4)
        self.scen_text = tk.Text(scen, height=6, width=60, font=("Consolas", 10))
        self.scen_text.grid(row=2, column=0, sticky="nsew", padx=4, pady=(0, 4))
        scen.rowconfigure(2, weight=1)
        scen.columnconfigure(0, weight=1)

        recset = ttk.LabelFrame(tab, text="Настройки записи")
        recset.grid(row=3, column=0, sticky="ew", padx=6, pady=4)
        self.rec_mode_var = tk.StringVar(value="light")
        self.rb_light = ttk.Radiobutton(recset, text="Лёгкий (метрики)", value="light",
                                        variable=self.rec_mode_var)
        self.rb_full = ttk.Radiobutton(recset, text="Полный (+CIR)", value="full",
                                       variable=self.rec_mode_var)
        self.rb_light.grid(row=0, column=0, **pad)
        self.rb_full.grid(row=0, column=1, **pad)
        ttk.Label(recset, text="префикс:").grid(row=0, column=2, **pad)
        self.rec_prefix_var = tk.StringVar(value="mks_rec")
        ttk.Entry(recset, textvariable=self.rec_prefix_var, width=12).grid(row=0, column=3, **pad)
        self.btn_folder = ttk.Button(recset, text="Папка…", command=self.on_folder)
        self.btn_folder.grid(row=0, column=4, **pad)

    def _build_waterfall(self, tab):
        self.wf_fig = Figure(figsize=(6, 4), dpi=100)
        self.wf_ax = self.wf_fig.add_subplot(111)
        self.wf_ax.set_xlabel("индекс отсчёта")
        self.wf_ax.set_ylabel("кадры (свежие сверху)")
        self.wf_cbar = None
        self._last_wf_time = 0.0
        self.wf_canvas = FigureCanvasTkAgg(self.wf_fig, master=tab)
        self.wf_canvas.get_tk_widget().pack(fill="both", expand=True)

    def _reset_axes(self):
        self.ax.clear()
        self.ax.set_xlabel("индекс отсчёта")
        self.ax.set_ylabel("|CIR| = sqrt(I²+Q²)")

    # ------------------------------------------------- состояние кнопок --
    def _refresh_controls(self):
        connected = self.dev is not None
        manual = (self.mode_var.get() == MANUAL_LABEL)
        cfg_ok = connected and not self.detecting and not self.busy
        for child in self._manual_entries():
            child.configure(state=("normal" if (manual and cfg_ok) else "disabled"))
        self.btn_connect.configure(text=("Отключить" if connected else "Подключить"),
                                   state=("disabled" if self.busy else "normal"))
        self.btn_start.configure(state=("normal" if cfg_ok else "disabled"))
        self.btn_stop.configure(state=("normal" if (connected and self.detecting) else "disabled"))
        # Конфиг (режим/content/сценарий) — только вне потока.
        st_cfg = "normal" if cfg_ok else "disabled"
        st_cfg_ro = "readonly" if cfg_ok else "disabled"
        self.mode_cb.configure(state=st_cfg_ro)
        self.rb_c1.configure(state=st_cfg)
        self.rb_c2.configure(state=st_cfg)
        for b in (self.btn_scen_load, self.btn_scen_reset, self.btn_scen_save, self.btn_scen_saveas):
            b.configure(state=st_cfg)
        self.scen_text.configure(state=("normal" if cfg_ok else "disabled"))
        # Запись: во время потока; настройки записи — вне записи.
        self.btn_rec.configure(state=("normal" if (connected and self.detecting and not self.recording) else "disabled"))
        self.btn_rec_stop.configure(state=("normal" if self.recording else "disabled"))
        rec_cfg = "disabled" if self.recording else "normal"
        self.rb_light.configure(state=rec_cfg)
        self.rb_full.configure(state=rec_cfg)
        self.btn_folder.configure(state=rec_cfg)

    def _manual_entries(self):
        out = []
        for frame in self.mode_cb.master.winfo_children():
            if isinstance(frame, ttk.Frame):
                out.extend(w for w in frame.winfo_children() if isinstance(w, ttk.Entry))
        return out

    # --------------------------------------- сценарий старта --
    def _collect_phy(self) -> dict:
        label = self.mode_var.get()
        if label != MANUAL_LABEL:
            return dict(PHY_MODES[label])
        return {key: int(v.get()) for key, v in self.manual_vars.items()}

    def _reset_scenario(self):
        try:
            phy = self._collect_phy()
            label = self.mode_var.get()
        except ValueError:
            phy = dict(PHY_MODES[DEFAULT_MODE])
            label = DEFAULT_MODE
        text = default_scenario(phy, label, self.content_var.get())
        self.scen_text.configure(state="normal")
        self.scen_text.delete("1.0", "end")
        self.scen_text.insert("1.0", text)
        self.scenario_path = None
        self.scen_path_lbl.configure(text="(дефолтный, не сохранён)", foreground="gray")

    def on_scenario_load(self):
        p = filedialog.askopenfilename(title="Загрузить сценарий",
                                       filetypes=[("Текст", "*.txt"), ("Все", "*.*")])
        if not p:
            return
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read()
            parse_scenario(text)                 # валидация при загрузке
        except Exception as e:
            messagebox.showerror("Сценарий", f"Не загружен: {e}")
            return
        self.scen_text.delete("1.0", "end")
        self.scen_text.insert("1.0", text)
        self.scenario_path = p
        self.scen_path_lbl.configure(text=p, foreground="black")

    def on_scenario_save(self):
        if not self.scenario_path:
            return self.on_scenario_saveas()
        self._write_scenario(self.scenario_path)

    def on_scenario_saveas(self):
        p = filedialog.asksaveasfilename(title="Сохранить сценарий как",
                                         defaultextension=".txt",
                                         filetypes=[("Текст", "*.txt")])
        if p:
            self._write_scenario(p)

    def _write_scenario(self, path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.scen_text.get("1.0", "end"))
            self.scenario_path = path
            self.scen_path_lbl.configure(text=path, foreground="black")
            self.status.set(f"Сценарий сохранён: {path}")
        except Exception as e:
            messagebox.showerror("Сценарий", f"Не сохранён: {e}")

    # ------------------------------------------------------- обработчики --
    def on_connect(self):
        if self.dev is not None:
            self.on_stop()
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
            self.conn_status.configure(text="не подключено", foreground="gray")
            self.status.set("Отключено.")
            self._refresh_controls()
            return
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("Порт", "Укажите COM-порт (напр. COM3).")
            return

        def do_connect():
            dev = mks.MKS(port)
            try:                                 # страховка от залипшего потока
                dev.flush_input()
                dev.set_stream_mode(0)
                dev.flush_input()
            except Exception:
                pass
            return dev

        def ok(dev):
            self.dev = dev
            self.conn_status.configure(text=f"подключено ({port})", foreground="green")
        self._run_async(f"Подключение к {port}", do_connect, ok)

    def _run_async(self, title, fn, on_ok=None):
        if self.busy:
            return
        self.busy = True
        self._refresh_controls()
        self.status.set(f"{title}...")

        def worker():
            try:
                res = fn()
                self.q.put(("op_ok", title, res, on_ok))
            except Exception as e:
                self.q.put(("op_err", title, str(e), None))

        threading.Thread(target=worker, daemon=True).start()

    # --------------------------------------- Старт: выполнить сценарий --
    def on_start(self):
        if self.dev is None or self.detecting or self.busy:
            return
        text = self.scen_text.get("1.0", "end")
        try:
            steps = parse_scenario(text)
        except ValueError as e:
            messagebox.showerror("Сценарий", str(e))
            return
        self.busy = True
        self._current_mode = self.mode_var.get()
        self._refresh_controls()
        self.status.set("Старт: выполняю сценарий...")
        threading.Thread(target=self._run_scenario, args=(steps,), daemon=True).start()

    def _run_scenario(self, steps):
        stream_on = None
        try:
            for cmd, iargs in steps:
                st = self._exec_step(cmd, iargs)
                self.q.put(("scen_log", f"{cmd} {' '.join(map(str, iargs))} → {mks.status_name(st)}"))
                if st != 0x00:
                    self.q.put(("scen_err", f"{cmd}: {mks.status_name(st)}"))
                    return
                if cmd == "stream" and iargs[0] in (1, 2):
                    stream_on = iargs[0]
        except Exception as e:
            self.q.put(("scen_err", str(e)))
            return
        self.q.put(("scen_ok", stream_on))

    def _exec_step(self, cmd, iargs) -> int:
        """Выполнить одну команду сценария → STATUS. Только SCENARIO_CMDS."""
        d = self.dev
        if cmd == "init":
            st, _ = d.init(timeout=20.0)
        elif cmd == "setphy":
            ch, dr, plen, code, prf, pac = iargs
            params = bytes([ch & 0xFF, dr & 0xFF, plen & 0xFF, (plen >> 8) & 0xFF,
                            code & 0xFF, prf & 0xFF, pac & 0xFF])
            st, _ = d.command(mks.CMD_SET_PHY_CONFIG, params)
        elif cmd == "mode":
            p = phy_by_mode_num(iargs[0])
            params = bytes([p["ch"], p["dr"], p["plen"] & 0xFF, (p["plen"] >> 8) & 0xFF,
                            p["code"], p["prf"], p["pac"]])
            st, _ = d.command(mks.CMD_SET_PHY_CONFIG, params)
        elif cmd == "rxstart":
            st, _ = d.rx_start()
        elif cmd == "rxstop":
            st, _ = d.rx_stop()
        elif cmd == "stream":
            st, _ = d.set_stream_mode(iargs[0])
        elif cmd == "txstop":
            st, _ = d.tx_stop()
        else:
            raise ValueError(f"неизвестная команда сценария: {cmd}")
        return st

    def on_stop(self):
        if not self.detecting:
            return
        self._stop_record()
        self.detecting = False
        t = self.stream_thread
        if t is not None:
            t.join(timeout=1.0)
        self.stream_thread = None
        try:
            if self.dev is not None:
                self.dev.set_stream_mode(0)
                self.dev.flush_input()
                self.dev.rx_stop()
        except Exception:
            pass
        self.status.set("Поток остановлен.")
        self._refresh_controls()

    def _begin_stream(self, content):
        self._stream_content = content
        self.detecting = True
        self._fps_count = None
        self._prev_seq = None
        self._stream_host_lost = 0
        self._stream_dropped = 0
        self._last_frame_time = 0.0
        self._plot_blanked = False
        self.tel_vars["mode"].set(self._current_mode)
        self.waterfall.clear()                 # новая сессия — чистый водопад
        if content == 2:
            self._draw_cir_stub("CIR отключён (content=2 — только метрики)")
        self._draw_waterfall()                 # заглушка/пустая карта под текущий content
        self.stream_thread = threading.Thread(target=self._stream_loop, daemon=True)
        self.stream_thread.start()
        self.nb.select(self.tab_mon)
        self.status.set(f"Поток запущен (content={content}).")

    def on_save_png(self):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"cir_{ts}.png"
        try:
            self.fig.savefig(name, dpi=120)
            self.status.set(f"График сохранён: {name}")
        except Exception as e:
            messagebox.showerror("Сохранение", str(e))

    # ------------------------------------------------------- запись CSV --
    def on_folder(self):
        d = filedialog.askdirectory(title="Папка для CSV-файлов")
        if d:
            self.rec_dir = d
            self.status.set(f"Папка записи: {d}")

    def on_start_record(self):
        if not self.detecting or self.recording:
            return
        # content=2 → CIR нет, cir.csv не пишем (даже при выборе «Полный»).
        full = (self.rec_mode_var.get() == "full" and self._stream_content == 1)
        prefix = (self.rec_prefix_var.get().strip() or "mks_rec")
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dirp = self.rec_dir or "."
        mpath = os.path.join(dirp, f"{prefix}_{ts}_metrics.csv")
        cpath = os.path.join(dirp, f"{prefix}_{ts}_cir.csv") if full else None
        try:
            mf = open(mpath, "w", newline="", encoding="utf-8")
            mw = csv.writer(mf)
            mw.writerow(METRICS_HEADER)
            cf = cw = None
            if full:
                cf = open(cpath, "w", newline="", encoding="utf-8")
                cw = csv.writer(cf)
                cw.writerow(CIR_HEADER)
        except Exception as e:
            messagebox.showerror("Запись", f"Не удалось открыть файл(ы): {e}")
            return
        with self.rec_lock:
            self._rec = dict(mode=("full" if full else "light"), mf=mf, mw=mw, cf=cf, cw=cw,
                             mpath=mpath, cpath=cpath,
                             frame_id=0, last_count=None, written=0, since_flush=0)
            self.recording = True
        self._rec_written = 0
        self._rec_info = mpath + (f" (+{os.path.basename(cpath)})" if cpath else "")
        self.rec_status.configure(text=f"● запись → {self._rec_info}", foreground="red")
        self.status.set(f"Запись начата: {self._rec_info}")
        self._refresh_controls()

    def on_stop_record(self):
        self._stop_record()

    def _stop_record(self):
        with self.rec_lock:
            rec = self._rec
            self.recording = False
            self._rec = None
        if not rec:
            return
        for fh in (rec["mf"], rec["cf"]):
            if fh is not None:
                try:
                    fh.flush()
                    fh.close()
                except Exception:
                    pass
        info = f"Записано {rec['written']} кадров → {rec['mpath']}"
        if rec["cpath"]:
            info += f" + {rec['cpath']}"
        self.rec_status.configure(text="не пишется", foreground="gray")
        self.status.set(info)
        self._refresh_controls()

    def _maybe_record(self, metrics, cir):
        if metrics is None:
            return
        with self.rec_lock:
            rec = self._rec
            if rec is None or not self.recording:
                return
            count = metrics.get("count", 0)
            if rec["last_count"] is not None and count == rec["last_count"]:
                return
            rec["last_count"] = count
            fid = rec["frame_id"]
            try:
                rec["mw"].writerow(self._metrics_row(fid, metrics))
                if rec["mode"] == "full" and cir is not None:
                    start = cir.get("start_index", 0)
                    samples = cir.get("samples", [])
                    amps = cir.get("amps", [])
                    for k, (i, q) in enumerate(samples):
                        a = amps[k] if k < len(amps) else (i * i + q * q) ** 0.5
                        rec["cw"].writerow([fid, start + k, i, q, round(a, 1)])
            except Exception as e:
                self.q.put(("rec_err", str(e)))
                return
            rec["frame_id"] += 1
            rec["written"] += 1
            rec["since_flush"] += 1
            if rec["since_flush"] >= REC_FLUSH_EVERY:
                rec["since_flush"] = 0
                try:
                    rec["mf"].flush()
                    if rec["cf"] is not None:
                        rec["cf"].flush()
                except Exception:
                    pass
            self._rec_written = rec["written"]

    def _metrics_row(self, fid, m):
        final = (m.get("format") == "final")

        def fld(key):
            return m.get(key, "")

        def valf(key, validkey):
            return round(m[key], 2) if (final and m.get(validkey)) else ""

        return [
            fid,
            datetime.datetime.now().isoformat(timespec="milliseconds"),
            fld("count"),
            f"{self._fps:.2f}",
            fld("RXPACC"),
            (m["rxpacc_nosat"] if final else ""),
            (m["N_corrected"] if final else ""),
            fld("CIR_PWR"),
            fld("STD_NOISE"),
            fld("FP_INDEX"),
            valf("rssi_dbm", "rssi_valid"),
            valf("fp_power_dbm", "fp_valid"),
            valf("snr_db", "snr_valid"),
            self._current_mode,
        ]

    def on_close(self):
        try:
            self.on_stop()
        except Exception:
            pass
        try:
            if self.dev is not None:
                self.dev.close()
        except Exception:
            pass
        self.root.destroy()

    # ------------------------------------------- фоновый поток чтения --
    def _stream_loop(self):
        reader = StreamReader(self.dev.ser)
        while self.detecting:
            try:
                frames = reader.poll()
            except Exception as e:
                self.q.put(("stream_err", str(e)))
                break
            latest = None
            for body, crc_ok in frames:
                if not crc_ok:
                    continue
                try:
                    fr = parse_stream_body(body)
                except mks.ProtocolError:
                    continue
                seq = fr["seq"]
                if self._prev_seq is not None:
                    gap = (seq - ((self._prev_seq + 1) & 0xFFFF)) & 0xFFFF
                    if gap:
                        self._stream_host_lost += gap
                self._prev_seq = seq
                self._stream_dropped = fr["dropped"]
                self._maybe_record(fr["metrics"], fr["cir"])
                if fr["cir"] is not None:
                    self.waterfall.append(fr["cir"])
                latest = fr
            if latest is not None:
                self.q.put(("frame", latest))

    # ------------------------------------------- главный цикл обновления --
    def _pump_queue(self):
        last_frame = None
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "frame":
                    last_frame = item[1]
                elif kind == "op_ok":
                    _, title, res, on_ok = item
                    self.busy = False
                    self.status.set(f"{title}: OK")
                    if on_ok is not None:
                        try:
                            on_ok(res)
                        except Exception as e:
                            self.status.set(f"{title}: обработка результата — {e}")
                    self._refresh_controls()
                elif kind == "op_err":
                    _, title, err, _ = item
                    self.busy = False
                    self.status.set(f"{title}: ОШИБКА — {err}")
                    self._refresh_controls()
                elif kind == "scen_log":
                    self.status.set(item[1])
                elif kind == "scen_err":
                    self.busy = False
                    self.status.set(f"Сценарий остановлен: {item[1]}")
                    messagebox.showerror("Сценарий", item[1])
                    self._refresh_controls()
                elif kind == "scen_ok":
                    self.busy = False
                    if item[1] in (1, 2):
                        self._begin_stream(item[1])
                    else:
                        self.status.set("Сценарий выполнен (поток не включён).")
                    self._refresh_controls()
                elif kind == "stream_err":
                    self.status.set(f"Поток прерван: {item[1]}")
                    self.detecting = False
                    self._refresh_controls()
                elif kind == "rec_err":
                    self.status.set(f"Запись: ОШИБКА — {item[1]}")
                    self._stop_record()
        except queue.Empty:
            pass

        if last_frame is not None:
            self._apply_frame(last_frame)
        if self.recording:
            self.rec_status.configure(
                text=f"● запись: {self._rec_written} кадров → {self._rec_info}",
                foreground="red")
        # Водопад перерисовываем прорежённо и ТОЛЬКО когда его вкладка видима.
        if self.detecting and self._wf_visible():
            twf = time.time()
            if twf - self._last_wf_time >= WF_PERIOD_S:
                self._draw_waterfall()
                self._last_wf_time = twf
        self._update_indicator()
        self.root.after(PUMP_MS, self._pump_queue)

    def _apply_frame(self, fr):
        now = time.time()
        m = fr["metrics"]
        cir = fr["cir"]
        self._last_frame_time = now
        self._plot_blanked = False

        count = m.get("count", 0)
        if self._fps_count is not None and self._fps_time is not None:
            dt = now - self._fps_time
            if dt > 0:
                self._fps = max(0.0, (count - self._fps_count) / dt)
        self._fps_count = count
        self._fps_time = now

        self.tel_vars["fps"].set(f"{self._fps:.0f}")
        self.tel_vars["count"].set(str(count))
        self.tel_vars["dropped"].set(str(self._stream_dropped))
        self.tel_vars["host_lost"].set(str(self._stream_host_lost))
        if m.get("format") == "final":
            self.tel_vars["snr"].set(f"{m['snr_db']:.2f}" if m.get("snr_valid") else "н/д")
            self.tel_vars["rssi"].set(f"{m['rssi_dbm']:.2f}" if m.get("rssi_valid") else "н/д")
            self.tel_vars["fp_power"].set(f"{m['fp_power_dbm']:.2f}" if m.get("fp_valid") else "н/д")

        if cir is not None:
            self.tel_vars["fp_index"].set(str(cir.get("fp_index", "—")))
            self._draw_cir(cir)
        # content=2 (cir None) — график остаётся заглушкой (нарисована при старте).

    def _draw_cir(self, cir):
        amps = cir.get("amps") or []
        start = cir.get("start_index", 0)
        fp = cir.get("fp_index", None)
        xs = [start + k for k in range(len(amps))]
        self._reset_axes()
        if amps:
            self.ax.plot(xs, amps, marker=".", linewidth=1.0)
            if fp is not None:
                self.ax.axvline(fp, color="red", linestyle="--", linewidth=1.0)
                self.ax.text(fp, max(amps), " FP", color="red", va="top")
        self.canvas.draw_idle()

    def _draw_cir_stub(self, text):
        self._reset_axes()
        self.ax.text(0.5, 0.5, text, ha="center", va="center", transform=self.ax.transAxes,
                     color="gray")
        self.canvas.draw_idle()

    # ------------------------------------------------- водопад --
    def _wf_visible(self):
        try:
            return self.nb.select() == str(self.tab_wf)
        except Exception:
            return False

    def _on_tab_changed(self, event=None):
        # при переключении на «Водопад» — сразу отрисовать текущий буфер
        if self._wf_visible():
            self._draw_waterfall()
            self._last_wf_time = time.time()

    def _draw_waterfall(self):
        self.wf_ax.clear()
        self.wf_ax.set_xlabel("индекс отсчёта")
        self.wf_ax.set_ylabel("кадры (свежие сверху)")
        if self._stream_content == 2:
            self.wf_ax.text(0.5, 0.5, "CIR отключён (content=2 — только метрики)",
                            ha="center", va="center", transform=self.wf_ax.transAxes, color="gray")
            self.wf_canvas.draw_idle()
            return
        mat, x0, x1 = build_waterfall_matrix(list(self.waterfall))
        if mat is None:
            self.wf_ax.text(0.5, 0.5, "нет данных (ждём кадры)", ha="center", va="center",
                            transform=self.wf_ax.transAxes, color="gray")
            self.wf_canvas.draw_idle()
            return
        vmin = float(np.nanmin(mat))
        vmax = float(np.nanmax(mat))
        im = self.wf_ax.imshow(mat, aspect="auto", origin="upper", cmap=WF_CMAP,
                               extent=[x0, x1, mat.shape[0], 0], interpolation="nearest",
                               vmin=vmin, vmax=(vmax if vmax > vmin else vmin + 1.0))
        self.wf_ax.set_xlabel("индекс отсчёта")
        self.wf_ax.set_ylabel("кадры (свежие сверху)")
        if self.wf_cbar is None:
            self.wf_cbar = self.wf_fig.colorbar(im, ax=self.wf_ax, label="|CIR|")
        else:
            self.wf_cbar.update_normal(im)
        self.wf_canvas.draw_idle()

    def _update_indicator(self):
        live = (self.detecting and self._last_frame_time > 0.0
                and (time.time() - self._last_frame_time) < PRESENCE_WINDOW_S)
        self.circle.itemconfigure(self._circle_id, fill=("#1e8e3e" if live else "#3a3a3a"))
        if not live and not self._plot_blanked:
            if self._stream_content == 2:
                self._draw_cir_stub("CIR отключён (content=2 — только метрики)")
            else:
                self._reset_axes()
                self.canvas.draw_idle()
            for k in ("snr", "rssi", "fp_power", "fp_index"):
                self.tel_vars[k].set("—")
            self.tel_vars["fps"].set("0")
            self._plot_blanked = True


def main():
    default_port = sys.argv[1] if len(sys.argv) > 1 else ""
    root = tk.Tk()
    MKSGui(root, default_port)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
