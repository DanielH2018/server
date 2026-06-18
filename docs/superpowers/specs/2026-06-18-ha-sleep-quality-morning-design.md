# HA sleep-quality-aware morning

**Date:** 2026-06-18
**Status:** Approved — implementing
**Area:** Home Assistant bedroom (`ansible/roles/containers/home-assistant`)

## Problem

Adapt the morning wake to how well you slept: after a short night (under ~6 h per
`sensor.pixel_9_pro_sleep_duration`), wake more gently and add a "you slept N h" note. Closes the
bedtime → dynamic-wake → sleep-aware loop.

## Goals / decisions

- **Soften, not delay** (the backlog offered either): lower the ramp *peak* for a short night —
  a one-variable change to `bedroom_apply_natural`'s morning exception. Delaying would have to shift
  `sensor.bedroom_wake_start` itself (moving the `morning_reset` trigger + `presence_on` window), so
  it's deferred. The window and `presence_on` are untouched here.
- **Threshold:** under 6 h (`0 < sleep_min < 360`) → gentler wake. Unknown/0 → normal (graceful).
- **Gentler = lower peak:** 30% instead of the usual 50% (same 1%→peak over the 15-min window).
- **Note:** a routine `bedroom_notify` at the wake — short night vs good morning + the hours.
- **Best-effort data:** Google's Sleep API finalizes `sleep_duration` around wake, so at
  alarm−15 min it may be stale/yesterday's. Falls back to a normal wake when unknown; the note
  reports whatever's there. Observe for a few mornings (like the air-quality tuning pass).

## Architecture

### Part 1 — sleep-aware ramp peak (`scripts.yaml`, `bedroom_apply_natural` morning exception)

In the morning exception's `variables:`, add:
```
sleep_min: "{{ states('sensor.pixel_9_pro_sleep_duration') | float(0) }}"
wake_peak: "{{ 30 if (0 < sleep_min < 360) else 50 }}"
```
and use `wake_peak` in place of the literal `50` in the brightness formula
(`brightness_pct = 1 + (wake_peak - 1) * elapsed / 900`). Transition unchanged (`900 - elapsed`).

### Part 2 — "you slept N h" note (`automations.yaml`, `bedroom_morning_reset`)

In the existing alarm + FP300-present wake block (after `script.bedroom_apply_natural`):
```
slept_h = sleep_duration / 60 (rounded 1dp); if slept_h > 0:
  bedroom_notify(
    title  = '😴 Short night' if slept_h < 6 else '☀️ Good morning',
    message= 'You slept {{ slept_h }}h' + (' — gentler wake.' if < 6 else '.'),
    tag    = sleep_morning)   # routine
```
Only on the `alarm` trigger + present (you're actually waking); skipped if `sleep_duration` is 0/unknown.

## Data flow

`sensor.pixel_9_pro_sleep_duration` → (apply_natural) ramp peak 30/50 + (morning_reset) note.

## Edge cases

- **sleep_duration unknown/0:** `0 < 0` false → normal peak 50; `slept_h > 0` false → no note.
- **Stale/yesterday value:** accepted (best-effort, soft impact — a slightly gentler or normal wake).
- **Away / not present at alarm:** the note is inside the present-gated wake block, so it won't fire
  if you slept elsewhere.
- **Note during DND:** routine → silent; you see it on waking. Fine.

## Testing (manual)

- Before deploy: HA Developer Tools → YAML → Check Configuration.
- After deploy: confirm `bedroom_apply_natural` + `bedroom_morning_reset` still load. Functional:
  set `sensor.pixel_9_pro_sleep_duration` < 360 via Developer Tools → States, run the morning wake
  (or button 4 in the wake window) → confirm the ramp peaks ~30% and the "short night" note; set
  ≥ 360 → normal 50% + "good morning".

## Files touched

- `ansible/roles/containers/home-assistant/files/scripts.yaml` — sleep-aware ramp peak
- `ansible/roles/containers/home-assistant/files/automations.yaml` — morning sleep note
- `ansible/roles/containers/home-assistant/CLAUDE.md` — document it
- `ansible/PLANS.md` — move the item to done

## Future / out of scope

- "Delay" the wake (vs soften) for a short night — would shift `sensor.bedroom_wake_start`.
- Richer sleep data via Health Connect (the phone Sleep API is what we have).
