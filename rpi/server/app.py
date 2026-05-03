"""
Flask + SocketIO server for ESP32 spot welder control
Handles TCP connection, web UI, and real-time updates
"""

import os
import json
import time
import socket
import threading
import uuid
from collections import defaultdict, deque
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

# ========== CONFIGURATION ==========
# ESP32 target can be overridden at runtime:
# 1) Environment variables: ESP32_IP / ESP32_PORT
# 2) server/settings.json keys: esp32_ip / esp32_port
DEFAULT_ESP32_IP = "192.168.1.77"
DEFAULT_ESP32_PORT = 8888
ESP32_IP = DEFAULT_ESP32_IP
ESP32_PORT = DEFAULT_ESP32_PORT

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

# Deadband for STATUS vpack emission smoothing.
# Only update UI if voltage changes by more than 50mV.
VPACK_DEADBAND_V = 0.05

# Deadband for STATUS vcap emission smoothing.
# Only update UI if voltage changes by more than 50mV.
VCAP_DEADBAND_V = 0.05

# If your ESP supports a "CELLS" command, keep True. Otherwise set False.
REQUEST_CELLS_ON_CONNECT = True

# Settings save/ACK robustness tuning
SETTINGS_ACK_TIMEOUT_S = 0.7
SETTINGS_ACK_RETRIES = 1

LEAD_RESISTANCE_DEFAULT_MOHM = 1.87
LEAD_RESISTANCE_MIN_MOHM = 0.10
LEAD_RESISTANCE_MAX_MOHM = 10.00

# Waveform fallback sample interval (used when payload has no explicit timestamps).
WAVEFORM_SAMPLE_INTERVAL_MS = 0.1
DEFAULT_PULSE_START_SAMPLE = 10

# ========== FLASK SETUP ==========
app = Flask(__name__)
app.config["SECRET_KEY"] = "spot-welder-secret-2024"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ========== GLOBAL STATE ==========
esp_link = None
esp_connected = False
last_status = {}
current_settings = {}
_esp_manager_started = False
last_weld_duration_ms = 0.0
last_weld_meta = {}

# STATUS vpack/vcap deadband anchor state.
last_emitted_vpack = 0.0
has_emitted_vpack = False
last_emitted_vcap = 0.0
has_emitted_vcap = False

# ACK wait/notify bridge for reliable command application.
_ack_lock = threading.Lock()
_ack_waiters: dict[str, deque] = defaultdict(deque)


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


