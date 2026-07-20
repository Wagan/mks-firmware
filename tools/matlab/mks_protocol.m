classdef mks_protocol < handle
% MKS_PROTOCOL  Host-side driver for the MKS board (STM32F411 + DWM1000).
%
%   MATLAB port of tools/mks_protocol.py + tools/mks_stream.py, kept
%   byte-for-byte compatible with the firmware (see docs/PROTOCOL_SPEC.md, v1.5).
%
%   Command frame : SYNC(0xAA 0x55) | LEN | CMD_ID | PARAMS | CRC
%   Response frame: SYNC(0xAA 0x55) | LEN | STATUS | DATA   | CRC
%   Stream frame  : SMARK(0xDE 0xCA)| LEN16 | SEQ | DROPPED | CONTENT | PAYLOAD | CRC
%   CRC8: poly=0x07, init=0x00, no reflection, xorout=0x00, computed WITHOUT SYNC.
%
%   Transport is abstracted: connect over a local serial COM port, OR over TCP
%   (e.g. through an SSH tunnel to a remote MATLAB server via the Python bridge
%   tools/mks_tcp_bridge.py). The rest of the API is identical for both.
%
%   Serial backend auto-detects the API: new serialport() (R2019b+) if available,
%   otherwise falls back to the legacy serial() object. TCP uses tcpclient().
%
%   Usage:
%       dev = mks_protocol('COM3');            % local serial
%       dev = mks_protocol('tcp','127.0.0.1',5555);  % remote via bridge
%       [st, data] = dev.init();
%       dev.set_phy(2,0,1024,9,64,32);         % Mode 3
%       dev.rx_start();
%       ...
%       dev.close();
%
%   Authorship (mirrors the Python sources so responsibility stays traceable
%   when researchers edit this file):
%     Wagan  — protocol core, framing/CRC, RX, TX, GET_CIR, streaming, live-module
%              detection. Everything not marked otherwise.
%     Andrey — RSSI/FP_POWER power estimate (UM 4.7) and LOS/gray/NLOS class.
%     Sergey — SET_TX_POWER (0x11) manual TX power.
%     Dima   — default PHY preset (Mode 3, listen to two EVK kits).
%   Please keep this convention: mark your edits as  <Name>: YYYY-MM-DD - <what/why>.

    properties (Constant)
        % Wagan: 2026-07-16 - command IDs (docs/PROTOCOL_SPEC.md 5).
        CMD_PING             = uint8(hex2dec('00'))
        CMD_INIT             = uint8(hex2dec('01'))
        CMD_GET_STATUS       = uint8(hex2dec('02'))
        CMD_SET_PHY_CONFIG   = uint8(hex2dec('10'))
        CMD_SET_TX_POWER     = uint8(hex2dec('11'))   % Sergey
        CMD_TX_FRAME         = uint8(hex2dec('20'))
        CMD_TX_PERIODIC      = uint8(hex2dec('21'))
        CMD_TX_STOP          = uint8(hex2dec('22'))
        CMD_RX_START         = uint8(hex2dec('30'))
        CMD_RX_STOP          = uint8(hex2dec('31'))
        CMD_GET_SIGNAL_METRICS = uint8(hex2dec('40'))
        CMD_GET_CIR          = uint8(hex2dec('41'))
        CMD_SET_STREAM_MODE  = uint8(hex2dec('42'))

        STATUS_OK            = uint8(0)
        SMARK0 = uint8(hex2dec('DE'))   % stream frame marker byte 0
        SMARK1 = uint8(hex2dec('CA'))   % stream frame marker byte 1
    end

    properties
        transport   % 'serial' | 'tcp'
        api         % 'serialport' | 'serial' | 'tcp'  (which backend is live)
        port        % underlying object (serialport/serial/tcpclient)
        default_timeout = 2.0    % seconds for a normal command round-trip
        init_timeout    = 20.0   % INIT is slow (LDE microcode load)
    end

    methods
        % ---------- construction / transport ----------
        function obj = mks_protocol(varargin)
            % mks_protocol('COM3')                 - local serial (baud 115200)
            % mks_protocol('COM3', baud)           - local serial, custom baud
            % mks_protocol('tcp', host, tcpport)   - remote via TCP bridge
            if nargin >= 1 && strcmpi(varargin{1}, 'tcp')
                host = varargin{2};
                tcpport = varargin{3};
                obj.transport = 'tcp';
                obj.api = 'tcp';
                obj.port = tcpclient(host, tcpport, 'Timeout', obj.default_timeout);
                return;
            end
            portname = varargin{1};
            if nargin >= 2, baud = varargin{2}; else, baud = 115200; end
            obj.transport = 'serial';
            % Auto-detect serial API: prefer new serialport (R2019b+).
            if exist('serialport', 'file') == 2 || exist('serialport', 'builtin') == 5
                obj.api = 'serialport';
                obj.port = serialport(portname, baud, 'Timeout', obj.default_timeout);
                configureTerminator(obj.port, "LF"); %#ok<*NASGU> % not used, we do binary
                flush(obj.port);
            else
                % Legacy fallback (<= R2019a).
                obj.api = 'serial';
                obj.port = serial(portname, 'BaudRate', baud, 'Timeout', obj.default_timeout);
                set(obj.port, 'InputBufferSize', 65536);
                fopen(obj.port);
            end
        end

        function close(obj)
            try
                switch obj.api
                    case 'serialport', clear obj.port;      % serialport closes on delete
                    case 'serial',     fclose(obj.port); delete(obj.port);
                    case 'tcp',        clear obj.port;
                end
            catch
            end
        end

        % ---------- low-level transport (private-ish) ----------
        function port_write(obj, bytes)
            bytes = uint8(bytes);
            switch obj.api
                case 'serialport', write(obj.port, bytes, 'uint8');
                case 'serial',     fwrite(obj.port, bytes, 'uint8');
                case 'tcp',        write(obj.port, bytes, 'uint8');
            end
        end

        function b = port_read(obj, n, deadline)
            % Read exactly n bytes or error on timeout (deadline = tic-based secs).
            % Works across serialport/serial/tcpclient by polling NumBytesAvailable.
            b = uint8([]);
            while numel(b) < n
                avail = obj.bytes_available();
                if avail > 0
                    take = min(avail, n - numel(b));
                    chunk = obj.raw_read(take);
                    b = [b, uint8(chunk(:).')]; %#ok<AGROW>
                else
                    if toc(deadline) > obj.current_timeout()
                        error('mks:timeout', 'Read timeout (%d of %d bytes)', numel(b), n);
                    end
                    pause(0.001);
                end
            end
        end

        function n = bytes_available(obj)
            switch obj.api
                case 'serialport', n = obj.port.NumBytesAvailable;
                case 'serial',     n = obj.port.BytesAvailable;
                case 'tcp',        n = obj.port.NumBytesAvailable;
            end
        end

        function c = raw_read(obj, n)
            switch obj.api
                case 'serialport', c = read(obj.port, n, 'uint8');
                case 'serial',     c = fread(obj.port, n, 'uint8').';
                case 'tcp',        c = read(obj.port, n, 'uint8');
            end
        end

        function t = current_timeout(obj)
            t = obj.default_timeout;
        end

        % ---------- framing / CRC ----------
        function frame = build_command(obj, cmd_id, params)
            % Wagan: 2026-07-16 - LEN = 1(CMD_ID)+numel(params); CRC over body (no SYNC).
            if nargin < 3, params = uint8([]); end
            params = uint8(params(:).');
            len = uint8(1 + numel(params));
            body = [len, uint8(cmd_id), params];
            frame = [obj.SMARK_none_sync(), body, mks_protocol.crc8(body)];
        end

        function s = SMARK_none_sync(~)
            s = uint8([hex2dec('AA'), hex2dec('55')]);  % command SYNC
        end

        % ---------- generic command round-trip ----------
        function [status, data] = command(obj, cmd_id, params, timeout)
            % Send a command, read and validate the response. Returns STATUS + DATA.
            if nargin < 3, params = uint8([]); end
            if nargin < 4, timeout = obj.default_timeout; end
            old = obj.default_timeout; obj.default_timeout = timeout;
            cleaner = onCleanup(@() obj.restore_timeout(old));

            obj.port_write(obj.build_command(cmd_id, params));

            dl = tic;
            % Find command SYNC 0xAA 0x55 (skip any stray bytes / stream leftovers).
            obj.sync_to(uint8([hex2dec('AA'), hex2dec('55')]), dl);
            lenb = obj.port_read(1, dl);
            len  = double(lenb(1));
            rest = obj.port_read(len + 1, dl);   % STATUS+DATA (len bytes) + CRC (1)
            body = [lenb, rest(1:end-1)];
            crc_rx = rest(end);
            if mks_protocol.crc8(body) ~= crc_rx
                error('mks:crc', 'Response CRC mismatch');
            end
            status = rest(1);
            data   = rest(2:end-1);
        end

        function restore_timeout(obj, old), obj.default_timeout = old; end

        function sync_to(obj, marker, deadline)
            % Slide until the two marker bytes appear consecutively.
            m0 = marker(1); m1 = marker(2);
            prev = uint8(0); have_prev = false;
            while true
                b = obj.port_read(1, deadline);
                if have_prev && prev == m0 && b(1) == m1, return; end
                prev = b(1); have_prev = true;
            end
        end

        % ---------- commands (thin wrappers) ----------
        function [st,data] = ping(obj)
            [st,data] = obj.command(obj.CMD_PING);
        end

        function [st,data] = init(obj)
            % Wagan: 2026-07-18 - INIT reports live modules: DATA=[live_count,live_mask].
            [st,data] = obj.command(obj.CMD_INIT, uint8([]), obj.init_timeout);
        end

        function [st,data] = get_status(obj)
            [st,data] = obj.command(obj.CMD_GET_STATUS);
        end

        function [st,data] = set_phy(obj, ch, dr, plen, code, prf, pac)
            % Params: channel u8, data_rate u8, preamble_length u16 LE, code u8, PRF u8, PAC u8.
            p = [uint8(ch), uint8(dr), mks_protocol.u16le(plen), uint8(code), uint8(prf), uint8(pac)];
            [st,data] = obj.command(obj.CMD_SET_PHY_CONFIG, p);
        end

        function [st,data] = set_tx_power(obj, level)
            % Sergey: 2026-07-17 - SET_TX_POWER (0x11): bigger level = more power; DATA=power u32 LE.
            [st,data] = obj.command(obj.CMD_SET_TX_POWER, uint8(level));
        end

        function [st,data] = tx_frame(obj, payload)
            % TX_FRAME (0x20): length u16 LE, payload[length] (payload WITHOUT FCS).
            payload = uint8(payload(:).');
            p = [mks_protocol.u16le(numel(payload)), payload];
            [st,data] = obj.command(obj.CMD_TX_FRAME, p);
        end

        function [st,data] = tx_periodic(obj, period_ms, payload)
            % Wagan: 2026-07-17 - TX_PERIODIC (0x21): period_ms u16, length u16, payload.
            payload = uint8(payload(:).');
            p = [mks_protocol.u16le(period_ms), mks_protocol.u16le(numel(payload)), payload];
            [st,data] = obj.command(obj.CMD_TX_PERIODIC, p);
        end

        function [st,data] = tx_stop(obj)
            [st,data] = obj.command(obj.CMD_TX_STOP);
        end

        function [st,data] = rx_start(obj)
            [st,data] = obj.command(obj.CMD_RX_START);
        end

        function [st,data] = rx_stop(obj)
            [st,data] = obj.command(obj.CMD_RX_STOP);
        end

        function [st,data] = get_signal_metrics(obj)
            [st,data] = obj.command(obj.CMD_GET_SIGNAL_METRICS);
        end

        function [st,data] = get_cir(obj, half)
            % Wagan: 2026-07-17 - GET_CIR (0x41): half u8 (0=default 16, max 30).
            if nargin < 2, half = 0; end
            [st,data] = obj.command(obj.CMD_GET_CIR, uint8(half));
        end

        function [st,data] = set_stream_mode(obj, mode)
            % Wagan: 2026-07-17 - SET_STREAM_MODE (0x42): 0=off, 1=metrics+CIR, 2=metrics only.
            [st,data] = obj.command(obj.CMD_SET_STREAM_MODE, uint8(mode));
        end

        % ---------- streaming ----------
        function frame = read_stream_frame(obj, timeout)
            % Wagan: 2026-07-17 - read ONE stream frame (SMARK|LEN16|SEQ|DROPPED|CONTENT|PAYLOAD|CRC).
            % Returns struct with fields seq,dropped,content,metrics,cir (cir empty if content==2).
            % Errors on timeout. Re-synchronises on the SMARK 0xDE 0xCA marker.
            if nargin < 2, timeout = obj.default_timeout; end
            old = obj.default_timeout; obj.default_timeout = timeout;
            cleaner = onCleanup(@() obj.restore_timeout(old));

            dl = tic;
            obj.sync_to([obj.SMARK0, obj.SMARK1], dl);
            len16b = obj.port_read(2, dl);
            len16  = double(len16b(1)) + 256*double(len16b(2));   % LE
            rest   = obj.port_read(len16 + 1, dl);                % body + CRC
            body   = [len16b, rest(1:end-1)];
            crc_rx = rest(end);
            if mks_protocol.crc8(body) ~= crc_rx
                error('mks:stream_crc', 'Stream frame CRC mismatch');
            end
            payload_all = rest(1:end-1);   % SEQ(2)+DROPPED(2)+CONTENT(1)+PAYLOAD
            frame = mks_protocol.parse_stream_body(payload_all);
        end
    end

    methods (Static)
        % ---------- CRC / little-endian helpers ----------
        function c = crc8(data)
            % Wagan: CRC-8, poly=0x07, init=0x00, no reflection, xorout=0x00.
            data = uint8(data(:).');
            crc = uint16(0);
            for i = 1:numel(data)
                crc = bitxor(crc, uint16(data(i)));
                for k = 1:8
                    if bitand(crc, uint16(128))
                        crc = bitand(bitxor(bitshift(crc,1), uint16(7)), uint16(255));
                    else
                        crc = bitand(bitshift(crc,1), uint16(255));
                    end
                end
            end
            c = uint8(crc);
        end

        function b = u16le(v)
            v = double(v);
            b = uint8([mod(v,256), mod(floor(v/256),256)]);
        end

        function v = rd_u16le(data, off)
            % 0-based offset like the SPEC; data is a uint8 row vector.
            v = double(data(off+1)) + 256*double(data(off+2));
        end

        function v = rd_i16le(data, off)
            u = double(data(off+1)) + 256*double(data(off+2));
            if u >= 32768, v = u - 65536; else, v = u; end
        end

        function v = rd_u32le(data, off)
            v = double(data(off+1)) + 256*double(data(off+2)) + ...
                65536*double(data(off+3)) + 16777216*double(data(off+4));
        end

        % ---------- parsers ----------
        function m = parse_signal_metrics(data)
            % Wagan: 2026-07-17 - 30-byte strict metrics block (docs/PROTOCOL_SPEC.md 8).
            % First 18 bytes: raw u16 diag fields. Last 12: strict values (firmware, UM 4.7).
            % Signed fields use INT16_MIN as "n/a".
            data = uint8(data(:).');
            m = struct();
            if numel(data) < 18
                error('mks:metrics', 'metrics too short (%d bytes)', numel(data));
            end
            m.count        = mks_protocol.rd_u16le(data,0);
            m.CIR_PWR      = mks_protocol.rd_u16le(data,2);
            m.RXPACC       = mks_protocol.rd_u16le(data,4);
            m.STD_NOISE    = mks_protocol.rd_u16le(data,6);
            m.FP_AMPL1     = mks_protocol.rd_u16le(data,8);
            m.FP_AMPL2     = mks_protocol.rd_u16le(data,10);
            m.FP_AMPL3     = mks_protocol.rd_u16le(data,12);
            m.FP_INDEX_raw = mks_protocol.rd_u16le(data,14);   % raw 10.6 fixed point
            m.FP_INDEX     = floor(m.FP_INDEX_raw / 64);        % sample index
            m.MAX_NOISE    = mks_protocol.rd_u16le(data,16);
            % Strict block (28/30-byte formats). NaN where not present/na.
            NA = -32768;
            m.RXPACC_NOSAT = NaN; m.N_corrected = NaN;
            m.RSSI_dBm = NaN; m.FP_POWER_dBm = NaN; m.A_used = NaN; m.SNR_dB = NaN;
            if numel(data) >= 28
                m.RXPACC_NOSAT = mks_protocol.rd_u16le(data,18);
                m.N_corrected  = mks_protocol.rd_u16le(data,20);
                rssi = mks_protocol.rd_i16le(data,22);
                fpp  = mks_protocol.rd_i16le(data,24);
                a    = mks_protocol.rd_u16le(data,26);
                if rssi ~= NA, m.RSSI_dBm     = rssi/100; end
                if fpp  ~= NA, m.FP_POWER_dBm = fpp/100;  end
                m.A_used = a/100;
            end
            if numel(data) >= 30
                snr = mks_protocol.rd_i16le(data,28);
                if snr ~= NA, m.SNR_dB = snr/100; end
            end
            % Andrey: 2026-07-17 - LOS/gray/NLOS from diff = RSSI - FP_POWER (UM 4.7.1:
            % <6 dB LOS, >10 dB NLOS, else gray). Classification is host-side logic.
            m.channel_class = 'n/a';
            if ~isnan(m.RSSI_dBm) && ~isnan(m.FP_POWER_dBm)
                diff = m.RSSI_dBm - m.FP_POWER_dBm;
                m.diff_dB = diff;
                if diff < 6,      m.channel_class = 'LOS';
                elseif diff > 10, m.channel_class = 'NLOS';
                else,             m.channel_class = 'gray';
                end
            end
        end

        function c = parse_cir(data)
            % Wagan: 2026-07-17 - GET_CIR/stream CIR window: header 6 B + count*(I,Q) int16 LE.
            data = uint8(data(:).');
            if numel(data) < 6
                error('mks:cir', 'CIR too short (%d bytes)', numel(data));
            end
            c = struct();
            c.fp_index    = mks_protocol.rd_u16le(data,0);
            c.start_index = mks_protocol.rd_u16le(data,2);
            c.count       = mks_protocol.rd_u16le(data,4);
            need = 6 + c.count*4;
            if numel(data) < need
                error('mks:cir', 'CIR body short (count=%d needs %d, have %d)', ...
                    c.count, need, numel(data));
            end
            I = zeros(1, c.count); Q = zeros(1, c.count);
            off = 6;
            for k = 1:c.count
                I(k) = mks_protocol.rd_i16le(data, off);
                Q(k) = mks_protocol.rd_i16le(data, off+2);
                off = off + 4;
            end
            c.I = I; c.Q = Q;
            c.amp = sqrt(double(I).^2 + double(Q).^2);
            c.sample_index = c.start_index + (0:c.count-1);
        end

        function f = parse_stream_body(body)
            % Wagan: 2026-07-17 - body = SEQ(2)+DROPPED(2)+CONTENT(1)+PAYLOAD.
            % PAYLOAD = metrics(30) [+ CIR window if CONTENT==1].
            body = uint8(body(:).');
            f = struct();
            f.seq     = mks_protocol.rd_u16le(body,0);
            f.dropped = mks_protocol.rd_u16le(body,2);
            f.content = double(body(5));
            payload   = body(6:end);
            f.metrics = mks_protocol.parse_signal_metrics(payload(1:min(30,numel(payload))));
            f.cir = [];
            if f.content == 1 && numel(payload) > 30
                f.cir = mks_protocol.parse_cir(payload(31:end));
            end
        end

        function name = status_name(st)
            names = {'OK','UNKNOWN_CMD','INVALID_PARAM','RADIO_BUSY', ...
                     'RADIO_ERROR','BUFFER_OVERFLOW','TIMEOUT','INTERNAL_ERROR'};
            st = double(st);
            if st >= 0 && st < numel(names), name = names{st+1}; else, name = sprintf('0x%02X', st); end
        end
    end
end
