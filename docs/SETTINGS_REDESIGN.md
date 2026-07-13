# Spot Welder — Settings Management Redesign (Draft / Apply / Sync)

> **Status: compile-verified only.** The ESP32 firmware builds cleanly with
> `pio run`, and the Flask page passes `node --check` (JavaScript) and
> `python3 -m py_compile` (server). **None of this has been bench-tested on real
> hardware.** Please validate on the actual welder using the test procedure at
> the end of this document before relying on it.

---

## 1. The problem

Two user-facing bugs, same underlying cause — **live STATUS telemetry was
fighting the user's own edits**:

### Flask web UI
- You change a setting (pulse time, joule target, power, preheat, lead R), but
  it "snaps back" almost immediately.
- Root cause: the STM32 streams a `STATUS` line continuously. The browser's
  `applyStatusSettings()` blindly wrote every value from STATUS back into the
  form fields. The only protections were *time-window* guards that started **on
  Save** (not on edit) plus "is this field focused / is a slider being dragged"
  checks. As soon as a field lost focus — which happens the instant you click
  **Save** — the next STATUS frame (arriving every few hundred ms) overwrote
  your value with the STM32's *old* value before the save round-trip finished.

### ESP32 touchscreen
- On the **Joule** tab, pressing **APPLY** changed the button label to
  `APPLIED` permanently. Nothing ever reset it, so the button looked dead and
  you couldn't tell whether a second apply would work.
- Root cause: `on_joule_apply()` called `lv_label_set_text(lbl_joule_apply,
  "APPLIED")` and never restored it. The Joule draft was also seeded **once**
  (`_joule_draft_init`) and then never re-synced from STATUS, so a change made
  on the web UI never appeared on the Joule tab.

The **Pulse** tab on the ESP32 already worked correctly — it used a proper
`applied / draft / dirty` model. The fix was to make everything else follow
that same proven pattern.

---

## 2. The new model — "user input is sacred"

A single mental model now governs **both** UIs:

```
            ┌──────────────┐  edit   ┌──────────────┐  Apply/Save   ┌─────────────┐
   STATUS → │  applied[]   │ ──────▶ │   draft[]    │ ────────────▶ │  send to    │
   sync     │ (last known  │         │ (what the    │   wait for     │  STM32      │
   (clean)  │  STM32 truth)│ ◀────── │  user sees / │   confirm      │ (authority) │
            └──────────────┘  re-sync│  is editing) │ ◀───────────── └─────────────┘
                               after  └──────────────┘   STATUS
                               apply       │  ▲
                                  dirty ────┘  │ clean
                                               │
                                   dirty == (draft != applied)
```

Rules (all six original design principles map onto this):

1. **User input is sacred.** While a group is *dirty* (edited but not applied),
   incoming STATUS is **not** allowed to write those fields.
2. **Dirty/draft mode suspends STATUS for those fields only.** Other groups keep
   syncing normally.
3. **Apply = send → wait for confirmation → clear draft.** On the web UI the
   server applies each command, waits for the STM32 ACK, persists
   `settings.json`, and only then returns success; the browser then clears
   dirty. On the ESP32 the apply callback sends the command and optimistically
   marks the draft as applied (button turns green); the next authoritative
   STATUS re-confirms it.
4. **You can always edit and apply again.** Nothing latches into a permanent
   "applied" dead state — the APPLY button is purely a function of
   `dirty` and is re-evaluated on every repaint.
5. **Both UIs use the same logic** (applied/draft/dirty + clear-on-confirm).
6. **Clear visual feedback:** ESP32 APPLY button is **red + "APPLY"** when there
   are unsaved changes, **green + "SYNCED"** when the draft matches the STM32.
   The web UI shows `Settings saved ✓` on confirmed apply.

The **STM32 (with its Flash) remains the single source of truth.** After any
apply, both UIs re-converge on the STM32's reported values.

---

## 3. What changed — file by file

### 3a. ESP32 firmware — `Spot-Welder/Spotwelder Full/src/ui.cpp`

**Joule tab rebuilt to mirror the Pulse tab.**

1. **New applied-state globals** (after the existing pulse globals):
   `_joule_applied_mode`, `_joule_applied_target`, `_joule_applied_maxms`,
   `_joule_applied_init`, `_joule_draft_dirty`. These hold the last known STM32
   truth for the Joule group, exactly like the pulse `applied_*` set.

2. **`paint_joule_tab()`** now calls a new helper `update_joule_dirty()` at the
   top. It compares the live draft (mode, joule target with a 0.5 J tolerance,
   max-ms) against `_joule_applied_*` and sets `_joule_draft_dirty`. The APPLY
   button is then driven entirely by that flag:
   - dirty → `C_RED`, label **"APPLY"**
   - clean → `C_GREEN`, label **"SYNCED"**
   The old, permanently-stuck `"APPLIED"` label is gone.

3. **`on_joule_apply()`** no longer latches the label. It now:
   - calls the apply callback (sends `SET_MODE` / `SET_JOULE_TARGET` /
     `SET_JOULE_MAX` to the STM32 via the ESP32 → UART link),
   - **optimistically** copies the draft into `_joule_applied_*` so the button
     immediately turns green/"SYNCED",
   - repaints. The button stays clickable for the next edit/apply.

4. **`ui_update()` Joule block** replaced the one-shot `_joule_draft_init` seed
   with a proper STATUS sync that mirrors the pulse logic: it tracks
   `_joule_applied_*` from `st.control_mode` / `st.joule_target_j` /
   `st.joule_max_ms`, and copies *applied → draft* **only when the draft is
   clean** (`!_joule_draft_dirty`) or on first init. This is what lets a change
   made on the **web UI** appear on the **Joule tab** after it's applied, while
   never stomping an in-progress on-screen edit.

