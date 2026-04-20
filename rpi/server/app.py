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
RECV_TIMEOUT_S = 1.0
NO_TELEM_GRACE_S = 12.0
NO_TELEM_RECONNECT_S = 120.0
RECONNECT_BASE_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 8.0

HEARTBEAT_INTERVAL_S = 5.0  # Send PING every 5 seconds to keep connection alive

# Deadband for pack voltage display smoothing (STATUS2 INA226 telemetry).
# UI voltage is only allowed to move if absolute change exceeds this threshold.
STATUS2_VPACK_DEADBAND_V = 0.08

# If your ESP supports a "CELLS" command, keep True. Otherwise set False.
REQUEST_CELLS_ON_CONNECT = True

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

SETTINGS_PERSIST_DEBOUNCE_S = 0.6

# Authoritative settings fields learned from ESP32 STATUS.
PERSISTABLE_STATUS_KEYS = {
    "mode",
    "d1",
    "gap1",
    "d2",
    "gap2",
    "d3",
    "power_pct",
    "preheat_en",
    "preheat_ms",
    "preheat_pct",
    "preheat_gap_ms",
    "trigger_mode",
    "contact_hold_steps",
    "contact_with_pedal",
}

# Live-only runtime fields from STATUS/STATUS2: never persisted as defaults.
LIVE_RUNTIME_STATUS_KEYS = {
    "enabled", "armed", "ready", "welding", "state",
    "cooldown_ms", "charger_on", "chg_en",
    "vpack", "vlow", "vmid", "vcap",
    "cell1", "cell2", "cell3", "cell_1", "cell_2", "cell_3", "C1", "C2", "C3",
    "ichg", "charge_a", "iweld", "ina_ok", "temp", "temperature", "current", "i", "weld_count", "weld_counter",
}

persistable_settings_state = {}
live_runtime_state = {}
_settings_save_greenlet = None


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
            if "contact_with_pedal" not in settings:
                settings["contact_with_pedal"] = True

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
        "contact_with_pedal": True,
    }


def save_settings(settings: dict) -> bool:
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
        return True
    except Exception as e:
        log(f"⚠️ Failed to save settings: {e}")
        return False


def _normalize_trigger_mode(raw_mode) -> str:
    if isinstance(raw_mode, str):
        mode_s = raw_mode.strip().lower()
        if mode_s in ("pedal", "contact"):
            return mode_s
        if mode_s == "2":
            return "contact"
        return "pedal"

    try:
        mode_num = int(raw_mode)
    except Exception:
        return "pedal"

    # Protocol: 1=pedal, 2=contact
    return "contact" if mode_num == 2 else "pedal"


def _normalize_contact_hold_steps(raw_steps) -> int:
    try:
        steps = int(raw_steps)
    except Exception:
        steps = 2
    if steps < 1:
        return 1
    if steps > 10:
        return 10
    return steps


def _persist_current_settings_debounced() -> None:
    global _settings_save_greenlet, current_settings

    eventlet.sleep(SETTINGS_PERSIST_DEBOUNCE_S)

    try:
        save_settings(current_settings)
    finally:
        _settings_save_greenlet = None


def schedule_settings_persist() -> None:
    global _settings_save_greenlet
    if _settings_save_greenlet is None:
        _settings_save_greenlet = eventlet.spawn(_persist_current_settings_debounced)


def learn_persistable_settings_from_status(status: dict) -> bool:
    """
    Learn persistable defaults from authoritative ESP32 STATUS.
    Returns True if any persisted field changed.
    """
    global current_settings, persistable_settings_state

    if not status:
        return False

    s = dict(current_settings or load_settings())
    changed = False

    mapping = {
        "mode": ("mode", int),
        "d1": ("d1", int),
        "gap1": ("gap1", int),
        "d2": ("d2", int),
        "gap2": ("gap2", int),
        "d3": ("d3", int),
        "power_pct": ("power", int),
        "preheat_en": ("preheat_enabled", lambda v: int(v) == 1),
        "preheat_ms": ("preheat_duration", int),
        "preheat_pct": ("preheat_power", int),
        "preheat_gap_ms": ("preheat_gap_ms", int),
        "trigger_mode": ("trigger_mode", _normalize_trigger_mode),
        "contact_hold_steps": ("contact_hold_steps", _normalize_contact_hold_steps),
        "contact_with_pedal": ("contact_with_pedal", lambda v: int(v) == 1),
    }

    for src_key in PERSISTABLE_STATUS_KEYS:
        if src_key not in status or src_key not in mapping:
            continue

        dest_key, caster = mapping[src_key]
        try:
            new_val = caster(status[src_key])
        except Exception:
            continue

        if s.get(dest_key) != new_val:
            s[dest_key] = new_val
            changed = True

        persistable_settings_state[dest_key] = new_val

    if changed:
        current_settings = s
        schedule_settings_persist()

    return changed


