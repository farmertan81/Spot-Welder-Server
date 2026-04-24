"""
Flask + SocketIO server for ESP32 spot welder control
Handles TCP connection, web UI, and real-time updates
"""

import eventlet
eventlet.monkey_patch()

import os
import json
import time
import socket
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

# ========== CONFIGURATION ==========
ESP32_IP = "192.168.1.77"
ESP32_PORT = 8888
SETTINGS_FILE = "settings.json"
PRESETS_FILE = "presets.json"
LOG_FILE = "welder.log"

CONNECT_TIMEOUT_S = 3.0
RECV_TIMEOUT_S = 3.0
NO_TELEM_GRACE_S = 12.0
NO_TELEM_RECONNECT_S = 120.0
RECONNECT_BASE_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 8.0

# Large waveform lines can be several KB. Use larger recv chunks and guard
# against unbounded growth if newline terminator is missing.
SOCKET_RECV_CHUNK_BYTES = 4096
MAX_RX_BUFFER_BYTES = 262144

HEARTBEAT_INTERVAL_S = 5.0  # Send PING every 5 seconds to keep connection alive

# Filter tiny INA226 charger-current noise from STATUS2 before UI emit.
# Values below this threshold are treated as 0.0A to suppress phantom flashes.
STATUS2_CHARGE_NOISE_THRESHOLD_A = 0.2

# Deadband for pack voltage display smoothing (STATUS2 INA226 telemetry).
# UI voltage is only allowed to move if absolute change exceeds this threshold.
STATUS2_VPACK_DEADBAND_V = 0.08

# If your ESP supports a "CELLS" command, keep True. Otherwise set False.
REQUEST_CELLS_ON_CONNECT = True

LEAD_RESISTANCE_DEFAULT_MOHM = 1.87
LEAD_RESISTANCE_MIN_MOHM = 0.10
LEAD_RESISTANCE_MAX_MOHM = 10.00

# Waveform display alignment constants (STM32 sample interval is 0.1ms/sample).
WAVEFORM_SAMPLE_INTERVAL_MS = 0.1
DEFAULT_PULSE_START_SAMPLE = 10

# ========== FLASK SETUP ==========
app = Flask(__name__)
app.config["SECRET_KEY"] = "spot-welder-secret-2024"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ========== GLOBAL STATE ==========
esp_link = None
esp_connected = False
last_status = {}
current_settings = {}
_esp_manager_started = False
last_weld_duration_ms = 0.0
last_weld_meta = {}


