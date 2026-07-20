#!/usr/bin/env python3
"""
*******************************************************************************
  МКС — Модуль коммуникации и сопряжения
  Хостовые инструменты (ПК) для STM32F411 + 2x DWM1000 (DW1000)

  Файл:     mks_stream.py
  Описание: общий разбор потокового кадра (SET_STREAM_MODE 0x42, CIR-2a).
            Один код для mks_stream_probe.py и mks_gui.py.

  Copyright (c) 2026 NCPR, Flexlab LLC. Все права защищены.
*******************************************************************************

Потоковый кадр (СВОЙ формат, отдельный от командного SYNC 0xAA55):
    SMARK(0xDE 0xCA) | LEN16(u16 LE) | SEQ(u16) | DROPPED(u16) | CONTENT(u8) | PAYLOAD | CRC8
    LEN16   = число байт после LEN16 и до CRC (SEQ+DROPPED+CONTENT+PAYLOAD).
    CONTENT = 1 → PAYLOAD = метрики(30) + окно CIR; 2 → PAYLOAD = метрики(30).
    CRC8    = poly 0x07 по [LEN16 .. конец PAYLOAD) (SMARK не входит).

История изменений (для будущих правщиков: помечать правки в формате
  <Имя>: ГГГГ-ММ-ДД — описание — чтобы различать авторов):
  Wagan: 2026-07-20 — модуль выделен: общий разбор потокового кадра (parse_stream_body)
                      и ре-синхронизация по SMARK (StreamReader) вынесены из
                      mks_stream_probe.py, чтобы probe и GUI использовали ОДИН код.
"""

from __future__ import annotations

import struct

import mks_protocol as mks

SMARK = b"\xDE\xCA"


# Wagan: 2026-07-20 — разбор тела потокового кадра (общий для probe и GUI).
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


# Wagan: 2026-07-20 — ре-синхронизация по SMARK: устойчив к мусору/битому CRC (общий).
class StreamReader:
    """Извлекает потоковые кадры из байтового потока pyserial с ре-синхронизацией.
    poll() дочитывает доступные байты и возвращает список (body, crc_ok) готовых
    кадров; при битом CRC / мусоре сдвигается и ищет следующий SMARK (не падает)."""

    def __init__(self, ser):
        self.ser = ser
        self.buf = bytearray()

    def poll(self):
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
