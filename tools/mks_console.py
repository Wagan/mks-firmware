#!/usr/bin/env python3
"""
mks_console.py — интерактивная консоль для управления МКС.

Удобный аналог терминала, но для БИНАРНОГО протокола: ты пишешь человеческие
команды (ping, init, status, setphy ...), а консоль под капотом собирает кадры,
считает CRC, шлёт и разбирает ответ. Использует mks_protocol.py.

Запуск:
    python mks_console.py COM3
    python mks_console.py COM3 --baud 115200

Команды (набирать в приглашении > ):
    ping                      — PING
    init                      — INIT (долгая; таймаут больше)
    status                    — GET_STATUS (разбор 7 байт)
    setphy <ch> <dr> <plen> <code> <prf> <pac>
                              — SET_PHY_CONFIG
    txpower <level>           — SET_TX_POWER (мощность TX: больше level = мощнее)
    mode3                     — то же, что setphy 2 0 1024 9 64 32 (EVK Mode 3)
    rxstart                   — RX_START (включить непрерывный приём)
    rxstop                    — RX_STOP  (выключить приём)
    txframe <b0> <b1> ...     — TX_FRAME (послать кадр, payload в hex)
    txperiodic <T> <b0> ...   — TX_PERIODIC (период T мс + payload hex)
    txstop                    — TX_STOP
    metrics [prf]             — GET_SIGNAL_METRICS: сырьё + приближ. RSSI/FP_POWER
    raw <hex...>              — послать произвольные PARAMS к произвольному CMD:
                                raw <cmd_id> <b0> <b1> ...   (всё в hex)
    hex                       — переключить показ ответа в hex вкл/выкл
    help                      — список команд
    quit / exit / q           — выход

Ctrl+C прерывает текущее ожидание/выходит.
"""

import sys
import argparse
import mks_protocol as mks


HELP = """\
Доступные команды:
  ping                                   PING -> PONG
  init                                   INIT (инициализация DW1000, долгая)
  status                                 GET_STATUS (разбор полей)
  setphy <ch> <dr> <plen> <code> <prf> <pac>
                                         SET_PHY_CONFIG (0x10)
                                         пример: setphy 2 0 1024 9 64 32
  mode3                                  = setphy 2 0 1024 9 64 32 (EVK Mode 3)
  txpower <level>                        SET_TX_POWER (0x11) — мощность TX (вариант A)
                                         level 0..223: БОЛЬШЕ level = БОЛЬШЕ мощность
                                         (0 ≈ минимум, 223 ≈ максимум; шаг ≈ 0.5 dB)
                                         пример: txpower 223 ; txpower 120
                                         (требует предварительного mode3/setphy)
  rxstart                                RX_START (0x30) — включить приём
  rxstop                                 RX_STOP  (0x31) — выключить приём
  txframe <b0> <b1> ...                  TX_FRAME (0x20) — послать кадр (payload hex)
                                         пример: txframe DE AD BE EF 01
  txperiodic <T_ms> <b0> <b1> ...        TX_PERIODIC (0x21) — периодическая посылка
                                         T_ms — период (мс, >= 5); payload в hex
                                         пример: txperiodic 100 DE AD BE EF 01
                                         останов: txstop
  txstop                                 TX_STOP  (0x22) — стоп (в т.ч. периодики)
  metrics [prf]                          GET_SIGNAL_METRICS (0x40): сырьё +
                                         приближ. RSSI/FP_POWER (dBm, UM §4.7).
                                         prf = 16 или 64 (по умолч. 64, Mode 3)
  raw <cmd_id> [b0 b1 ...]               произвольная команда, всё в hex
                                         пример: raw 00           (PING)
                                         пример: raw 10 02 00 00 04 09 40 20
  hex                                    вкл/выкл показ ответа в hex
  help                                   эта справка
  quit | exit | q                        выход
"""


