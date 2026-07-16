"""
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
"""

from __future__ import annotations
import struct
import time

SYNC = bytes([0xAA, 0x55])

CMD_PING             = 0x00
CMD_INIT             = 0x01
CMD_GET_STATUS       = 0x02
CMD_RESET_RADIO      = 0x03
CMD_SET_PHY_CONFIG   = 0x10
CMD_SET_TX_POWER     = 0x11

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


def parse_get_status(data: bytes) -> dict:
    if len(data) != 7:
        raise ProtocolError(f"GET_STATUS: ожидалось 7 байт, получено {len(data)}")
    return {
        "TX_state": data[0],
        "RX_state": data[1],
        "channel": data[2],
        "data_rate": data[3],
        "preamble_length": struct.unpack_from("<H", data, 4)[0],
        "PRF": data[6],
    }
