#!/usr/bin/env python3
"""
*******************************************************************************
  МКС — Модуль коммуникации и сопряжения
  Хостовые инструменты (ПК) для STM32F411 + 2x DWM1000 (DW1000)

  Файл:     mks_msg.py
  Описание: прикладной слой канала данных поверх UWB (§15.1 стадия 1) — заголовок
            сообщения, фрагментация >119 Б, реассемблер по паре (src_id, msg_id),
            эхо-фильтр. ЧИСТАЯ логика (без COM/потока) — юнит-тестируемо.

  Copyright (c) 2026 NCPR, Flexlab LLC. Все права защищены.
*******************************************************************************

Кадр канала данных = 6-байтовый ЗАГОЛОВОК + данные, всё внутри 125-байтового
UWB-кадра (транспорт — content=3, см. mks_stream.py / прошивка):

    src_id u8 | msg_id u8 | frag_idx u8 | frag_cnt u8 | msg_type u8 | flags u8 | data[<=119]

  src_id   — id станции-источника (1..255; 0 зарезервирован под broadcast, не исп.).
  msg_id   — номер сообщения В ПРЕДЕЛАХ src_id (u8, оборачивается). Уникален только
             для своего src_id → ключ реассемблера — ПАРА (src_id, msg_id).
  frag_idx — индекс фрагмента 0..frag_cnt-1.
  frag_cnt — всего фрагментов в сообщении.
  msg_type — 0=TEXT, 1=BEACON, 2=CALL.
  flags    — 0 на этой стадии (иначе кадр нераспознан, но не падаем).
  data     — <=119 Б полезной нагрузки фрагмента.

Эхо-фильтр: приёмник отбрасывает кадр с src_id == собственному (М2 слышит свой М1),
кроме loopback-режима (там слушаем себя намеренно). Это чинит индикатор связи
(свои beacon не считаются за «слышу партнёра») и отражение своих TEXT.

История изменений (для будущих правщиков: помечать правки в формате
  <Имя>: ГГГГ-ММ-ДД — описание — чтобы различать авторов):
  Wagan: 2026-07-22 — прикладной слой канала данных (§15.1 стадия 1): 6-байтовый
                      заголовок (src_id), фрагментация 119 Б, реассемблер по
                      (src_id,msg_id), эхо-фильтр.
"""

from __future__ import annotations

import collections

HDR_LEN   = 6
FRAG_DATA = 119                 # 125 (TX_FRAME_MAX) - 6 (заголовок)
MAX_FRAME = HDR_LEN + FRAG_DATA # 125

MSG_TEXT   = 0
MSG_BEACON = 1
MSG_CALL   = 2
_KNOWN_TYPES = (MSG_TEXT, MSG_BEACON, MSG_CALL)
TYPE_NAME = {MSG_TEXT: "TEXT", MSG_BEACON: "BEACON", MSG_CALL: "CALL"}

SRC_BROADCAST = 0               # зарезервирован, на этой стадии не используется


# ------------------------------------------------------------------- заголовок --
def pack_header(src_id: int, msg_id: int, frag_idx: int, frag_cnt: int,
                msg_type: int, flags: int = 0) -> bytes:
    """Собрать 6-байтовый заголовок. Все поля u8 (0..255)."""
    fields = (("src_id", src_id), ("msg_id", msg_id), ("frag_idx", frag_idx),
              ("frag_cnt", frag_cnt), ("msg_type", msg_type), ("flags", flags))
    for name, v in fields:
        if not (0 <= v <= 255):
            raise ValueError(f"{name} вне 0..255: {v}")
    return bytes([src_id, msg_id, frag_idx, frag_cnt, msg_type, flags])


def build_frame(src_id: int, msg_id: int, frag_idx: int, frag_cnt: int,
                msg_type: int, data: bytes, flags: int = 0) -> bytes:
    """Заголовок + данные фрагмента (<=119 Б) → кадр канала данных (<=125 Б)."""
    if len(data) > FRAG_DATA:
        raise ValueError(f"data > {FRAG_DATA} байт ({len(data)}) — фрагментируйте")
    return pack_header(src_id, msg_id, frag_idx, frag_cnt, msg_type, flags) + bytes(data)


def parse_frame(frame: bytes) -> dict:
    """Разобрать кадр канала данных (заголовок 6 Б + данные). Не бросает — при
    нераспознанном возвращает recognized=False (кадр не роняет приёмник).
    recognized=True только если: len>=6, flags==0, msg_type известен, src_id!=0,
    frag_cnt>=1, frag_idx<frag_cnt."""
    if len(frame) < HDR_LEN:
        return {"recognized": False, "reason": "short", "raw": bytes(frame)}
    src_id, msg_id, frag_idx, frag_cnt, msg_type, flags = frame[:HDR_LEN]
    data = bytes(frame[HDR_LEN:])
    recognized = (flags == 0 and msg_type in _KNOWN_TYPES and src_id != SRC_BROADCAST
                  and frag_cnt >= 1 and frag_idx < frag_cnt)
    return {"recognized": recognized, "src_id": src_id, "msg_id": msg_id,
            "frag_idx": frag_idx, "frag_cnt": frag_cnt, "msg_type": msg_type,
            "flags": flags, "data": data,
            "reason": None if recognized else "bad-header"}