def update_live_runtime_state(status: dict) -> None:
    if not status:
        return
    for key in LIVE_RUNTIME_STATUS_KEYS:
        if key in status:
            live_runtime_state[key] = status[key]


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

    trigger_mode = _normalize_trigger_mode(s.get("trigger_mode", "pedal"))
    contact_hold_steps = _normalize_contact_hold_steps(s.get("contact_hold_steps", 2))
    contact_with_pedal = 1 if bool(s.get("contact_with_pedal", True)) else 0

    # ESP protocol:
    # 1 = pedal
    # 2 = contact
    trigger_mode_num = 2 if trigger_mode == "contact" else 1

    cmd_pulse = f"SET_PULSE,{mode},{d1},{gap1},{d2},{gap2},{d3}"
    cmd_power = f"SET_POWER,{power}"
    cmd_pre = f"SET_PREHEAT,{pre_en},{pre_ms},{pre_pct},{pre_gap}"
    cmd_trigger = f"SET_TRIGGER_MODE,{trigger_mode_num}"
    cmd_contact = f"SET_CONTACT_HOLD,{contact_hold_steps}"
    cmd_contact_with_pedal = f"SET_CONTACT_WITH_PEDAL,{contact_with_pedal}"

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

    log(f"{log_prefix}➡️ Syncing contact+pedal behavior to ESP: {cmd_contact_with_pedal}")
    esp_link.send_command(cmd_contact_with_pedal)


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
                data = self.sock.recv(1024)  # type: ignore[union-attr]
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
        elif line.startswith("CELLS,"):
            self._parse_cells(line[6:])
        elif line.startswith("STATUS2,"):
            self._parse_status2(line[8:])
        elif line.startswith("WAVEFORM,"):
            self._parse_waveform(line)
        elif line.startswith("EVENT,"):
            self._parse_event(line[6:])
        elif line.startswith("DENY,"):
            reason = line[5:]
            log(f"🚫 DENY: {reason}")
            socketio.emit("deny_event", {"reason": reason})
        elif line.startswith("BOOT,"):
            msg = line[5:]
            log(f"🔄 STM32 BOOT: {msg}")
            socketio.emit("boot_event", {"message": msg})
        elif line.startswith("CHARGER:") or line.startswith("CHARGER,"):
            payload = line.split(",", 1)[1] if "," in line else line.split(":", 1)[1]
            self._parse_charger(payload)
        else:
            socketio.emit("esp32_message", {"message": line})

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

            # Runtime-only state bucket: updated immediately, never persisted.
            update_live_runtime_state(status)

            # Persistable bucket: learn defaults from authoritative ESP STATUS.
            learned = learn_persistable_settings_from_status(status)
            if learned:
                log("🧠 Learned/persisting settings from authoritative STATUS")

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

            update_live_runtime_state(cells)
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
                update_live_runtime_state({"current": current, "i": current})
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

            # Charger-enable flag can arrive as chg_en (STATUS2) and/or charger_on (STATUS).
            # Resolve robustly so telemetry isn't accidentally clamped during active charging.
            chg_en = 0
            for raw_flag in (
                parsed.get("chg_en"),
                parsed.get("charger_on"),
                last_status.get("charger_on"),
                last_status.get("chg_en"),
            ):
                if raw_flag is None:
                    continue
                try:
                    chg_en = 1 if int(float(raw_flag)) == 1 else 0
                    break
                except Exception:
                    continue

            # Keep aliases in sync for downstream consumers.
            parsed["chg_en"] = chg_en
            parsed["charger_on"] = chg_en

            # STATUS2 charge telemetry must be gated by charger enable.
            if chg_en == 0:
                for charge_key in ("charge_a", "ichg"):
                    if charge_key in parsed:
                        parsed[charge_key] = 0.0
                # Reset deadband memory when charger is off to avoid stale-hold behavior.
                self.last_vpack_emitted = None
            else:
                # Charger is on: preserve actual current readings (no extra clamping).
                # Deadband filter for pack-voltage display smoothing.
                if "vpack" in parsed:
                    try:
                        new_vpack = float(parsed["vpack"])

                        if self.last_vpack_emitted is None:
                            self.last_vpack_emitted = new_vpack
                        elif abs(new_vpack - self.last_vpack_emitted) <= STATUS2_VPACK_DEADBAND_V:
                            parsed["vpack"] = self.last_vpack_emitted
                        else:
                            self.last_vpack_emitted = new_vpack
                    except Exception:
                        pass

            # Charge-current alias alignment.
            if "ichg" in parsed and "charge_a" not in parsed:
                parsed["charge_a"] = parsed["ichg"]
            elif "charge_a" in parsed and "ichg" not in parsed:
                parsed["ichg"] = parsed["charge_a"]

            # Dialect alignment aliases for older/newer frontend keys.
            cell_aliases = {
                "cell1": ("cell_1", "C1"),
                "cell2": ("cell_2", "C2"),
                "cell3": ("cell_3", "C3"),
            }
            for src_key, alias_keys in cell_aliases.items():
                if src_key in parsed:
                    for alias_key in alias_keys:
                        parsed[alias_key] = parsed[src_key]

            # If vpack is not available for any reason, fall back to vcap if present.
            if "vpack" not in parsed and "vcap" in last_status:
                parsed["vpack"] = last_status["vcap"]

            update_live_runtime_state(parsed)
            log(f"🧩 Parsed STATUS2: {parsed}")
            last_status.update(parsed)
            emit_status_update({})
            log("📤 Emitting event: status_update (from STATUS2)")

        except Exception as e:
            log(f"⚠️ Failed to parse STATUS2: {e}")

    def _parse_waveform(self, data: str) -> None:
        """Parse WAVEFORM,count,i1,v1,i2,v2,... and emit waveform_data."""
        try:
            parts = data.split(",")
            if len(parts) < 2:
                log(f"⚠️ WAVEFORM malformed (too short): {data}")
                return

            expected_count = int(parts[1])
            samples = []

            # Parse flattened current/voltage pairs.
            for i in range(expected_count):
                idx = 2 + (i * 2)
                if idx + 1 < len(parts):
                    try:
                        current = float(parts[idx])
                        voltage = float(parts[idx + 1])
                        samples.append({
                            "current": current,
                            "voltage": voltage,
                            "index": i,
                        })
                    except Exception:
                        continue

            # Map samples to weld time so final sample lands exactly at duration.
            # Fence-post fix: N samples have (N-1) intervals.
            sample_count = len(samples)
            duration_ms = float(last_status.get("pulse_ms", 0.0) or 0.0)
            if sample_count > 0 and last_weld_duration_ms > 0:
                duration_ms = float(last_weld_duration_ms)

            dt_ms = duration_ms / (sample_count - 1) if sample_count > 1 else 0.0
            for i, sample in enumerate(samples):
                sample["time_ms"] = i * dt_ms

            payload = {
                "samples": samples,
                "count": expected_count,
            }
            log(f"🧩 Parsed WAVEFORM: expected={expected_count}, parsed={len(samples)}, duration_ms={duration_ms:.3f}, dt_ms={dt_ms:.6f}")
            socketio.emit("waveform_data", payload)
            log("📤 Emitting event: waveform_data")

        except Exception as e:
            log(f"⚠️ Error parsing waveform: {e}")

    def _parse_event(self, event: str) -> None:
        global last_weld_duration_ms
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

                # Prefer physically-correct weld energy using tip voltage.
                # E = V * I * t, with t in seconds.
                if v_tips > 0 and total_ms > 0:
                    joules = v_tips * avg_a * (total_ms / 1000.0)
                elif "energy_j" in parsed:
                    # Fall back to STM32-provided energy if available.
                    joules = float(parsed.get("energy_j", 0.0))
                else:
                    # Legacy fallback if older firmware does not provide v_tips/energy_j.
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


@app.route("/api/get_settings")
def api_get_settings():
    settings = current_settings or load_settings()
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
    if "contact_with_pedal" not in data:
        data["contact_with_pedal"] = True

    data["trigger_mode"] = _normalize_trigger_mode(data.get("trigger_mode"))
    data["contact_hold_steps"] = _normalize_contact_hold_steps(data.get("contact_hold_steps"))
    data["contact_with_pedal"] = bool(data.get("contact_with_pedal", True))

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
    global esp_link, esp_connected, current_settings, persistable_settings_state

    current_settings = load_settings()
    persistable_settings_state = dict(current_settings)
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
                    log("✅ TCP connected; waiting for authoritative STATUS from ESP32 (no auto-push)")
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