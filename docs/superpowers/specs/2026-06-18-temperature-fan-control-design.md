# Temperature-driven bedroom fan control (AirGradient → DREO tower fan)

**Date:** 2026-06-18
**Status:** Approved — ready for implementation
**Area:** Home Assistant bedroom climate (`ansible/roles/containers/home-assistant`)

## Problem

Turn the DREO tower fan on and set its speed automatically from the bedroom temperature, with a
quieter ceiling overnight, a manual-override escape hatch, and a one-press "back to automatic"
control on the Tap Dial.

## Entities (verified from the live registry / recorder DB)

- **Temperature:** `sensor.bedroom_airgradient_one_temperature` — unit **°F** (~74 currently).
  The AirGradient ONE reading (not the fan's onboard `sensor.tower_fan_temperature`).
- **Fan:** `fan.tower_fan` (DREO `DR-HTF024S`) — `percentage_step ≈ 11.11` ⇒ **9 speed levels**
  (≈11/22/33/44/56/67/78/89/100%). Supports `fan.set_percentage`, on/off, preset modes,
  oscillation. Cloud-backed (`dreo` HACS integration).

## Behavior

### Temperature → speed bands (°F)

| Temp | Speed | Fan % (level) |
|---|---|---|
| < 72 | Off | — |
| 72–74 | Low | 33 (3) |
| 74–76 | Medium | 67 (6) |
| ≥ 76 | High | 100 (9) |

### Night cap

22:00–06:00 the target is clamped to **Medium (67%)** — a hot night gives Medium, never High.
Night is computed by hour: `now().hour >= 22 or now().hour < 6`.

### Anti-flap (hysteresis)

A **0.5°F deadband** at each boundary so jitter near a boundary never oscillates the fan, and the
fan is only commanded when the target level actually changes (no redundant set_percentage calls,
which also avoids spurious manual-detect triggers — see below).

Band selection (`cb` = the fan's current band 0–3, derived from its current percentage):

```
abs_band = 3 if t>=76 else 2 if t>=74 else 1 if t>=72 else 0   # pure rising thresholds
if abs_band >= cb:
    band = abs_band                       # rising (or unchanged) — jump straight to it
else:
    # would step down: only do so once t is 0.5°F below the current band's lower edge
    band = abs_band if t < [71.5, 73.5, 75.5][cb-1] else cb
band = min(band, 2) if is_night else band  # night cap
```

`cb` from current percentage: `<17→0, <50→1, <84→2, else→3` (midpoints of 0/33/67/100).
`band → percentage`: `[off, 33, 67, 100]`.

## Architecture

Mirror the lights' dispatcher pattern: one reusable script computes + applies the fan state;
thin automations trigger it. Lives in the existing templated static files.

### `script.bedroom_apply_fan` (new, in `files/scripts.yaml`)

Reads temp + clock + the fan's current level, computes `band` (above), then:
- `band == 0` → `fan.turn_off` (only if currently on).
- else → `fan.turn_on` + `fan.set_percentage` to the band's % (only if it differs from current).

Stateless and reusable. Does **not** check the override — callers gate that (same split as the
lights dispatcher: "what value" here, "whether to act" in the caller).

### `automation bedroom_fan_temperature` (new, in `files/automations.yaml`)

- **Triggers:** state change of `sensor.bedroom_airgradient_one_temperature`; time `22:00`; time
  `06:00` (the clock triggers make the night cap engage/release even when temp is steady).
- **Condition:** `input_boolean.bedroom_fan_manual` is `off`.
- **Action:** `script.bedroom_apply_fan`.
- `mode: single`.

### `automation bedroom_fan_manual_detect` (new — best-effort override setter)

- **Triggers:** `fan.tower_fan` `attribute: percentage`, `attribute: preset_mode`, `to: "on"`,
  `to: "off"` (deliberately NOT a bare state trigger — the fan's onboard `temperature` attribute
  drifts and would false-trigger).
- **Condition:** `trigger.to_state.context.parent_id == none` — i.e. the change was **not** caused
  by one of our automations/scripts (those carry a parent context).
- **Action:** `input_boolean.turn_on bedroom_fan_manual`.
- `mode: queued`.
- **Caveat:** a cloud-polled DREO can echo its own state with a fresh context, so this is
  best-effort and may occasionally mis-fire. It is bounded by the daily reset, and the toggle is
  also flippable by hand (a guaranteed manual pause). If it proves noisy, drop to explicit-toggle-
  only. The "only command on change" rule in the script reduces self-echo.

### `input_boolean.bedroom_fan_manual` (new helper, `configuration.yaml.j2`)

Manual-fan override, parallel to `bedroom_manual_off`. When on, `bedroom_fan_temperature` skips.

### Caller changes

- **Tap Dial button 3** (`bedroom_tap_dial_control`, `button_3_press`): **repurposed** from the
  Relax scene to the fan reset — `input_boolean.turn_off bedroom_fan_manual` then
  `script.bedroom_apply_fan` (which respects the night cap / "bedtime"). No longer touches the
  lights. The `bedroom_relax` scene stays defined in `scenes.yaml` (callable from the dashboard)
  but is unbound from the dial — same as the nightlight scene after button 4 was repurposed.
- **`bedroom_morning_reset`** (existing): also `input_boolean.turn_off bedroom_fan_manual` and
  call `script.bedroom_apply_fan`, so the fan override resets daily at 06:00/07:00 alongside the
  lights. Independent of presence.

## Data flow

```
temp change / 22:00 / 06:00 ─► bedroom_fan_temperature ─(override off)─► script.bedroom_apply_fan ─► fan
manual fan change (app/physical/UI) ─► bedroom_fan_manual_detect ─(not ours)─► bedroom_fan_manual = on  (suppresses temp automation)
Tap Dial button 3 ─► clear bedroom_fan_manual ─► script.bedroom_apply_fan
morning reset (06:00/07:00) ─► clear bedroom_fan_manual ─► script.bedroom_apply_fan
```

## Ansible / deploy

No new files/wiring beyond what exists — `scripts.yaml`, `automations.yaml`, and the
`input_boolean:` block in `configuration.yaml.j2` are already templated and feed
`common_config_changed`. Deploy: `uv run ansible-playbook ansible/deploy.yml --tags
"home-assistant"`; gate on `probe.py health home-assistant`; positively verify with
`docker exec home-assistant python -m homeassistant --script check_config --info script` (and
`--info automation`).

## Out of scope

- Not presence-gated (it tracks air temperature, runs whether or not the room is occupied).
- Oscillation and preset modes left as-is (only the night cap touches speed).
- No change to the lights system beyond freeing button 3.

## Verification

- `check_config` clean; `script.bedroom_apply_fan`, `bedroom_fan_temperature`,
  `bedroom_fan_manual_detect` parse; button 3 + morning reset reference the script.
- Force temps across boundaries (Developer Tools → States, or wait) → fan steps Low/Med/High and
  off below 72; deadband prevents oscillation at a boundary.
- During 22:00–06:00, a ≥76°F temp yields Medium, not High.
- Change the fan in the DREO app → `bedroom_fan_manual` flips on and the temp automation stops
  fighting it; press Tap Dial button 3 → override clears and the fan returns to the night-cap-aware
  band.