# ========== LOGGING ==========
def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {msg}"
    print(log_line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(log_line + "\n")
    except Exception as e:
        print(f"⚠️ Failed to write log: {e}")


def _normalize_lead_resistance_mohm(value) -> float:
    """Clamp/validate user-supplied lead resistance (mΩ)."""
    try:
        v = float(value)
    except Exception:
        return LEAD_RESISTANCE_DEFAULT_MOHM

    if v < LEAD_RESISTANCE_MIN_MOHM:
        return LEAD_RESISTANCE_MIN_MOHM
    if v > LEAD_RESISTANCE_MAX_MOHM:
        return LEAD_RESISTANCE_MAX_MOHM
    return round(v, 3)


def emit_status_update(patch: dict | None = None) -> None:
    """
    Emit a single merged status_update payload to the UI.

    - Merges `patch` into `last_status`
    - Always includes `esp_connected`
    """
    global last_status, esp_connected
    try:
        if patch:
            last_status.update(patch)
        payload = {**last_status, "esp_connected": esp_connected}
        socketio.emit("status_update", payload)
    except Exception as e:
        log(f"⚠️ emit_status_update failed: {e}")


# ========== SETTINGS / PRESETS ==========
def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                settings = json.load(f)

            # Backfill new keys if missing
            if "trigger_mode" not in settings:
                settings["trigger_mode"] = "pedal"
            if "contact_hold_steps" not in settings:
                settings["contact_hold_steps"] = 2
            if "lead_resistance_mohm" not in settings:
                settings["lead_resistance_mohm"] = LEAD_RESISTANCE_DEFAULT_MOHM

            settings["lead_resistance_mohm"] = _normalize_lead_resistance_mohm(
                settings.get("lead_resistance_mohm", LEAD_RESISTANCE_DEFAULT_MOHM)
            )

            return settings
        except Exception as e:
            log(f"⚠️ Failed to load settings: {e}")

    return {
        "mode": 1,
        "d1": 50,
        "gap1": 0,
        "d2": 0,
        "gap2": 0,
        "d3": 0,
        "power": 100,
        "preheat_enabled": False,
        "preheat_duration": 20,
        "preheat_power": 30,
        "preheat_gap_ms": 3,
        "active_preset": None,
        "trigger_mode": "pedal",
        "contact_hold_steps": 2,
        "lead_resistance_mohm": LEAD_RESISTANCE_DEFAULT_MOHM,
    }


def save_settings(settings: dict) -> bool:
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
        return True
    except Exception as e:
        log(f"⚠️ Failed to save settings: {e}")
        return False


def load_presets() -> dict:
    if os.path.exists(PRESETS_FILE):
        try:
            with open(PRESETS_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            log(f"⚠️ Failed to load presets: {e}")

    return {
        "P1": {
            "name": "Preset 1",
            "mode": 1,
            "d1": 50,
            "gap1": 0,
            "d2": 0,
            "gap2": 0,
            "d3": 0,
            "power": 100,
            "preheat_enabled": False,
            "preheat_duration": 20,
            "preheat_power": 30,
            "preheat_gap_ms": 3
        },
        "P2": {
            "name": "Preset 2",
            "mode": 1,
            "d1": 80,
            "gap1": 0,
            "d2": 0,
            "gap2": 0,
            "d3": 0,
            "power": 100,
            "preheat_enabled": False,
            "preheat_duration": 20,
            "preheat_power": 30,
            "preheat_gap_ms": 3
        },
        "P3": {
            "name": "Preset 3",
            "mode": 2,
            "d1": 50,
            "gap1": 10,
            "d2": 50,
            "gap2": 0,
            "d3": 0,
            "power": 100,
            "preheat_enabled": False,
            "preheat_duration": 20,
            "preheat_power": 30,
            "preheat_gap_ms": 3
        },
        "P4": {
            "name": "Preset 4",
            "mode": 1,
            "d1": 100,
            "gap1": 0,
            "d2": 0,
            "gap2": 0,
            "d3": 0,
            "power": 100,
            "preheat_enabled": False,
            "preheat_duration": 20,
            "preheat_power": 30,
            "preheat_gap_ms": 3
        },
        "P5": {
            "name": "Preset 5",
            "mode": 3,
            "d1": 40,
            "gap1": 10,
            "d2": 40,
            "gap2": 10,
            "d3": 40,
            "power": 100,
            "preheat_enabled": False,
            "preheat_duration": 20,
            "preheat_power": 30,
            "preheat_gap_ms": 3
        },
    }


def save_presets(presets: dict) -> bool:
    try:
        with open(PRESETS_FILE, "w") as f:
            json.dump(presets, f, indent=2)
        return True
    except Exception as e:
        log(f"⚠️ Failed to save presets: {e}")
        return False



def push_settings_to_esp(log_prefix: str = "") -> None:
    """
    Push current_settings to ESP so ESP matches UI after reboot/reconnect.

    IMPORTANT:
    - Do NOT request STATUS after each SET_*; ESP is already pushing STATUS/CELLS periodically.
    """
    global esp_link, current_settings

    if not (esp_link and esp_link.connected):
        return

    s = current_settings or load_settings()

    mode = s.get("mode", 1)
    d1 = s.get("d1", 50)
    gap1 = s.get("gap1", 0)
    d2 = s.get("d2", 0)
    gap2 = s.get("gap2", 0)
    d3 = s.get("d3", 0)
    power = s.get("power", 100)

    pre_en = 1 if s.get("preheat_enabled", False) else 0
    pre_ms = s.get("preheat_duration", 20)
    pre_pct = s.get("preheat_power", 30)
    pre_gap = s.get("preheat_gap_ms", 3)

    trigger_mode = str(s.get("trigger_mode", "pedal")).strip().lower()
    lead_r_mohm = _normalize_lead_resistance_mohm(
        s.get("lead_resistance_mohm", LEAD_RESISTANCE_DEFAULT_MOHM)
    )

    try:
        contact_hold_steps = int(s.get("contact_hold_steps", 2))
    except Exception:
        contact_hold_steps = 2

    if contact_hold_steps < 1:
        contact_hold_steps = 1
    if contact_hold_steps > 10:
        contact_hold_steps = 10

    # ESP protocol:
    # 1 = pedal
    # 2 = contact
    trigger_mode_num = 2 if trigger_mode == "contact" else 1

    cmd_pulse   = f"SET_PULSE,{mode},{d1},{gap1},{d2},{gap2},{d3}"
    cmd_power   = f"SET_POWER,{power}"
    cmd_pre     = f"SET_PREHEAT,{pre_en},{pre_ms},{pre_pct},{pre_gap}"
    cmd_trigger = f"SET_TRIGGER_MODE,{trigger_mode_num}"
    cmd_contact = f"SET_CONTACT_HOLD,{contact_hold_steps}"
    cmd_lead_r = f"SET_LEAD_R,{lead_r_mohm:.3f}"

    log(f"{log_prefix}➡️ Syncing settings to ESP: {cmd_pulse}")
    esp_link.send_command(cmd_pulse)

    log(f"{log_prefix}➡️ Syncing power to ESP: {cmd_power}")
    esp_link.send_command(cmd_power)

    log(f"{log_prefix}➡️ Syncing preheat to ESP: {cmd_pre}")
    esp_link.send_command(cmd_pre)

    log(f"{log_prefix}➡️ Syncing trigger mode to ESP: {cmd_trigger}")
    esp_link.send_command(cmd_trigger)

    log(f"{log_prefix}➡️ Syncing contact hold to ESP: {cmd_contact}")
    esp_link.send_command(cmd_contact)

    log(f"{log_prefix}➡️ Syncing lead resistance to ESP: {cmd_lead_r}")
    esp_link.send_command(cmd_lead_r)


# ========== ESP32 TCP LINK ==========
class ESP32Link:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.connected = False
        self.running = True

        self.connected_at = 0.0
        self.last_rx = 0.0

        # Last pack voltage value that was allowed through deadband filtering.
        self.last_vpack_emitted: float | None = None

        self._hb_greenlet = None
        self._rx_greenlet = None

        # Chunked waveform assembly state (WAVEFORM_START/DATA/END).
        self._reset_chunked_waveform_state()

    def connect(self) -> bool:
        try:
            self.running = True

            self.sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT_S)
            self.sock.settimeout(RECV_TIMEOUT_S)

            # Keepalive (best-effort)
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                if hasattr(socket, "TCP_KEEPIDLE"):
                    self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
                if hasattr(socket, "TCP_KEEPINTVL"):
                    self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 2)
                if hasattr(socket, "TCP_KEEPCNT"):
                    self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 2)
            except Exception as e:
                log(f"⚠️ Keepalive not applied (non-fatal): {e}")

            self.connected = True
            self.connected_at = time.time()
            self.last_rx = time.time()

            # Reset deadband state on fresh connection so first STATUS2 sample emits.
            self.last_vpack_emitted = None

            log(f"✅ Connected to ESP32 at {self.host}:{self.port}")

            # Start RX loop
            self._rx_greenlet = eventlet.spawn_n(self._receive_loop)

            # Heartbeat loop optional (disabled)
            # self._hb_greenlet = eventlet.spawn_n(self._heartbeat_loop)

            # One-time snapshot request
            self.send_command("STATUS", log_send=False)
            if REQUEST_CELLS_ON_CONNECT:
                self.send_command("CELLS", log_send=False)

            return True

        except Exception as e:
            log(f"❌ Failed to connect to ESP32: {e}")
            self.connected = False
            self._close_sock()
            return False

    def _close_sock(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def disconnect(self) -> None:
        was_connected = self.connected

        self.running = False
        self.connected = False

        # Kill greenlets so we never have two loops alive
        try:
            if self._rx_greenlet is not None:
                eventlet.kill(self._rx_greenlet)
        except Exception:
            pass
        try:
            if self._hb_greenlet is not None:
                eventlet.kill(self._hb_greenlet)
        except Exception:
            pass

        self._rx_greenlet = None
        self._hb_greenlet = None

        self._close_sock()

        if was_connected:
            log("🔌 Disconnected from ESP32")

    def send_command(self, cmd: str, log_send: bool = True) -> bool:
        if not self.connected or not self.sock:
            if log_send:
                log(f"⚠️ Cannot send command (not connected): {cmd}")
            return False

        try:
            if not cmd.endswith("\n"):
                cmd += "\n"
            self.sock.sendall(cmd.encode("utf-8"))
            if log_send:
                log(f"📤 Sent: {cmd.strip()}")
            return True
        except Exception as e:
            if log_send:
                log(f"❌ Failed to send command: {e}")
            self.disconnect()
            return False

    def _heartbeat_loop(self) -> None:
        while self.running and self.connected:
            eventlet.sleep(HEARTBEAT_INTERVAL_S)
            if self.connected:
                self.send_command("PING", log_send=False)

    def _receive_loop(self) -> None:
        global esp_connected

        buffer = ""

        while self.running and self.connected:
            try:
                data = self.sock.recv(SOCKET_RECV_CHUNK_BYTES)  # type: ignore[union-attr]
                if not data:
                    log("⚠️ ESP32 connection closed (recv returned empty)")
                    break

                self.last_rx = time.time()
                buffer += data.decode("utf-8", errors="ignore")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._handle_line(line)

            except socket.timeout:
                now = time.time()

                if (now - self.connected_at) < NO_TELEM_GRACE_S:
                    continue

                if (now - self.last_rx) > NO_TELEM_RECONNECT_S:
                    log(f"⚠️ No telemetry for {NO_TELEM_RECONNECT_S:.0f}s; forcing reconnect")
                    break

            except Exception as e:
                log(f"❌ Receive error: {e}")
                break

        log("🧹 Receive loop exiting, cleaning up connection...")
        self.disconnect()
        esp_connected = False
        emit_status_update({"esp_connected": False})

    def _handle_line(self, line: str) -> None:
        global esp_connected

        # Drop noisy keepalives / spammy heartbeats
        if line in ("TICK_1S", "PING", "PONG"):
            return

        log(f"📥 Received: {line}")

        if not esp_connected:
            esp_connected = True
            emit_status_update({"esp_connected": True})

        if line.startswith("STATUS,"):
            self._parse_status(line[7:])
    def _receive_loop(self) -> None:
        global esp_connected

        buffer = ""

        while self.running and self.connected:
            try:
                data = self.sock.recv(SOCKET_RECV_CHUNK_BYTES)  # type: ignore[union-attr]
                if not data:
                    log("⚠️ ESP32 connection closed (recv returned empty)")
                    break

                self.last_rx = time.time()
                buffer += data.decode("utf-8", errors="ignore")

                if len(buffer) > MAX_RX_BUFFER_BYTES:
                    log(
                        "⚠️ RX buffer exceeded limit without newline; "
                        f"trimming to last {MAX_RX_BUFFER_BYTES} bytes"
                    )
                    buffer = buffer[-MAX_RX_BUFFER_BYTES:]

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._handle_line(line)

            except socket.timeout:
                now = time.time()

                if (now - self.connected_at) < NO_TELEM_GRACE_S:
                    continue

                if (now - self.last_rx) > NO_TELEM_RECONNECT_S:
                    log(f"⚠️ No telemetry for {NO_TELEM_RECONNECT_S:.0f}s; forcing reconnect")
                    break

            except Exception as e:
                log(f"❌ Receive error: {e}")
                break

        log("🧹 Receive loop exiting, cleaning up connection...")
        self.disconnect()
        esp_connected = False
        emit_status_update({"esp_connected": False})

    def _handle_line(self, line: str) -> None:
        global esp_connected

        # Drop noisy keepalives / spammy heartbeats
        if line in ("TICK_1S", "PING", "PONG"):
            return

        log(f"📥 Received: {line}")

        if not esp_connected:
            esp_connected = True
            emit_status_update({"esp_connected": True})

        try:
            if line.startswith("STATUS,"):
                log("🧭 Routing packet -> _parse_status")
                self._parse_status(line[7:])
            elif line.startswith("CELLS,"):
                log("🧭 Routing packet -> _parse_cells")
                self._parse_cells(line[6:])
            elif line.startswith("STATUS2,"):
                log("🧭 Routing packet -> _parse_status2")
                self._parse_status2(line[8:])
            elif line.startswith("WAVEFORM_START,"):
                log("🧭 Routing packet -> _parse_waveform_start")
                self._parse_waveform_start(line)
            elif line.startswith("WAVEFORM_DATA,"):
                log("🧭 Routing packet -> _parse_waveform_data")
                self._parse_waveform_data(line)
            elif line == "WAVEFORM_END":
                log("🧭 Routing packet -> _finalize_chunked_waveform")
                self._finalize_chunked_waveform()
            elif line.startswith("WAVEFORM,"):
                # Backward compatibility: legacy single-line waveform payload.
                # If chunked mode is active, keep chunked parser as source of truth.
                log(f"📏 Legacy WAVEFORM line length={len(line)} chars")
                if self.chunked_waveform_active:
                    log("⚠️ Ignoring legacy WAVEFORM because chunked waveform assembly is active")
                    return
                log("🧭 Routing packet -> _parse_waveform (legacy)")
                self._parse_waveform(line)
            elif line.startswith("EVENT,"):
                log("🧭 Routing packet -> _parse_event")
                self._parse_event(line[6:])
            elif line.startswith("DENY,"):
                reason = line[5:]
                log(f"🧭 Routing packet -> DENY handler ({reason})")
                socketio.emit("deny_event", {"reason": reason})
            elif line.startswith("BOOT,"):
                msg = line[5:]
                log(f"🧭 Routing packet -> BOOT handler ({msg})")
                socketio.emit("boot_event", {"message": msg})
            elif line.startswith("CHARGER:") or line.startswith("CHARGER,"):
                payload = line.split(",", 1)[1] if "," in line else line.split(":", 1)[1]
                log("🧭 Routing packet -> _parse_charger")
                self._parse_charger(payload)
            else:
                log(f"⚠️ Unhandled packet type, forwarding raw to UI: {line}")
                socketio.emit("esp32_message", {"message": line})
        except Exception as e:
            log(f"❌ Exception while routing line '{line}': {e}")

    def _parse_status(self, data: str) -> None:
        """Parse STATUS,<k=v,...> and emit merged status_update."""
        global last_status
        try:
            status = {}
            for pair in data.split(","):
                if "=" in pair:
                    key, val = pair.split("=", 1)
                    key = key.strip()
                    val = val.strip()
                    try:
                        if "." in val:
                            status[key] = float(val)
                        else:
                            status[key] = int(val)
                    except Exception:
                        status[key] = val

            # Dialect alignment:
            # STM32 STATUS publishes vcap; UI primary voltage card uses vpack.
            # Keep STATUS2 as source of truth when available, but provide fallback.
            if "vcap" in status and "vpack" not in status and "vpack" not in last_status:
                status["vpack"] = status["vcap"]

            log(f"🧩 Parsed STATUS: {status}")
            last_status.update(status)
            emit_status_update({})
            log("📤 Emitting event: status_update (from STATUS)")

        except Exception as e:
            log(f"⚠️ Failed to parse STATUS: {e}")

    def _parse_cells(self, data: str) -> None:
        global last_status
        try:
            cells = {}
            for pair in data.split(","):
                if "=" in pair:
                    key, val = pair.split("=", 1)
                    key = key.strip()
                    val = val.strip()
                    try:
                        cells[key] = float(val)
                    except Exception:
                        pass

            last_status.update(cells)
            emit_status_update({})

        except Exception as e:
            log(f"⚠️ Failed to parse CELLS: {e}")

    def _parse_charger(self, data: str) -> None:
        global last_status
        try:
            if "current=" in data:
                val = data.split("current=")[1].split("A")[0].strip()
                current = float(val)
                last_status["current"] = current
                last_status["i"] = current
                emit_status_update({})
        except Exception as e:
            log(f"⚠️ Failed to parse CHARGER: {e}")

    def _parse_status2(self, data: str) -> None:
        """Parse STATUS2,<k=v,...> for INA226 telemetry and cell details."""
        global last_status
        try:
            parsed = {}
            for pair in data.split(","):
                if "=" in pair:
                    key, val = pair.split("=", 1)
                    key = key.strip()
                    val = val.strip()
                    try:
                        parsed[key] = float(val)
                    except Exception:
                        parsed[key] = val

            # Suppress tiny phantom charger-current noise from INA226 telemetry.
            # Keep this narrow so real charging current still shows up promptly.
            for charge_key in ("charge_a", "ichg"):
                if charge_key in parsed:
                    try:
                        charge_current = float(parsed[charge_key])
                        if abs(charge_current) < STATUS2_CHARGE_NOISE_THRESHOLD_A:
                            parsed[charge_key] = 0.0
                    except Exception:
                        # Non-numeric value; leave untouched.
                        pass

            # Deadband filter for pack-voltage display smoothing.
            # Keep STATUS2 update cadence unchanged; only clamp tiny vpack moves.
            if "vpack" in parsed:
                try:
                    new_vpack = float(parsed["vpack"])

                    if self.last_vpack_emitted is None:
                        # First sample always passes through.
                        self.last_vpack_emitted = new_vpack
                    elif abs(new_vpack - self.last_vpack_emitted) <= STATUS2_VPACK_DEADBAND_V:
                        # Within deadband: hold last emitted value to suppress twitching.
                        parsed["vpack"] = self.last_vpack_emitted
                    else:
                        # Meaningful voltage movement: allow update.
                        self.last_vpack_emitted = new_vpack
                except Exception:
                    # Non-numeric value; leave untouched.
                    pass

            # Dialect alignment aliases for older/newer frontend keys.
            # STM32 emits: cell1/cell2/cell3 (and optionally cell4 in some builds).
            # Keep both naming styles to avoid breaking any UI variant.
            cell_aliases = {
                "cell1": ("cell_1", "C1"),
                "cell2": ("cell_2", "C2"),
                "cell3": ("cell_3", "C3"),
                "cell4": ("cell_4", "C4"),
            }
            for src_key, alias_keys in cell_aliases.items():
                if src_key in parsed:
                    for alias_key in alias_keys:
                        parsed[alias_key] = parsed[src_key]

            if any(k in parsed for k in ("cell1", "cell2", "cell3", "cell4")):
                log(
                    "🧩 STATUS2 cell mapping: "
                    f"cell1={parsed.get('cell1')}→{parsed.get('cell_1')}, "
                    f"cell2={parsed.get('cell2')}→{parsed.get('cell_2')}, "
                    f"cell3={parsed.get('cell3')}→{parsed.get('cell_3')}, "
                    f"cell4={parsed.get('cell4')}→{parsed.get('cell_4')}"
                )

            # If vpack is not available for any reason, fall back to vcap if present.
            if "vpack" not in parsed and "vcap" in last_status:
                parsed["vpack"] = last_status["vcap"]

            log(f"🧩 Parsed STATUS2: {parsed}")
            last_status.update(parsed)
            emit_status_update({})
            log("📤 Emitting event: status_update (from STATUS2)")

        except Exception as e:
            log(f"⚠️ Failed to parse STATUS2: {e}")

    def _reset_chunked_waveform_state(self) -> None:
        self.chunked_waveform_expected_count = 0
        self.chunked_waveform_pulse_start = DEFAULT_PULSE_START_SAMPLE
        self.chunked_waveform_pulse_end = DEFAULT_PULSE_START_SAMPLE
        self.chunked_waveform_samples: dict[int, tuple[float, float]] = {}
        self.chunked_waveform_expected_indices: set[int] = set()
        self.chunked_waveform_received_chunks = 0
        self.chunked_waveform_active = False

    def run_waveform_parser_self_test(self) -> dict:
        """Run a local chunk-assembly self-test and log every step."""
        log("🧪 Starting waveform parser self-test")

        try:
            # Synthetic waveform: 8 samples in 2 chunks
            test_samples = [
                (100.0, 8.90),
                (200.0, 8.89),
                (300.0, 8.88),
                (400.0, 8.87),
                (500.0, 8.86),
                (600.0, 8.85),
                (700.0, 8.84),
                (800.0, 8.83),
            ]

            self._parse_waveform_start("WAVEFORM_START,8,1,8")

            chunk0_tokens = ["WAVEFORM_DATA", "0", "4"]
            for c, v in test_samples[:4]:
                chunk0_tokens.extend([f"{c}", f"{v}"])
            self._parse_waveform_data(",".join(chunk0_tokens))

            chunk1_tokens = ["WAVEFORM_DATA", "4", "4"]
            for c, v in test_samples[4:]:
                chunk1_tokens.extend([f"{c}", f"{v}"])
            self._parse_waveform_data(",".join(chunk1_tokens))

            # This will emit waveform_data through socketio and then reset parser state.
            self._finalize_chunked_waveform()

            result = {
                "status": "ok",
                "message": "Self-test executed; check logs for chunk assembly and emit trace",
            }
            log(f"✅ Waveform parser self-test complete: {result}")
            return result

        except Exception as e:
            log(f"❌ Waveform parser self-test failed: {e}")
            return {"status": "error", "message": str(e)}

    def _parse_waveform_start(self, line: str) -> None:
        log(f"🧪 _parse_waveform_start called with line: {line}")
        try:
            parts = line.split(",")
            if len(parts) < 4:
                log(f"⚠️ WAVEFORM_START malformed: {line}")
                return

            expected_count = int(parts[1])
            pulse_start = int(parts[2])
            pulse_end = int(parts[3])

            if expected_count <= 0:
                log(f"⚠️ WAVEFORM_START invalid sample count: {expected_count}")
                return

            # Recovery path: new start arrived before previous waveform finished.
            if self.chunked_waveform_active:
                prev_expected = self.chunked_waveform_expected_count
                prev_received = len(self.chunked_waveform_samples)
                prev_missing = max(0, prev_expected - prev_received)
                log(
                    "⚠️ New WAVEFORM_START arrived before previous waveform completed; "
                    f"discarding previous assembly (expected={prev_expected}, "
                    f"received={prev_received}, missing={prev_missing})"
                )

            self._reset_chunked_waveform_state()
            self.chunked_waveform_expected_count = expected_count
            self.chunked_waveform_pulse_start = max(0, min(pulse_start, expected_count - 1))
            self.chunked_waveform_pulse_end = max(self.chunked_waveform_pulse_start, min(pulse_end, expected_count))
            self.chunked_waveform_expected_indices = set(range(expected_count))
            self.chunked_waveform_active = True

            log(
                "📥 WAVEFORM_START received: "
                f"{expected_count} samples (pulse_start={self.chunked_waveform_pulse_start}, "
                f"pulse_end={self.chunked_waveform_pulse_end})"
            )

        except Exception as e:
            log(f"⚠️ Failed to parse WAVEFORM_START: {e}")

    def _parse_waveform_data(self, line: str) -> None:
        log(f"🧪 _parse_waveform_data called with line prefix: {line[:120]}")
        try:
            if not self.chunked_waveform_active:
                log("⚠️ WAVEFORM_DATA received without active WAVEFORM_START; dropping chunk")
                return

            parts = line.split(",")
            if len(parts) < 3:
                log(f"⚠️ WAVEFORM_DATA malformed: {line}")
                return

            start_idx = int(parts[1])
            count = int(parts[2])
            raw = parts[3:]

            if start_idx < 0 or count <= 0:
                log(f"⚠️ WAVEFORM_DATA invalid header: start={start_idx}, count={count}")
                return

            expected_tokens = count * 2
            if len(raw) < expected_tokens:
                log(
                    "⚠️ WAVEFORM_DATA token count short: "
                    f"start={start_idx}, count={count}, expected_tokens={expected_tokens}, got={len(raw)}"
                )

            parsed_in_chunk = 0
            overwritten = 0
            first_idx = start_idx
            last_idx = start_idx - 1

            for i in range(count):
                token_idx = i * 2
                if token_idx + 1 >= len(raw):
                    break

                sample_idx = start_idx + i
                if sample_idx >= self.chunked_waveform_expected_count:
                    log(
                        "⚠️ WAVEFORM_DATA sample index out of range: "
                        f"idx={sample_idx}, expected_count={self.chunked_waveform_expected_count}"
                    )
                    continue

                try:
                    current = float(raw[token_idx])
                    voltage = float(raw[token_idx + 1])
                except Exception as token_err:
                    log(
                        "⚠️ WAVEFORM_DATA non-numeric sample token: "
                        f"sample_idx={sample_idx}, token_pair=({raw[token_idx]}, {raw[token_idx + 1]}), err={token_err}"
                    )
                    continue

                if sample_idx in self.chunked_waveform_samples:
                    overwritten += 1

                self.chunked_waveform_samples[sample_idx] = (current, voltage)
                parsed_in_chunk += 1
                last_idx = sample_idx

            self.chunked_waveform_received_chunks += 1

            received_total = len(self.chunked_waveform_samples)
            expected_total = self.chunked_waveform_expected_count
            missing_total = max(0, expected_total - received_total)

            if parsed_in_chunk < count:
                log(
                    "⚠️ WAVEFORM_DATA sample truncation detected: "
                    f"start={start_idx}, expected={count}, parsed={parsed_in_chunk}, "
                    f"missing={count - parsed_in_chunk}"
                )

            log(
                "📥 WAVEFORM_DATA chunk "
                f"{first_idx}-{last_idx} ({parsed_in_chunk} samples, declared={count}, overwritten={overwritten})"
            )
            log(
                "📊 Assembly progress: "
                f"received={received_total}/{expected_total} samples, "
                f"chunks={self.chunked_waveform_received_chunks}, missing={missing_total}"
            )

        except Exception as e:
            log(f"⚠️ Failed to parse WAVEFORM_DATA: {e}")

    def _finalize_chunked_waveform(self) -> None:
        log("🧪 _finalize_chunked_waveform called")
        try:
            if not self.chunked_waveform_active:
                log("⚠️ WAVEFORM_END received without active WAVEFORM_START")
                return

            expected = self.chunked_waveform_expected_count
            log(f"📥 WAVEFORM_END received: expected_total_samples={expected}")

            received_indices = set(self.chunked_waveform_samples.keys())
            missing_indices = sorted(self.chunked_waveform_expected_indices - received_indices)

            if missing_indices:
                preview = ",".join(str(v) for v in missing_indices[:20])
                if len(missing_indices) > 20:
                    preview += ",..."
                log(
                    "⚠️ WAVEFORM chunk validation failed: "
                    f"expected={expected}, received={len(received_indices)}, "
                    f"missing_count={len(missing_indices)}, missing_idx=[{preview}]"
                )

            samples = []
            for i in range(expected):
                pair = self.chunked_waveform_samples.get(i)
                if pair is None:
                    continue
                current, voltage = pair
                samples.append({
                    "current": current,
                    "voltage": voltage,
                    "index": i,
                    "time_ms": (i - self.chunked_waveform_pulse_start) * WAVEFORM_SAMPLE_INTERVAL_MS,
                })

            if samples:
                log(
                    "🧪 Sample preview before emit: "
                    f"first={samples[0]}, mid={samples[len(samples)//2]}, last={samples[-1]}"
                )
            else:
                log("⚠️ No samples assembled before emit")

            wf_samples = expected
            pulse_start_sample = self.chunked_waveform_pulse_start
            if pulse_start_sample < 0 or pulse_start_sample >= wf_samples:
                pulse_start_sample = 0
            axis_max_time_ms = ((wf_samples - pulse_start_sample) - 1) * WAVEFORM_SAMPLE_INTERVAL_MS

            payload = {
                "samples": samples,
                "count": len(samples),
                "expected_count": expected,
                "meta": {
                    "wf_samples": wf_samples,
                    "pulse_start_sample": pulse_start_sample,
                    "pulse_end_sample": self.chunked_waveform_pulse_end,
                    "axis_max_time_ms": axis_max_time_ms,
                    "sample_interval_ms": WAVEFORM_SAMPLE_INTERVAL_MS,
                    "chunked": True,
                    "received_chunks": self.chunked_waveform_received_chunks,
                    "missing_samples": len(missing_indices),
                },
            }

            log(
                "🧩 Parsed chunked WAVEFORM: "
                f"expected={expected}, parsed={len(samples)}, "
                f"chunks={self.chunked_waveform_received_chunks}, missing={len(missing_indices)}"
            )
            log(f"📤 Emitting waveform_data with count={payload['count']} expected={payload['expected_count']}")
            socketio.emit("waveform_data", payload)
            log(f"✅ WAVEFORM complete! Emitting {len(samples)} samples to UI")

        except Exception as e:
            log(f"⚠️ Error finalizing chunked waveform: {e}")
        finally:
            self._reset_chunked_waveform_state()
            log("🧹 Chunked waveform state reset")

    def _parse_waveform(self, data: str) -> None:
        """Parse WAVEFORM payloads and emit waveform_data.

        Supported wire formats:
        1) WAVEFORM,count,current1,voltage1,current2,voltage2,...
        2) WAVEFORM,count,t_start_us,voltage1,current1,voltage2,current2,...
        3) WAVEFORM,count,t1_us,voltage1,current1,t2_us,voltage2,current2,...
        """
        global last_weld_meta
        try:
            # Legacy single-line waveform cancels any partial chunked assembly.
            self._reset_chunked_waveform_state()

            parts = data.split(",")
            if len(parts) < 3 or parts[0] != "WAVEFORM":
                log(f"⚠️ WAVEFORM malformed: {data}")
                return

            expected_count = int(parts[1])
            if expected_count <= 0:
                log(f"⚠️ WAVEFORM invalid count: {expected_count}")
                return

            raw = parts[2:]
            raw_tokens = len(raw)
            samples = []

            def _f(v: str):
                try:
                    return float(v)
                except Exception:
                    return None

            # Format 3: per-sample timestamps (triplets)
            if len(raw) >= expected_count * 3:
                ts_probe = []
                probe_n = min(expected_count, 6)
                for i in range(probe_n):
                    t = _f(raw[i * 3])
                    if t is None:
                        ts_probe = []
                        break
                    ts_probe.append(t)

                is_monotonic_ts = bool(ts_probe) and all(
                    ts_probe[i] <= ts_probe[i + 1] for i in range(len(ts_probe) - 1)
                )

                if is_monotonic_ts:
                    t0_us = _f(raw[0])
                    if t0_us is None:
                        t0_us = 0.0

                    for i in range(expected_count):
                        base = i * 3
                        if base + 2 >= len(raw):
                            break
                        t_us = _f(raw[base])
                        voltage = _f(raw[base + 1])
                        current = _f(raw[base + 2])
                        if t_us is None or voltage is None or current is None:
                            continue
                        samples.append({
                            "current": current,
                            "voltage": voltage,
                            "index": i,
                            "time_ms": max(0.0, (t_us - t0_us) / 1000.0),
                        })

            # Format 2: one start timestamp, then v/i pairs
            if not samples and len(raw) >= (expected_count * 2 + 1):
                t_start_us = _f(raw[0])
                if t_start_us is not None:
                    for i in range(expected_count):
                        base = 1 + (i * 2)
                        if base + 1 >= len(raw):
                            break
                        voltage = _f(raw[base])
                        current = _f(raw[base + 1])
                        if voltage is None or current is None:
                            continue
                        samples.append({
                            "current": current,
                            "voltage": voltage,
                            "index": i,
                            "time_ms": (t_start_us / 1000.0) + (i * WAVEFORM_SAMPLE_INTERVAL_MS),
                        })

            # Format 1: current/voltage pairs (no timestamps)
            if not samples:
                for i in range(expected_count):
                    base = i * 2
                    if base + 1 >= len(raw):
                        break
                    current = _f(raw[base])
                    voltage = _f(raw[base + 1])
                    if current is None or voltage is None:
                        continue
                    samples.append({
                        "current": current,
                        "voltage": voltage,
                        "index": i,
                    })

                # Rebuild time axis from sample index, aligned to pulse start sample.
                # This preserves the full capture window and puts t=0 at
                # pulse_start_sample.
                sample_count = len(samples)
                pulse_start = int(last_weld_meta.get("pulse_start_sample", DEFAULT_PULSE_START_SAMPLE))

                if pulse_start < 0:
                    pulse_start = 0
                elif sample_count > 0 and pulse_start >= sample_count:
                    pulse_start = 0

                for i, sample in enumerate(samples):
                    sample["time_ms"] = (i - pulse_start) * WAVEFORM_SAMPLE_INTERVAL_MS

            # Build timing metadata for frontend X-axis scaling.
            # Prefer STM32-reported wf_samples from WELD_DONE so dynamic pulse
            # durations (e.g. 220 samples for 20ms pulse) render full time range.
            wf_samples_raw = last_weld_meta.get("wf_samples", expected_count)
            pulse_start_raw = last_weld_meta.get("pulse_start_sample", DEFAULT_PULSE_START_SAMPLE)

            try:
                wf_samples = int(wf_samples_raw)
            except Exception:
                wf_samples = expected_count

            try:
                pulse_start_sample = int(pulse_start_raw)
            except Exception:
                pulse_start_sample = DEFAULT_PULSE_START_SAMPLE

            if wf_samples <= 0:
                wf_samples = max(expected_count, len(samples))

            if pulse_start_sample < 0:
                pulse_start_sample = 0
            elif pulse_start_sample >= wf_samples:
                pulse_start_sample = 0

            axis_max_time_ms = ((wf_samples - pulse_start_sample) - 1) * WAVEFORM_SAMPLE_INTERVAL_MS

            payload = {
                "samples": samples,
                "count": len(samples),
                "expected_count": expected_count,
                "meta": {
                    "wf_samples": wf_samples,
                    "pulse_start_sample": pulse_start_sample,
                    "axis_max_time_ms": axis_max_time_ms,
                    "sample_interval_ms": WAVEFORM_SAMPLE_INTERVAL_MS,
                },
            }
            parsed_count = len(samples)
            min_expected_tokens = expected_count * 2
            if raw_tokens < min_expected_tokens:
                log(
                    "⚠️ WAVEFORM token count short: "
                    f"expected_tokens>={min_expected_tokens}, got={raw_tokens}"
                )

            if parsed_count < expected_count:
                log(
                    "⚠️ WAVEFORM sample truncation detected: "
                    f"expected={expected_count}, parsed={parsed_count}, "
                    f"missing={expected_count - parsed_count}"
                )

            log(
                "🧩 Parsed WAVEFORM: "
                f"expected={expected_count}, parsed={parsed_count}, "
                f"raw_tokens={raw_tokens}, wf_samples={wf_samples}, "
                f"pulse_start={pulse_start_sample}, x_max={axis_max_time_ms:.2f}ms"
            )
            socketio.emit("waveform_data", payload)
            log("📤 Emitting event: waveform_data")

        except Exception as e:
            log(f"⚠️ Error parsing waveform: {e}")

    def _parse_event(self, event: str) -> None:
        global last_weld_duration_ms, last_weld_meta
        if event == "WELD_START":
            socketio.emit("weld_event", {"message": "WELD_START", "active": True})
            log("📤 Emitting event: weld_event (WELD_START)")

        elif event.startswith("WELD_DONE,"):
            try:
                parsed = {}
                for pair in event[10:].split(","):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        k = k.strip()
                        v = v.strip()
                        try:
                            parsed[k] = float(v) if "." in v else int(v)
                        except Exception:
                            parsed[k] = v

                vcap_b = float(parsed.get("vcap_b", 0.0))
                vcap_a = float(parsed.get("vcap_a", 0.0))
                avg_a = float(parsed.get("avg_a", 0.0))
                total_ms = float(parsed.get("total_ms", 0.0))
                v_tips = float(parsed.get("v_tips", 0.0))
                last_weld_duration_ms = total_ms
                last_weld_meta = parsed.copy()

                # Use STM32-provided energy directly when available.
                # This avoids host-side recomputation mismatch (e.g. 2x when using pack voltage).
                if "energy_j" in parsed:
                    joules = float(parsed.get("energy_j", 0.0))
                elif v_tips > 0 and total_ms > 0:
                    # Legacy fallback if firmware omits energy_j but provides tip voltage.
                    joules = v_tips * avg_a * (total_ms / 1000.0)
                else:
                    # Last-resort legacy estimate using capacitor voltage.
                    joules = ((vcap_b + vcap_a) * avg_a * total_ms / 2000.0)

                voltage_drop = float(parsed.get("delta_v", vcap_b - vcap_a))

                weld_payload = {
                    "peak_current_amps": float(parsed.get("peak_a", 0.0)),
                    "avg_current_amps": avg_a,
                    "duration_ms": total_ms,
                    "energy_joules": joules,
                    "vcap_before": vcap_b,
                    "vcap_after": vcap_a,
                    "voltage_drop": voltage_drop,
                }

                log(f"🧩 Parsed WELD_DONE: {parsed}")
                socketio.emit("weld_complete", weld_payload)
                log(f"📤 Emitting event: weld_complete {weld_payload}")
            except Exception as e:
                log(f"⚠️ Failed to parse WELD_DONE: {e}")

        elif event == "PEDAL_PRESS":
            socketio.emit("pedal_active", {"active": True})

        else:
            # CONTACT_TRIGGER, ARM_TIMEOUT, READY_TIMEOUT — pass through raw
            socketio.emit("esp32_message", {"message": f"EVENT,{event}"})


# ==== WEB ROUTES ====
@app.route("/")
def index():
    return render_template("control.html")


@app.route("/control")
def control():
    return render_template("control.html")


@app.route('/monitor')
def monitor():
    """Waveform monitoring page"""
    return render_template('monitor.html')


@app.route("/logs")
def logs():
    try:
        with open(LOG_FILE, "r") as f:
            log_lines = f.readlines()[-100:]
        return render_template("logs.html", logs=log_lines)
    except Exception:
        return render_template("logs.html", logs=[])


@app.route("/api/status")
def api_status():
    return jsonify({"status": "ok", "esp_connected": esp_connected, "data": last_status})


@app.route("/api/debug_waveform_parser", methods=["POST"])
def api_debug_waveform_parser():
    """Manual debug endpoint for chunked waveform assembly logic."""
    global esp_link

    if esp_link is None:
        return jsonify({"status": "error", "message": "ESP link not initialized"}), 503

    result = esp_link.run_waveform_parser_self_test()
    code = 200 if result.get("status") == "ok" else 500
    return jsonify(result), code


@app.route("/api/get_settings")
def api_get_settings():
    settings = load_settings()
    return jsonify({"status": "ok", "settings": settings})


@app.route("/api/save_settings", methods=["POST"])
def api_save_settings():
    global current_settings

    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No data provided"}), 400

    # Make sure new keys exist even if older UI doesn't send them
    if "trigger_mode" not in data:
        data["trigger_mode"] = "pedal"
    if "contact_hold_steps" not in data:
        data["contact_hold_steps"] = 2
    if "lead_resistance_mohm" not in data:
        data["lead_resistance_mohm"] = LEAD_RESISTANCE_DEFAULT_MOHM

    data["lead_resistance_mohm"] = _normalize_lead_resistance_mohm(
        data.get("lead_resistance_mohm", LEAD_RESISTANCE_DEFAULT_MOHM)
    )

    if not save_settings(data):
        return jsonify({"status": "error", "message": "Failed to save settings"}), 500

    current_settings = data

    # Push immediately to ESP on save
    if esp_link and esp_link.connected:
        push_settings_to_esp(log_prefix="[SAVE] ")

    return jsonify({"status": "ok"})
@app.route("/api/set_power", methods=["POST"])
def api_set_power():
    global current_settings
    data = request.get_json() or {}
    power = data.get("power", 100)

    s = current_settings or load_settings()
    s["power"] = power
    current_settings = s
    save_settings(s)

    if esp_link and esp_link.connected:
        cmd = f"SET_POWER,{power}"
        log(f"⚡ Sending to ESP32 (TCP): {cmd}")
        ok = esp_link.send_command(cmd)
        return jsonify({"status": "ok" if ok else "error"})

    return jsonify({"status": "error", "message": "ESP32 not connected"}), 503


@app.route("/api/set_preheat", methods=["POST"])
def api_set_preheat():
    global current_settings
    data = request.get_json() or {}

    enabled = bool(data.get("enabled", False))
    duration = data.get("duration", 20)
    power = data.get("power", 30)
    gap_ms = data.get("gap_ms", 3)

    s = current_settings or load_settings()
    s["preheat_enabled"] = enabled
    s["preheat_duration"] = duration
    s["preheat_power"] = power
    s["preheat_gap_ms"] = gap_ms
    current_settings = s
    save_settings(s)

    if esp_link and esp_link.connected:
        cmd = f"SET_PREHEAT,{1 if enabled else 0},{duration},{power},{gap_ms}"
        log(f"🔥 Sending to ESP32 (TCP): {cmd}")
        ok = esp_link.send_command(cmd)
        return jsonify({"status": "ok" if ok else "error"})

    return jsonify({"status": "error", "message": "ESP32 not connected"}), 503


@app.route("/api/get_presets")
def api_get_presets():
    presets = load_presets()
    settings = load_settings()
    return jsonify({"status": "ok", "presets": presets, "active_preset": settings.get("active_preset")})


@app.route("/api/save_preset", methods=["POST"])
def api_save_preset():
    data = request.get_json() or {}
    preset_id = data.get("preset_id")
    preset_data = data.get("data")

    if not preset_id or not preset_data:
        return jsonify({"status": "error", "message": "Missing preset_id or data"}), 400

    # Force trigger settings OUT of presets if UI accidentally sends them
    preset_data.pop("trigger_mode", None)
    preset_data.pop("contact_hold_steps", None)
    preset_data.pop("lead_resistance_mohm", None)

    presets = load_presets()
    presets[preset_id] = preset_data

    if not save_presets(presets):
        return jsonify({"status": "error", "message": "Failed to save preset"}), 500

    return jsonify({"status": "ok"})


@app.route("/api/arm", methods=["POST"])
def api_arm():
    if esp_link and esp_link.connected:
        esp_link.send_command("ARM,1")
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "ESP32 not connected"}), 503


