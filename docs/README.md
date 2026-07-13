# Documentation index — Spot-Welder-Server

Design notes and analyses for the Flask dashboard/control server. For how to run
the server and the repo layout, see the [root README](../README.md) and
[`AGENTS.md`](../AGENTS.md).

| Document | What it covers |
|----------|----------------|
| [`SETTINGS.md`](SETTINGS.md) | User-facing guide to the settings system: `settings.default.json` vs the live `settings.json`, what each field does, and how the UI persists changes. |
| [`SETTINGS_REDESIGN.md`](SETTINGS_REDESIGN.md) | Design record for the settings/control-page redesign (grouping, validation, layout rationale). |
| [`FLASK_ESP32_SYNC.md`](FLASK_ESP32_SYNC.md) | How the Flask server keeps settings in sync with the ESP32/STM32: the ACK-gated command plan, export sequence, and field mapping. |
| [`FLASK_SERVER_ANALYSIS.md`](FLASK_SERVER_ANALYSIS.md) | Deeper architectural walkthrough of `app.py` (routes, `ESP32Link`, telemetry parsing). **Historical** — written before the lead-resistance calibration feature was built; see the dated banner at the top of the file. |

## Protocol reference

This server only *parses* telemetry — it does not define it. The authoritative
wire-protocol reference lives in the companion firmware repo:

- **`PROTOCOL.md`** in [`farmertan81/Spot-Welder`](https://github.com/farmertan81/Spot-Welder).

Any change to a telemetry field name, order, type, or units is a breaking change
across the STM32 emitter, the ESP32-P4 parsers, and this Flask parser.