def show_response(status, data, show_hex):
    name = mks.status_name(status)
    print(f"  STATUS = 0x{status:02X} ({name})")
    if show_hex or not data:
        dump = " ".join(f"{x:02X}" for x in data) if data else "(пусто)"
        print(f"  DATA   = {dump}")


def cmd_status(dev, show_hex):
    st, data = dev.get_status()
    show_response(st, data, show_hex)
    if st == 0x00:
        try:
            for k, v in mks.parse_get_status(data).items():
                print(f"    {k:16} = {v}")
        except Exception as e:
            print(f"    (разбор не удался: {e})")


def cmd_setphy(dev, args, show_hex):
    if len(args) != 6:
        print("  использование: setphy <ch> <dr> <plen> <code> <prf> <pac>")
        return
    ch, dr, plen, code, prf, pac = (int(a) for a in args)
    params = bytes([ch & 0xFF, dr & 0xFF,
                    plen & 0xFF, (plen >> 8) & 0xFF,   # preamble_length u16 LE
                    code & 0xFF, prf & 0xFF, pac & 0xFF])
    st, data = dev.command(mks.CMD_SET_PHY_CONFIG, params)
    show_response(st, data, show_hex)


def cmd_metrics(dev, args, show_hex):
    # опциональный аргумент: PRF в МГц (16/64), по умолчанию 64 (Mode 3)
    prf = 64
    if args:
        try:
            prf = int(args[0])
        except ValueError:
            print("  использование: metrics [prf]   (prf = 16 или 64, по умолч. 64)")
            return
    st, data = dev.get_signal_metrics()
    show_response(st, data, show_hex)
    if st == 0x00:
        try:
            m = mks.parse_signal_metrics(data)
            for k in mks.SIGNAL_METRICS_FIELDS:
                print(f"    {k:10} = {m[k]}")
            verdict = "ПРИЁМ ПОДТВЕРЖДЁН" if mks.signal_metrics_ok(m) \
                else "поля нулевые — содержательный приём под вопросом"
            print(f"    -> {verdict}")
            # приближённая оценка мощности (UM §4.7), N без SFD-коррекции
            try:
                p = mks.estimate_power(m, prf_mhz=prf)
                print(f"    RX_LEVEL   = {p['rx_level_dbm']:7.2f} dBm  (PRF {prf}М, приближ.)")
                print(f"    FP_POWER   = {p['fp_power_dbm']:7.2f} dBm")
                print(f"    diff       = {p['diff_db']:7.2f} dB  -> {p['channel']}")
            except Exception as e:
                print(f"    (оценка мощности не удалась: {e})")
        except Exception as e:
            print(f"    (разбор не удался: {e})")
    elif st == 0x06:  # TIMEOUT
        print("    (кадр ещё не принят — valid=0; жди пакет EVK и повтори metrics)")


def cmd_txpower(dev, args, show_hex):
    if not args:
        print("  использование: txpower <level>   (0..223; больше level = мощнее, 223 ≈ максимум)")
        print("  пример: txpower 223    (макс. мощность)")
        return
    try:
        level = int(args[0])
    except ValueError:
        print("  ошибка: level должен быть целым (0..223)")
        return
    if not (0 <= level <= 0xFF):
        print("  ошибка: level вне диапазона u8 (0..255); прошивка примет 0..223)")
        return
    st, data = dev.set_tx_power(level)
    show_response(st, data, show_hex)
    if st == 0x00:
        if len(data) == 4:
            power = int.from_bytes(data, "little")
            octet = 0xFF - level
            print(f"    применено: level={level}  power=0x{power:08X}  "
                  f"(октет 0x{octet:02X} ×4)")
        else:
            print(f"    применено: level={level}")


def cmd_txframe(dev, args, show_hex):
    if not args:
        print("  использование: txframe <b0> <b1> ...  (payload в hex)")
        print("  пример: txframe DE AD BE EF 01")
        return
    try:
        payload = bytes(int(a, 16) for a in args)
    except ValueError:
        print("  ошибка: все байты payload должны быть hex (напр. DE AD BE EF)")
        return
    st, data = dev.tx_frame(payload)
    show_response(st, data, show_hex)
    if st == 0x00:
        print(f"    кадр отправлен ({len(payload)} байт payload + авто-FCS)")


