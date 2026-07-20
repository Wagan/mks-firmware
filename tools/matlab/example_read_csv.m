% EXAMPLE_READ_CSV  Offline analysis of CSV recorded by the GUI (mks_gui.py).
%
%   The GUI writes:
%     <prefix>_YYYYMMDD_HHMMSS_metrics.csv  - one row per received frame
%     <prefix>_YYYYMMDD_HHMMSS_cir.csv      - one row per CIR sample (full mode)
%   The two are linked by frame_id. This script loads both and builds a CIR
%   waterfall from the recorded samples (no board needed).
%
%   Set METRICS_CSV / CIR_CSV to your files. CIR_CSV is optional (light mode
%   records metrics only).
%
%   Author: Wagan (CSV format / streaming). Compatible with any MATLAB with readtable.

clear; clc;

METRICS_CSV = "mks_rec_20260720_120000_metrics.csv";
CIR_CSV     = "mks_rec_20260720_120000_cir.csv";   % set to "" if none

% ---------- metrics ----------
M = readtable(METRICS_CSV);
fprintf('metrics: %d rows, columns: %s\n', height(M), strjoin(M.Properties.VariableNames, ', '));

% Example plots from metrics (empty cells load as NaN).
figure('Name','Recorded metrics');
subplot(3,1,1); plot(M.frame_id, M.SNR_dB, '.-');    grid on; ylabel('SNR, dB');
subplot(3,1,2); plot(M.frame_id, M.RSSI_dBm, '.-');  grid on; ylabel('RSSI, dBm');
subplot(3,1,3); plot(M.frame_id, M.FP_INDEX, '.-');  grid on; ylabel('FP\_INDEX'); xlabel('frame\_id');

% ---------- CIR waterfall (optional) ----------
if strlength(CIR_CSV) > 0 && isfile(CIR_CSV)
    C = readtable(CIR_CSV);
    fprintf('cir: %d rows\n', height(C));

    frames = unique(C.frame_id, 'stable');
    % Common absolute sample axis across all frames (FP drifts -> windows shift).
    smin = min(C.sample_index); smax = max(C.sample_index);
    xaxis = smin:smax;
    W = nan(numel(frames), numel(xaxis));   % rows = frames, cols = sample index

    for i = 1:numel(frames)
        rows = C(C.frame_id == frames(i), :);
        cols = rows.sample_index - smin + 1;    % map absolute index -> column
        W(i, cols) = rows.amplitude;
    end

    % Newest on top (flip so row 1 of the image is the last recorded frame).
    figure('Name','CIR waterfall (from CSV)');
    imagesc(xaxis, 1:numel(frames), flipud(W));
    set(gca, 'YDir', 'normal');   % with flipud, top row = newest
    colormap(turbo); colorbar;
    xlabel('sample index'); ylabel('frame (newest on top)');
    title('CIR waterfall (recorded)');
else
    fprintf('No CIR CSV given - skipping waterfall.\n');
end

fprintf('Done.\n');
