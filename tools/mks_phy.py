#!/usr/bin/env python3
"""
*******************************************************************************
  МКС — Модуль коммуникации и сопряжения
  Хостовые инструменты (ПК) для STM32F411 + 2x DWM1000 (DW1000)

  Файл:     mks_phy.py
  Описание: единый источник PHY-пресетов (Mode 1..8) и сборки PARAMS для
            SET_PHY_CONFIG (0x10).

  Copyright (c) 2026 NCPR, Flexlab LLC. Все права защищены.
*******************************************************************************

ЗАЧЕМ ЭТОТ МОДУЛЬ. Таблица PHY-пресетов на момент создания продублирована в
ЧЕТЫРЁХ файлах (mks_stream_probe.py, mks_data_probe.py, mks_gui.py — плюс сборка
params ещё и в mks_console.py), причём в GUI ключи строковые, в пробниках
числовые. Расхождение копий даёт молчаливый рассинхрон приёмника и передатчика →
НОЛЬ принятых кадров, то есть симптом, неотличимый от аппаратной неисправности.
Модуль сделан, чтобы новые инструменты не заводили пятую копию.

Существующие скрипты НЕ тронуты — они продолжают работать на своих локальных
копиях. Миграция их на этот модуль — отдельная правка, по решению владельца.

РАСКЛАДКА PARAMS SET_PHY_CONFIG (0x10) — 7 байт:
    ch u8 | dr u8 | plen u16 LE | code u8 | prf u8 | pac u8
Сверено по ЧЕТЫРЁМ независимым источникам, все совпадают:
    mks_stream_probe.phy_params(), mks_data_probe.phy_params(),
    mks_gui._exec_step() (ветки "setphy" и "mode"), mks_console.cmd_setphy().
Источник истины по протоколу — docs/PROTOCOL_SPEC.md.

Значения полей: dr — код скорости (0=110k, 1=850k, 2=6M8); prf — число МГц
(16/64); plen — длина преамбулы в символах; code — номер preamble code.

История изменений (для будущих правщиков: помечать правки в формате
  <Имя>: ГГГГ-ММ-ДД — описание — чтобы различать авторов):
  Wagan: 2026-07-26 — модуль выделен: единая таблица Mode 1..8 + phy_params()
                      вместо четвёртой копии (нужен для mks_rawcir.py).
"""

from __future__ import annotations

# Пресеты PHY. Значения идентичны PHY_MODES в mks_stream_probe.py / mks_data_probe.py
# и (по содержанию) строковым ключам mks_gui.py — сверено построчно.
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

# Человекочитаемые подписи (те же, что ключи PHY_MODES в mks_gui.py).
MODE_LABELS = {
    1: "Mode 1 (ch2, 110k, PRF16, code3)",
    2: "Mode 2 (ch2, 6M8, PRF16, code3)",
    3: "Mode 3 (ch2, 110k, PRF64, code9)",
    4: "Mode 4 (ch2, 6M8, PRF64, code9)",
    5: "Mode 5 (ch5, 110k, PRF16, code3)",
    6: "Mode 6 (ch5, 6M8, PRF16, code3)",
    7: "Mode 7 (ch5, 110k, PRF64, code9)",
    8: "Mode 8 (ch5, 6M8, PRF64, code9)",
}

# Дефолт по всем инструментам проекта — Mode 3 (слушаем киты EVK).
DEFAULT_MODE = 3

PHY_FIELDS = ("ch", "dr", "plen", "code", "prf", "pac")


def phy_params(p: dict) -> bytes:
    """Собрать 7 байт PARAMS для SET_PHY_CONFIG (0x10): ch, dr, plen u16 LE,
    code, prf, pac. Раскладка — см. шапку модуля (сверена по 4 источникам)."""
    missing = [k for k in PHY_FIELDS if k not in p]
    if missing:
        raise ValueError(f"PHY: не хватает полей {missing}")
    return bytes([p["ch"] & 0xFF, p["dr"] & 0xFF,
                  p["plen"] & 0xFF, (p["plen"] >> 8) & 0xFF,
                  p["code"] & 0xFF, p["prf"] & 0xFF, p["pac"] & 0xFF])


def by_mode(n: int) -> dict:
    """Копия пресета по номеру Mode 1..8 (копия, чтобы вызывающий не портил таблицу)."""
    if n not in PHY_MODES:
        raise ValueError(f"Mode {n} не существует (есть 1..8)")
    return dict(PHY_MODES[n])


def parse_phy_string(s: str) -> dict:
    """Разобрать ручной PHY из 6 чисел: "ch dr plen code prf pac".
    Тот же порядок, что у --phy в mks_stream_probe.py и setphy в консоли."""
    vals = s.replace(",", " ").split()
    if len(vals) != 6:
        raise ValueError('PHY: нужно 6 чисел, напр. "2 2 128 9 64 8"')
    try:
        nums = [int(v) for v in vals]
    except ValueError:
        raise ValueError('PHY: все 6 значений должны быть целыми, напр. "2 2 128 9 64 8"')
    return dict(zip(PHY_FIELDS, nums))


def describe(p: dict, label: str | None = None) -> str:
    """Компактное описание PHY для логов."""
    body = (f"ch={p['ch']} dr={p['dr']} plen={p['plen']} "
            f"code={p['code']} prf={p['prf']} pac={p['pac']}")
    return f"{label}: {body}" if label else body
