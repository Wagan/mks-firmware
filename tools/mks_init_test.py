#!/usr/bin/env python3
"""
mks_init_test.py — отдельная проверка INIT с длинным таймаутом.

Цель: выяснить, INIT реально долго выполняется (и ответ приходит, если подождать),
или прошивка зависает. Между командами чистим входной буфер, чтобы запоздавшие
ответы не сбивали синхронизацию.

Использование:
    python mks_init_test.py COM3
    python mks_init_test.py COM3 --timeout 15
"""

import sys
import argparse
import time
import mks_protocol as mks


def hexdump(b):
    return " ".join(f"{x:02X}" for x in b) if b else "(пусто)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()

    print(f"Открываю {args.port}, таймаут {args.timeout} c ...")
    dev = mks.MKS(args.port, args.baud, args.timeout)

    with dev:
        # 1) PING — проверка что связь есть
        print("\n[PING]")
        dev.ser.reset_input_buffer()
        try:
            st, data = dev.ping()
            print(f"  STATUS=0x{st:02X} ({mks.status_name(st)}) DATA={hexdump(data)}")
        except Exception as e:
            print(f"  ОШИБКА: {e}")
            return 1

        # 2) INIT — с замером времени
        print("\n[INIT] отправляю, жду ответ (может занять секунды)...")
        dev.ser.reset_input_buffer()
        t0 = time.time()
        dev.send_command(mks.CMD_INIT)
        try:
            st, data = dev.read_response()
            dt = time.time() - t0
            print(f"  Ответ за {dt:.2f} c: STATUS=0x{st:02X} ({mks.status_name(st)}) DATA={hexdump(data)}")
        except Exception as e:
            dt = time.time() - t0
            print(f"  За {dt:.2f} c ответа нет: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
