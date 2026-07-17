#!/usr/bin/env python3
"""
*******************************************************************************
  МКС — Модуль коммуникации и сопряжения
  Хостовые инструменты (ПК) для STM32F411 + 2x DWM1000 (DW1000)

  Файл:     mks_gui.py
  Описание: GUI-детектор присутствия СШП/UWB-сигнала (tkinter + matplotlib).
            Выбор PHY-режима, приём, индикатор присутствия «СШП В КАНАЛЕ»,
            живой график CIR. Поверх mks_protocol.py (протокол не меняется).

  Copyright (c) 2026 NCPR, Flexlab LLC. Все права защищены.
*******************************************************************************

mks_gui.py — графический детектор присутствия СШП для НИР.

Простой детектор: «за последнюю секунду принят хотя бы один кадр → СШП ЕСТЬ».
Пороговый детектор с P_false и автосканер по режимам — будущие задачи, не здесь.
Всё поверх уже проверенных на железе команд: INIT, SET_PHY_CONFIG, RX_START,
RX_STOP, GET_SIGNAL_METRICS, GET_CIR.

Запуск:
    python mks_gui.py            # порт вводится в окне
    python mks_gui.py COM3       # порт аргументом (автозаполнить поле)

Типовой сценарий (НИР):
    1. Подключить (COM3).
    2. INIT.
    3. Выбрать режим (по умолчанию Mode 3) → Применить режим.
    4. Старт детекции.
    5. Смотреть лампу «СШП В КАНАЛЕ» + график CIR. При совместимом источнике
       (киты EVK в том же режиме) — лампа зелёная, CIR живой, счётчики растут.
    6. Сменить режим (источник в другом): Стоп → Применить режим → Старт.

Требования (Windows, Python 3.14): pyserial, tkinter (встроен), matplotlib.
"""

from __future__ import annotations

import sys
import time
import queue
import threading
import datetime

import tkinter as tk
from tkinter import ttk, messagebox

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import mks_protocol as mks

# Версия GUI (отдельный фронтенд; HOST_VERSION протокола НЕ трогаем).
GUI_VERSION = "1"

# Пресеты PHY EVK1000 (из первоисточника dw_main.c chConfig[8], TASK §3).
# dr — код (0=110k, 1=850k, 2=6M8); prf — число МГц (16/64).
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
DEFAULT_MODE = "Mode 3 (ch2, 110k, PRF64, code9)"

PRESENCE_WINDOW_S = 1.0    # «есть сигнал», если кадр был не дальше этого назад
POLL_PERIOD_S     = 0.15   # шаг фонового опроса платы
PLOT_PERIOD_S     = 0.3    # не чаще этого перерисовываем CIR (не грузим GUI)