@app.route("/api/disarm", methods=["POST"])
def api_disarm():
    if esp_link and esp_link.connected:
        esp_link.send_command("ARM,0")
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "ESP32 not connected"}), 503


@app.route("/api/fire", methods=["POST"])
def api_fire():
    if esp_link and esp_link.connected:
        esp_link.send_command("FIRE")
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "ESP32 not connected"}), 503


@app.route("/api/charge_on", methods=["POST"])
def api_charge_on():
    if esp_link and esp_link.connected:
        esp_link.send_command("CHARGE_ON")
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "ESP32 not connected"}), 503


@app.route("/api/charge_off", methods=["POST"])
def api_charge_off():
    if esp_link and esp_link.connected:
        esp_link.send_command("CHARGE_OFF")
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "ESP32 not connected"}), 503


@socketio.on("connect")
def handle_connect():
    log(f"🌐 Client connected: {request.sid}")
    emit("status_update", {**last_status, "esp_connected": esp_connected})


@socketio.on("disconnect")
def handle_disconnect():
    log(f"🌐 Client disconnected: {request.sid}")


def init_esp32_connection():
    global esp_link, esp_connected, current_settings

    current_settings = load_settings()
    log("🔌 Starting ESP32 connection manager...")

    delay = RECONNECT_BASE_DELAY_S

    while True:
        try:
            if (esp_link is None) or (not esp_link.connected):
                if esp_link is not None:
                    try:
                        esp_link.disconnect()
                    except Exception:
                        pass
                    esp_link = None

                esp_connected = False
                emit_status_update({"esp_connected": False})

                esp_link = ESP32Link(ESP32_IP, ESP32_PORT)

                if esp_link.connect():
                    esp_connected = True
                    emit_status_update({"esp_connected": True})
                    log("✅ TCP connected; syncing saved settings to ESP...")
                    push_settings_to_esp(log_prefix="[ESP CONNECT] ")
                    delay = RECONNECT_BASE_DELAY_S
                else:
                    log(f"❌ Connection failed, retrying in {delay:.0f} seconds...")
                    eventlet.sleep(delay)
                    delay = min(RECONNECT_MAX_DELAY_S, delay * 2)
            else:
                eventlet.sleep(1)

        except Exception as e:
            log(f"❌ Connection manager error: {e}")
            esp_connected = False
            emit_status_update({"esp_connected": False})
            eventlet.sleep(2)


if __name__ == "__main__":

    log("🚀 Starting Spot Welder Control Server")
    log(f"📡 ESP32 Target: {ESP32_IP}:{ESP32_PORT}")

    if not _esp_manager_started:
        _esp_manager_started = True
        eventlet.spawn_n(init_esp32_connection)

    log("🌐 Starting web server on http://0.0.0.0:8080")
    socketio.run(app, host="0.0.0.0", port=8080, debug=False, use_reloader=False)