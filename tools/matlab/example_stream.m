% EXAMPLE_STREAM  Receive the MKS data stream and plot live CIR + metrics.
%
%   The board, after SET_STREAM_MODE(1), sends one stream frame per received
%   UWB frame WITHOUT being polled (metrics + CIR window). This example enables
%   the stream, reads frames in a loop, parses them, and updates a live plot.
%
%   This is the core template for MATLAB researchers: take it and build your
%   own detection / analysis on top of the parsed frames (f.metrics, f.cir).
%
%   Compatible with local serial and remote TCP (bridge). See CONNECT below.
%   Requires mks_protocol.m on the MATLAB path.
%
%   Author: Wagan (streaming/CIR). LOS/gray/NLOS: Andrey. Default Mode 3: Dima.

clear; clc;

% ---------- CONNECT ----------
PORT = "COM3";
dev = mks_protocol(PORT);
% dev = mks_protocol('tcp', '127.0.0.1', 5555);   % remote via bridge
cleanup = onCleanup(@() cleanup_stream(dev));

% ---------- BRING-UP ----------
fprintf('INIT ...\n');  [st,~] = dev.init();        assert(st==0, 'INIT failed');
% Dima: 2026-07-20 - Mode 3 by default (listen to two EVK kits).
[st,~] = dev.set_phy(2,0,1024,9,64,32);             assert(st==0, 'SET_PHY failed');
[st,~] = dev.rx_start();                            assert(st==0, 'RX_START failed');

% Optional: drive the on-board transmitter M1 for a self-contained loopback.
% Wagan: 2026-07-17 - uncomment to make the board transmit to itself (M1 -> M2).
% dev.tx_periodic(20, uint8([hex2dec('DE') hex2dec('AD') hex2dec('BE') hex2dec('EF') 1]));

% Wagan: 2026-07-17 - enable stream, content=1 (metrics + CIR).
[st,~] = dev.set_stream_mode(1);                    assert(st==0, 'SET_STREAM_MODE failed');
fprintf('Stream ON. Close the figure window (or Ctrl+C) to stop.\n');

% ---------- LIVE PLOT SETUP ----------
fig = figure('Name','MKS live stream');
ax = axes(fig); grid(ax,'on');
hLine = plot(ax, nan, nan, '-o'); hold(ax,'on');
hFP = xline(ax, nan, 'r--', 'FP');
xlabel(ax,'sample index'); ylabel(ax,'|CIR|');
title(ax,'Live CIR (stream)');

% ---------- READ LOOP ----------
last_seq = -1;
n_recv = 0; n_lost = 0;
t0 = tic; last_report = tic;
redraw_period = 0.05;   % s (~20 fps for the eye; the stream itself is faster)
last_redraw = tic;

while ishandle(fig)
    try
        f = dev.read_stream_frame(1.0);   % one stream frame, 1 s timeout
    catch err
        if strcmp(err.identifier, 'mks:timeout')
            continue;   % no frame right now (no signal); keep waiting
        else
            rethrow(err);
        end
    end
    n_recv = n_recv + 1;

    % Track host-side losses via SEQ gaps (firmware drops are in f.dropped).
    if last_seq >= 0
        gap = mod(f.seq - last_seq - 1, 65536);
        n_lost = n_lost + gap;
    end
    last_seq = f.seq;

    % --- your analysis goes here: f.metrics, f.cir ---
    % Example: update the live CIR plot (thinned for the eye).
    if ~isempty(f.cir) && toc(last_redraw) > redraw_period
        set(hLine, 'XData', f.cir.sample_index, 'YData', f.cir.amp);
        set(hFP, 'Value', f.cir.fp_index);
        drawnow limitrate;
        last_redraw = tic;
    end

    % Periodic console report.
    if toc(last_report) > 1.0
        fps = n_recv / toc(t0);
        m = f.metrics;
        fprintf('fps=%.1f  recv=%d  fw_dropped=%d  host_lost=%d  SNR=%.2f RSSI=%.2f class=%s\n', ...
            fps, n_recv, f.dropped, n_lost, m.SNR_dB, m.RSSI_dBm, m.channel_class);
        last_report = tic;
    end
end

fprintf('Stopped. Total recv=%d, fw_dropped=%d, host_lost=%d.\n', n_recv, f.dropped, n_lost);

% ---------- cleanup ----------
function cleanup_stream(dev)
    try, dev.set_stream_mode(0); catch, end   % turn stream OFF (important!)
    try, dev.tx_stop();          catch, end
    try, dev.rx_stop();          catch, end
    dev.close();
end
