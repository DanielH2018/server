# HA dynamic morning wake to the watch alarm

**Date:** 2026-06-18
**Status:** Approved — implementing
**Area:** Home Assistant bedroom (`ansible/roles/containers/home-assistant`)

## Problem

The morning wake ramp uses a hardcoded 06:00 (Mon–Fri) / 07:00 (Sat–Sun) window, duplicated across
three places (`script.bedroom_apply_natural`'s exception, `bedroom_presence_on`'s window template,
and `bedroom_morning_reset`'s time triggers) with a "keep in sync" comment. Drive it instead off the
operator's real alarm so the lights fade up ending *at* the alarm.

## Goals / decisions

- **Source = the WATCH alarm** `sensor.pixel_watch_3_next_alarm` (per the operator; the phone's
  `next_alarm` is unreliable — it surfaced a stray 9 PM value). Timestamp, device_class timestamp,
  enabled.
- **Ramp = 15 min, ending AT the alarm** (1%→50%), unchanged from today's curve — only the *start*
  becomes dynamic (`alarm − 15 min`).
- **Morning alarms only:** only alarms with a local hour in **[03:00, 11:00)** count as wake alarms;
  nap/evening alarms must not ramp the bedroom lights.
- **No-alarm-day fallback:** a fixed **09:00** reset still clears the overnight overrides (sleep
  mode, AL sleep, manual-off, fan-manual) so sleep state can't persist all day — but with **no light
  ramp** (you're sleeping in). Tunable.

## Architecture

### Component 1 — `sensor.bedroom_wake_start` (new template sensor, `configuration.yaml.j2`)

A new top-level `template:` section (none exists yet):

```yaml
template:
  - sensor:
      - name: "Bedroom wake start"
        unique_id: bedroom_wake_start
        device_class: timestamp
        availability: >-
          {% set na = states('sensor.pixel_watch_3_next_alarm') %}
          {{ na not in ['unknown', 'unavailable', 'none', ''] and 3 <= (as_datetime(na) | as_local).hour < 11 }}
        state: >-
          {{ (as_datetime(states('sensor.pixel_watch_3_next_alarm')) - timedelta(minutes=15)).isoformat() }}
```

`availability:` false (no alarm / non-morning alarm) → the sensor is `unavailable`, which makes the
time trigger not arm and the window templates evaluate false. The single source of truth for the
wake window `[wake_start, alarm)` — `alarm = wake_start + 15 min`.

### Component 2 — `bedroom_morning_reset` restructure (`automations.yaml`)

- **Triggers:** `platform: time, at: sensor.bedroom_wake_start, id: alarm` and
  `platform: time, at: "09:00:00", id: fallback`. The fixed 06:00/07:00 triggers and the
  weekday/weekend `condition:` are removed.
- **Action (both triggers — daily hygiene):** `input_boolean.turn_off` on
  `[bedroom_manual_off, bedroom_fan_manual, bedroom_sleep_mode]` + `switch.turn_off` AL sleep mode;
  then `if person home → script.bedroom_apply_fan` (re-apply now the sleep cap is cleared).
- **Action (`alarm` trigger only — the wake ramp):**
  `if trigger.id == 'alarm' and FP300 present → script.bedroom_apply_natural` (ramps because now is
  in the window). The 09:00 fallback deliberately never calls `apply_natural`, so it can't clobber a
  scene set after waking.

### Component 3 — read `wake_start` in the two window consumers

- `script.bedroom_apply_natural` morning exception: replace the `today_at('06:00')…` start with
  `as_datetime(states('sensor.bedroom_wake_start'))`; window = `0 <= (now() - wake_start) < 900`
  (guarded on the sensor being available). Ramp math unchanged
  (`brightness = 1 + 49 * elapsed/900`, `transition = 900 - elapsed`).
- `bedroom_presence_on` window template: same swap — `in_window` reads `sensor.bedroom_wake_start`
  instead of the hardcoded formula.

Both now read the same sensor, so they are **structurally** in sync — the "keep in sync" comments
are removed/updated.

## Data flow

`sensor.pixel_watch_3_next_alarm` → `sensor.bedroom_wake_start` (alarm − 15 min, morning-only) →
(a) `time` trigger on `bedroom_morning_reset` (id `alarm`) → reset + `apply_natural` (ramp);
(b) `apply_natural`'s exception window; (c) `presence_on`'s wake window. The 09:00 fallback trigger
handles no-alarm-day hygiene.

## Error handling / edge cases

- **No alarm / nap or evening alarm:** `wake_start` unavailable → time trigger doesn't arm, ramp
  exception false, presence window false. The 09:00 fallback still clears overnight overrides.
- **Alarm changed during the day / snoozed:** the template sensor recomputes and the time trigger
  re-arms automatically.
- **Alarm rings → next_alarm rolls to the following day:** `wake_start` updates; the trigger
  re-arms for the next morning (it already fired today).
- **Button-4 resume mid-ramp:** still works — `apply_natural` reads the live `wake_start`/elapsed.
- **Timezone:** `as_datetime` yields a UTC-aware datetime; the hour guard uses `as_local`; `now()`
  is tz-aware, so the elapsed math is correct.

## Testing (manual — repo has no HA unit harness)

- Before deploy: HA Developer Tools → YAML → Check Configuration.
- After deploy: confirm `sensor.bedroom_wake_start` exists and reads `alarm − 15 min` (≈05:45 for a
  06:00 alarm), and goes `unavailable` if the alarm is cleared / set to afternoon (test via the
  phone Clock). Confirm `bedroom_morning_reset` loads with the two new triggers. Functional: set a
  near-future morning alarm and confirm the ramp starts 15 min before and reaches ~50% at the alarm;
  confirm the 09:00 fallback clears `bedroom_sleep_mode` with no alarm.

## Files touched

- `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` — `template:` + wake-start sensor
- `ansible/roles/containers/home-assistant/files/automations.yaml` — `bedroom_morning_reset` + `bedroom_presence_on`
- `ansible/roles/containers/home-assistant/files/scripts.yaml` — `bedroom_apply_natural` exception
- `ansible/roles/containers/home-assistant/CLAUDE.md` — document the dynamic wake
- `ansible/PLANS.md` — move the item to done

HA-only deploy; all edits feed `common_config_changed`.

## Future / out of scope

- Sleep-quality-aware morning (separate item) — would soften/delay this ramp off `sleep_duration`.
- A second person's alarm / multi-occupant wake.
