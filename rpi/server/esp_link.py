import socket
import threading
import time
import traceback


class ESP32Link:
    def __init__(self, host='192.168.1.77', port=8888,
                 status_callback=None, weld_data_callback=None,
                 fired_callback=None, socketio=None):
        self.host = host
        self.port = port
        self.sock = None
        self.running = False
        self.thread = None
        self.status_callback = status_callback
        self.weld_data_callback = weld_data_callback
        self.fired_callback = fired_callback
        self.socketio = socketio          # pass your Flask-SocketIO instance in
        self.last_status = {}
        self.last_cells = {}
        self.connected = False
        self.last_data_time = time.time()
        self._expecting_waveform = False  # flag: next bare CSV is waveform data
        self._pending_weld = {}           # holds weld stats until waveform arrives

    def start(self):
        if self.running:
            return True
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        print(f"[ESP32Link] Started TCP client thread for {self.host}:{self.port}")
        return True

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        if self.thread:
            self.thread.join(timeout=2)
        print("[ESP32Link] Stopped")

    def send_command(self, cmd: str) -> bool:
        if not self.sock:
            print(f"[ESP32Link] Cannot send '{cmd}': not connected")
            return False
        try:
            msg = (cmd.strip() + "\n").encode("utf-8")
            self.sock.sendall(msg)
            print(f"[ESP32Link] Sent: {cmd}")
            return True
        except Exception as e:
            print(f"[ESP32Link] Send error: {e}")
            return False

    def _run(self):
        buffer = ""
        while self.running:
            try:
                print(f"[ESP32Link] Connecting to {self.host}:{self.port}...")

                # ── FIX: actually create the socket ──
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(10)
                self.sock.connect((self.host, self.port))

                # TCP keep-alives
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 2)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 2)

                self.connected = True
                self.last_data_time = time.time()
                print(f"[ESP32Link] ✓ Connected to {self.host}:{self.port}")

                while self.running:
                    try:
                        chunk = self.sock.recv(4096)
                        if not chunk:
                            print("[ESP32Link] Connection closed by ESP32")
                            break

                        self.last_data_time = time.time()
                        buffer += chunk.decode("utf-8", errors="ignore")

                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if line:
                                self._handle_line(line)

                    except socket.timeout:
                        continue
                    except Exception as e:
                        print(f"[ESP32Link] Read error: {e}")
                        traceback.print_exc()
                        break

            except Exception as e:
                print(f"[ESP32Link] Connection error: {e}")
            finally:
                self.connected = False
                if self.sock:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
                if self.running:
                    print("[ESP32Link] Reconnecting in 3s...")
                    time.sleep(3)

        print("[ESP32Link] Worker thread exiting")

    def _parse_kv(self, parts):
        """Parse list of 'key=value' strings into a dict."""
        d = {}
        for p in parts:
            if '=' in p:
                k, v = p.split('=', 1)
                d[k.strip()] = v.strip()
        return d

    def _fget(self, d, key, default=0.0):
        try:
            val = d.get(key, str(default))
            if val in ("NaN", "nan", ""):
                return default
            return float(val)
        except Exception:
            return default

    def _handle_line(self, line: str):
        print(f"[ESP32Link] RX: {line}")

        # ── Ignore noise ──
        if line.startswith("DBG,") or line.startswith("HB:") or line.startswith("WELCOME"):
            self._expecting_waveform = False
            return

        # ── STATUS: live telemetry ──
        if line.startswith("STATUS,"):
            parts = line.split(',')
            self.last_status = self._parse_kv(parts[1:])
            self._expecting_waveform = False
            self._emit_status_update()

        # ── STATUS2: cell voltages (new protocol) ──
        elif line.startswith("STATUS2,"):
            parts = line.split(',')
            self.last_cells = self._parse_kv(parts[1:])
            self._expecting_waveform = False
            self._emit_status_update()

        # ── EVENT,WELD_START ──
        elif line.startswith("EVENT,WELD_START"):
            self._expecting_waveform = False
            self._pending_weld = {}
            if self.socketio:
                self.socketio.emit('weld_start', {})

        # ── EVENT,WELD_DONE ──
        elif line.startswith("EVENT,WELD_DONE"):
            parts = line.split(',')
            kv = self._parse_kv(parts[2:])  # skip EVENT and WELD_DONE

            vcap_b = self._fget(kv, 'vcap_b')
            vcap_a = self._fget(kv, 'vcap_a')
            avg_a  = self._fget(kv, 'avg_a')
            total_ms = self._fget(kv, 'total_ms')

            # Energy calculation: V_avg * I_avg * t
            energy = ((vcap_b + vcap_a) / 2.0) * avg_a * (total_ms / 1000.0)

            self._pending_weld = {
                'peak_a':       self._fget(kv, 'peak_a'),
                'avg_a':        avg_a,
                'total_ms':     total_ms,
                'delta_v':      self._fget(kv, 'delta_v'),
                'vcap_before':  vcap_b,
                'vcap_after':   vcap_a,
                'energy_joules': round(energy, 2),
            }

            # Emit weld_complete immediately with stats
            if self.socketio:
                self.socketio.emit('weld_complete', self._pending_weld)
                print(f"[ESP32Link] Emitted weld_complete: {self._pending_weld}")

            # Also call legacy callback
            if self.weld_data_callback:
                self.weld_data_callback(line)

            # Next bare CSV line is the waveform
            self._expecting_waveform = True

        # ── Waveform: bare CSV line after WELD_DONE ──
        elif self._expecting_waveform and ',' in line and line[0].isdigit():
            self._expecting_waveform = False
            self._parse_and_emit_waveform(line)

        # ── Legacy: CELLS ──
        elif line.startswith("CELLS,"):
            parts = line.split(',')
            self.last_cells = self._parse_kv(parts[1:])
            self._emit_status_update()

        # ── ACK / OK ──
        elif line.startswith("OK") or line.startswith("ACK:"):
            if self.socketio:
                self.socketio.emit('command_ack', {'response': line})

        # ── Unknown ──
        else:
            if self.weld_data_callback:
                try:
                    self.weld_data_callback(line)
                except Exception as e:
                    print(f"[ESP32Link] weld_data_callback error: {e}")

    def _parse_and_emit_waveform(self, line: str):
        """Parse bare CSV waveform line and emit waveform_data to frontend."""
        try:
            values = [float(x) for x in line.split(',')]
            # Format: t, v, i, v, i, v, i, ...  (time_us, vcap, amps pairs)
            samples = []
            if len(values) >= 3:
                # First value is timestamp of first sample in µs
                t_us = values[0]
                i = 1
                sample_idx = 0
                while i + 1 < len(values):
                    samples.append({
                        'time_ms': round(t_us / 1000.0 + sample_idx * 0.2, 3),
                        'voltage': round(values[i], 4),
                        'current': round(values[i + 1], 2),
                    })
                    i += 2
                    sample_idx += 1

            if samples and self.socketio:
                payload = {
                    'count': len(samples),
                    'samples': samples,
                    'weld_stats': self._pending_weld,
                }
                self.socketio.emit('waveform_data', payload)
                print(f"[ESP32Link] Emitted waveform_data: {len(samples)} samples")

        except Exception as e:
            print(f"[ESP32Link] Waveform parse error: {e}")
            traceback.print_exc()

    def _emit_status_update(self):
        if not self.status_callback:
            return
        if not self.last_status:
            return

        try:
            payload = {
                # STATUS fields (new protocol)
                "vpack":        self._fget(self.last_status, "vpack"),
                "vcap":         self._fget(self.last_status, "vcap"),
                "state":        self.last_status.get("state", "OFF"),
                "enabled":      self.last_status.get("enabled", "0"),
                "cooldown_ms":  int(self._fget(self.last_status, "cooldown_ms", 0)),
                "pulse_ms":     int(self._fget(self.last_status, "pulse_ms", 10)),
                "weld_count":   int(self._fget(self.last_status, "weld_count", 0)),
                "temp":         self._fget(self.last_status, "temp") + 5.0,

                # STATUS2 cell fields
                "cell1":        self._fget(self.last_cells, "cell1"),
                "cell2":        self._fget(self.last_cells, "cell2"),
                "cell3":        self._fget(self.last_cells, "cell3"),
                "ichg":         self._fget(self.last_cells, "ichg"),
                "ina_ok":       self.last_cells.get("ina_ok", "0"),

                "esp_connected": self.connected,
            }

            self.status_callback(payload)

        except Exception as e:
            print(f"[ESP32Link] _emit_status_update error: {e}")
            traceback.print_exc()