def cmd_txperiodic(dev, args, show_hex):
    if len(args) < 2:
        print("  использование: txperiodic <T_ms> <b0> <b1> ...  (период мс, payload hex)")
        print("  пример: txperiodic 100 DE AD BE EF 01")
        return
    try:
        period_ms = int(args[0])
    except ValueError:
        print("  ошибка: период (первый аргумент) должен быть целым числом мс (напр. 100)")
        return
    try:
        payload = bytes(int(a, 16) for a in args[1:])
    except ValueError:
        print("  ошибка: все байты payload должны быть hex (напр. DE AD BE EF)")
        return
    st, data = dev.tx_periodic(period_ms, payload)
    show_response(st, data, show_hex)
    if st == 0x00:
        print(f"    периодика включена: период {period_ms} мс, "
              f"{len(payload)} байт payload (+ авто-FCS). Останов: txstop")


def cmd_raw(dev, args, show_hex):
    if not args:
        print("  использование: raw <cmd_id> [b0 b1 ...]  (всё в hex)")
        return
    try:
        vals = [int(a, 16) for a in args]
    except ValueError:
        print("  ошибка: все значения должны быть hex (напр. 10 02 00)")
        return
    cmd_id, params = vals[0], bytes(vals[1:])
    st, data = dev.command(cmd_id, params)
    show_response(st, data, show_hex)


def main():
    ap = argparse.ArgumentParser(description="Интерактивная консоль МКС")
    ap.add_argument("port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--timeout", type=float, default=3.0)
    ap.add_argument("--init-timeout", type=float, default=20.0)
    args = ap.parse_args()

    print(f"Открываю {args.port} @ {args.baud} 8N1 ...")
    try:
        dev = mks.MKS(args.port, args.baud, args.timeout)
    except Exception as e:
        print(f"ОШИБКА открытия порта: {e}")
        return 2

    print("Готово. 'help' — список команд, 'quit' — выход.")
    show_hex = False

    with dev:
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            parts = line.split()
            cmd, cargs = parts[0].lower(), parts[1:]

            try:
                if cmd in ("quit", "exit", "q"):
                    break
                elif cmd == "help":
                    print(HELP)
                elif cmd == "hex":
                    show_hex = not show_hex
                    print(f"  показ hex: {'вкл' if show_hex else 'выкл'}")
                elif cmd == "ping":
                    show_response(*dev.ping(), show_hex)
                elif cmd == "init":
                    print(f"  INIT... (жду до {args.init_timeout:.0f} c)")
                    show_response(*dev.init(timeout=args.init_timeout), show_hex)
                elif cmd == "status":
                    cmd_status(dev, show_hex)
                elif cmd == "setphy":
                    cmd_setphy(dev, cargs, show_hex)
                elif cmd == "mode3":
                    cmd_setphy(dev, ["2", "0", "1024", "9", "64", "32"], show_hex)
                elif cmd == "txpower":
                    cmd_txpower(dev, cargs, show_hex)
                elif cmd == "rxstart":
                    show_response(*dev.rx_start(), show_hex)
                elif cmd == "rxstop":
                    show_response(*dev.rx_stop(), show_hex)
                elif cmd == "metrics":
                    cmd_metrics(dev, cargs, show_hex)
                elif cmd == "txframe":
                    cmd_txframe(dev, cargs, show_hex)
                elif cmd == "txperiodic":
                    cmd_txperiodic(dev, cargs, show_hex)
                elif cmd == "txstop":
                    show_response(*dev.tx_stop(), show_hex)
                elif cmd == "raw":
                    cmd_raw(dev, cargs, show_hex)
                else:
                    print(f"  неизвестная команда: {cmd} (help — список)")
            except KeyboardInterrupt:
                print("\n  (прервано)")
            except Exception as e:
                print(f"  ОШИБКА: {e}")

    print("Выход.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
