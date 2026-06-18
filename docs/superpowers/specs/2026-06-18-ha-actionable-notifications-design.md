# HA actionable notifications (action buttons + tap dispatcher)

**Date:** 2026-06-18
**Status:** Approved — implementing
**Area:** Home Assistant bedroom (`ansible/roles/containers/home-assistant`)

## Problem

Add one-tap action buttons to notifications so the operator can respond without opening the app.
Three buttons (chosen after ruling out snooze — the engine never nags — and restart-offline-device —
the device is unreachable): **air-quality → "Boost fan"**, **away "left on" → "Turn back on"**
(undo a false-away), and **bedtime → "Start now"** (which needs a new nightly bedtime prompt to host
it).

## Goals / decisions

- **Reusable infra:** an `actions` field on `script.bedroom_notify` (pass-through to the companion
  app's `data.actions`) + one `mobile_app_notification_action` event dispatcher
  (`bedroom_notification_action`). Every future alert can sprout buttons for free.
- **Namespaced action ids** (`BEDROOM_*`) so they can't collide with other apps' actions.
- **Bedtime prompt:** fixed **22:00**, only if FP300-present **and** `bedroom_sleep_mode` off **and**
  home (nudge only when you're actually around and haven't started bedtime).
- Actions are **phone-only** (the watch mirror stays button-less).

## Architecture

### Component 1 — `actions` on `script.bedroom_notify` (`scripts.yaml`)

Add an optional `actions` field; include `actions: "{{ actions | default([]) }}"` in the phone
notify's `data:` (empty list ⇒ no buttons). Watch notify unchanged.

### Component 2 — `automation: bedroom_notification_action` (new, `automations.yaml`)

`mode: queued`. Trigger: `platform: event, event_type: mobile_app_notification_action`. `choose` on
`trigger.event.data.action`:
- `BEDROOM_BOOST_FAN` → engage `input_boolean.bedroom_fan_manual` + `fan.turn_on` +
  `fan.set_percentage: 100` (max circulation; the override makes it persist until button-3 / morning
  reset clears it). Honest caveat: moves air, doesn't lower CO₂.
- `BEDROOM_START_BEDTIME` → `script.bedroom_bedtime`.
- `BEDROOM_AWAY_TURN_ON` → `script.bedroom_apply_natural` + `script.bedroom_apply_fan` (restore
  lights + fan; called directly so they ignore the home-gates — a deliberate undo of a false-away).

### Component 3 — air-quality bad alert gets the Boost-fan button (`bedroom_threshold_alert`)

Add a `boost_actions` variable: `[{'action':'BEDROOM_BOOST_FAN','title':'Boost fan'}]` when
`category in ['airquality','airqualitysevere']` else `[]`. The bad branch passes
`actions: "{{ boost_actions }}"` to `bedroom_notify`. (Kept out of `cfg` to avoid bloating the map.)
Recovery passes no actions.

### Component 4 — away "left on" gets the Turn-back-on button (`bedroom_away`)

Its `bedroom_notify` call passes `actions: [{action: BEDROOM_AWAY_TURN_ON, title: "Turn back on"}]`.

### Component 5 — `automation: bedroom_bedtime_prompt` (new)

`mode: single`. Trigger `time: "22:00:00"`. Conditions: `binary_sensor.aqara_fp300_presence` on,
`input_boolean.bedroom_sleep_mode` off, `person.daniel` home. Action: `script.bedroom_notify` with a
`🌙 Ready for bed?` message, `tag: bedtime_prompt`, and `actions: [{action: BEDROOM_START_BEDTIME,
title: "Start now"}]`. Routine severity (no pierce/watch).

## Data flow

alert / prompt → `bedroom_notify(actions=[...])` → companion app renders buttons → tap fires
`mobile_app_notification_action` → `bedroom_notification_action` dispatches on the action id.

## Error handling / edge cases

- **No actions passed:** `actions | default([])` ⇒ no buttons (every existing alert stays
  button-less unless it opts in).
- **Boost fan persists** (via `bedroom_fan_manual`) until button-3 or the morning reset — intended;
  the user deliberately boosted.
- **Accidental "Turn back on" while genuinely away:** restores lights/fan that then stay on until
  you return (arrive-home nudge) — cost of a deliberate tap; acceptable.
- **Bedtime prompt while in DND:** routine, so delivered silently — fine.

## Testing (manual — repo has no HA unit harness)

- Before deploy: HA Developer Tools → YAML → Check Configuration.
- After deploy: confirm `bedroom_notification_action` + `bedroom_bedtime_prompt` load. Functional:
  fire a test notify with an action (Developer Tools → Actions → `script.bedroom_notify` with
  `actions`), tap the button on the phone, confirm the dispatcher runs (check the fan / lights /
  bedtime). Confirm the 22:00 prompt only fires when present + not in sleep mode.

## Files touched

- `ansible/roles/containers/home-assistant/files/scripts.yaml` — `actions` field on `bedroom_notify`
- `ansible/roles/containers/home-assistant/files/automations.yaml` — dispatcher + bedtime prompt + boost/away actions
- `ansible/roles/containers/home-assistant/CLAUDE.md` — document the actionable layer
- `ansible/PLANS.md` — move the item to done

HA-only deploy; all edits feed `common_config_changed`.

## Future / out of scope

- Timed boost (auto-revert after N min) — currently persists until reset (YAGNI).
- Buttons on the watch mirror.
