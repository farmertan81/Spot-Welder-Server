# Flask Dashboard — ESP32 Feature Sync

Brings the Flask web dashboard (`templates/control.html`) in line with the new
ESP32 firmware features. The basic waveform graph (on the `/monitor` page) is
unchanged — this work only updates the **Control** page (`/`) UI and the data
that feeds it.

## What changed

### 1. New telemetry forwarded by the ESP32 (`ESP32P4/main/welder_main.cpp`)
`buildStatus()` (the periodic `STATUS,...` packet the ESP32 sends to Flask over
TCP) now also includes the WiFi and System fields that previously only existed
on the on-device Setup tab:

| Field | Meaning |
| --- | --- |
| `wifi_connected` | 1 = connected (STA) or AP up, 0 = offline |
| `wifi_ap_mode` | 1 = device is in setup-AP mode |
| `wifi_ssid` | network name (commas/equals sanitised to spaces) |
| `wifi_ip` | current IP address |
| `wifi_rssi` | signal strength in dBm (STA mode only) |
| `fw_version` | firmware version (`FW_VERSION`, e.g. `1.0.0`) |
| `chip_model` | ESP32 chip model reported by the bridge (e.g. `ESP32-P4`) |
| `flash_size` | flash chip size in bytes |
| `free_heap` | free heap in bytes |
| `uptime_s` | seconds since boot |

The Flask STATUS parser is a generic `k=v` passthrough, so these flow straight
through to the browser in the `status_update` event — **no Python parsing
changes were required.** (Verified end-to-end with a simulated STATUS line.)

### 2. Last Weld panel (4 values) — Control page
A new **Last Weld** panel shows, in a 2×2 grid:

- **Duration (ms)** — STM32-measured weld time (falls back to configured `d1`
  in time mode)
- **Peak current (A)**  — `peak_a`
- **Avg current (A)**  — `avg_a` *(now shown on the Control page too)*
- **Joules (J)**  — `energy_weld_j` (or `joule_workpiece_j` in Joule mode)

Driven by the existing `weld_complete` SocketIO event (already parsed by the
backend from the STM32 `WELD_DONE` message). The `/monitor` page already had
these four values; this adds them to the main Control dashboard.

### 3. WiFi Status panel — Control page
Shows Status (Connected / Setup AP / Disconnected, colour-coded), Network
(SSID), IP address, and Signal (RSSI in dBm; `n/a` while in setup-AP mode).
Driven by the new WiFi fields in `status_update`.

### 4. System Info panel — Control page
Shows Firmware version, ESP32 chip model, Uptime (formatted `1d 2h 3m`), Total
Welds (the persistent NVS counter), Flash size, and Free Heap.

### 5. Lead R display format → `X.Xm`
All **displayed** lead-resistance readouts now use the same compact format as
the ESP32 Setup tab via a new `formatLeadR()` helper:

- `2.055 mΩ` → **`2.1m`**
- `3.42 mΩ` → **`3.4m`**

Applied to: the live "Current STM32:" note, and the calibration modal's
result / previous / change values. The editable **input** field still accepts
values in mΩ (its label and range note keep the `mΩ` unit, since that's what
the user types).

### 6. Weld counter sync
The weld counter is the ESP32's persistent NVS value, forwarded as
`weld_count` in every STATUS. The dashboard simply displays it (in both the
Status card and the new System Info "Total Welds" row). Flask never resets it —
the ESP32 remains the single source of truth, so the count survives reboots and
reconnects.

### 7. Sacred User Input — preserved
The new panels are **display-only**: they read incoming telemetry into new
read-only elements and never touch the dirty-group gating
(`dirtyGroups` / `markGroupDirty` / `isGroupDirty` / `clearAllDirty`) or any
editable settings field. STATUS polling still cannot overwrite a field the user
is editing. Verified that all dirty-group infrastructure is unchanged.

## How to run the Flask server

```bash
cd Spot-Welder-Server
pip install -r requirements.txt

# Point it at your ESP32 (defaults to 192.168.1.77:8888 if omitted)
export ESP32_IP=192.168.1.42        # your welder's IP (shown on the Setup tab)
export ESP32_PORT=8888

python3 app.py
# → web UI on http://0.0.0.0:8080  (open http://<this-host>:8080/)
```

> The server auto-connects to the ESP32 in a background thread and keeps
> retrying, so you can start Flask before the welder is online.

## What's new in the UI (Control page `/`)

- **Last Weld** panel — Duration / Peak / Avg / Joules
- **WiFi Status** panel — Status / SSID / IP / RSSI
- **System Info** panel — Firmware / Chip / Uptime / Total Welds / Flash / Free Heap
- Lead-resistance readouts now read like the device: **`2.1m`**

## Testing checklist

Software (already verified here):
- [x] `app.py` compiles; Flask starts with no errors
- [x] `/` and `/monitor` both return HTTP 200
- [x] Inline JS passes `node --check`
- [x] Simulated STATUS line → all WiFi/System fields reach `status_update`
- [x] Simulated WELD_DONE → Duration/Peak/Avg/Joules reach `weld_complete`
- [x] `formatLeadR()` → `2.1m`, `3.4m` (matches task spec)
- [x] ESP32 firmware compiles (RAM 55.0%, Flash 22.8%)

On real hardware (please verify on the bench):
- [ ] Flash the ESP32 firmware so STATUS carries the new fields
- [ ] WiFi panel shows correct SSID / IP / RSSI when connected
- [ ] System panel shows firmware, chip, uptime ticking, total welds, flash
- [ ] Fire a weld → Last Weld panel updates with all 4 values
- [ ] Lead R shows as `X.Xm` in the live note and after calibration
- [ ] Edit a settings field while STATUS is streaming → your edit is NOT
      overwritten (Sacred User Input)
- [ ] Reboot the welder → Total Welds counter is unchanged (persistent)

## Status

**Compile-verified and smoke-tested in software only — NOT hardware-tested.**
The ESP32 firmware change must be flashed for the WiFi/System panels to
populate; until then those panels show `--` (the dashboard degrades gracefully).