# ----------------------------------------------------------------- фрагментация --
def fragment(payload: bytes, src_id: int, msg_id: int, msg_type: int) -> list:
    """Нарезать payload на кадры по FRAG_DATA (119 Б). Пустой payload → ОДИН
    пустой фрагмент (валидное пустое сообщение). Возвращает список кадров (bytes,
    каждый <=125). frag_cnt<=255 (иначе ValueError)."""
    payload = bytes(payload)
    chunks = [payload[i:i + FRAG_DATA] for i in range(0, len(payload), FRAG_DATA)] or [b""]
    cnt = len(chunks)
    if cnt > 255:
        raise ValueError(f"слишком много фрагментов ({cnt} > 255)")
    return [build_frame(src_id, msg_id, idx, cnt, msg_type, ch)
            for idx, ch in enumerate(chunks)]


def fragment_text(text: str, src_id: int, msg_id: int) -> list:
    """UTF-8 упаковка текста → фрагменты MSG_TEXT."""
    return fragment(text.encode("utf-8"), src_id, msg_id, MSG_TEXT)


def decode_text(data: bytes) -> str:
    """Декод собранного TEXT: UTF-8 с errors='replace' (битые байты → U+FFFD, не падаем)."""
    return bytes(data).decode("utf-8", errors="replace")


# --------------------------------------------------------------------- эхо-фильтр --
def is_own_echo(src_id: int, own_src_id: int, loopback: bool = False) -> bool:
    """True → кадр надо ОТБРОСИТЬ как собственное эхо (М2 слышит свой М1).
    В loopback-режиме (одна плата, отладка) эхо-фильтр выключен — слушаем себя."""
    return (not loopback) and (src_id == own_src_id)


# --------------------------------------------------------------------- реассемблер --
class Reassembler:
    """Копит фрагменты по КЛЮЧУ (src_id, msg_id) и собирает сообщения.
    События из add()/sweep():
      ('complete',   src_id, msg_id, msg_type, data)
      ('incomplete', src_id, msg_id, msg_type, partial, missing_list, frag_cnt)
    Неполное помечается: (а) когда ТОТ ЖЕ src переходит на новый msg_id (серия
    предыдущего сообщения закончилась), (б) по таймауту (sweep). Повторы уже
    завершённого (src,msg_id) — напр. CALL x3 — игнорируются (dedup)."""

    def __init__(self, timeout: float = 5.0, dedup: int = 64):
        self.timeout = timeout
        self.building = {}                                   # (src,msg_id) -> entry
        self.done = collections.deque(maxlen=dedup)          # недавно завершённые ключи

    def add(self, f: dict, now: float) -> list:
        """f — dict из parse_frame (ожидается recognized=True). now — монотонное время."""
        key = (f["src_id"], f["msg_id"])
        if key in self.done:
            return []                                        # дубликат (CALL x3 / поздний фраг)

        events = []
        # тот же источник начал НОВОЕ сообщение → предыдущее его незавершённое — неполное
        for k in list(self.building):
            if k[0] == key[0] and k[1] != key[1]:
                events += self._flush(k)

        e = self.building.get(key)
        if e is None:
            e = self.building[key] = {"cnt": f["frag_cnt"], "type": f["msg_type"],
                                      "frags": {}, "t": now}
        e["t"] = now
        e["cnt"] = f["frag_cnt"]
        e["type"] = f["msg_type"]
        if f["frag_idx"] < f["frag_cnt"]:
            e["frags"][f["frag_idx"]] = f["data"]

        if len(e["frags"]) == e["cnt"] and all(i in e["frags"] for i in range(e["cnt"])):
            data = b"".join(e["frags"][i] for i in range(e["cnt"]))
            del self.building[key]
            self.done.append(key)
            events.append(("complete", key[0], key[1], e["type"], data))
        return events

    def sweep(self, now: float) -> list:
        """Пометить неполными сообщения старше timeout. Звать периодически."""
        events = []
        for k in list(self.building):
            if now - self.building[k]["t"] > self.timeout:
                events += self._flush(k)
        return events

    def _flush(self, key) -> list:
        e = self.building.pop(key)
        present = sorted(e["frags"])
        missing = [i for i in range(e["cnt"]) if i not in e["frags"]]
        data = b"".join(e["frags"][i] for i in present)     # собранный префикс/куски
        self.done.append(key)                               # не воскрешать поздними фрагментами
        return [("incomplete", key[0], key[1], e["type"], data, missing, e["cnt"])]
