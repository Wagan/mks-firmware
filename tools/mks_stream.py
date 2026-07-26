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
  Wagan: 2026-07-26 — разбор content=5 (⚙️ эксперимент RAW_CIR_MONITOR): ПОЛНЫЙ сырой
                      аккумулятор CIR, снятый по раннему событию (RXSFDD/RXPHE/RXFCE).
  Wagan: 2026-07-26 — StreamReader: проверка правдоподобности LEN16 (MAX_BODY_LEN).
                      Нужна именно из-за content=5: 4 КБ сырых I/Q — высокоэнтропийные
                      данные, в которых байтовая пара 0xDE 0xCA встречается случайно
                      примерно в 6% кадров. При аллигнированном чтении это безвредно
                      (кадр забирается по длине), но после потери байтов / битого CRC
                      ре-синхронизация садилась на ЛОЖНЫЙ SMARK внутри CIR, читала
                      мусорный LEN16 (до 65535) и вставала ждать байты, которые не
                      придут. Теперь неправдоподобная длина — сразу сдвиг на 1.
"""

from __future__ import annotations

import struct

import mks_protocol as mks

SMARK = b"\xDE\xCA"

# Wagan: 2026-07-26 — границы content=5 (⚙️ RAW_CIR_MONITOR).
# Полный аккумулятор DW1000: 1016 отсчётов при PRF64, 992 при PRF16 (ACC_MEM 4064 Б).
RAW_CIR_MAX_SAMPLES = 1016
# PAYLOAD content=5 = 6 (event+prf+read_ms+sample_count) + sample_count*4.
RAW_CIR_MAX_PAYLOAD = 6 + RAW_CIR_MAX_SAMPLES * 4          # 4070
# Максимально возможное тело кадра = SEQ+DROPPED+CONTENT + самый большой PAYLOAD.
MAX_BODY_LEN = 5 + RAW_CIR_MAX_PAYLOAD                     # 4075

# Источник события съёма (поле event в PAYLOAD content=5).
RAW_CIR_EVENTS = {1: "RXSFDD", 2: "RXPHE", 3: "RXFCE"}


# Wagan: 2026-07-20 — разбор тела потокового кадра (общий для probe и GUI).
# Wagan: 2026-07-22 — разбор content=3 (канал данных): PAYLOAD = data_len(u16 LE)+data.
# Wagan: 2026-07-22 — разбор content=4 (данные+метрики, RSSI-в-beacon): метрики30 + data.
# Wagan: 2026-07-26 — разбор content=5 (полный сырой CIR, эксперимент).
def parse_stream_body(body: bytes) -> dict:
    """Разобрать тело потокового кадра (SEQ+DROPPED+CONTENT+PAYLOAD, без SMARK/LEN16/
    CRC). Возвращает dict: seq, dropped, content, metrics, cir, data, raw_cir.
      Раскладка общая: SEQ[0:2], DROPPED[2:4], CONTENT[4].
      content=1: метрики[5:35] + окно CIR[35:]  → metrics, cir; data=None.
      content=2: метрики[5:35]                  → metrics; cir=None, data=None.
      content=3: data_len u16 LE [5:7] + data[7:7+data_len] → data (bytes);
                 metrics=None, cir=None.
      content=4: метрики[5:35] + data_len u16 LE [35:37] + data[37:37+data_len]
                 → metrics И data (тот же кадр); cir=None.
      content=5: ⚙️ эксперимент RAW_CIR_MONITOR — ПОЛНЫЙ сырой аккумулятор:
                 event u8[5] | prf u8[6] | read_ms u16[7:9] | sample_count u16[9:11] |
                 sample_count×(I int16, Q int16)[11:]  → raw_cir; metrics/cir/data=None.

    Ключ raw_cir присутствует во ВСЕХ ветках (None, если это не content=5), чтобы
    вызывающий мог обращаться к нему не проверяя content.

    ВНИМАНИЕ по content=5: структура raw_cir — СВОЯ, она НЕ повторяет формат возврата
    mks.parse_cir (окно вокруг first path). Сделано осознанно: это разные сущности —
    там усечённое окно с центрированием по FP, здесь весь аккумулятор с нуля, и FP при
    съёме по RXSFDD ещё не готов (LDE стартует по RXPHD), поэтому центрировать нечем.
    Поля raw_cir:
        event        int  — 1=RXSFDD, 2=RXPHE, 3=RXFCE (см. RAW_CIR_EVENTS)
        event_name   str  — то же словом, для логов
        prf          int  — 16 или 64
        read_ms      int  — время dwt_readaccdata на плате, мс (HAL_GetTick, ±1 мс)
        sample_count int  — 1016 (PRF64) или 992 (PRF16)
        iq           list — [(I, Q), ...] длиной sample_count, int16 со знаком
    Амплитуду/огибающую здесь НЕ считаем: разбор остаётся чистым, счёт — в потребителе.
    """
    if len(body) < 5:
        raise mks.ProtocolError(f"поток: тело короче заголовка ({len(body)})")
    seq, dropped = struct.unpack_from("<HH", body, 0)
    content = body[4]

    if content == 3:                       # только данные (тело кадра без FCS)
        if len(body) < 7:
            raise mks.ProtocolError(f"поток content=3: нет data_len ({len(body)})")
        data_len = struct.unpack_from("<H", body, 5)[0]
        if len(body) < 7 + data_len:
            raise mks.ProtocolError(
                f"поток content=3: тело короче data_len (нужно {7 + data_len}, есть {len(body)})")
        data = bytes(body[7:7 + data_len])
        return {"seq": seq, "dropped": dropped, "content": content,
                "metrics": None, "cir": None, "data": data, "raw_cir": None}

    if content == 4:                       # данные + метрики (того же принятого кадра)
        if len(body) < 5 + 30 + 2:
            raise mks.ProtocolError(f"поток content=4: тело короче метрик+data_len ({len(body)})")
        metrics = mks.parse_signal_metrics(body[5:35])
        data_len = struct.unpack_from("<H", body, 35)[0]
        if len(body) < 37 + data_len:
            raise mks.ProtocolError(
                f"поток content=4: тело короче data_len (нужно {37 + data_len}, есть {len(body)})")
        data = bytes(body[37:37 + data_len])
        return {"seq": seq, "dropped": dropped, "content": content,
                "metrics": metrics, "cir": None, "data": data, "raw_cir": None}

    # Wagan: 2026-07-26 — content=5: полный сырой аккумулятор (эксперимент).
    if content == 5:
        if len(body) < 5 + 6:
            raise mks.ProtocolError(
                f"поток content=5: тело короче заголовка PAYLOAD ({len(body)})")
        event, prf, read_ms, sample_count = struct.unpack_from("<BBHH", body, 5)
        if sample_count > RAW_CIR_MAX_SAMPLES:
            raise mks.ProtocolError(
                f"поток content=5: sample_count={sample_count} > {RAW_CIR_MAX_SAMPLES}")
        need = 5 + 6 + sample_count * 4
        if len(body) < need:
            raise mks.ProtocolError(
                f"поток content=5: тело короче CIR (нужно {need}, есть {len(body)})")
        # I/Q подряд, int16 LE со знаком: I1,Q1,I2,Q2,... (dummy-байт отброшен прошивкой)
        flat = struct.unpack_from(f"<{sample_count * 2}h", body, 11)
        raw_cir = {
            "event": event,
            "event_name": RAW_CIR_EVENTS.get(event, f"UNKNOWN({event})"),
            "prf": prf,
            "read_ms": read_ms,
            "sample_count": sample_count,
            "iq": list(zip(flat[0::2], flat[1::2])),
        }
        return {"seq": seq, "dropped": dropped, "content": content,
                "metrics": None, "cir": None, "data": None, "raw_cir": raw_cir}

    # content=1/2: метрики (+CIR)
    if len(body) < 5 + 30:
        raise mks.ProtocolError(f"поток: тело короче минимума ({len(body)})")
    metrics = mks.parse_signal_metrics(body[5:35])
    cir = mks.parse_cir(body[35:]) if content == 1 else None
    return {"seq": seq, "dropped": dropped, "content": content,
            "metrics": metrics, "cir": cir, "data": None, "raw_cir": None}


# Wagan: 2026-07-20 — ре-синхронизация по SMARK: устойчив к мусору/битому CRC (общий).
# Wagan: 2026-07-26 — проверка правдоподобности LEN16 (см. историю в шапке).
class StreamReader:
    """Извлекает потоковые кадры из байтового потока pyserial с ре-синхронизацией.
    poll() дочитывает доступные байты и возвращает список (body, crc_ok) готовых
    кадров; при битом CRC / мусоре сдвигается и ищет следующий SMARK (не падает).

    Большие кадры (content=5, ~4080 Б) собираются за несколько poll() — это нормально:
    пока тело не пришло целиком, _extract просто ждёт."""

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
            # Wagan: 2026-07-26 — ложный SMARK внутри сырого CIR даёт мусорный LEN16;
            # без этой проверки читатель вставал ждать до 65535 байт, которые не придут.
            if body_len < 5 or body_len > MAX_BODY_LEN:
                del self.buf[:1]                    # неправдоподобная длина — сдвиг на 1
                continue
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
