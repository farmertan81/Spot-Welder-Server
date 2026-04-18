import socket
import threading
import time
import traceback


class ESP32Link:
    def __init__(self, host='192.168.68.77', port=8888,
                 status_callback=None, weld_data_callback=None, fired_callback=None):
        self.host = host
        self.port = port
        self.sock = None
        self.running = False
        self.thread = None
        self.status_callback = status_callback
        self.weld_data_callback = weld_data_callback
        self.fired_callback = fired_callback  # currently unused, but kept for future
        self.last_status = {}
        self.last_cells = {}
        self.connected = False
        self.last_data_time = time.time()

    def start(self):
        if self.running:
            return True  # already running
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
        """Send a command to the ESP32 over TCP."""
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
        """Main worker thread: connect, read lines, parse, callback."""
        buffer = ""
        while self.running:
            try:
                print(f"[ESP32Link] Connecting to {self.host}:{self.port}...")
                # Enable TCP keep-alives for fast reconnect detection
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)   # Start probing after 5s idle
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 2)  # Probe every 2s
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 2)    # Give up after 2 failed probes
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
                    time.sleep(3)

        print("[ESP32Link] Worker thread exiting")

    def _handle_line(self, line: str):
        print(f"[ESP32Link] RX: {line}")
        """Parse a line from the ESP32 and trigger callbacks."""
        # Debug / heartbeat
        if line.startswith("DBG ") or line.startswith("HB:") or line.startswith("WELCOME"):
            return

        # STATUS: key=value pairs
        if line.startswith("STATUS,"):
            parts = line.split(',')
            status_data = {}
            for p in parts[1:]:
                if '=' in p:
                    k, v = p.split('=', 1)
                    status_data[k] = v
            self.last_status = status_data
            self._emit_status_update()

        # CELLS: cell voltages
        elif line.startswith("CELLS,"):
            parts = line.split(',')
            cells_data = {}
            for p in parts[1:]:
                if '=' in p:
                    k, v = p.split('=', 1)
                    cells_data[k] = v
            self.last_cells = cells_data
            self._emit_status_update()

        # Voltage-only weld data (no current yet)
        elif line.startswith("VDATA,"):
            if self.weld_data_callback:
                try:
                    self.weld_data_callback(line)
                except Exception as e:
                    print(f"[ESP32Link] weld_data_callback error (VDATA): {e}")
                    traceback.print_exc()

        # Weld data with current
        elif line.startswith("WDATA,"):
            if self.weld_data_callback:
                try:
                    self.weld_data_callback(line)
                except Exception as e:
                    print(f"[ESP32Link] weld_data_callback error (WDATA): {e}")
                    traceback.print_exc()

        # End of weld data
        elif line.startswith("WDATA_END,"):
            if self.weld_data_callback:
                try:
                    self.weld_data_callback(line)
                except Exception as e:
                    print(f"[ESP32Link] weld_data_callback error (WDATA_END): {e}")
                    traceback.print_exc()

        # FIRED: weld completed
        elif line.startswith("FIRED"):
            if self.weld_data_callback:
                try:
                    self.weld_data_callback(line)
                except Exception as e:
                    print(f"[ESP32Link] weld_data_callback error (FIRED): {e}")
                    traceback.print_exc()

        # Command ack (OK, ACK:CMD,…)
        elif line.startswith("OK") or line.startswith("ACK:"):
            return

        else:
            # General / unknown lines – treat as ESP32 logs and forward to weld_data_callback
            if self.weld_data_callback:
                try:
                    self.weld_data_callback(line)
                except Exception as e:
                    print(f"[ESP32Link] weld_data_callback error (GEN): {e}")
                    traceback.print_exc()
            else:
                print(f"[ESP32Link] Unhandled line: {line}")

    def _emit_status_update(self):
        """Combine STATUS (and CELLS if present) and call status_callback."""
        if not self.status_callback:
            return
        if not self.last_status:
            return

        try:
            def fget(d, key, default=0.0):
                try:
                    val = d.get(key, str(default))
                    if val in ("NaN", "nan", ""):
                        return default
                    return float(val)
                except Exception:
                    return default

            raw_temp = fget(self.last_status, "temp")
            cal_temp = raw_temp + 5.0  # simple calibration

            payload = {
                "vpack": fget(self.last_status, "vpack"),
                "temp": cal_temp,
                "current": fget(self.last_status, "i"),
                "state": self.last_status.get("state", "OFF"),
                "cooldown_ms": int(fget(self.last_status, "cooldown_ms", 0)),
                "pulse_ms": int(fget(self.last_status, "pulse_ms", 10)),

                # Individual cell voltages from CELLS line (will just be 0.0 until we get CELLS)
                "C1": fget(self.last_cells, "C1"),
                "C2": fget(self.last_cells, "C2"),
                "C3": fget(self.last_cells, "C3"),

                # Also expose cumulative voltages
                "V1": fget(self.last_cells, "V1"),
                "V2": fget(self.last_cells, "V2"),
                "V3": fget(self.last_cells, "V3"),

                "esp_connected": self.connected,
            }

            self.status_callback(payload)

        except Exception as e:
            print(f"[ESP32Link] _emit_status_update error: {e}")
            traceback.print_exc()