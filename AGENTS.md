# AGENTS.md — Spot-Welder-Server (Flask dashboard)

Guidance for AI coding agents working in this repo. Read this before editing.

## What this repo is

This is **Tier 3** of the capacitor-bank spot welder system: a Python 3 / Flask +
Flask-SocketIO web dashboard and control server. It is a **TCP client** to the
ESP32-P4 WiFi bridge, which in turn bridges to the STM32 weld controller over UART.

```
Browser ⇄ (HTTP/WebSocket) ⇄ Flask (app.py) ⇄ (TCP :8888) ⇄ ESP32-P4 ⇄ (UART 576k) ⇄ STM32
```

The firmware for the ESP32-P4, STM32, and legacy ESP32-8048S043C boards lives in a
**separate repository**, [`farmertan81/Spot-Welder`](https://github.com/farmertan81/Spot-Welder).

## Repository map

| Path | Role |
|------|------|
| `app.py`                 | **The whole server.** Flask routes, Socket.IO events, the inlined `ESP32Link` TCP client, telemetry parser, settings persistence, ACK-gated command planner, calibration endpoints. Start here. |
| `ina226_reader.py`       | Helper for the INA226 current/voltage sensor math used when parsing telemetry. |
| `esp32_terminal.py`      | Standalone CLI tool to open a raw TCP session to the P4 bridge for manual packet poking. |
| `esp32_terminal_quiet.py`| Same as above with reduced logging. |
| `esp_link.py`            | **Legacy / unused.** An earlier standalone TCP-link class, superseded by the `ESP32Link` inlined in `app.py`. Kept for reference; do not build new work on it. |
| `settings.default.json`  | Shipped defaults. Copied to `settings.json` (git-ignored) on first run; the live file is the user's persisted settings. |
| `presets.json`           | Named weld-parameter presets. |
| `requirements.txt`       | Python deps (Flask, Flask-SocketIO, etc.). |
| `templates/`             | Jinja pages: `status.html` (landing), `control.html` (settings), `monitor.html` (live waveform), `firmware.html`, `logs.html`. |
| `static/`                | JS/CSS assets for the pages. |
| `docs/`                  | Design notes and analyses — see `docs/README.md`. |

## Run & validation commands

```bash
pip install -r requirements.txt
python app.py                 # serves dashboard on 0.0.0.0:8080, TCP-connects to the P4 bridge at 192.168.1.77:8888
```

- Web dashboard: `http://<host>:8080/` (defaults to the `/status` landing page).
- ESP32 target host/port live near the top of `app.py` (`DEFAULT_ESP32_*`); the
  active target is also settable at runtime via the UI / settings.
- There is no automated test suite. After changing `app.py`, at minimum confirm it
  imports and starts (`python -c "import app"`) and exercise the affected page in a browser.

## ⚠️ Source of truth: the telemetry protocol

The `STATUS` / `WAVEFORM_*` packets are ASCII `key=value` CSV lines **produced by the
STM32** and relayed by the P4. This server **re-parses** them in `app.py`. The
authoritative description lives in the firmware repo:

- **`PROTOCOL.md`** (and `AGENTS.md`) in [`farmertan81/Spot-Welder`](https://github.com/farmertan81/Spot-Welder).

**Rule:** any change to a field name, order, type, or units is a breaking change across
all three tiers (STM32 emitter → P4 parsers → this Flask parser). Do not change the parser
here to "fix" a field without coordinating the firmware change in the other repo. Prefer
additive changes (append new fields) over reordering. When you touch the parser, state
which packet(s) and field(s) you changed.

## Conventions

- Python: match existing style in `app.py` (no reformatting unrelated code). Keep the
  telemetry parser tolerant — missing/extra fields must not crash the socket loop.
- Do **not** commit `settings.json`, `weld_history*`, `__pycache__/`, `*.log`, or virtualenv
  dirs — see `.gitignore`.
- This repo tracks Markdown docs only (no generated `.pdf`/`.docx`). If a tool emits binary
  siblings next to a `.md`, do not stage them.
- **Workflow:** ongoing work happens on `Dev`; `main` is promoted from `Dev` when stable.
  Never commit to `main` directly. Never commit, push, open a PR, merge, or auto-merge unless
  explicitly requested. When changes are complete, provide a per-repository diff summary.
