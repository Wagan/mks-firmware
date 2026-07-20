"""
*******************************************************************************
  МКС — Модуль коммуникации и сопряжения
  Хостовые инструменты (ПК) для STM32F411 + 2x DWM1000 (DW1000)

  Файл:     mks_protocol.py
  Описание: библиотека протокола обмена ПК <-> МКС (кадры, CRC8, команды,
            разбор метрик приёма).

  Copyright (c) 2026 NCPR, Flexlab LLC. Все права защищены.
*******************************************************************************

mks_protocol.py — библиотека протокола обмена ПК <-> МКС.

Источник истины: docs/PROTOCOL_SPEC.md
  - Кадр:  SYNC(0xAA 0x55) | LEN | CMD_ID | PARAMS | CRC
  - Ответ: SYNC(0xAA 0x55) | LEN | STATUS | DATA   | CRC
  - LEN покрывает (CMD_ID + PARAMS) в запросе и (STATUS + DATA) в ответе.
  - CRC8: poly=0x07, init=0x00, без рефлексии, xorout=0x00.
  - CRC считается БЕЗ SYNC: по [LEN, CMD_ID, *PARAMS] / [LEN, STATUS, *DATA].
  - Все многобайтовые значения — little-endian.

Чтение сделано короткими порциями в цикле (poll), чтобы:
  - Ctrl+C срабатывал сразу (а не после длинного блокирующего read);
  - таймаут задавался на команду и переживал долгие операции (INIT).

История изменений (для будущих правщиков: помечать правки в формате
  <Имя>: ГГГГ-ММ-ДД — описание — чтобы различать авторов; до сих пор всё делал Wagan):
  Wagan: 2026-07-16 — первые хостовые скрипты протокола (PING/INIT/GET_STATUS, кадры,
                      CRC8, прерываемое чтение poll-порциями под Ctrl+C и таймаут INIT).
  Wagan: 2026-07-17 — приёмный слой: RX_START(0x30)/RX_STOP(0x31)/GET_SIGNAL_METRICS(0x40)
                      + parse_signal_metrics() под интерим-формат (18 Б, сырьё dwt_rxdiag_t).
  Wagan: 2026-07-17 — приближённая оценка мощности estimate_power() (RSSI/FP_POWER,
                      UM §4.7) + классификатор LOS/gray/NLOS (на хосте, для проверки).
  Wagan: 2026-07-17 — TX-команды: tx_frame/tx_stop (0x20/0x22), затем tx_periodic (0x21, v3).
  Wagan: 2026-07-17 — set_tx_power (0x11, v4): ручная мощность TX, ответ power u32 LE.
  Wagan: 2026-07-17 — parse_signal_metrics 18→28→30 байт (v5): строгий RSSI/FP_POWER
                      (RXPACC_NOSAT) + total SNR (= RSL + delta) — считает прошивка.
  Wagan: 2026-07-17 — GET_CIR (0x41, v6): окно CIR вокруг first path + parse_cir().
  Wagan: 2026-07-17 — SET_STREAM_MODE (0x42): set_stream_mode() для потокового режима.
  Wagan: 2026-07-18 — рантайм-обнаружение живых DW-модулей в INIT (не падать без M1) +
                      parse_get_status толерантен к расширенному ответу (17 Б).
"""

from __future__ import annotations
import struct
import time

# Единый источник версии хостовых инструментов (баннер консоли берёт отсюда).
HOST_VERSION = "6"

SYNC = bytes([0xAA, 0x55])

CMD_PING               = 0x00
CMD_INIT               = 0x01
CMD_GET_STATUS         = 0x02
CMD_RESET_RADIO        = 0x03
CMD_SET_PHY_CONFIG     = 0x10
CMD_SET_TX_POWER       = 0x11
CMD_TX_FRAME           = 0x20
CMD_TX_PERIODIC        = 0x21
CMD_TX_STOP            = 0x22
CMD_RX_START           = 0x30
CMD_RX_STOP            = 0x31
CMD_GET_SIGNAL_METRICS = 0x40
CMD_GET_CIR            = 0x41
CMD_SET_STREAM_MODE    = 0x42

STATUS_NAMES = {
    0x00: "OK",
    0x01: "UNKNOWN_CMD",
    0x02: "INVALID_PARAM",
    0x03: "RADIO_BUSY",
    0x04: "RADIO_ERROR",
    0x05: "BUFFER_OVERFLOW",
    0x06: "TIMEOUT",
    0x07: "INTERNAL_ERROR",
}


