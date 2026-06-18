# HA home/away automations (person.daniel → off-while-away + arrive nudge)

**Date:** 2026-06-18
**Status:** Approved — implementing
**Area:** Home Assistant bedroom (`ansible/roles/containers/home-assistant`)

## Problem

The bedroom automations react to *room* presence (Aqara FP300) but have no concept of whether
anyone is *home*. So leaving the house leaves the fan running (the temperature automation has no
presence/home awareness at all) and can leave lights on if the FP300 falsely holds presence. The
operator wants: leaving home → bedroom lights + fan off with a notification of what was left on;
a "nobody home for ~30 min" failsafe; and a clean return — all while respecting the existing
`bedroom_manual_off` / `bedroom_fan_manual` overrides.

## Goals / decisions

- **Trigger source:** `person.daniel` (HA person entity — combines trackers, future-proofs a
  second device like a watch), triggered on `from: "home"` so it catches leaving to *any* away
  state (`not_home` or a named zone). `device_tracker.pixel_9_pro` and `zone.home` also exist;
  `person.daniel` reads identically today and is the idiomatic choice.
- **Two-stage leave** (GPS jitter demands a debounce): a 10-min "left home" reaction and a 30-min
  failsafe.
- **Arrive = nudge, not welcome-home:** resume the gated automations promptly, never force lights
  on when arriving mid-day in another room.
- **Respect overrides = never write them from home/away logic.** Leave turns off unconditionally
  (you're gone); arrive only acts via `apply_fan` / `apply_natural`, which honor the overrides
  internally. A deliberately-set `bedroom_manual_off` survives a leave/return cycle.

## Architecture

The crux: several existing automations turn the lights/fan **on with no home awareness**, so
"off while away" only holds if every on-path is gated. Three existing automations get a
`person.daniel == home` gate; two new automations add the leave/arrive behavior.

### Component 1 — gate the existing on-paths (`files/automations.yaml`)

| Automation | Change | Why |
|---|---|---|
| `bedroom_fan_temperature` | add condition `state person.daniel == home` | else the temp automation re-runs the fan seconds after the away-sweep turns it off |
| `bedroom_presence_on` | add condition `state person.daniel == home` | else an FP300 radar false-positive lights an empty house |
| `bedroom_morning_reset` | wrap its `script.bedroom_apply_fan` call in `if person home` | it calls the fan script *directly*, bypassing the gate above — else 06:00/07:00 turns the fan on in an empty house |

The morning *light* wake is already gated by `if FP300 present` (away ⇒ not present ⇒ no wake), and
`bedroom_absence_off` already kills lights 1 min after the room empties. So the away logic's unique
value is the **fan**, the **notify**, and catching a **stuck FP300 presence** while you're out.

### Component 2 — `automation: bedroom_away` (new)

`mode: single`. Two triggers, shared action (the approved two-stage):
- `leave`: `person.daniel` `from: "home"`, `for: "00:10:00"`.
- `failsafe`: `person.daniel` `from: "home"`, `for: "00:30:00"`.

Action:
- `variables.on_items` = a list built from `is_state('light.bedroom_lights','on')` →
  `"lights"` and `is_state('fan.tower_fan','on')` → `"fan"`.
- `choose` on `on_items | length > 0`:
  - turn off `light.bedroom_lights` + `fan.tower_fan` (unconditional — leaving always allows off;
    turning off an already-off entity is a harmless no-op).
  - notify `🏠 Left on` / `Turned off bedroom {{ on_items | join(' + ') }} (you're away)`,
    `data: {tag: bedroom_away}`.
  - if `on_items` is empty → no branch matches → silent no-op (so the 30-min failsafe stays quiet
    unless it actually caught something).

Each `from:"home"` trigger fires once per home→away transition once the away state has persisted
its `for:` window; returning home before the window cancels it (handles the mailbox/jitter bounce).

### Component 3 — `automation: bedroom_arrive_home` (new)

`mode: single`. Trigger: `person.daniel` `to: "home"`. Action (the "nudge"):
- if `bedroom_fan_manual` is off → `script.bedroom_apply_fan` (fan resumes immediately rather than
  waiting for the next temperature reading).
- if `binary_sensor.aqara_fp300_presence` is on **and** `bedroom_manual_off` is off →
  `script.bedroom_apply_natural` (covers "already standing in the bedroom when GPS catches up";
  gated on real presence so there's **no forced-on** when arriving elsewhere in the house).

## Data flow

Leave: `person.daniel` home→away → (10-min) `bedroom_away` off+notify → (30-min) failsafe re-sweep.
While away: the three gates keep the fan/lights from turning on. Arrive: `person.daniel` →home →
`bedroom_arrive_home` resumes fan + re-checks lights if present; the gated automations resume.

## Error handling / edge cases

- **GPS jitter / short trips:** the `for:` windows cancel on an early return home.
- **Lights vs. the fan:** `bedroom_absence_off` usually turns lights off 1 min after the room
  empties; `bedroom_away`'s light-off is the backstop for a **stuck FP300 presence** while away.
- **Overrides untouched:** verified by construction — leave writes no booleans; arrive routes
  through `apply_fan`/`apply_natural`, which read the overrides.
- **HA restart while already away (known limitation):** a `from:"home"` trigger needs a live
  transition, so a restart mid-absence misses stage-1/stage-2 for that window. Accepted: the three
  gates still prevent anything turning on while away, so it self-corrects without a periodic
  time-pattern sweep.
- **Arriving mid-day elsewhere in the house:** the arrive light re-check is gated on actual FP300
  presence, so no lights unless you're in the bedroom.

## Testing (manual — repo has no HA unit harness)

- Before deploy: HA Developer Tools → YAML → Check Configuration.
- After deploy: confirm both new automations exist and are `on`, and that the three modified
  automations still load. Functional check via `Developer Tools → States`: set
  `person.daniel` to `not_home` (with lights/fan on) and wait out a temporarily-shortened `for:`
  (or call the automation) → confirm off + notify; set back to `home` → confirm the fan resumes.
  Confirm the temp automation does **not** re-light the fan while `person.daniel` is `not_home`.

## Files touched

- `ansible/roles/containers/home-assistant/files/automations.yaml` — 3 edits + 2 new automations
- `ansible/roles/containers/home-assistant/CLAUDE.md` — document the home/away subsystem + the gates
- `ansible/PLANS.md` — move the item to done

`automations.yaml` feeds `common_config_changed`, so a deploy recreates HA (~120s). No
`configuration.yaml` change (no new helpers — overrides untouched). Z2M untouched.

## Future / out of scope

- **Unexpected-occupancy tripwire** (separate backlog item) — FP300 presence while `person.daniel`
  is away → alert. This design is its prerequisite (establishes the home/away signal + gates).
- **DND-aware routing** (separate item) — would wrap the away notify.
- Multi-occupant logic / a second `person.*` entity (single-person home today).
- A periodic time-pattern away-sweep (only needed if the HA-restart-while-away gap proves real).
