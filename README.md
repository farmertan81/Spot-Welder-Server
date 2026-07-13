# Spot-Welder-Server

Flask + Socket.IO web dashboard and control server for the capacitor-bank spot
welder. This is **Tier 3** of the system — an optional remote dashboard that
connects over TCP/WiFi to the ESP32-P4 bridge, which in turn talks to the STM32
weld controller.

```
┌────────────┐   HTTP / WebSocket   ┌──────────────────┐   TCP :8888      ┌────────────┐   UART 576k     ┌────────────┐
│  Browser   │ ◄──────────────────► │  Flask server    │ ◄──────────────► │  ESP32-P4  │ ◄─────────────► │   STM32    │
│ (dashboard)│   (Flask-SocketIO)   │  app.py          │  line-based      │  (bridge)  │   text packets  │  (G474CE)  │
└────────────┘                      └──────────────────┘                  └────────────┘                 └────────────┘
   UI + JS                            web + bridge client                   transparent bridge            source of truth
```

- The **STM32** is the authoritative real-time controller (charge / fire /
  measure) and the source of truth for all telemetry.
- The **ESP32-P4** is a transparent WiFi ↔ serial bridge; it enriches the
  `STATUS` packet and relays everything else raw.
- This **Flask server** is the orchestration + UI layer: it holds persisted
  settings, builds ACK-gated command plans, parses telemetry, and pushes live
  updates to the browser over Socket.IO.

> **Companion repo:** the firmware (ESP32-P4, STM32, legacy ESP32-8048S043C) and the
> canonical wire-protocol reference (`PROTOCOL.md`) live in
> [`farmertan81/Spot-Welder`](https://github.com/farmertan81/Spot-Welder).
> Any change to a telemetry field name/order/units is a breaking change across
> both repos — see that repo's `PROTOCOL.md` and `AGENTS.md`.

---

## Quick start

```bash
cd Spot-Welder-Server
pip install -r requirements.txt

# Point it at your welder's ESP32 (defaults to 192.168.1.77:8888 if omitted).
# The IP is shown on the device's on-screen Setup tab.
export ESP32_IP=192.168.1.42
export ESP32_PORT=8888

python3 app.py
# → web UI on http://0.0.0.0:8080   (open http://<this-host>:8080/)
```

The server auto-connects to the ESP32 in a background thread with retry/backoff,
so you can start Flask before the welder is online. Telemetry panels degrade
gracefully (show `--`) until the bridge is reachable.

### Configuration

| Setting | Default | Override |
|---|---|---|
| Web server bind | `0.0.0.0:8080` | (edit `socketio.run(...)` in `app.py`) |
| ESP32 bridge IP | `192.168.1.77` | `ESP32_IP` env, or `esp32_ip` in `settings.json` |
| ESP32 bridge port | `8888` | `ESP32_PORT` env, or `esp32_port` in `settings.json` |

Settings use a template system — `settings.default.json` is committed; your
runtime `settings.json` is auto-created from it and git-ignored. See
[docs/SETTINGS.md](docs/SETTINGS.md).

---

## Web pages

| Route | Page | Purpose |
|---|---|---|
| `/` , `/status` | `status.html` | Landing page: live status, arm/disarm, weld counter |
| `/control` | `control.html` | Weld settings: mode, pulse, power, preheat, joule target, lead-R calibration |
| `/monitor` | `monitor.html` | Waveform monitor + Latest Weld Stats |
| `/firmware` | `firmware.html` | Trigger ESP32 / STM32 firmware flashing via the bridge |
| `/logs` | `logs.html` | Log view |

Key JSON/API endpoints include `/api/status`, `/api/get_settings`,
`/api/save_settings` (full ACK-gated sync), `/api/arm` · `/api/disarm` ·
`/api/fire` · `/api/charge_on` · `/api/charge_off`, `/api/calibrate_lead_r`, and
preset management under `/api/get_presets` · `/api/save_preset`.

---

## Repository layout

| Path | Role |
|---|---|
| `app.py` | Main Flask + Socket.IO server: routing, ESP32 TCP bridge client, ACK layer, telemetry parsing |
| `ina226_reader.py` | INA226 current/voltage sensor helper |
| `esp32_terminal.py` / `esp32_terminal_quiet.py` | Standalone serial terminal utilities for talking to the ESP32 directly |
| `esp_link.py` | **Legacy / unused** — superseded by the inlined `ESP32Link` class in `app.py`; kept for reference |
| `presets.json` | Saved weld presets |
| `settings.default.json` | Settings template (runtime `settings.json` is git-ignored) |
| `requirements.txt` | Python dependencies (Flask, Flask-SocketIO, gunicorn, …) |
| `templates/` | Jinja/HTML pages (status, control, monitor, firmware, logs) |
| `static/` | Front-end JS/CSS assets |
| `docs/` | Project documentation — see [docs/README.md](docs/README.md) |

---

## Documentation

See **[docs/README.md](docs/README.md)** for the full index. Highlights:

- [docs/SETTINGS.md](docs/SETTINGS.md) — settings template system
- [docs/FLASK_ESP32_SYNC.md](docs/FLASK_ESP32_SYNC.md) — dashboard ↔ ESP32 telemetry feature sync
- [docs/SETTINGS_REDESIGN.md](docs/SETTINGS_REDESIGN.md) — "user input is sacred" draft/apply/sync model
- [docs/FLASK_SERVER_ANALYSIS.md](docs/FLASK_SERVER_ANALYSIS.md) — historical Joule-mode & lead-resistance analysis

Build/agent guidance: [AGENTS.md](AGENTS.md).