def crc8(data: bytes) -> int:
    """CRC-8, poly=0x07, init=0x00, без рефлексии, xorout=0x00."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def build_command(cmd_id: int, params: bytes = b"") -> bytes:
    """Собрать кадр команды. LEN = 1(CMD_ID) + len(params). CRC без SYNC."""
    if len(params) > 254:
        raise ValueError("PARAMS слишком длинные")
    length = 1 + len(params)
    body = bytes([length, cmd_id]) + params
    return SYNC + body + bytes([crc8(body)])


def status_name(status: int) -> str:
    return STATUS_NAMES.get(status, f"UNKNOWN(0x{status:02X})")


class ProtocolError(Exception):
    pass


class MKS:
    """Обёртка над COM-портом. Чтение — короткими порциями (прерываемое)."""

    POLL = 0.05  # шаг опроса порта; на нём же ловится Ctrl+C

    def __init__(self, port: str, baud: int = 115200, timeout: float = 5.0):
        import serial
        self.ser = serial.Serial(port, baud, timeout=self.POLL)
        self.default_timeout = timeout
        time.sleep(0.2)
        self.ser.reset_input_buffer()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def flush_input(self):
        self.ser.reset_input_buffer()

    def send_command(self, cmd_id: int, params: bytes = b"") -> None:
        self.ser.write(build_command(cmd_id, params))
        self.ser.flush()

    def _read_exact(self, n: int, deadline: float) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            if time.time() > deadline:
                raise ProtocolError(f"таймаут: получено {len(buf)} из {n} байт")
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def read_response(self, timeout=None):
        t = self.default_timeout if timeout is None else timeout
        deadline = time.time() + t

        prev = None
        while True:
            if time.time() > deadline:
                raise ProtocolError("SYNC не найден (таймаут — плата не ответила)")
            b = self.ser.read(1)
            if not b:
                continue
            if prev == 0xAA and b[0] == 0x55:
                break
            prev = b[0]

        length = self._read_exact(1, deadline)[0]
        body = self._read_exact(length, deadline)
        crc = self._read_exact(1, deadline)[0]

        expect = crc8(bytes([length]) + body)
        if expect != crc:
            raise ProtocolError(f"CRC не сошёлся: получен 0x{crc:02X}, ожидался 0x{expect:02X}")

        return body[0], body[1:]

    def command(self, cmd_id, params=b"", timeout=None):
        self.flush_input()
        self.send_command(cmd_id, params)
        return self.read_response(timeout)

    def ping(self, timeout=None):
        return self.command(CMD_PING, timeout=timeout)

    def init(self, timeout=None):
        return self.command(CMD_INIT, timeout=timeout)

    def get_status(self, timeout=None):
        return self.command(CMD_GET_STATUS, timeout=timeout)

    def rx_start(self, timeout=None):
        return self.command(CMD_RX_START, timeout=timeout)

    def rx_stop(self, timeout=None):
        return self.command(CMD_RX_STOP, timeout=timeout)

    def get_signal_metrics(self, timeout=None):
        return self.command(CMD_GET_SIGNAL_METRICS, timeout=timeout)

    def tx_frame(self, payload: bytes, timeout=None):
        """TX_FRAME (0x20): послать один кадр. PARAMS = length u16 LE + payload.
        length — число байт payload (без FCS; DW1000 добавит FCS сам)."""
        if len(payload) > 0xFFFF:
            raise ValueError("payload слишком длинный")
        params = struct.pack("<H", len(payload)) + payload
        return self.command(CMD_TX_FRAME, params, timeout=timeout)

    # Wagan: 2026-07-17 — TX_PERIODIC (0x21, v3): периодическая передача кадра.
    def tx_periodic(self, period_ms: int, payload: bytes, timeout=None):
        """TX_PERIODIC (0x21): периодически слать один и тот же кадр.
        PARAMS = period_ms u16 LE + length u16 LE + payload.
        period_ms — период между посылками (мс; прошивка требует >= 5).
        length — число байт payload (без FCS; DW1000 добавит FCS сам).
        Команда лишь ВЗВОДИТ режим и сразу возвращает OK; реальная посылка
        идёт в main loop прошивки. Останов — tx_stop()."""
        if not (0 <= period_ms <= 0xFFFF):
            raise ValueError("period_ms вне диапазона u16")
        if len(payload) > 0xFFFF:
            raise ValueError("payload слишком длинный")
        params = struct.pack("<HH", period_ms, len(payload)) + payload
        return self.command(CMD_TX_PERIODIC, params, timeout=timeout)

    def tx_stop(self, timeout=None):
        return self.command(CMD_TX_STOP, timeout=timeout)

    # Wagan: 2026-07-17 — SET_TX_POWER (0x11, v4): ручная мощность TX, ответ power u32 LE.
    def set_tx_power(self, power_level: int, timeout=None):
        """SET_TX_POWER (0x11): ручная регулировка мощности передатчика (вариант A).
        PARAMS = power_level u8. БОЛЬШЕ power_level → БОЛЬШЕ мощность (0 ≈ минимум,
        POWER_LEVEL_MAX=0xDF ≈ максимум; шаг ≈ 0.5 dB). Прошивка ограничивает
        сверху 0xDF. Требует предварительного SET_PHY_CONFIG (нужен канал).
        Ответ DATA = применённое значение регистра power (u32 LE, 4 байта).
        Реализация: octet = 0xFF - power_level, дублируется во все 4 октета —
        т.е. рост power_level уменьшает аттенюацию DA/mixer → мощность растёт."""
        if not (0 <= power_level <= 0xFF):
            raise ValueError("power_level вне диапазона u8")
        return self.command(CMD_SET_TX_POWER, bytes([power_level]), timeout=timeout)

    # Wagan: 2026-07-17 — GET_CIR (0x41, v6): окно CIR вокруг first path.
    def get_cir(self, half: int = 0, timeout=None):
        """GET_CIR (0x41): окно CIR вокруг first path.
        PARAMS = half u8 (полуширина окна; 0 = дефолт прошивки, макс. 30).
        Ответ DATA: fp_index u16 LE, start_index u16 LE, count u16 LE, затем
        count пар I/Q (int16 LE). Снимок делается прошивкой при приёме кадра
        (в ветке RXFCG, до перевзвода RX). Требует, чтобы после RX_START был
        принят хотя бы один кадр (иначе STATUS=TIMEOUT)."""
        if not (0 <= half <= 0xFF):
            raise ValueError("half вне диапазона u8")
        return self.command(CMD_GET_CIR, bytes([half]), timeout=timeout)

    # Wagan: 2026-07-17 — SET_STREAM_MODE (0x42): вкл/выкл потоковый режим (CIR-2a).
    def set_stream_mode(self, mode: int, timeout=None):
        """SET_STREAM_MODE (0x42): 0=выкл (командный режим); 1=вкл (метрики+CIR);
        2=вкл (только метрики). DATA нет. При вкл плата после КАЖДОГО принятого
        UWB-кадра сама шлёт потоковый кадр (свой формат: SMARK 0xDE 0xCA | LEN16 |
        SEQ | DROPPED | CONTENT | PAYLOAD | CRC8; CONTENT=1 → метрики30+окноCIR,
        CONTENT=2 → только метрики30) — читать отдельным приёмником (см.
        tools/mks_stream_probe.py). Пока поток включён, командный канал занят потоком."""
        if mode not in (0, 1, 2):
            raise ValueError("mode должен быть 0, 1 или 2")
        return self.command(CMD_SET_STREAM_MODE, bytes([mode]), timeout=timeout)


# Wagan: 2026-07-18 — толерантен к расширенному GET_STATUS (17 Б: +live_count/mask/dev_ids).
def parse_get_status(data: bytes) -> dict:
    """Разобрать DATA GET_STATUS. Первые 7 байт — прежний формат; при наличии
    расширения (>=9 байт) добавляются live_count, live_mask и dev_ids (по модулю,
    u32 LE, hex-строки). Терпимо к длине >=7 (старая прошивка = 7 байт)."""
    if len(data) < 7:
        raise ProtocolError(f"GET_STATUS: ожидалось >=7 байт, получено {len(data)}")
    m = {
        "TX_state": data[0],
        "RX_state": data[1],
        "channel": data[2],
        "data_rate": data[3],
        "preamble_length": struct.unpack_from("<H", data, 4)[0],
        "PRF": data[6],
    }
    if len(data) >= 9:                       # расширение: живость модулей
        m["live_count"] = data[7]
        m["live_mask"] = f"0x{data[8]:02X}"
        dev_ids = []
        off = 9
        while off + 4 <= len(data):
            dev_ids.append(f"0x{struct.unpack_from('<I', data, off)[0]:08X}")
            off += 4
        m["dev_ids"] = dev_ids
    return m


# Порядок полей ИНТЕРИМ-формата GET_SIGNAL_METRICS (PROTOCOL_SPEC §8):
# 18 байт = 9 x u16 LE, сырые поля dwt_rxdiag_t последнего принятого кадра.
SIGNAL_METRICS_FIELDS = (
    "count",       # число принятых кадров (счётчик прошивки, отладка)
    "CIR_PWR",     # maxGrowthCIR  -> "C" в формуле RX_LEVEL (UM §4.7.2)
    "RXPACC",      # rxPreamCount  -> "N" (может требовать SFD-коррекции)
    "STD_NOISE",   # stdNoise
    "FP_AMPL1",    # firstPathAmp1 -> "F1"
    "FP_AMPL2",    # firstPathAmp2 -> "F2"
    "FP_AMPL3",    # firstPathAmp3 -> "F3"
    "FP_INDEX",    # firstPath
    "MAX_NOISE",   # maxNoise
)


# Wagan: 2026-07-17 — разбор метрик по длине 18/28/30 Б (v5): +строгий RSSI/FP_POWER/SNR.
def parse_signal_metrics(data: bytes) -> dict:
    """Разобрать DATA GET_SIGNAL_METRICS. Поддержаны два формата по ДЛИНЕ:

    - 18 байт (ИНТЕРИМ, 9x u16 LE): только сырые поля dwt_rxdiag_t.
    - 28 байт (ФИНАЛ): первые 18 = те же сырые поля, затем 5 полей LE —
      RXPACC_NOSAT u16, N_corrected u16, RSSI i16 (dBm×100), FP_POWER i16
      (dBm×100), A_used u16 (A×100). RSSI/FP_POWER посчитаны В ПРОШИВКЕ (UM §4.7)
      с корректным N (SFD-коррекция по RXPACC==RXPACC_NOSAT).

    Ключ 'format' в результате = 'interim' | 'final'. Для финала добавлены поля
    rxpacc_nosat, N_corrected, rssi_dbm, fp_power_dbm, A_used (уже в человеческих
    единицах: dBm и A — float).
    """
    if len(data) == 18:
        values = struct.unpack("<9H", data)
        m = dict(zip(SIGNAL_METRICS_FIELDS, values))
        m["format"] = "interim"
        return m
    if len(data) in (28, 30):
        head = struct.unpack("<9H", data[:18])
        m = dict(zip(SIGNAL_METRICS_FIELDS, head))
        nosat, n_corr, rssi_q, fp_q, a_q = struct.unpack("<HHhhH", data[18:28])
        m["format"]       = "final"
        m["rxpacc_nosat"] = nosat
        m["N_corrected"]  = n_corr
        m["rssi_dbm"]     = rssi_q / 100.0
        m["fp_power_dbm"] = fp_q / 100.0
        m["A_used"]       = a_q / 100.0
        # признак 'н/д' из прошивки (INT16_MIN = -32768 → /100 = -327.68)
        m["rssi_valid"]   = (rssi_q != -32768)
        m["fp_valid"]     = (fp_q   != -32768)
        if len(data) == 30:
            (snr_q,) = struct.unpack("<h", data[28:30])
            m["snr_db"]    = snr_q / 100.0
            m["snr_valid"] = (snr_q != -32768)
        return m
    raise ProtocolError(
        f"GET_SIGNAL_METRICS: ожидалось 18 (интерим), 28 или 30 (финал) байт, "
        f"получено {len(data)}"
    )


def parse_cir(data: bytes) -> dict:
    """Разобрать DATA GET_CIR: заголовок 6 байт (fp_index, start_index, count —
    u16 LE) + count пар I/Q (int16 LE). Возвращает dict с fp_index, start_index,
    count, samples (список (i, q)) и amps (список sqrt(i^2+q^2))."""
    if len(data) < 6:
        raise ProtocolError(f"GET_CIR: заголовок 6 байт не помещается ({len(data)})")
    fp_index, start_index, count = struct.unpack_from("<HHH", data, 0)
    need = 6 + count * 4
    if len(data) < need:
        raise ProtocolError(
            f"GET_CIR: тело короче заявленного (count={count} → надо {need}, "
            f"есть {len(data)})")
    samples = []
    amps = []
    off = 6
    for _ in range(count):
        i, q = struct.unpack_from("<hh", data, off)
        off += 4
        samples.append((i, q))
        amps.append((i * i + q * q) ** 0.5)
    return {
        "fp_index": fp_index,
        "start_index": start_index,
        "count": count,
        "samples": samples,
        "amps": amps,
    }


def signal_metrics_ok(m: dict) -> bool:
    """Критерий 'кадр реально принят' для проверки на железе.

    valid=1 в прошивке уже гарантирован (иначе был бы STATUS=TIMEOUT),
    но дополнительно убеждаемся, что ключевые сырые поля ненулевые —
    это доказывает содержательный приём, а не пустой кадр.
    """
    return (
        m.get("count", 0) > 0
        and m.get("CIR_PWR", 0) != 0
        and m.get("RXPACC", 0) != 0
        and m.get("FP_AMPL1", 0) != 0
    )


# --- Оценка мощности приёма (DW1000 UM §4.7) ---------------------------------
# Формулы дословно из UM (стр. 44–45):
#   RX_LEVEL  = 10*log10( C * 2^17 / N^2 ) - A   [dBm]   (§4.7.2)
#   FP_POWER  = 10*log10( (F1^2+F2^2+F3^2) / N^2 ) - A  [dBm]   (§4.7.1)
# где C = CIR_PWR, F* = FP_AMPL1..3, N = RXPACC,
#   A = 113.77 (PRF 16 МГц) / 121.74 (PRF 64 МГц).
#
# ВАЖНО (ограничение интерим-формата): N по UM может требовать SFD-коррекции,
# но только если RXPACC == RXPACC_NOSAT. RXPACC_NOSAT в интерим-формате НЕТ,
# поэтому здесь N берётся как есть (без SFD-коррекции). Для Mode 3 (преамбула
# 1024) замер даёт RXPACC << 1024 → счётчик насытился рано → коррекция, скорее
# всего, не нужна, но СТРОГО это не подтверждено. Значит RSSI/FP_POWER здесь —
# ПРИБЛИЖЁННЫЕ. Строгий расчёт — после добавления чтения RXPACC_NOSAT в прошивку.
import math as _math

DWT_A_PRF16 = 113.77
DWT_A_PRF64 = 121.74


def _a_const(prf_mhz: int) -> float:
    """A-константа по PRF (МГц). Только 16/64 определены в UM."""
    if prf_mhz == 16:
        return DWT_A_PRF16
    if prf_mhz == 64:
        return DWT_A_PRF64
    raise ProtocolError(f"PRF {prf_mhz} МГц: A-константа не определена (только 16/64)")


# Wagan: 2026-07-17 — приближённая оценка RSSI/FP_POWER (UM §4.7) + LOS/gray/NLOS (хост).
def estimate_power(m: dict, prf_mhz: int = 64) -> dict:
    """Приближённая оценка RX_LEVEL и FP_POWER (dBm) из сырых метрик.

    prf_mhz — PRF принятого сигнала (наш Mode 3 = 64). N без SFD-коррекции
    (см. ограничение выше). Возвращает dict с rx_level_dbm, fp_power_dbm,
    diff_db и грубой классификацией канала LOS/NLOS (UM §4.7.1: <6 дБ LOS,
    >10 дБ NLOS).
    """
    A = _a_const(prf_mhz)
    N = m["RXPACC"]
    if N == 0:
        raise ProtocolError("RXPACC=0 — оценка мощности невозможна")
    n2 = float(N) * float(N)

    C = m["CIR_PWR"]
    rx_level = 10.0 * _math.log10((C * (1 << 17)) / n2) - A

    f1, f2, f3 = m["FP_AMPL1"], m["FP_AMPL2"], m["FP_AMPL3"]
    fp_sum = float(f1) * f1 + float(f2) * f2 + float(f3) * f3
    fp_power = 10.0 * _math.log10(fp_sum / n2) - A

    diff = rx_level - fp_power
    if diff < 6.0:
        channel = "LOS"
    elif diff > 10.0:
        channel = "NLOS"
    else:
        channel = "gray (6..10 дБ)"

    return {
        "rx_level_dbm": rx_level,
        "fp_power_dbm": fp_power,
        "diff_db": diff,
        "channel": channel,
        "approx": True,  # без SFD-коррекции N; см. ограничение в модуле
    }
