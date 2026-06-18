# HA unexpected-occupancy tripwire

**Date:** 2026-06-18
**Status:** Approved — implementing
**Area:** Home Assistant bedroom (`ansible/roles/containers/home-assistant`)

## Problem

Alert when the bedroom is occupied (`binary_sensor.aqara_fp300_presence`) while the operator is
away (`person.daniel`) — "someone's in the bedroom and you're not home." Pure logic over two
already-trusted sensors; pairs with the home/away work.

## Goals / decisions

- **Trigger on the presence edge** (`off→on`, `for: 30s`) — a fresh detection, not a held state, so
  a GPS glitch while you're physically present can't fire it (presence already `on`, no edge).
- **Away guard:** `person.daniel` not `home`/`unknown`/`unavailable` AND has been away **>5 min** —
  filters brief away-glitch-then-return. (The fan being off while away already removes the main
  mmWave false-trigger source.)
- **Severity:** `watch: true` + `pierce: true` — a security event should reach you even in DND.
  Tunable (drop `pierce` if undesired).
- Alert-only (no "all clear" recovery — keeps it a one-shot, low-noise).

## Architecture

### Single `automation: bedroom_unexpected_occupancy` (`automations.yaml`)

```yaml
trigger: state binary_sensor.aqara_fp300_presence off→on, for: "00:00:30"
condition: template
  {{ states('person.daniel') not in ['home','unknown','unavailable']
     and (now() - states.person.daniel.last_changed).total_seconds() > 300 }}
action: script.bedroom_notify(title "🚨 Bedroom occupied",
  message "Motion detected in the bedroom while you're away.",
  tag unexpected_occupancy, watch true, pierce true)
```

`mode: single`.

## Edge cases

- **Leaving home:** by the time `person` flips away, you've already left the room → presence is
  `off`, so no edge → no false alarm. A later genuine detection while away does fire.
- **GPS false-away while present:** presence is already `on` (no edge) → no fire; and the >5-min
  away guard filters brief glitches.
- **mmWave false-positive while empty:** fan is off while away (no airflow); the 30s debounce drops
  momentary blips. A sustained false-positive could still alarm — tune the FP300 sensitivity or the
  debounce if it proves noisy.
- **`pierce` while away+DND:** intended — it's a security alert.

## Testing (manual)

- Before deploy: HA Developer Tools → YAML → Check Configuration.
- After deploy: confirm `automation.bedroom_unexpected_occupancy` loads. Functional: set
  `person.daniel` to `not_home` (Developer Tools → States) >5 min, then trigger FP300 presence →
  expect the alert (watch + pierce). Restore.

## Files touched

- `ansible/roles/containers/home-assistant/files/automations.yaml` — the tripwire automation
- `ansible/roles/containers/home-assistant/CLAUDE.md` — document it
- `ansible/PLANS.md` — move the item to done

## Future / out of scope

- Snapshot/camera capture (no camera in scope).
- An "all clear" recovery or escalation.