def load_runtime_config() -> tuple[str, int]:
    """Resolve ESP32 target IP/port from env or settings file."""
    ip = DEFAULT_ESP32_IP
    port = DEFAULT_ESP32_PORT

    # Optional server/settings.json overrides (kept in same file for easy editing).
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                file_ip = str(cfg.get("esp32_ip", "")).strip()
                if file_ip:
                    ip = file_ip

                if "esp32_port" in cfg:
                    try:
                        port = int(cfg.get("esp32_port", port))
                    except Exception:
                        pass
        except Exception as e:
            log(f"⚠️ Failed to parse {SETTINGS_FILE} for esp32 config: {e}")

    # Environment variables take highest precedence.
    env_ip = os.getenv("ESP32_IP", "").strip()
    if env_ip:
        ip = env_ip

    env_port = os.getenv("ESP32_PORT", "").strip()
    if env_port:
        try:
            port = int(env_port)
        except Exception:
            log(f"⚠️ Invalid ESP32_PORT env override: {env_port!r}; keeping {port}")

    if port <= 0 or port > 65535:
        log(f"⚠️ Invalid ESP32 port {port}; falling back to {DEFAULT_ESP32_PORT}")
        port = DEFAULT_ESP32_PORT

    return ip, port


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
    - Applies STATUS vpack/vcap deadband smoothing when those keys are present in patch
    - Always includes `esp_connected`
    """
    global last_status, esp_connected, last_emitted_vpack, has_emitted_vpack, last_emitted_vcap, has_emitted_vcap
    try:
        if patch:
            filtered_patch = dict(patch)

            # STATUS-side vpack deadband to suppress 1-2mV jitter flicker in UI.
            if "vpack" in filtered_patch:
                try:
                    new_vpack = float(filtered_patch["vpack"])
                    if (not has_emitted_vpack) or abs(new_vpack - last_emitted_vpack) > VPACK_DEADBAND_V:
                        last_emitted_vpack = new_vpack
                        has_emitted_vpack = True
                    else:
                        # Keep previously emitted value while still allowing
                        # other status fields to propagate.
                        filtered_patch["vpack"] = last_emitted_vpack
                except Exception:
                    pass

            # STATUS-side vcap deadband to suppress tiny telemetry jitter.
            if "vcap" in filtered_patch:
                try:
                    new_vcap = float(filtered_patch["vcap"])
                    if (not has_emitted_vcap) or abs(new_vcap - last_emitted_vcap) > VCAP_DEADBAND_V:
                        last_emitted_vcap = new_vcap
                        has_emitted_vcap = True
                    else:
                        # Keep previously emitted value while still allowing
                        # other status fields to propagate.
                        filtered_patch["vcap"] = last_emitted_vcap
                except Exception:
                    pass

            last_status.update(filtered_patch)

        payload = {**last_status, "esp_connected": esp_connected}
        socketio.emit("status_update", payload)
    except Exception as e:
        log(f"⚠️ emit_status_update failed: {e}")


def _coerce_scalar(raw: str):
    raw = raw.strip()
    if raw == "":
        return raw
    try:
        if any(ch in raw for ch in (".", "e", "E")):
            return float(raw)
        return int(raw)
    except Exception:
        return raw


def _parse_ack_line(line: str) -> dict | None:
    """Parse ACK lines into structured payload for UI + waiter matching."""
    if not line.startswith("ACK,"):
        return None

    parts = [p.strip() for p in line.split(",") if p.strip()]
    if len(parts) < 2:
        return None

    command = parts[1]
    fields = {}
    raw_values = []

    for token in parts[2:]:
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key.strip()] = _coerce_scalar(value)
        else:
            raw_values.append(_coerce_scalar(token))

    payload = {
        "raw": line,
        "command": command,
        "fields": fields,
        "raw_values": raw_values,
        "timestamp_ms": int(time.time() * 1000),
    }

    # Friendly fallback for ACK,ARM,1 / ACK,READY,0 styles.
    if raw_values:
        payload["value"] = raw_values[0]

    return payload


def _register_ack_waiter(command: str):
    event = threading.Event()
    waiter = {"event": event, "payload": None}
    with _ack_lock:
        _ack_waiters[command].append(waiter)
    return waiter


def _notify_ack_waiters(payload: dict) -> None:
    command = str(payload.get("command", "")).strip()
    if not command:
        return

    with _ack_lock:
        queue = _ack_waiters.get(command)
        if not queue:
            return
        waiter = queue.popleft()
        if not queue:
            _ack_waiters.pop(command, None)

    waiter["payload"] = payload
    waiter["event"].set()


def _send_command_with_ack(cmd: str, ack_command: str, timeout_s: float = SETTINGS_ACK_TIMEOUT_S,
                           retries: int = SETTINGS_ACK_RETRIES) -> dict:
    """Send command and wait for matching ACK, with one retry for resilience."""
    global esp_link

    attempts = 0
    while attempts <= retries:
        attempts += 1

        if not (esp_link and esp_link.connected):
            return {
                "ok": False,
                "ack": None,
                "attempts": attempts,
                "error": "ESP32 not connected",
            }

        waiter = _register_ack_waiter(ack_command)
        sent = esp_link.send_command(cmd)
        if not sent:
            with _ack_lock:
                queue = _ack_waiters.get(ack_command)
                if queue and waiter in queue:
                    queue.remove(waiter)
                    if not queue:
                        _ack_waiters.pop(ack_command, None)
            continue

        if waiter["event"].wait(timeout_s):
            return {
                "ok": True,
                "ack": waiter.get("payload"),
                "attempts": attempts,
                "error": None,
            }

        # Timeout: remove stale waiter then retry.
        with _ack_lock:
            queue = _ack_waiters.get(ack_command)
            if queue and waiter in queue:
                queue.remove(waiter)
                if not queue:
                    _ack_waiters.pop(ack_command, None)

    return {
        "ok": False,
        "ack": None,
        "attempts": attempts,
        "error": f"ACK timeout for {ack_command}",
    }


def _normalize_trigger_mode_value(raw_mode) -> int:
    s = str(raw_mode).strip().lower()
    if s in ("contact", "probe", "probe_contact", "2", "0"):
        return 2
    return 1


def _build_settings_command_plan(settings: dict) -> list[dict]:
    """Build ordered STM32 command plan with ACK expectations."""
    mode = int(settings.get("mode", 1))
    d1 = int(settings.get("d1", 50))
    gap1 = int(settings.get("gap1", 0))
    d2 = int(settings.get("d2", 0))
    gap2 = int(settings.get("gap2", 0))
    d3 = int(settings.get("d3", 0))
    power = int(settings.get("power", 100))

    pre_en = 1 if bool(settings.get("preheat_enabled", False)) else 0
    pre_ms = int(settings.get("preheat_duration", 20))
    pre_pct = int(settings.get("preheat_power", 30))
    pre_gap = int(settings.get("preheat_gap_ms", 3))

    trigger_mode_num = _normalize_trigger_mode_value(settings.get("trigger_mode", "pedal"))

    contact_hold_steps = int(settings.get("contact_hold_steps", 2))
    if contact_hold_steps < 1:
        contact_hold_steps = 1
    if contact_hold_steps > 10:
        contact_hold_steps = 10

    lead_r_mohm = _normalize_lead_resistance_mohm(
        settings.get("lead_resistance_mohm", LEAD_RESISTANCE_DEFAULT_MOHM)
    )

    return [
        {
            "name": "pulse",
            "cmd": f"SET_PULSE,{mode},{d1},{gap1},{d2},{gap2},{d3}",
            "ack": "SET_PULSE",
            "fields": ["mode", "d1", "gap1", "d2", "gap2", "d3"],
        },
        {
            "name": "power",
            "cmd": f"SET_POWER,{power}",
            "ack": "SET_POWER",
            "fields": ["power"],
        },
        {
            "name": "preheat",
            "cmd": f"SET_PREHEAT,{pre_en},{pre_ms},{pre_pct},{pre_gap}",
            "ack": "SET_PREHEAT",
            "fields": ["preheat_enabled", "preheat_duration", "preheat_power", "preheat_gap_ms"],
        },
        {
            "name": "trigger",
            "cmd": f"SET_TRIGGER_MODE,{trigger_mode_num}",
            "ack": "SET_TRIGGER_MODE",
            "fields": ["trigger_mode"],
        },
        {
            "name": "contact_hold",
            "cmd": f"SET_CONTACT_HOLD,{contact_hold_steps}",
            "ack": "SET_CONTACT_HOLD",
            "fields": ["contact_hold_steps"],
        },
        {
            "name": "lead_r",
            "cmd": f"SET_LEAD_R,{lead_r_mohm:.3f}",
            "ack": "LEAD_R",
            "fields": ["lead_resistance_mohm"],
        },
    ]


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
        # Preserve server runtime-config keys if UI payload does not include them.
        preserved = {}
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    for key in ("esp32_ip", "esp32_port"):
                        if key in existing and key not in settings:
                            preserved[key] = existing[key]
            except Exception:
                pass

        payload = dict(settings)
        payload.update(preserved)

        with open(SETTINGS_FILE, "w") as f:
            json.dump(payload, f, indent=2)
        return True
    except Exception as e:
        log(f"⚠️ Failed to save settings: {e}")
        return False


def settings_with_live_status(base_settings: dict) -> dict:
    """
    Compose API settings payload with clear source-of-truth boundaries:

    - Static recipe/config fields come from persisted settings.json
      (mode/pulse timings/power/preheat/trigger/etc).
    - Dynamic runtime telemetry comes from live STATUS/STATUS2 when available
      (enabled, vpack, temp, weld_count, C1/C2/C3).
    """
    merged = dict(base_settings or {})
    live = dict(last_status or {})

    def _live_first(*keys):
        for key in keys:
            if key in live:
                value = live.get(key)
                if value is not None:
                    return value
        return None

    def _to_int(value):
        try:
            return int(value)
        except Exception:
            return None

    def _to_float(value):
        try:
            return float(value)
        except Exception:
            return None

    enabled = _to_int(_live_first("enabled", "armed"))
    if enabled is not None:
        merged["enabled"] = 1 if enabled == 1 else 0

    vpack = _to_float(_live_first("vpack", "vcap"))
    if vpack is not None:
        merged["vpack"] = vpack

    temp = _live_first("temp")
    if temp is not None:
        temp_f = _to_float(temp)
        merged["temp"] = temp_f if temp_f is not None else temp

    weld_count = _to_int(_live_first("weld_count", "weld_counter"))
    if weld_count is not None:
        merged["weld_count"] = weld_count

    for out_key, aliases in (
        ("C1", ("C1", "cell1", "cell_1", "c1")),
        ("C2", ("C2", "cell2", "cell_2", "c2")),
        ("C3", ("C3", "cell3", "cell_3", "c3")),
    ):
        cell_v = _to_float(_live_first(*aliases))
        if cell_v is not None:
            merged[out_key] = cell_v

    return merged


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
    plan = _build_settings_command_plan(s)

    for item in plan:
        cmd = item["cmd"]
        log(f"{log_prefix}➡️ Syncing {item['name']} to ESP: {cmd}")
        esp_link.send_command(cmd)


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

        self._hb_thread: threading.Thread | None = None
        self._rx_thread: threading.Thread | None = None

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

            # Start RX loop in a dedicated daemon thread.
            self._rx_thread = threading.Thread(target=self._receive_loop, daemon=True, name="esp32-rx")
            self._rx_thread.start()

            # Heartbeat loop optional (disabled)
            # self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True, name="esp32-heartbeat")
            # self._hb_thread.start()

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

        # Thread loops exit when running=False and socket is closed.
        # We do not force-kill threads; they are daemonized and naturally unwind.
        self._close_sock()
        self._rx_thread = None
        self._hb_thread = None

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
            time.sleep(HEARTBEAT_INTERVAL_S)
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
                parts = line.split(",")
                if len(parts) >= 8:
                    socketio.emit("waveform_start", {
                        "total": int(parts[1]),
                        "pre_start": int(parts[2]),
                        "pre_end": int(parts[3]),
                        "gap_start": int(parts[4]),
                        "gap_end": int(parts[5]),
                        "main_start": int(parts[6]),
                        "main_end": int(parts[7]),
                    })
                self._parse_waveform_start(line)
            elif line.startswith("WAVEFORM_DATA,"):
                log("🧭 Routing packet -> _parse_waveform_data")
                self._parse_waveform_data(line)
            elif line == "WAVEFORM_END":
                log("🧭 Routing packet -> _finalize_chunked_waveform")
                self._finalize_chunked_waveform()
            elif line.startswith("WAVEFORM_PHASES,"):
                # NEW protocol (6 fields):
                # WAVEFORM_PHASES,preheat_start=X,preheat_end=Y,gap_start=Y,gap_end=Z,main_start=A,main_end=B
                # Legacy protocol (4 fields):
                # WAVEFORM_PHASES,preheat_start=X,preheat_end=Y,main_start=A,main_end=B
                try:
                    parts = line.replace("WAVEFORM_PHASES,", "").split(",")
                    parsed_fields = {}
                    for part in parts:
                        if "=" in part:
                            key, val = part.split("=", 1)
                            parsed_fields[key.strip()] = int(val.strip())

                    # Always emit a stable payload shape for frontend compatibility.
                    preheat_start = parsed_fields.get("preheat_start")
                    preheat_end = parsed_fields.get("preheat_end")
                    gap_start = parsed_fields.get("gap_start")
                    gap_end = parsed_fields.get("gap_end")
                    main_start = parsed_fields.get("main_start")
                    main_end = parsed_fields.get("main_end")

                    # Backward compatibility: old firmware has no explicit gap_start/gap_end.
                    # Use the inferred interval between preheat_end and main_start.
                    if gap_start is None and preheat_end is not None:
                        gap_start = preheat_end
                    if gap_end is None and main_start is not None:
                        gap_end = main_start

                    phase_data = {
                        "preheat_start": preheat_start,
                        "preheat_end": preheat_end,
                        "gap_start": gap_start,
                        "gap_end": gap_end,
                        "main_start": main_start,
                        "main_end": main_end,
                    }

                    socketio.emit("waveform_phases", phase_data)
                    log(f"📤 Emitting waveform_phases: {phase_data}")
                except Exception as phase_err:
                    log(f"⚠️ Failed to parse WAVEFORM_PHASES: {phase_err} | line={line}")
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
            elif line.startswith("ACK,"):
                ack_payload = _parse_ack_line(line)
                if ack_payload:
                    socketio.emit("command_ack", ack_payload)
                    _notify_ack_waiters(ack_payload)
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
            emit_status_update(status)
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

                # 1. Suppress tiny phantom charger-current noise from INA226 telemetry.
                for charge_key in ("charge_a", "ichg"):
                    if charge_key in parsed:
                        try:
                            charge_current = float(parsed[charge_key])
                            if abs(charge_current) < STATUS2_CHARGE_NOISE_THRESHOLD_A:
                                parsed[charge_key] = 0.0
                        except Exception:
                            pass

                # 2. Deadband filter for pack-voltage AND cell-voltage display smoothing.
                # We map keys to their specific deadband thresholds.
                smoothing_map = {
                    "vpack": 0.06,  # 60mV jitter filter
                    "cell1": 0.015, # 15mV jitter filter (kills 2.97<->2.98 bounce)
                    "cell2": 0.015,
                    "cell3": 0.015,
                    "vlow":  0.015,
                    "vmid":  0.015
                }

                # Initialize tracking dict if not exists
                if not hasattr(self, 'last_emitted_values'):
                    self.last_emitted_values = {}

                for key, threshold in smoothing_map.items():
                    if key in parsed:
                        try:
                            new_val = float(parsed[key])
                            last_val = self.last_emitted_values.get(key)

                            if last_val is None:
                                self.last_emitted_values[key] = new_val
                            elif abs(new_val - last_val) <= threshold:
                                # WITHIN DEADBAND: Use the last value to stop the flicker
                                parsed[key] = last_val
                                # CRITICAL: We don't update last_emitted_values here
                                # so that the 'anchor' stays solid until a real move occurs.
                            else:
                                # SIGNIFICANT MOVE: Update the anchor and allow the value
                                self.last_emitted_values[key] = new_val
                        except Exception:
                            pass

                # 3. Dialect alignment aliases
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

                # 4. Fallback logic
                if "vpack" not in parsed and "vcap" in last_status:
                    parsed["vpack"] = last_status["vcap"]

                last_status.update(parsed)
                emit_status_update({})

            except Exception as e:
                log(f"⚠️ Failed to parse STATUS2: {e}")

    def _reset_chunked_waveform_state(self) -> None:
        self.chunked_waveform_expected_count = 0
        self.chunked_waveform_pulse_start = DEFAULT_PULSE_START_SAMPLE
        self.chunked_waveform_pulse_end = DEFAULT_PULSE_START_SAMPLE

        # Optional multi-phase markers (sample indices in full capture window).
        self.chunked_waveform_preheat_start = None
        self.chunked_waveform_preheat_end = None
        self.chunked_waveform_gap_start = None
        self.chunked_waveform_gap_end = None
        self.chunked_waveform_main_start = None
        self.chunked_waveform_main_end = None

        # sample tuple = (timestamp_us_or_none, current_amps, voltage_volts)
        self.chunked_waveform_samples: dict[int, tuple[float | None, float, float]] = {}
        self.chunked_waveform_expected_indices: set[int] = set()
        self.chunked_waveform_received_chunks = 0
        self.chunked_waveform_active = False
        self.chunked_waveform_format = "unknown"
        self.chunked_waveform_has_timestamps = False

    def run_waveform_parser_self_test(self) -> dict:
        """Run a local chunk-assembly self-test and log every step."""
        log("🧪 Starting waveform parser self-test")

        try:
            # Synthetic waveform: 8 samples in 2 chunks (timestamp, voltage, current)
            test_samples = [
                (0.0, 8.90, 100.0),
                (100.0, 8.89, 200.0),
                (200.0, 8.88, 300.0),
                (300.0, 8.87, 400.0),
                (400.0, 8.86, 500.0),
                (500.0, 8.85, 600.0),
                (600.0, 8.84, 700.0),
                (700.0, 8.83, 800.0),
            ]

            self._parse_waveform_start("WAVEFORM_START,8,1,8")

            chunk0_tokens = ["WAVEFORM_DATA", "0", "4"]
            for t_us, v, c in test_samples[:4]:
                chunk0_tokens.extend([f"{t_us}", f"{v}", f"{c}"])
            self._parse_waveform_data(",".join(chunk0_tokens))

            chunk1_tokens = ["WAVEFORM_DATA", "4", "4"]
            for t_us, v, c in test_samples[4:]:
                chunk1_tokens.extend([f"{t_us}", f"{v}", f"{c}"])
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

            # Backward compatibility:
            # - Legacy format: WAVEFORM_START,total,main_start,main_end
            # - New format:    WAVEFORM_START,total,pre_s,pre_e,gap_s,gap_e,main_s,main_e
            if len(parts) >= 8:
                preheat_start = int(parts[2])
                preheat_end = int(parts[3])
                gap_start = int(parts[4])
                gap_end = int(parts[5])
                main_start = int(parts[6])
                main_end = int(parts[7])
                pulse_start = main_start
                pulse_end = main_end
            else:
                preheat_start = None
                preheat_end = None
                gap_start = None
                gap_end = None
                main_start = int(parts[2])
                main_end = int(parts[3])
                pulse_start = main_start
                pulse_end = main_end

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

            def _clamp_marker(v):
                if v is None:
                    return None
                return max(0, min(int(v), expected_count))

            self._reset_chunked_waveform_state()
            self.chunked_waveform_expected_count = expected_count
            self.chunked_waveform_pulse_start = max(0, min(pulse_start, expected_count - 1))
            self.chunked_waveform_pulse_end = max(self.chunked_waveform_pulse_start, min(pulse_end, expected_count))

            self.chunked_waveform_preheat_start = _clamp_marker(preheat_start)
            self.chunked_waveform_preheat_end = _clamp_marker(preheat_end)
            self.chunked_waveform_gap_start = _clamp_marker(gap_start)
            self.chunked_waveform_gap_end = _clamp_marker(gap_end)
            self.chunked_waveform_main_start = _clamp_marker(main_start)
            self.chunked_waveform_main_end = _clamp_marker(main_end)

            self.chunked_waveform_expected_indices = set(range(expected_count))
            self.chunked_waveform_active = True

            log(
                "📥 WAVEFORM_START received: "
                f"{expected_count} samples (pulse_start={self.chunked_waveform_pulse_start}, "
                f"pulse_end={self.chunked_waveform_pulse_end}, "
                f"preheat={self.chunked_waveform_preheat_start}-{self.chunked_waveform_preheat_end}, "
                f"gap={self.chunked_waveform_gap_start}-{self.chunked_waveform_gap_end}, "
                f"main={self.chunked_waveform_main_start}-{self.chunked_waveform_main_end})"
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

            if self.chunked_waveform_format == "unknown":
                if len(raw) >= (count * 3):
                    self.chunked_waveform_format = "timestamp_tvi"
                    self.chunked_waveform_has_timestamps = True
                else:
                    self.chunked_waveform_format = "legacy_vi"

            tokens_per_sample = 3 if self.chunked_waveform_format == "timestamp_tvi" else 2
            expected_tokens = count * tokens_per_sample
            if len(raw) < expected_tokens:
                log(
                    "⚠️ WAVEFORM_DATA token count short: "
                    f"start={start_idx}, count={count}, expected_tokens={expected_tokens}, got={len(raw)}, format={self.chunked_waveform_format}"
                )

            parsed_in_chunk = 0
            overwritten = 0
            first_idx = start_idx
            last_idx = start_idx - 1

            for i in range(count):
                token_idx = i * tokens_per_sample
                if (token_idx + tokens_per_sample - 1) >= len(raw):
                    break

                sample_idx = start_idx + i
                if sample_idx >= self.chunked_waveform_expected_count:
                    log(
                        "⚠️ WAVEFORM_DATA sample index out of range: "
                        f"idx={sample_idx}, expected_count={self.chunked_waveform_expected_count}"
                    )
                    continue

                timestamp_us = None

                try:
                    if tokens_per_sample == 3:
                        timestamp_us = float(raw[token_idx])
                        voltage = float(raw[token_idx + 1])
                        current = float(raw[token_idx + 2])
                    else:
                        current = float(raw[token_idx])
                        voltage = float(raw[token_idx + 1])
                except Exception as token_err:
                    bad_tokens = raw[token_idx:token_idx + tokens_per_sample]
                    log(
                        "⚠️ WAVEFORM_DATA non-numeric sample token: "
                        f"sample_idx={sample_idx}, tokens={bad_tokens}, err={token_err}"
                    )
                    continue

                if sample_idx in self.chunked_waveform_samples:
                    overwritten += 1

                self.chunked_waveform_samples[sample_idx] = (timestamp_us, current, voltage)
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
                f"{first_idx}-{last_idx} ({parsed_in_chunk} samples, declared={count}, overwritten={overwritten}, format={self.chunked_waveform_format})"
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
            timestamp_count = 0
            for i in range(expected):
                sample_tuple = self.chunked_waveform_samples.get(i)
                if sample_tuple is None:
                    continue

                timestamp_us, current, voltage = sample_tuple
                sample = {
                    "current": current,
                    "voltage": voltage,
                    "index": i,
                }

                if timestamp_us is not None:
                    sample["timestamp_us"] = timestamp_us
                    timestamp_count += 1

                samples.append(sample)

            wf_samples = expected
            pulse_start_sample = self.chunked_waveform_pulse_start
            if pulse_start_sample < 0 or pulse_start_sample >= wf_samples:
                pulse_start_sample = 0

            derived_interval_ms = WAVEFORM_SAMPLE_INTERVAL_MS
            time_source = "sample_index"

            if timestamp_count > 0 and samples:
                pulse_start_tuple = self.chunked_waveform_samples.get(pulse_start_sample)
                pulse_start_time_us = None
                if pulse_start_tuple and pulse_start_tuple[0] is not None:
                    pulse_start_time_us = pulse_start_tuple[0]
                else:
                    pulse_start_time_us = samples[0].get("timestamp_us", 0.0)

                previous_t_us = None
                timestamp_deltas_us = []
                for sample in samples:
                    t_us = sample.get("timestamp_us")
                    if t_us is None:
                        sample["time_ms"] = (sample["index"] - pulse_start_sample) * WAVEFORM_SAMPLE_INTERVAL_MS
                        continue

                    sample["time_ms"] = (t_us - pulse_start_time_us) / 1000.0

                    if previous_t_us is not None and t_us >= previous_t_us:
                        timestamp_deltas_us.append(t_us - previous_t_us)
                    previous_t_us = t_us

                if timestamp_deltas_us:
                    derived_interval_ms = (sum(timestamp_deltas_us) / len(timestamp_deltas_us)) / 1000.0

                time_source = "timestamp_us"
            else:
                for sample in samples:
                    sample["time_ms"] = (sample["index"] - pulse_start_sample) * WAVEFORM_SAMPLE_INTERVAL_MS

            if samples:
                log(
                    "🧪 Sample preview before emit: "
                    f"first={samples[0]}, mid={samples[len(samples)//2]}, last={samples[-1]}"
                )
            else:
                log("⚠️ No samples assembled before emit")

            sample_times = [float(s.get("time_ms", 0.0)) for s in samples if isinstance(s, dict)]
            fallback_max = ((wf_samples - pulse_start_sample) - 1) * WAVEFORM_SAMPLE_INTERVAL_MS
            axis_max_time_ms = max(sample_times) if sample_times else fallback_max
            axis_min_time_ms = min(sample_times) if sample_times else 0.0

            def _sample_to_time_ms(sample_idx):
                if sample_idx is None:
                    return None
                sample_idx = int(sample_idx)
                tuple_sample = self.chunked_waveform_samples.get(sample_idx)
                if tuple_sample and tuple_sample[0] is not None and timestamp_count > 0:
                    pulse_tuple = self.chunked_waveform_samples.get(pulse_start_sample)
                    pulse_start_time_us = pulse_tuple[0] if (pulse_tuple and pulse_tuple[0] is not None) else tuple_sample[0]
                    return (tuple_sample[0] - pulse_start_time_us) / 1000.0
                return (sample_idx - pulse_start_sample) * derived_interval_ms

            phase_meta = {
                "preheat_start_sample": self.chunked_waveform_preheat_start,
                "preheat_end_sample": self.chunked_waveform_preheat_end,
                "gap_start_sample": self.chunked_waveform_gap_start,
                "gap_end_sample": self.chunked_waveform_gap_end,
                "main_start_sample": self.chunked_waveform_main_start,
                "main_end_sample": self.chunked_waveform_main_end,
            }

            payload = {
                "samples": samples,
                "count": len(samples),
                "expected_count": expected,
                "meta": {
                    "wf_samples": wf_samples,
                    "pulse_start_sample": pulse_start_sample,
                    "pulse_end_sample": self.chunked_waveform_pulse_end,
                    "axis_min_time_ms": axis_min_time_ms,
                    "axis_max_time_ms": axis_max_time_ms,
                    "sample_interval_ms": derived_interval_ms,
                    "time_source": time_source,
                    "chunked": True,
                    "waveform_format": self.chunked_waveform_format,
                    "received_chunks": self.chunked_waveform_received_chunks,
                    "missing_samples": len(missing_indices),
                    **phase_meta,
                    "preheat_start_time_ms": _sample_to_time_ms(self.chunked_waveform_preheat_start),
                    "preheat_end_time_ms": _sample_to_time_ms(self.chunked_waveform_preheat_end),
                    "gap_start_time_ms": _sample_to_time_ms(self.chunked_waveform_gap_start),
                    "gap_end_time_ms": _sample_to_time_ms(self.chunked_waveform_gap_end),
                    "main_start_time_ms": _sample_to_time_ms(self.chunked_waveform_main_start),
                    "main_end_time_ms": _sample_to_time_ms(self.chunked_waveform_main_end),
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

            def _int_meta(name, default=None):
                raw_v = last_weld_meta.get(name, default)
                try:
                    return int(raw_v) if raw_v is not None else None
                except Exception:
                    return default

            preheat_start_sample = _int_meta("preheat_start_sample")
            preheat_end_sample = _int_meta("preheat_end_sample")
            gap_start_sample = _int_meta("gap_start_sample")
            gap_end_sample = _int_meta("gap_end_sample")
            main_start_sample = _int_meta("main_start_sample", pulse_start_sample)
            main_end_sample = _int_meta("main_end_sample", wf_samples)

            payload = {
                "samples": samples,
                "count": len(samples),
                "expected_count": expected_count,
                "meta": {
                    "wf_samples": wf_samples,
                    "pulse_start_sample": pulse_start_sample,
                    "pulse_end_sample": main_end_sample,
                    "axis_max_time_ms": axis_max_time_ms,
                    "sample_interval_ms": WAVEFORM_SAMPLE_INTERVAL_MS,
                    "preheat_start_sample": preheat_start_sample,
                    "preheat_end_sample": preheat_end_sample,
                    "gap_start_sample": gap_start_sample,
                    "gap_end_sample": gap_end_sample,
                    "main_start_sample": main_start_sample,
                    "main_end_sample": main_end_sample,
                    "preheat_start_time_ms": ((preheat_start_sample - pulse_start_sample) * WAVEFORM_SAMPLE_INTERVAL_MS) if preheat_start_sample is not None else None,
                    "preheat_end_time_ms": ((preheat_end_sample - pulse_start_sample) * WAVEFORM_SAMPLE_INTERVAL_MS) if preheat_end_sample is not None else None,
                    "gap_start_time_ms": ((gap_start_sample - pulse_start_sample) * WAVEFORM_SAMPLE_INTERVAL_MS) if gap_start_sample is not None else None,
                    "gap_end_time_ms": ((gap_end_sample - pulse_start_sample) * WAVEFORM_SAMPLE_INTERVAL_MS) if gap_end_sample is not None else None,
                    "main_start_time_ms": ((main_start_sample - pulse_start_sample) * WAVEFORM_SAMPLE_INTERVAL_MS) if main_start_sample is not None else None,
                    "main_end_time_ms": ((main_end_sample - pulse_start_sample) * WAVEFORM_SAMPLE_INTERVAL_MS) if main_end_sample is not None else None,
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

                # Use STM32-provided weld-tip energy directly when available.
                # This avoids host-side recomputation mismatch (e.g. 2x when using pack voltage).
                if "energy_weld_j" in parsed:
                    joules = float(parsed.get("energy_weld_j", 0.0))
                elif "energy_j" in parsed:
                    joules = float(parsed.get("energy_j", 0.0))
                elif v_tips > 0 and total_ms > 0:
                    # Legacy fallback if firmware omits energy fields but provides tip voltage.
                    joules = v_tips * avg_a * (total_ms / 1000.0)
                else:
                    # Last-resort legacy estimate using capacitor voltage.
                    joules = ((vcap_b + vcap_a) * avg_a * total_ms / 2000.0)

                voltage_drop = float(parsed.get("delta_v", vcap_b - vcap_a))

                def _float_field(*keys):
                    for key in keys:
                        if key not in parsed:
                            continue
                        try:
                            return float(parsed.get(key))
                        except Exception:
                            continue
                    return None

                def _int_field(*keys):
                    for key in keys:
                        if key not in parsed:
                            continue
                        try:
                            return int(round(float(parsed.get(key))))
                        except Exception:
                            continue
                    return None

                settings_snapshot = current_settings if isinstance(current_settings, dict) else {}

                try:
                    preheat_enabled = int(float(parsed.get("preheat_en", settings_snapshot.get("preheat_enabled", 0))))
                except Exception:
                    preheat_enabled = 0

                # Configured values (what user set) for UI labels.
                config_main_ms = _int_field("d1")
                if config_main_ms is None:
                    try:
                        config_main_ms = int(round(float(settings_snapshot.get("d1"))))
                    except Exception:
                        config_main_ms = None

                config_preheat_ms = _int_field("preheat_ms")
                if config_preheat_ms is None:
                    try:
                        config_preheat_ms = int(round(float(settings_snapshot.get("preheat_duration"))))
                    except Exception:
                        config_preheat_ms = None

                config_gap_ms = _int_field("preheat_gap_ms", "gap1_ms", "gap1")
                if config_gap_ms is None:
                    try:
                        config_gap_ms = int(round(float(settings_snapshot.get("preheat_gap_ms"))))
                    except Exception:
                        config_gap_ms = None

                # Keep measured pulse_ms in payload for diagnostics/backward compatibility.
                pulse_ms = _float_field("pulse_ms", "main_pulse_ms")
                pulse_us = _float_field("pulse_us", "main_pulse_us")
                if pulse_ms is None and pulse_us is not None:
                    pulse_ms = pulse_us / 1000.0

                weld_payload = {
                    "peak_current_amps": float(parsed.get("peak_a", 0.0)),
                    "avg_current_amps": avg_a,
                    "duration_ms": total_ms,
                    "energy_weld_j": joules,
                    "energy_joules": joules,
                    "vcap_before": vcap_b,
                    "vcap_after": vcap_a,
                    "voltage_drop": voltage_drop,
                    "preheat_en": preheat_enabled,
                    "preheat_ms": config_preheat_ms,
                    "preheat_gap_ms": config_gap_ms,
                    "pulse_ms": pulse_ms,
                    "d1": config_main_ms,
                    "configured_main_ms": config_main_ms,
                    "configured_preheat_ms": config_preheat_ms,
                    "configured_gap_ms": config_gap_ms,
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


@app.route("/api/status")
def api_status():
    payload = {**last_status, "status": "ok", "esp_connected": esp_connected, "data": last_status}
    return jsonify(payload)


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
    global current_settings
    settings = settings_with_live_status(current_settings or load_settings())
    current_settings = settings
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

    tx_id = str(uuid.uuid4())

    # Persist success should still return to UI even when hardware is offline.
    if not (esp_link and esp_link.connected):
        return jsonify({
            "status": "ok",
            "tx_id": tx_id,
            "offline": True,
            "message": "Settings saved locally; ESP32 not connected",
            "results": [],
            "failed": [],
        })

    plan = _build_settings_command_plan(data)

    for item in plan:
        esp_link.send_command(item["cmd"])
        time.sleep(0.03)  # 30ms pacing only
    # Done. ESP32 will push STATUS back confirming the change.

    return jsonify({"status": "ok", "tx_id": tx_id, "results": [], "failed": []})
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
                    log("✅ TCP connected; waiting for live STATUS sync (STM32 runtime is source of truth)")
                    delay = RECONNECT_BASE_DELAY_S
                else:
                    log(f"❌ Connection failed, retrying in {delay:.0f} seconds...")
                    time.sleep(delay)
                    delay = min(RECONNECT_MAX_DELAY_S, delay * 2)
            else:
                time.sleep(1)

        except Exception as e:
            log(f"❌ Connection manager error: {e}")
            esp_connected = False
            emit_status_update({"esp_connected": False})
            time.sleep(2)


if __name__ == "__main__":
    ESP32_IP, ESP32_PORT = load_runtime_config()

    log("🚀 Starting Spot Welder Control Server")
    log(f"📡 ESP32 Target: {ESP32_IP}:{ESP32_PORT}")
    log("🛠️ Override target via ESP32_IP/ESP32_PORT env vars or settings.json keys esp32_ip/esp32_port")

    # PRODUCTION DEPLOYMENT:
    # For low-traffic deployments, threaded gunicorn is simple and reliable:
    #   gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 4 app:app
    #
    # For higher concurrency, gevent is also supported:
    #   gunicorn --bind 0.0.0.0:8080 --workers 1 --worker-class gevent app:app

    if not _esp_manager_started:
        _esp_manager_started = True
        threading.Thread(target=init_esp32_connection, daemon=True, name="esp32-manager").start()

    log("🌐 Starting web server on http://0.0.0.0:8080")
    socketio.run(
        app,
        host="0.0.0.0",
        port=8080,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )

