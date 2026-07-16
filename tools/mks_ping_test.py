#!/usr/bin/env python3
"""
mks_ping_test.py — проверка связи с МКС: PING / INIT / GET_STATUS.

Использование:
    python mks_ping_test.py COM3
    python mks_ping_test.py COM3 --init-timeout 20

Таймауты:
    PING / GET_STATUS — короткие (не трогают долгие операции).
    INIT — длинный (загрузка микрокода на модулях занимает время).

Ctrl+C прерывает в любой момент (чтение идёт короткими порциями).
"""

import sys
import argparse
import mks_protocol as mks


def hexdump(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b) if b else "(пусто)"


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка связи ПК <-> МКС")
    ap.add_argument("port", help="COM-порт, например COM3")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--timeout", type=float, default=3.0, help="таймаут обычных команд")
    ap.add_argument("--init-timeout", type=float, default=20.0, help="таймаут INIT")
    args = ap.parse_args()

    print(f"Открываю {args.port} @ {args.baud} 8N1 ...")
    try:
        dev = mks.MKS(args.port, args.baud, args.timeout)
    except Exception as e:
        print(f"  ОШИБКА открытия порта: {e}")
        return 2

    ok_all = True
    try:
        with dev:
            # PING
            print("\n[1] PING")
            try:
                st, data = dev.ping()
                print(f"    STATUS = 0x{st:02X} ({mks.status_name(st)})")
                print(f"    DATA   = {hexdump(data)}")
                if st == 0x00 and data == b"\x00":
                    print("    -> OK: PONG получен")
                else:
                    print("    -> НЕОЖИДАННО: ждали STATUS=OK, DATA=00")
                    ok_all = False
            except Exception as e:
                print(f"    ОШИБКА: {e}")
                ok_all = False

            # INIT (длинный таймаут)
            print(f"\n[2] INIT (жду до {args.init_timeout:.0f} c)")
            try:
                st, data = dev.init(timeout=args.init_timeout)
                print(f"    STATUS = 0x{st:02X} ({mks.status_name(st)})")
                print(f"    DATA   = {hexdump(data)}")
                if st == 0x00:
                    print("    -> OK: инициализация прошла")
                else:
                    print(f"    -> STATUS не OK ({mks.status_name(st)})")
                    ok_all = False
            except Exception as e:
                print(f"    ОШИБКА: {e}")
                ok_all = False

            # GET_STATUS
            print("\n[3] GET_STATUS")
            try:
                st, data = dev.get_status()
                print(f"    STATUS = 0x{st:02X} ({mks.status_name(st)})")
                print(f"    DATA   = {hexdump(data)}")
                if st == 0x00:
                    try:
                        for k, v in mks.parse_get_status(data).items():
                            print(f"      {k:16} = {v}")
                        print("    -> OK")
                    except Exception as e:
                        print(f"    -> разбор DATA не удался: {e}")
                        ok_all = False
                else:
                    ok_all = False
            except Exception as e:
                print(f"    ОШИБКА: {e}")
                ok_all = False
    except KeyboardInterrupt:
        print("\nПрервано (Ctrl+C).")
        return 130

    print("\n" + ("ИТОГ: всё прошло" if ok_all else "ИТОГ: есть проблемы"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