class MKSGui:
    def __init__(self, root: tk.Tk, default_port: str = ""):
        self.root = root
        self.dev = None                     # mks.MKS, обращаться только из воркеров/потока опроса
        self.detecting = False              # активен ли фоновый поток опроса
        self.busy = False                   # идёт блокирующая dev-операция (connect/init/phy)
        self.poll_thread = None
        self.q = queue.Queue()              # результаты из потока опроса в GUI

        # Состояние детектора присутствия.
        self._last_count = None
        self._last_frame_time = 0.0
        self._fps_count = None
        self._fps_time = None
        self._fps = 0.0
        self._last_plot_time = 0.0
        self._current_mode = "—"

        root.title(f"МКС — детектор присутствия СШП  (GUI v{GUI_VERSION})")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui(default_port)
        self._refresh_controls()
        self.root.after(100, self._pump_queue)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self, default_port: str):
        pad = dict(padx=4, pady=3)

        # --- Подключение ---
        conn = ttk.LabelFrame(self.root, text="Подключение")
        conn.grid(row=0, column=0, sticky="ew", padx=6, pady=4)
        ttk.Label(conn, text="COM-порт:").grid(row=0, column=0, **pad)
        self.port_var = tk.StringVar(value=default_port or "COM3")
        ttk.Entry(conn, textvariable=self.port_var, width=12).grid(row=0, column=1, **pad)
        self.btn_connect = ttk.Button(conn, text="Подключить", command=self.on_connect)
        self.btn_connect.grid(row=0, column=2, **pad)
        self.conn_status = ttk.Label(conn, text="не подключено", foreground="gray")
        self.conn_status.grid(row=0, column=3, **pad)

        # --- Управление / PHY ---
        ctl = ttk.LabelFrame(self.root, text="Управление")
        ctl.grid(row=1, column=0, sticky="ew", padx=6, pady=4)

        self.btn_init = ttk.Button(ctl, text="INIT", command=self.on_init)
        self.btn_init.grid(row=0, column=0, **pad)

        ttk.Label(ctl, text="Режим:").grid(row=0, column=1, **pad)
        self.mode_var = tk.StringVar(value=DEFAULT_MODE)
        self.mode_cb = ttk.Combobox(ctl, textvariable=self.mode_var, width=34,
                                    state="readonly",
                                    values=list(PHY_MODES.keys()) + [MANUAL_LABEL])
        self.mode_cb.grid(row=0, column=2, columnspan=4, sticky="w", **pad)
        self.mode_cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_controls())

        # Ручные поля (ch dr plen code prf pac)
        self.manual_vars = {}
        man = ttk.Frame(ctl)
        man.grid(row=1, column=1, columnspan=6, sticky="w")
        for i, (key, dflt) in enumerate(
                [("ch", 2), ("dr", 0), ("plen", 1024), ("code", 9), ("prf", 64), ("pac", 32)]):
            ttk.Label(man, text=key).grid(row=0, column=2 * i, padx=2)
            v = tk.StringVar(value=str(dflt))
            self.manual_vars[key] = v
            ttk.Entry(man, textvariable=v, width=6).grid(row=0, column=2 * i + 1, padx=2)

        self.btn_phy = ttk.Button(ctl, text="Применить режим", command=self.on_apply_phy)
        self.btn_phy.grid(row=2, column=1, columnspan=2, sticky="w", **pad)

        self.btn_start = ttk.Button(ctl, text="Старт детекции", command=self.on_start)
        self.btn_start.grid(row=2, column=3, **pad)
        self.btn_stop = ttk.Button(ctl, text="Стоп", command=self.on_stop)
        self.btn_stop.grid(row=2, column=4, **pad)

        # --- Индикатор присутствия ---
        self.lamp = tk.Label(self.root, text="СШП: НЕТ", font=("Segoe UI", 22, "bold"),
                             bg="#3a3a3a", fg="white", height=2)
        self.lamp.grid(row=2, column=0, sticky="ew", padx=6, pady=6)

        # --- Телеметрия ---
        tel = ttk.LabelFrame(self.root, text="Телеметрия")
        tel.grid(row=3, column=0, sticky="ew", padx=6, pady=4)
        self.tel_vars = {}
        rows = [("count", "Принято кадров"), ("fps", "Кадров/с"),
                ("snr", "SNR, dB"), ("rssi", "RSSI, dBm"),
                ("fp_power", "FP_POWER, dBm"), ("fp_index", "FP_INDEX (отсчёт)"),
                ("mode", "Текущий режим")]
        for i, (key, label) in enumerate(rows):
            ttk.Label(tel, text=label + ":").grid(row=i // 2, column=(i % 2) * 2,
                                                  sticky="e", **pad)
            v = tk.StringVar(value="—")
            self.tel_vars[key] = v
            ttk.Label(tel, textvariable=v, width=18, anchor="w",
                      font=("Consolas", 10)).grid(row=i // 2, column=(i % 2) * 2 + 1,
                                                  sticky="w", **pad)

        # --- График CIR ---
        plot = ttk.LabelFrame(self.root, text="CIR (окно вокруг first path)")
        plot.grid(row=4, column=0, sticky="nsew", padx=6, pady=4)
        self.root.rowconfigure(4, weight=1)
        self.root.columnconfigure(0, weight=1)

        self.fig = Figure(figsize=(6, 3), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("индекс отсчёта")
        self.ax.set_ylabel("|CIR| = sqrt(I²+Q²)")
        self.fig.tight_layout()
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        bottom = ttk.Frame(plot)
        bottom.pack(fill="x")
        ttk.Label(bottom, text="полуширина (0..30, 0=деф):").pack(side="left", padx=4)
        self.half_var = tk.StringVar(value="0")
        ttk.Entry(bottom, textvariable=self.half_var, width=5).pack(side="left")
        ttk.Button(bottom, text="Сохранить PNG", command=self.on_save_png).pack(side="right", padx=4)

        # --- Статусная строка ---
        self.status = tk.StringVar(value="Готово. Подключитесь к плате.")
        ttk.Label(self.root, textvariable=self.status, relief="sunken",
                  anchor="w").grid(row=5, column=0, sticky="ew", padx=6, pady=(0, 6))

    # ------------------------------------------------- состояние кнопок --
    def _refresh_controls(self):
        connected = self.dev is not None
        manual = (self.mode_var.get() == MANUAL_LABEL)
        # Ручные поля активны только в режиме «Ручной».
        for child in self._manual_entries():
            child.configure(state=("normal" if manual else "disabled"))
        # Во время детекции или блокирующей операции конфигурацию блокируем.
        cfg_ok = connected and not self.detecting and not self.busy
        self.btn_connect.configure(text=("Отключить" if connected else "Подключить"),
                                   state=("disabled" if self.busy else "normal"))
        self.btn_init.configure(state=("normal" if cfg_ok else "disabled"))
        self.btn_phy.configure(state=("normal" if cfg_ok else "disabled"))
        self.mode_cb.configure(state=("readonly" if cfg_ok else "disabled"))
        self.btn_start.configure(state=("normal" if cfg_ok else "disabled"))
        self.btn_stop.configure(state=("normal" if (connected and self.detecting) else "disabled"))

    def _manual_entries(self):
        # виджеты Entry внутри рамки ручного ввода
        out = []
        for frame in self.mode_cb.master.winfo_children():
            if isinstance(frame, ttk.Frame):
                out.extend(w for w in frame.winfo_children() if isinstance(w, ttk.Entry))
        return out

    # --------------------------------------- запуск блокирующих dev-операций --
    def _run_async(self, title, fn, on_ok=None):
        """Выполнить блокирующую операцию с платой в отдельном потоке, не морозя GUI.
        Пока идёт — контролы заблокированы; результат/ошибка — в статус."""
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

    # ------------------------------------------------------- обработчики --
    def on_connect(self):
        if self.dev is not None:
            # Отключение.
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
            return mks.MKS(port)

        def ok(dev):
            self.dev = dev
            self.conn_status.configure(text=f"подключено ({port})", foreground="green")
        self._run_async(f"Подключение к {port}", do_connect, ok)

    def on_init(self):
        if self.dev is None:
            return
        self._run_async("INIT (инициализация DW1000, ~20 c)",
                        lambda: self.dev.init(timeout=20.0),
                        lambda r: self._note_status(r, "INIT"))

    def on_apply_phy(self):
        if self.dev is None:
            return
        try:
            p = self._collect_phy()
        except ValueError as e:
            messagebox.showerror("Режим", f"Некорректные параметры: {e}")
            return
        params = bytes([p["ch"] & 0xFF, p["dr"] & 0xFF,
                        p["plen"] & 0xFF, (p["plen"] >> 8) & 0xFF,
                        p["code"] & 0xFF, p["prf"] & 0xFF, p["pac"] & 0xFF])
        label = self.mode_var.get()
        self._pending_mode = label

        def ok(r):
            st, _ = r
            if st == 0x00:
                self._current_mode = label
                self.tel_vars["mode"].set(label)
            self._note_status(r, "SET_PHY_CONFIG")
        self._run_async(f"Применение режима: {label}",
                        lambda: self.dev.command(mks.CMD_SET_PHY_CONFIG, params), ok)

    def _collect_phy(self) -> dict:
        label = self.mode_var.get()
        if label != MANUAL_LABEL:
            return dict(PHY_MODES[label])
        out = {}
        for key, v in self.manual_vars.items():
            out[key] = int(v.get())
        return out

    def on_start(self):
        if self.dev is None or self.detecting:
            return
        try:
            st, _ = self.dev.rx_start()
        except Exception as e:
            messagebox.showerror("RX_START", str(e))
            return
        if st != 0x00:
            self.status.set(f"RX_START вернул STATUS=0x{st:02X} ({mks.status_name(st)})")
            return
        self.detecting = True
        self._last_count = None
        self._last_frame_time = 0.0
        self._fps_count = None
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()
        self.status.set("Детекция запущена.")
        self._refresh_controls()

    def on_stop(self):
        if not self.detecting:
            return
        self.detecting = False
        t = self.poll_thread
        if t is not None:
            t.join(timeout=1.0)
        self.poll_thread = None
        try:
            if self.dev is not None:
                self.dev.rx_stop()
        except Exception:
            pass
        self.status.set("Детекция остановлена.")
        self._refresh_controls()

    def on_save_png(self):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"cir_{ts}.png"
        try:
            self.fig.savefig(name, dpi=120)
            self.status.set(f"График сохранён: {name}")
        except Exception as e:
            messagebox.showerror("Сохранение", str(e))

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

    # ------------------------------------------- фоновый поток опроса --
    def _poll_loop(self):
        """Только этот поток обращается к self.dev, пока идёт детекция."""
        try:
            half = int(self.half_var.get())
        except ValueError:
            half = 0
        while self.detecting:
            try:
                st_m, data_m = self.dev.get_signal_metrics()
                metrics = mks.parse_signal_metrics(data_m) if st_m == 0x00 else None
                st_c, data_c = self.dev.get_cir(half)
                cir = mks.parse_cir(data_c) if st_c == 0x00 else None
                self.q.put(("data", metrics, cir))
            except Exception as e:
                self.q.put(("poll_err", str(e)))
                break
            time.sleep(POLL_PERIOD_S)

    # ------------------------------------------- главный цикл обновления --
    def _pump_queue(self):
        last_data = None
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "data":
                    last_data = item
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
                elif kind == "poll_err":
                    self.status.set(f"Опрос прерван: {item[1]}")
                    self.detecting = False
                    self._refresh_controls()
        except queue.Empty:
            pass

        if last_data is not None:
            self._apply_data(last_data[1], last_data[2])

        # Лампа гаснет сама, если кадров давно не было.
        self._update_lamp()
        self.root.after(100, self._pump_queue)

    def _apply_data(self, metrics, cir):
        now = time.time()
        if metrics is not None:
            count = metrics.get("count", 0)
            if self._last_count is not None and count > self._last_count:
                self._last_frame_time = now
            # fps по приращению count за интервал
            if self._fps_count is not None and self._fps_time is not None:
                dt = now - self._fps_time
                if dt > 0:
                    self._fps = max(0.0, (count - self._fps_count) / dt)
            self._fps_count = count
            self._fps_time = now
            self._last_count = count

            self.tel_vars["count"].set(str(count))
            self.tel_vars["fps"].set(f"{self._fps:.1f}")
            if metrics.get("format") == "final":
                self.tel_vars["snr"].set(f"{metrics['snr_db']:.2f}" if metrics.get("snr_valid") else "н/д")
                self.tel_vars["rssi"].set(f"{metrics['rssi_dbm']:.2f}" if metrics.get("rssi_valid") else "н/д")
                self.tel_vars["fp_power"].set(f"{metrics['fp_power_dbm']:.2f}" if metrics.get("fp_valid") else "н/д")
            else:
                for k in ("snr", "rssi", "fp_power"):
                    self.tel_vars[k].set("(интерим)")

        if cir is not None:
            self.tel_vars["fp_index"].set(str(cir.get("fp_index", "—")))
            if now - self._last_plot_time >= PLOT_PERIOD_S:
                self._draw_cir(cir)
                self._last_plot_time = now

    def _draw_cir(self, cir):
        amps = cir.get("amps") or []
        start = cir.get("start_index", 0)
        fp = cir.get("fp_index", None)
        xs = [start + k for k in range(len(amps))]
        self.ax.clear()
        self.ax.set_xlabel("индекс отсчёта")
        self.ax.set_ylabel("|CIR| = sqrt(I²+Q²)")
        if amps:
            self.ax.plot(xs, amps, marker=".", linewidth=1.0)
            if fp is not None:
                self.ax.axvline(fp, color="red", linestyle="--", linewidth=1.0)
                self.ax.text(fp, max(amps), " FP", color="red", va="top")
        self.fig.tight_layout()
        self.canvas.draw_idle()

    def _update_lamp(self):
        present = (self.detecting
                   and self._last_frame_time > 0.0
                   and (time.time() - self._last_frame_time) < PRESENCE_WINDOW_S)
        if present:
            self.lamp.configure(text="СШП В КАНАЛЕ: ЕСТЬ", bg="#1e8e3e")
        else:
            self.lamp.configure(text="СШП В КАНАЛЕ: НЕТ", bg="#3a3a3a")

    # --------------------------------------------------------- утилиты --
    def _note_status(self, r, cmd):
        try:
            st, _ = r
            self.status.set(f"{cmd}: STATUS=0x{st:02X} ({mks.status_name(st)})")
        except Exception:
            pass


def main():
    default_port = sys.argv[1] if len(sys.argv) > 1 else ""
    root = tk.Tk()
    MKSGui(root, default_port)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
