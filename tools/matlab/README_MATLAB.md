# MKS — MATLAB templates for researchers

Starter code for working with the MKS board (STM32F411 + DWM1000) from MATLAB:
send commands, receive the data stream (metrics + CIR), and analyse recorded CSV.
Kept byte-for-byte compatible with the firmware protocol — see `PROTOCOL_SPEC.md`
(source of truth is the firmware) and the Python host tools (`mks_protocol.py`,
`mks_console.py`, `mks_gui.py`).

## Files

- `mks_protocol.m` — the library (class). Framing, CRC8, all commands, stream-frame
  parser, metrics/CIR parsers. Transport is abstracted: **serial COM** or **TCP**.
  Serial backend auto-detects `serialport` (R2019b+) and falls back to legacy
  `serial()` on older MATLAB, so the same code runs on new and old versions.
- `example_commands.m` — send commands, read metrics and one CIR window.
- `example_stream.m` — **main template**: enable streaming and plot live CIR;
  build your detection/analysis on top of the parsed frames (`f.metrics`, `f.cir`).
- `example_read_csv.m` — offline: read GUI-recorded `*_metrics.csv` / `*_cir.csv`
  (linked by `frame_id`) and build a CIR waterfall. No board needed.
- `mks_tcp_bridge.py` — Python COM↔TCP bridge for driving the board from a
  **remote** MATLAB (RDP/SSH server) that cannot access the local USB COM port.

## Quick start (local, board on COM3)

Put the `.m` files on the MATLAB path, then:

```matlab
dev = mks_protocol('COM3');
dev.init();
dev.set_phy(2,0,1024,9,64,32);   % Mode 3
dev.rx_start();
dev.set_stream_mode(1);          % stream metrics + CIR
% ... read frames with dev.read_stream_frame(...) — see example_stream.m
dev.set_stream_mode(0);
dev.close();
```

Or just run `example_commands.m` / `example_stream.m` after setting `PORT`.

## Remote MATLAB (board on a local PC, MATLAB on a server)

If MATLAB runs on a remote server that can't see your local USB COM port, use the
bridge. On the **local** machine (with the board):

```
python mks_tcp_bridge.py COM3 --tcp-port 5555
```

Forward the port to the server (SSH reverse tunnel from the local machine):

```
ssh -R 5555:localhost:5555 user@server
```

Then in MATLAB **on the server**:

```matlab
dev = mks_protocol('tcp', '127.0.0.1', 5555);
% ... identical API from here on
```

The bridge is transparent (passes bytes both ways); all protocol logic stays in
the firmware and in `mks_protocol.m`.

### WireGuard VPN — the verified production setup (2026-07-20)

The end-to-end setup actually used and validated: a **WireGuard VPN** links the
local Windows PC (with the board) and the remote **Astra Linux** server running
MATLAB, and the bridge listens on the VPN interface — no SSH tunnel needed.

- Local Windows PC: board on **COM3**, WireGuard address **10.8.0.6**.
- Remote server (Astra Linux): MATLAB 2025, WireGuard address **10.8.0.2**.

On the **local** machine, bind the bridge to the VPN address so the server can
reach it (bind to the WireGuard IP, not `0.0.0.0`, so only VPN peers connect):

```
python mks_tcp_bridge.py COM3 --tcp-port 5555 --listen 10.8.0.6
```

Then in MATLAB **on the server**, connect straight to the local machine's VPN IP:

```matlab
dev = mks_protocol('tcp', '10.8.0.6', 5555);
% ... identical API from here on
```

Verified over this path: `ping`, `init` (module detection, live_count/mask),
`set_phy` Mode 3, metrics (RSSI/FP_POWER/SNR/FP_INDEX + LOS class), single CIR,
and live streaming via `example_stream.m` — parsed byte-for-byte, same as local.

> Security: the bridge has no authentication, so only expose it inside a trusted
> network. WireGuard already restricts access to VPN peers; binding to the VPN IP
> (`--listen 10.8.0.6`) keeps it off any public interface. Do not bind `0.0.0.0`
> on an untrusted network.

## Notes

- One COM port / one board = one client at a time. Close the Python console/GUI
  before using MATLAB on the same local port, and vice versa.
- `set_stream_mode(0)` on exit is important — otherwise the board keeps streaming
  and the next tool sees a flooded port. The examples do this in cleanup. INIT also
  resets the stream as a safety net.
- Metrics (RSSI/FP_POWER/SNR) are computed **in the firmware** (UM §4.7); MATLAB
  reads them as ready values. The LOS/gray/NLOS class (`diff = RSSI − FP_POWER`) is
  host-side.

## Authorship / editing convention

These templates mirror the responsibility split from the Python sources. When you
edit, mark changes as `<Name>: YYYY-MM-DD — <what/why>` so authorship stays clear:

- **Wagan** — protocol core, framing/CRC, RX, TX, CIR, streaming, module detection.
- **Andrey** — power estimate (RSSI/FP_POWER) and LOS/gray/NLOS classification.
- **Sergey** — SET_TX_POWER (0x11).
- **Dima** — default PHY preset (Mode 3), tcp_bridge and port forwarding.
