#!/usr/bin/env python3
"""
mks_tcp_bridge.py - COM <-> TCP bridge for the MKS board.

Purpose
-------
Run this on the LOCAL machine that has the board's USB COM port. It opens the
serial port and exposes it as a raw TCP server. A remote MATLAB (or any TCP
client) then talks to the board over TCP - e.g. through an SSH tunnel or VPN -
using the same byte protocol as over serial.

This lets researchers whose MATLAB runs on a remote server (reachable only via
RDP/SSH, where the local USB COM port cannot be forwarded directly) still drive
the board and receive the stream.

Bytes are passed through verbatim in BOTH directions (transparent bridge):
  serial -> TCP client   and   TCP client -> serial.
No framing/parsing here - the protocol (command frames, stream frames, CRC)
lives in the firmware and in mks_protocol.m / mks_protocol.py.

Author: Wagan (host tooling). Transparent bridge, no protocol knowledge.

Usage
-----
Local machine (with the board on COM3), listen on TCP port 5555:

    python mks_tcp_bridge.py COM3 --tcp-port 5555

Remote MATLAB then connects. If the server reaches the local machine only via
SSH, set up a tunnel FROM the server TO the local machine, or (more common)
FROM your local machine forward a port TO the server and have MATLAB connect to
localhost on the server side. Typical SSH reverse tunnel from the local machine:

    ssh -R 5555:localhost:5555 user@server

Then in MATLAB on the server:

    dev = mks_protocol('tcp', '127.0.0.1', 5555);

Notes
-----
- One TCP client at a time (the board is a single device). A new connection
  replaces the previous one.
- 115200 8N1 by default (matches the board). Override with --baud.
- Requires pyserial (pip install pyserial).
"""

import argparse
import socket
import sys
import threading

try:
    import serial  # pyserial
except ImportError:
    print("pyserial is required: pip install pyserial", file=sys.stderr)
    sys.exit(1)


def pump(src_read, dst_send, stop_evt, name):
    """Copy bytes from src to dst until stop_evt is set or an error occurs."""
    try:
        while not stop_evt.is_set():
            data = src_read()
            if data:
                dst_send(data)
    except Exception as e:  # noqa: BLE001 - bridge should not crash the process
        if not stop_evt.is_set():
            print(f"[{name}] stopped: {e}")
    finally:
        stop_evt.set()


def serve(ser, listen_host, tcp_port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((listen_host, tcp_port))
    srv.listen(1)
    print(f"Bridge ready: serial {ser.port} @ {ser.baudrate} <-> TCP {listen_host}:{tcp_port}")
    print("Waiting for a TCP client (Ctrl+C to quit)...")

    while True:
        conn, addr = srv.accept()
        print(f"Client connected: {addr}")
        conn.settimeout(0.05)
        ser.timeout = 0.05
        stop_evt = threading.Event()

        # Flush any stale bytes so a fresh client starts clean.
        try:
            ser.reset_input_buffer()
        except Exception:
            pass

        def ser_to_tcp():
            data = ser.read(4096)
            return data

        def tcp_to_ser_read():
            try:
                data = conn.recv(4096)
                if data == b"":
                    raise ConnectionError("client closed")
                return data
            except socket.timeout:
                return b""

        t1 = threading.Thread(target=pump,
                              args=(ser_to_tcp, conn.sendall, stop_evt, "serial->tcp"),
                              daemon=True)
        t2 = threading.Thread(target=pump,
                              args=(tcp_to_ser_read, ser.write, stop_evt, "tcp->serial"),
                              daemon=True)
        t1.start()
        t2.start()
        stop_evt.wait()
        try:
            conn.close()
        except Exception:
            pass
        print("Client disconnected. Waiting for a new client...")


def main():
    ap = argparse.ArgumentParser(description="COM<->TCP bridge for the MKS board")
    ap.add_argument("port", help="serial port, e.g. COM3 or /dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200, help="baud rate (default 115200)")
    ap.add_argument("--tcp-port", type=int, default=5555, help="TCP listen port (default 5555)")
    ap.add_argument("--listen", default="127.0.0.1",
                    help="TCP bind address (default 127.0.0.1; use 0.0.0.0 to accept "
                         "connections from other hosts - only behind a trusted network/VPN)")
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=0.05)
    try:
        serve(ser, args.listen, args.tcp_port)
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
