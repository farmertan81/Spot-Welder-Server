# Flask Server Analysis — Joule Mode & Lead Resistance Calibration

**Repository:** `farmertan81/Spot-Welder-Server` (branch: `Dev`)
**Scope:** Read-only reconnaissance of the Raspberry Pi Flask control server to understand the existing Joule-mode and lead-resistance machinery, and to recommend where to build a **lead-resistance calibration** feature first (Flask vs ESP32).
**Primary file analyzed:** `rpi/server/app.py` (2,320 lines) + `rpi/server/templates/control.html` (1,738 lines)
**Date:** 2026-06-03

> **TL;DR recommendation:** Prototype the lead-calibration *workflow and math* on the **Flask server first**. The server already owns the full settings/ACK pipeline, the live telemetry stream (V & I), and a rich HTML UI — so you can build a calibration routine that fires a known pulse, reads back `WELD_DONE` telemetry, computes lead resistance, and writes it via the existing `SET_LEAD_R` command **without touching firmware**. Migrate to the ESP32 only once the algorithm is proven. See [Section 5](#5-recommendation-flask-first-vs-esp32-first).

---

## 1. Architecture Overview

### 1.1 Three-tier control chain

```
┌─────────────┐   HTTP / WebSocket    ┌──────────────────┐   TCP socket      ┌─────────────┐   UART/serial   ┌─────────────┐
│  Browser    │  ◄─────────────────►  │  Flask server    │  ◄─────────────►  │   ESP32      │  ◄───────────►  │   STM32     │
│ control.html│   (Flask-SocketIO)    │  app.py (RPi)    │   :8888 line-based│  (Wi-Fi link)│   text packets  │ (G474, MCU) │
└─────────────┘                       └──────────────────┘                   └─────────────┘                 └─────────────┘
   UI + JS                              web + bridge                            transparent bridge              source of truth
```

- **STM32** is the authoritative real-time controller (charge/fire/measure). It is the *source of truth* for runtime state.
- **ESP32** is a transparent Wi-Fi ↔ serial bridge. The Flask server treats it as the network endpoint; commands/telemetry are plain newline-delimited text packets that pass through to/from the STM32.
- **Flask server (`app.py`)** is the orchestration + UI layer: it holds persisted settings, builds command plans, waits for hardware ACKs, parses telemetry, and pushes live updates to the browser over SocketIO.

### 1.2 Flask app setup & networking

| Concern | Detail | Location |
|---|---|---|
| Framework | Flask + Flask-SocketIO, `async_mode="threading"` | `app.py` (top, setup block) |
| Web server port | `0.0.0.0:8080` | `app.py:2311-2319` |
| ESP32 transport | Raw TCP socket, line-based protocol | `app.py:771-931` (`class ESP32Link`) |
| ESP32 target | `DEFAULT_ESP32_IP=192.168.1.77`, `DEFAULT_ESP32_PORT=8888` (overridable via `ESP32_IP`/`ESP32_PORT` env or `esp32_ip`/`esp32_port` in `settings.json`) | `app.py:103-143` (`load_runtime_config`) |
| Connection manager | Background daemon thread, auto-reconnect with exponential backoff | `app.py:2251-2309` (`init_esp32_connection`) |

> **Note / discrepancy to verify:** The ESP32 firmware analysis (prior task) referenced TCP port **8080** for the ESP32 listener, whereas the Flask server connects to the ESP32 on port **8888** (`DEFAULT_ESP32_PORT`). Confirm the actual ESP32 listening port before any deployment; the value is configurable so the two may simply be configured differently per environment.

### 1.3 ESP32Link — the TCP bridge class (`app.py:771-931`)

- `connect()` (`app.py:791-838`): opens `socket.create_connection`, sets TCP keepalive, spawns a daemon RX thread (`_receive_loop`), and requests a one-time `STATUS` (+ optional `CELLS`) snapshot.
- `send_command(cmd)` (`app.py:863-880`): appends `\n`, sends UTF-8; disconnects on error. **This is the single chokepoint for everything the server sends to hardware.**
- `_receive_loop()` (`app.py:888-933`): reads chunks, splits on `\n`, dispatches each complete line to `_handle_line()`.
- `_handle_line()` (`app.py:935-1065`): routes by packet prefix — `STATUS,` `CELLS,` `DISPLAY,` `STATUS2,` `WAVEFORM_*`, plus `ACK,`/`DENY,`/`WELD_DONE` handling further down.

### 1.4 Command/ACK reliability layer (`app.py:189-400`)

A robust request/response bridge sits on top of the otherwise fire-and-forget TCP link:

- `_parse_ack_line()` / `_parse_deny_line()` (`app.py:211-267`): parse `ACK,<command>,...` and `DENY,<command>,...` into structured payloads.
- Waiter registry: `_register_ack_waiter` / `_notify_ack_waiters` (and DENY equivalents) implement a thread-safe event-based wait-notify bridge (`app.py:270-339`).
- **`_send_command_with_ack(cmd, ack_command, timeout_s, retries)`** (`app.py:342-400+`): sends a command and blocks until the matching `ACK` arrives, fails fast on `DENY`, retries on timeout. **This is the function any new calibration command should reuse for reliability.**

### 1.5 SocketIO events emitted to the browser

`status_update`, `display_update`, `weld_event`, `weld_complete`, `waveform_start/data/end`, `waveform_data`, `pedal_active`, plus raw `esp32_message`. Live state is merged into a global `last_status` dict and broadcast via `emit_status_update()`.

### 1.6 HTTP API surface (browser → server)

| Route | Purpose | Location |
|---|---|---|
| `GET /` | Main control UI (`control.html`) | `app.py:1975` |
| `GET /monitor` | Waveform monitor page | `app.py:1978-1981` |
| `GET /api/status` | Latest merged status snapshot | `app.py:1984-1987` |
| `GET /api/get_settings` | Settings + live telemetry merge | `app.py:2003-2008` |
| **`POST /api/save_settings`** | **Full settings sync to STM32 with per-command ACK** | `app.py:2011-2120` |
| `POST /api/set_power`, `/api/set_preheat` | Single-field quick sets (no ACK wait) | `app.py:2123-2167` |
| `GET /api/get_presets`, `POST /api/save_preset` | Preset management | `app.py:2170-2197` |
| `POST /api/arm`, `/api/disarm`, `/api/fire`, `/api/charge_on`, `/api/charge_off` | Direct hardware actions | `app.py:2200-2237` |

---

## 2. Joule Mode Implementation

Joule mode = energy-controlled welding: instead of welding for a fixed pulse *time*, the STM32 keeps delivering energy until a target number of **joules** (into the workpiece) has been reached, then stops.

### 2.1 Configuration & clamping

```python
# app.py:436-439 (inside _build_settings_command_plan)
try:
    joule_target_j = int(round(float(settings.get("joule_target_j", 150))))
except Exception:
    joule_target_j = 150
joule_target_j = max(0, min(300, joule_target_j))   # clamp 0..300 J
```

- Default target: **150 J**, clamped to **0–300 J**. Same clamp repeated in `load_settings()` (`app.py:535-538`) and `POST /api/save_settings` (`app.py:2037-2040`).
- `control_mode`: `0` = normal/time mode, `1` = **Joule mode** (`app.py:430-433`).

### 2.2 Commands sent to the STM32

Inside `_build_settings_command_plan()` (`app.py:458-507`), the full sync sequence (each waits for its own ACK) is:

```
SET_MODE,{control_mode}          → ACK SET_MODE         (app.py:460-463)
SET_JOULE_TARGET,{joule_target_j}→ ACK SET_JOULE_TARGET (app.py:466-469)
SET_PULSE,...                    → ACK SET_PULSE
SET_POWER,...                    → ACK SET_POWER
SET_PREHEAT,...                  → ACK SET_PREHEAT
SET_TRIGGER_MODE,...             → ACK SET_TRIGGER_MODE
SET_CONTACT_HOLD,...             → ACK SET_CONTACT_HOLD
SET_LEAD_R,{lead_r_mohm:.3f}     → ACK LEAD_R           (app.py:502-506)
```

### 2.3 Joule telemetry on weld completion (`WELD_DONE`)

When a weld finishes the STM32 emits a `WELD_DONE,key=val,...` packet, parsed in `_parse_event()` (`app.py:1823-1957`).

**Energy resolution priority** (`app.py:1846-1855`):
```python
if "energy_weld_j" in parsed:           joules = float(parsed["energy_weld_j"])
elif "energy_j" in parsed:              joules = float(parsed["energy_j"])
elif v_tips and avg_a and total_ms:     joules = v_tips * avg_a * (total_ms/1000.0)        # legacy
else:                                   joules = (vcap_b+vcap_a)*avg_a*total_ms/2000.0      # fallback
```

**Joule-specific fields** extracted (`app.py:1925-1937`):
- `joule_workpiece_j` — compensated energy delivered to the *workpiece* (this is the **control target**, i.e. leads/circuit losses already removed).
- `joule_total_j` — total integrated energy including leads/circuit.
- `joule_loss_j` — estimated non-workpiece loss.

The distinction between `joule_total_j` and `joule_workpiece_j` is exactly where **lead resistance compensation** is applied by the firmware: `loss ≈ I²·R_lead·t`. This is the conceptual hook for calibration — *if the lead-resistance value is wrong, `joule_workpiece_j` is wrong.*

The parsed result is broadcast to the UI as a `weld_complete` SocketIO event with the full payload (`app.py:1929-1955`).

### 2.4 Joule mode in the UI (`control.html`)

| Element | Detail | Location |
|---|---|---|
| Mode radio (`value=1`) | Selects Joule mode | `control.html:613` |
| `jouleTargetPanel` | Hidden unless Joule mode active | `control.html:618, 1048` |
| `jouleTargetSlider` | Range `0–300`, step 1, default 150 | `control.html:622` |
| `updateJouleTargetDisplay()` | Live label update | `control.html:1037` |
| D1 relabel | Becomes "Safety Timeout (ms)" in Joule mode | `control.html:~1055` |
| Status ingest | `control_mode` / `joule_target_j` parsed from live status & echoed back into UI (guards drag) | `control.html:1162-1172` |
| Payload | `control_mode` + `joule_target_j` included in every save | `control.html:1321-1322` |

---

## 3. Lead Resistance — Current Usage

Lead resistance (`R_lead`, in milliohms) models the resistance of the welding cables/leads between the capacitor bank and the weld tips. It is used by the STM32 to subtract lead/circuit losses from total energy when computing workpiece joules.

### 3.1 Constants & normalization

```python
# app.py:62-64
LEAD_RESISTANCE_DEFAULT_MOHM = 1.87
LEAD_RESISTANCE_MIN_MOHM     = 0.10
LEAD_RESISTANCE_MAX_MOHM     = 10.00
```

```python
# app.py:145-156
def _normalize_lead_resistance_mohm(value) -> float:
    # clamps to [MIN, MAX], rounds to 3 decimals; falls back to default on error
    ...
```

### 3.2 How it is transmitted to hardware

```python
# app.py:502-506 (command plan entry)
{
    "name": "lead_r",
    "cmd":  f"SET_LEAD_R,{lead_r_mohm:.3f}",
    "ack":  "LEAD_R",                       # NOTE: ACK key is "LEAD_R", not "SET_LEAD_R"
    "fields": ["lead_resistance_mohm"],
}
```

- Sent as part of the `save_settings` plan, gated by `_send_command_with_ack` (so it is retried and DENY-aware).
- **Caveat for any new code:** the ACK token is `LEAD_R` (asymmetric with the `SET_LEAD_R` command name). Reuse the existing plan entry rather than re-deriving the ACK string.

### 3.3 How it is stored

- Persisted in `settings.json` as `lead_resistance_mohm` (defaults injected in `load_settings()` `app.py:522-523`, `save_settings` path `app.py:540-542`, default dict `app.py:563`).
- **Explicitly excluded from presets** — `preset_data.pop("lead_resistance_mohm", None)` (`app.py:2189`). Lead resistance is treated as a hardware/rig property, not a recipe parameter. **A calibration result must therefore be written to settings, never to a preset.**

### 3.4 How it is displayed & protected in the UI

| Element | Detail | Location |
|---|---|---|
| `leadResistance` number input | default 1.87, min 0.10, max 10.00, step 0.01 | `control.html:773` |
| `leadResistanceLive` note | "Current STM32: -- mΩ" live echo | `control.html:775, 1595-1597` |
| `extractLeadResistanceMohm(data)` | Accepts `lead_r_mohm`, `lead_r_ohm`(×1000), `lead_resistance_mohm`, `lead_resistance`(×1000) | `control.html:835-852` |
| **Lead-R status guard** | `LEAD_R_STATUS_GUARD_MS = 5000`; prevents incoming STATUS from overwriting a value the user just edited, for 5 s | `control.html:806-833` |
| `leadRUserEditedSinceLastSave` | Flag set on input/change, cleared after successful save | `control.html:808, 826-833, 1369` |
| Save inclusion | `lead_resistance_mohm: parseFloat(...)` in `collectSettingsPayload()` | `control.html:1330` |

The **guard mechanism is important context for calibration**: a calibration routine that writes a new value into the `leadResistance` input must cooperate with (or temporarily reuse) this guard so live STATUS doesn't clobber the freshly computed value before it is saved.

### 3.5 Data flow summary

```
User edits input ──► collectSettingsPayload() ──► POST /api/save_settings
                                                       │
                          _normalize_lead_resistance_mohm()  (app.py:2042-2044)
                                                       │
                          _build_settings_command_plan() ──► SET_LEAD_R,x.xxx  (ACK LEAD_R)
                                                       │
                          save_settings() → settings.json
STM32 STATUS telemetry ──► extractLeadResistanceMohm() ──► leadResistanceLive label (guarded)
```

---

## 4. Integration Points for a Calibration Feature

A lead-resistance calibration routine would: (a) fire a known/controlled pulse, (b) read back voltage & current telemetry, (c) compute `R_lead = ΔV / I` (or integrate over the pulse), (d) write the result via `SET_LEAD_R`, and (e) persist it. Everything needed for (a)–(e) already exists on the Flask side:

| Need | Existing mechanism | Location |
|---|---|---|
| Trigger a controlled pulse | `POST /api/fire` → `FIRE`; `POST /api/arm`/`disarm`; `charge_on/off` | `app.py:2200-2237` |
| Read back V & I from the pulse | `WELD_DONE` parse → `vcap_b, vcap_a, avg_a, peak_a, v_tips, delta_v, total_ms` | `app.py:1823-1937` |
| Existing energy/loss math to mirror | `energy_weld_j`, `joule_total_j`, `joule_loss_j`, `joule_workpiece_j` | `app.py:1846-1937` |
| Reliable command + ACK | `_send_command_with_ack()` | `app.py:342-400` |
| Write the calibrated value | `SET_LEAD_R,{mohm:.3f}` (ACK `LEAD_R`) — reuse plan entry | `app.py:502-506` |
| Persist the value | `lead_resistance_mohm` in `settings.json` | `app.py:540-563` |
| Live push to UI | `weld_complete` / `status_update` SocketIO events | `app.py:1929-1955` |
| UI surface (input, live label, guard) | `leadResistance`, `leadResistanceLive`, Lead-R guard | `control.html:773-852` |
| Normalization/clamp | `_normalize_lead_resistance_mohm()` | `app.py:145-156` |

### 4.1 Proposed new server pieces (minimal, additive)

1. **`POST /api/calibrate_lead_r`** — orchestrates: optional charge → arm → fire a known pulse → wait for `WELD_DONE` → compute `R_lead` from telemetry → return the candidate value (do **not** auto-persist; let the user confirm). Mirror the structure of `api_save_settings` (`app.py:2011`) and reuse `_send_command_with_ack`.
2. **Calibration math helper** — `R_lead_mohm = (delta_v / avg_a) * 1000`, or a more robust integral form using the same fields the firmware already reports. Reuse `_normalize_lead_resistance_mohm()` for clamping.
3. **SocketIO `calibration_result` event** (optional) — push the computed value + raw telemetry to the UI live.
4. **UI: a "Calibrate" button** next to the `leadResistance` input — calls the endpoint, shows the measured value, and on user "Apply" writes it through the *existing* `save_settings` flow (reusing the Lead-R guard so the value sticks). There is currently **no `calibrat*` code anywhere** in the repo (verified by grep across `app.py`, templates, and static JS), so this is a clean greenfield addition.

### 4.2 What NOT to do

- Do **not** put the calibrated value into a preset (it is intentionally stripped, `app.py:2189`).
- Do **not** bypass `_send_command_with_ack` for the write — you lose retry/DENY safety.
- Do **not** assume the ACK token equals the command name (it is `LEAD_R`, not `SET_LEAD_R`).

---

## 5. Recommendation: Flask-first vs ESP32-first

### Recommendation: **Build the calibration concept on the Flask server first**, then migrate the proven algorithm to the ESP32 (or STM32) once validated.

### 5.1 Why Flask first

| Factor | Flask server | ESP32 firmware |
|---|---|---|
| Iteration speed | Edit Python + reload, instant | Recompile + reflash + reconnect per change |
| Existing plumbing | Full ACK pipeline, settings persistence, telemetry parse, **rich UI already present** | Transparent bridge only — would need new command handlers + state machine |
| Telemetry access | `WELD_DONE` already fully parsed into V/I/energy/loss fields | Would need to parse/aggregate STM32 telemetry itself |
| UI/visualization | HTML/JS UI with live charts, input, guards ready to extend | No native UI; would still rely on the server to display results |
| Math/experimentation | Trivial to try different formulas, log raw data, compare | Fixed-point/embedded constraints, harder to log & compare |
| Risk to hardware | Read-only orchestration via existing safe commands | Direct firmware changes risk breaking the real-time control loop |
| Reversibility | Pure additive routes/UI; easy rollback | Firmware regressions are higher-stakes |

### 5.2 Why migrate to ESP32/STM32 *eventually*

- **Authority:** The STM32 already computes `joule_workpiece_j` using `R_lead`; the *most accurate* calibration would ultimately live where the high-rate ADC sampling happens (STM32 `capturePulseAmpsForDurationUs`-style sampling). Network round-trips on the Pi add latency/jitter unsuitable for sub-millisecond integration.
- **Standalone operation:** If the rig is ever used without the Pi, on-device calibration is valuable.

But these are *optimizations*. The **algorithm, target pulse parameters, acceptance thresholds, and UX should be proven on Flask first**, where you can log raw telemetry and iterate in minutes. Once the formula and pulse recipe are validated against known-resistance references, port the computation down to firmware and keep the Flask UI as the front-end.

### 5.3 Migration path

1. **Phase 1 (Flask):** Add `/api/calibrate_lead_r` + UI button. Fire a known pulse, compute `R_lead` from `WELD_DONE` telemetry in Python, display & let user apply via existing `SET_LEAD_R` save flow. Log raw V/I for analysis.
2. **Phase 2 (validation):** Calibrate against shunts/known resistances; tune pulse parameters and the formula; confirm `joule_workpiece_j` accuracy improves.
3. **Phase 3 (firmware):** Once stable, implement a `CALIBRATE_LEAD_R` command on the STM32 that runs the measurement on-device and returns the computed value; the ESP32 passes it through; the Flask UI just triggers it and shows the result. The Flask-side math becomes a fallback/verification path.

---

## 6. Implementation Plan (Flask-first, additive & reversible)

> All steps are additive. No existing behavior is modified, satisfying the "don't break anything" constraint.

### Step 1 — Calibration math helper (`app.py`)
- Add `def _compute_lead_resistance_from_weld(parsed: dict) -> float | None` near `_normalize_lead_resistance_mohm` (`app.py:145`).
- Formula (start simple): `R_mohm = (delta_v / avg_a) * 1000.0`, guarded for divide-by-zero and minimum current. Clamp via `_normalize_lead_resistance_mohm`.

### Step 2 — Capture the most recent weld telemetry
- In `_parse_event()` (`app.py:1823`), also stash the parsed `WELD_DONE` dict into a module global (e.g. `last_weld_telemetry`) so the calibration endpoint can read it after a fire. (Additive — one assignment.)

### Step 3 — Calibration endpoint (`app.py`)
- Add `POST /api/calibrate_lead_r` modeled on `api_save_settings` (`app.py:2011`):
  1. Require `esp_link.connected`.
  2. (Optional) ensure charged to a target Vcap.
  3. Arm + `FIRE` (reuse `esp_link.send_command`), then wait for a fresh `WELD_DONE` (poll `last_weld_telemetry` timestamp with a timeout).
  4. Compute candidate `R_lead` via Step 1 helper.
  5. Return `{status, measured_mohm, raw_telemetry}` — **do not auto-save**.

### Step 4 — Live event (optional)
- Emit a `calibration_result` SocketIO event with the candidate value + raw V/I for transparency.

### Step 5 — UI (`control.html`)
- Add a **"Calibrate"** button beside `leadResistance` (`control.html:773`).
- Handler: confirm safety → `POST /api/calibrate_lead_r` → show measured mΩ → on **Apply**, set the input value, call `markLeadRUserEdited()` (reuse guard, `control.html:826`), then `saveSettings()` (`control.html:1342`) to push `SET_LEAD_R` + persist.
- Add a small results readout (measured value, current, ΔV) for operator confidence.

### Step 6 — Validation
- Test with `esp_link` disconnected (must 503 cleanly), with low current (must reject divide-by-zero), and against a known resistance.
- Confirm the computed value persists to `settings.json` and round-trips into `leadResistanceLive`.

### Step 7 — Version control
- Commit to the `Dev` branch with a clear message; open a PR for review (do not merge automatically).

### Files that would change (Phase 1)
| File | Change | Type |
|---|---|---|
| `rpi/server/app.py` | math helper + telemetry stash + `/api/calibrate_lead_r` | additive |
| `rpi/server/templates/control.html` | Calibrate button + handler + results readout | additive |

---

## Appendix A — Key reference locations

| Topic | File:Lines |
|---|---|
| ESP32 target config | `app.py:103-143` |
| Lead-R constants | `app.py:62-64` |
| Lead-R normalize | `app.py:145-156` |
| ACK/DENY parse | `app.py:211-267` |
| ACK wait bridge | `app.py:270-400` |
| Command plan (SET_MODE…SET_LEAD_R) | `app.py:458-507` |
| `load_settings` / `save_settings` | `app.py:518-590` |
| `settings_with_live_status` | `app.py:595-653` |
| `ESP32Link` class | `app.py:771-931` |
| Packet router `_handle_line` | `app.py:935-1065` |
| `_parse_status` / `_parse_status2` / `_parse_display` | `app.py:1067-1204` |
| `WELD_DONE` / Joule telemetry parse | `app.py:1823-1957` |
| `save_settings` route | `app.py:2011-2120` |
| arm/fire/charge routes | `app.py:2200-2237` |
| Connection manager + run | `app.py:2251-2319` |
| Joule UI | `control.html:613-622, 1037-1055, 1162-1172, 1321-1322` |
| Lead-R UI + guard | `control.html:773-852, 1330, 1595-1597` |
| Settings payload + save | `control.html:1312-1380` |

## Appendix B — Verified facts

- **No `calibrat*` code exists** anywhere in `app.py`, `templates/*.html`, or `static/*.js` (grep-verified). The calibration feature is greenfield.
- Lead resistance is intentionally **excluded from presets** (`app.py:2189`) — it is a rig/hardware property.
- The `SET_LEAD_R` command's ACK token is **`LEAD_R`** (asymmetric), `app.py:503-504`.
- Joule target is clamped **0–300 J** in three places; default **150 J**.
- Web server is **:8080**; ESP32 TCP target is **:8888** (verify against ESP32 firmware's listening port).
