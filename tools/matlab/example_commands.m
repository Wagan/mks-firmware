% EXAMPLE_COMMANDS  Send commands to the MKS board and read metrics / CIR.
%
%   Minimal walkthrough of the command interface. Shows how to open the port,
%   initialise the radios, set the PHY, start reception and query metrics/CIR.
%
%   Compatible with local serial (COM port) and remote TCP (via the Python
%   bridge tools/mks_tcp_bridge.py, e.g. over an SSH tunnel). See CONNECT below.
%
%   Requires mks_protocol.m on the MATLAB path.
%
%   Author: Wagan (protocol/RX/CIR). Power/LOS output: Andrey. Default Mode 3: Dima.

clear; clc;

% ---------- CONNECT ----------
% Local serial:
PORT = "COM3";
dev = mks_protocol(PORT);
% Remote via TCP bridge (uncomment; run mks_tcp_bridge.py locally + SSH tunnel):
% dev = mks_protocol('tcp', '127.0.0.1', 5555);

cleanup = onCleanup(@() dev.close());   % always close the port on exit/error

% ---------- PING ----------
[st, ~] = dev.ping();
fprintf('PING     -> %s\n', mks_protocol.status_name(st));

% ---------- INIT (slow: up to 20 s) ----------
fprintf('INIT ... (up to 20 s)\n');
[st, data] = dev.init();
fprintf('INIT     -> %s', mks_protocol.status_name(st));
if st == 0 && numel(data) >= 2
    fprintf('  (live_count=%d, live_mask=0x%02X)', data(1), data(2));
end
fprintf('\n');
if st ~= 0
    error('INIT failed - check power, modules, antennas.');
end

% ---------- SET PHY ----------
% Dima: 2026-07-20 - default preset Mode 3 (ch2, 110k, PRF64, code9) to listen to EVK kits.
[st, ~] = dev.set_phy(2, 0, 1024, 9, 64, 32);   % Mode 3
fprintf('SET_PHY  -> %s (Mode 3)\n', mks_protocol.status_name(st));

% ---------- START RX ----------
[st, ~] = dev.rx_start();
fprintf('RX_START -> %s\n', mks_protocol.status_name(st));

% ---------- Wait for a frame, then read metrics ----------
% Feed a signal now (an EVK kit in Mode 3, or your own transmitter).
fprintf('Waiting for a frame, then reading metrics...\n');
got = false;
for attempt = 1:50
    [st, data] = dev.get_signal_metrics();
    if st == 0
        m = mks_protocol.parse_signal_metrics(data);
        fprintf('METRICS  -> OK\n');
        fprintf('   count=%d  RXPACC=%d  FP_INDEX=%d (raw %d)\n', ...
            m.count, m.RXPACC, m.FP_INDEX, m.FP_INDEX_raw);
        if ~isnan(m.RSSI_dBm)
            % Andrey: 2026-07-17 - power + LOS/gray/NLOS class.
            fprintf('   RSSI=%.2f dBm  FP_POWER=%.2f dBm  SNR=%.2f dB  class=%s\n', ...
                m.RSSI_dBm, m.FP_POWER_dBm, m.SNR_dB, m.channel_class);
        end
        got = true;
        break;
    elseif st == 6   % TIMEOUT: no frame yet
        pause(0.2);
    else
        fprintf('METRICS  -> %s\n', mks_protocol.status_name(st));
        break;
    end
end
if ~got
    fprintf('No frame received - check the source is on the same PHY mode.\n');
end

% ---------- Read a CIR window ----------
[st, data] = dev.get_cir(16);   % half-width 16 -> ~33 samples around first path
if st == 0
    c = mks_protocol.parse_cir(data);
    fprintf('CIR      -> OK  fp_index=%d  start=%d  count=%d\n', ...
        c.fp_index, c.start_index, c.count);
    figure('Name','CIR window');
    plot(c.sample_index, c.amp, '-o'); grid on;
    hold on; xline(c.fp_index, 'r--', 'FP');
    xlabel('sample index'); ylabel('|CIR| = sqrt(I^2+Q^2)');
    title('CIR window around first path');
else
    fprintf('CIR      -> %s\n', mks_protocol.status_name(st));
end

% ---------- STOP RX ----------
dev.rx_stop();
fprintf('Done.\n');
