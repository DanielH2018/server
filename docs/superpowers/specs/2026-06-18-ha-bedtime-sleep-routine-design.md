# HA bedtime / sleep routine (Bedtime mode → quiet, dim sleep state)

**Date:** 2026-06-18
**Status:** Approved — implementing
**Area:** Home Assistant bedroom (`ansible/roles/containers/home-assistant`)

## Problem

Fire a "going to bed" action automatically off a real phone signal, with a manual fallback, that
sets the bedroom up for sleep: dim warm light + a quieter fan + Adaptive Lighting in sleep mode —
and unwind it at the morning wake.

## Goals / decisions

- **Trigger:** `binary_sensor.pixel_watch_3_bedtime_mode` → `on` (the Pixel's Bedtime mode, exposed
  by the Wear OS companion app — verified live). **Charging is NOT used** (the operator charges at a
  desk in the same room, so it's a false signal). Manual fallback: Tap Dial **button-1 hold**.
- **Single action** (not two-stage): one routine sets the full sleep state; the operator turns the
  lights fully off themselves via the dial when ready.
- **Fan stays temperature-responsive, just quieter.** Sleep mode adds a *lower cap* to the existing
  `bedroom_apply_fan` temperature logic — it does NOT freeze the fan at a fixed speed.
- **Respect existing overrides:** the fan re-apply is gated on `bedroom_fan_manual` (a manual fan
  setting wins); the routine only runs while `person.daniel == home`.

## Architecture

### Component 1 — `input_boolean.bedroom_sleep_mode` (new, `configuration.yaml.j2`)

The master "bedroom is in sleep mode" flag. Drives the fan's quiet cap (Component 2). Distinct from
Adaptive Lighting's own sleep-mode switch (which governs the *lights*); the routine sets both.

### Component 2 — quiet fan cap in `bedroom_apply_fan` (`scripts.yaml`)

`bedroom_apply_fan` already caps the fan band to index 2 (Medium) during 22:00–06:00. Generalize the
cap: `cap = 1 (Low) if sleep_mode else (2 if night else 3)`, then `band = min(band_raw, cap)`. So in
sleep mode the fan still computes its band from temperature (off when cold) but never exceeds **Low**
— quieter than the Medium night-cap, still responsive. One-line change to the `band` derivation plus
a `sleep` variable. The cap level is tunable (raise to 2/Medium for more cooling headroom).

### Component 3 — `script.bedroom_bedtime` (new, `scripts.yaml`)

The shared "going to sleep" action (a script so the automation and the dial-hold both call it, like
`bedroom_apply_natural`):
1. `input_boolean.turn_on` → `bedroom_sleep_mode` (engages the quiet fan cap).
2. `switch.turn_on` → `switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom` (AL
   output goes warm/dim — `sleep_brightness:1`, `sleep_color_temp:2200` from the AL config).
3. `scene.turn_on` → `scene.bedroom_nightlight` (immediate warm amber 3%; AL `take_over_control`
   releases the light to the scene).
4. If `bedroom_fan_manual` is off → `script.bedroom_apply_fan` (re-apply so the quiet cap takes
   effect immediately rather than waiting for the next temperature change).

`mode: single`. No once-per-night guard needed — the trigger is a single edge, the dial-hold is
deliberate, and the action is idempotent.

### Component 4 — `automation: bedroom_bedtime` (new, `automations.yaml`)

- Trigger: `binary_sensor.pixel_watch_3_bedtime_mode` → `on`.
- Condition: `person.daniel == home` (don't set the room up for sleep — and switch the fan on —
  while away).
- Action: `script.bedroom_bedtime`.

### Component 5 — Tap Dial button-1 hold (`bedroom_tap_dial_control`)

Add a `choose` case: `trigger.payload_json.action == 'button_1_hold'` → `script.bedroom_bedtime`.
(The exact RDM002 hold-action string verified against Z2M at implementation.)

### Component 6 — morning unwind (`bedroom_morning_reset`)

The morning reset already clears `bedroom_fan_manual` and re-applies the fan/lights. Add: turn OFF
`bedroom_sleep_mode` and AL sleep mode **before** those re-applies, so the fan recomputes without the
quiet cap and the wake ramp runs on AL's normal (non-sleep) curve. (When the watch-alarm-driven wake
lands later, this unwind moves there.)

## Data flow

Bedtime mode on (while home) / dial-hold → `script.bedroom_bedtime` → sleep_mode on + AL sleep mode
on + nightlight scene + quiet-capped fan. Overnight, `bedroom_fan_temperature` keeps running
(gated on `fan_manual` off + home) and `bedroom_apply_fan` honors the Low cap. Morning reset →
sleep_mode off + AL sleep mode off → normal fan + wake ramp.

## Error handling / edge cases

- **Bedtime mode on while in another room (home):** harmless — the room is set up for sleep; the fan
  runs at most Low. Gated on home so it never acts while away.
- **Hot night:** the Low cap means less cooling while asleep (the accepted quiet/cool trade-off);
  Tap Dial button 3 resets the fan to full auto on demand.
- **Manual fan setting active (`bedroom_fan_manual` on):** bedtime won't touch the fan (gated), but
  still sets sleep_mode/lights; the cap applies whenever auto control resumes.
- **`scene.bedroom_nightlight` vs AL:** the scene re-marks the light as manually controlled, so AL
  pauses; AL sleep mode ensures any later AL-driven light tonight is dim/warm.

## Testing (manual — repo has no HA unit harness)

- Before deploy: HA Developer Tools → YAML → Check Configuration.
- After deploy: confirm `input_boolean.bedroom_sleep_mode`, `script.bedroom_bedtime`, and
  `automation.bedroom_bedtime` exist and load. Functional: toggle `binary_sensor.pixel_watch_3_bedtime_mode`
  on via Developer Tools → States (or enable Bedtime mode on the phone) → confirm nightlight scene,
  AL sleep mode on, and the fan dropping to ≤Low; then run the morning reset (or set sleep_mode off)
  and confirm it unwinds. Verify Tap Dial button-1 hold also fires it.

## Files touched

- `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` — `input_boolean.bedroom_sleep_mode`
- `ansible/roles/containers/home-assistant/files/scripts.yaml` — quiet cap in `bedroom_apply_fan`; new `bedroom_bedtime`
- `ansible/roles/containers/home-assistant/files/automations.yaml` — new `bedroom_bedtime`; button-1 hold; morning unwind
- `ansible/roles/containers/home-assistant/CLAUDE.md` — document the bedtime subsystem
- `ansible/PLANS.md` — move the item to done

HA-only deploy; all edits feed `common_config_changed`.

## Future / out of scope

- Watch-alarm-driven morning wake (separate backlog item) — will take over the sleep-mode unwind.
- A "fully asleep → lights off" second stage (DND / `sleep_confidence`) — deferred (single-action chosen).
- Night-time "got up" dim nightlight (separate item) — composes with this via `scene.bedroom_nightlight`.