> The Config tab (trigger mode / contact hold / lead R) was already
> *immediate-apply* (no draft) and was **not** broken, so it was left as-is.

### 3b. Flask web UI — `Spot-Welder-Server/templates/control.html`

1. **Dirty-group infrastructure** (new): a `dirtyGroups` `Set`, a
   `fieldGroupMap` that maps each control id to one of five groups
   (`pulse`, `joule`, `power`, `preheat`, `lead_r`), and helpers
   `markGroupDirty()`, `markFieldDirty(fieldId)`, `isGroupDirty()`,
   `clearAllDirty()`.

2. **`applyStatusSettings()` rewritten** so each group is gated by
   `isGroupDirty(group)`. If a group is dirty, STATUS will **not** touch any of
   its fields. The legacy focus / slider-drag / time-window guards were kept as a
   secondary safety net for the brief window around an in-flight save.
   - Pulse mode + d1/gap1/d2/gap2/d3 → gated by `pulse`.
   - control_mode + joule target → gated by `joule`.
   - power → gated by `power`; preheat fields → `preheat`; lead R → `lead_r`.
   - **Trigger mode + contact hold stay immediate-apply** (ungated) — they're
     not draft-based.

3. **Listener wiring:** every editable control now calls `markFieldDirty(id)`
   on `input`; the control-mode radios mark `joule` dirty; the pulse `mode`
   select marks `pulse` dirty on real user change.

4. **Clear-on-confirm:** `saveSettings()` calls `clearAllDirty()` in its success
   path (after the server confirms the apply + ACK + persist). STATUS sync then
   resumes and re-syncs the form to the STM32's authoritative values.

5. **No false dirties from programmatic updates:** STATUS-driven mode sync now
   calls `onModeChanged()` directly instead of dispatching a synthetic `change`
   event (which would have wrongly marked the pulse group dirty). `loadSettings()`
   calls `clearAllDirty()` right after loading authoritative values.

---

## 4. Why this fixes both bugs

- **Flask snap-back:** the moment you edit a field its group is dirty, so every
  STATUS frame skips that group. Your value survives until you click Save. The
  server applies + confirms, then `clearAllDirty()` lets STATUS resume — now the
  fields match the STM32 you just programmed, so nothing visibly changes.
- **Flask edit → apply → edit → apply again:** dirty is set on every `input`,
  cleared on every confirmed save, with no latch — so the cycle repeats forever.
- **ESP32 stuck APPLY:** the button is a pure function of `_joule_draft_dirty`,
  recomputed on every repaint, so it returns to a usable **APPLY** (red) state
  as soon as you change something, and shows **SYNCED** (green) when matched.
- **Cross-UI sync after apply:** because both sides re-sync *applied → draft/
  fields* only when clean, a change applied on one UI flows through the STM32's
  STATUS to the other UI without ever interrupting an active edit there.

---

## 5. Testing procedure (run on real hardware)

> Reminder: the changes are **compile-verified only**. Do these on the bench.

Prep: power the welder, connect the ESP32 touchscreen, open the Flask page in a
browser, and confirm the connection indicator is green and live STATUS is
flowing.

### Scenario 1 — Flask: change → apply → it sticks
1. On the web UI change a pulse time (e.g. d1) and the joule target.
2. Click **Save**. Expect `Settings saved ✓`.
3. Watch for 10–15 s. **Pass:** the values stay; they do **not** snap back.

### Scenario 2 — Flask: change → apply → change → apply (repeatable)
1. Do Scenario 1.
2. Immediately change the same fields to new values and **Save** again.
3. **Pass:** the second save also sticks; Save is never disabled/dead.

### Scenario 3 — ESP32: change → apply → it sticks
1. On the Joule tab change the target (and/or mode).
2. Button should turn **red / "APPLY"**. Press it.
3. **Pass:** button turns **green / "SYNCED"**, value holds, no `APPLIED` lock.

### Scenario 4 — ESP32: change → apply → change → apply (repeatable)
1. Do Scenario 3.
2. Change the target again — button returns to **red / "APPLY"**.
3. Press APPLY again. **Pass:** it applies and returns to **green / "SYNCED"**.

### Scenario 5 — Flask change → ESP32 reflects it (after apply)
1. On the web UI change the joule target / control mode and **Save**.
2. Look at the ESP32 Joule tab (do not touch it).
3. **Pass:** within a STATUS cycle the Joule tab shows the new value and
   **SYNCED** (green). If you were mid-edit on the ESP32, your edit is preserved
   instead (that's correct).

### Scenario 6 — ESP32 change → Flask reflects it (after apply)
1. On the ESP32 Joule tab change a value and press **APPLY** (→ SYNCED/green).
2. Look at the web UI (do not touch the relevant field).
3. **Pass:** within a STATUS cycle the web UI shows the new value. If you were
   mid-edit on the web UI for that group, your edit is preserved instead.

### Extra edge checks
- Edit a field on the web UI and **don't** save; on the ESP32 apply a different
  group. **Pass:** your unsaved web edit is untouched; the other group updates.
- Drag a slider on the web UI while STATUS is flowing. **Pass:** the slider
  doesn't jump under your finger.

---

## 6. Build / validate commands

```bash
# ESP32 firmware (path has a space — keep the quotes)
cd "Spot-Welder/Spotwelder Full"
export PATH="$HOME/.local/bin:$PATH"
pio run                       # -> SUCCESS

# Flask server
cd Spot-Welder-Server
python3 -m py_compile app.py  # -> OK
# JavaScript in the template is validated by extracting the <script> block
# and running `node --check` on it -> JS SYNTAX OK
```
