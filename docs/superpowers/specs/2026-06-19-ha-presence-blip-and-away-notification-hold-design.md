# HA: presence "too bright" blip + away-aware notification hold

**Date:** 2026-06-19
**Status:** Approved — ready for implementation plan
**Scope:** `ansible/roles/containers/home-assistant/files/` (automations.yaml, scripts.yaml). Two
independent features in the bedroom suite; can be implemented/committed separately.

## Problem

1. When the operator walks into the bedroom but the room is too bright for auto-on, nothing
   happens — `bedroom_presence_on`'s condition on `binary_sensor.bedroom_auto_light_allowed`
   fails silently. There's no feedback that presence was detected. Want a small "blip" of the
   lights to acknowledge it.
2. Non-critical notifications fire to the phone regardless of whether the operator is home. Want
   them to **wait** while outside the home geofence, and — if the underlying condition **resolves
   before return** (e.g. a sensor goes offline then comes back) — to be **dropped entirely**, not
   replayed.

## Feature 1 — Arrival "too bright" blip

### Behavior

On the arrival edge (`binary_sensor.aqara_fp300_presence` → `on`), if the lights *would* have come
on but the lux gate is blocking them, flash the lights softly to acknowledge presence:
**off → 15% warm (2700K, ~1s) → off.**

### New script — `script.bedroom_blip` (scripts.yaml)

Inverted twin of the existing `script.bedroom_alert_pulse`. Because it only ever runs when the
lights are already **off**, it needs no `scene.create` snapshot — a plain `light.turn_off` restores
the known prior state.

```yaml
bedroom_blip:
  alias: "Bedroom — too-bright acknowledgement blip"
  mode: single          # ignore re-triggers mid-blip
  sequence:
    - service: light.turn_on
      target: {entity_id: light.bedroom_lights}
      data: {brightness_pct: 15, color_temp_kelvin: 2700, transition: 0.4}
    - delay: "00:00:01"
    - service: light.turn_off
      target: {entity_id: light.bedroom_lights}
      data: {transition: 0.6}
```

### New automation — `bedroom_presence_blip` (automations.yaml)

Sibling of `bedroom_presence_on`, sharing the **arrival** trigger only (NOT the dusk lux-crossing
trigger — that path is when lights *do* come on), with the lux gate **inverted**.

```yaml
- id: bedroom_presence_blip
  alias: Bedroom presence blip (too bright)
  mode: single
  trigger:
    - platform: state
      entity_id: binary_sensor.aqara_fp300_presence
      to: "on"
  condition:
    - {input_boolean.bedroom_manual_off == "off"}          # didn't deliberately kill the lights
    - {person.daniel == "home"}                            # never blip an empty house (radar false-trip)
    - {binary_sensor.aqara_fp300_presence == "on"}         # still occupied
    - {binary_sensor.bedroom_auto_light_allowed == "off"}  # INVERTED gate: blocked because too bright
    - {light.bedroom_lights == "off"}                      # only "won't turn on" makes sense when off
  action:
    - service: script.bedroom_blip
```

(Conditions written in shorthand above; implement as standard `condition: state` blocks.)

### Design rationale

- **Separate automation, not an `else` in `presence_on`.** `presence_on` is `mode: single` and
  triggers on two edges (arrival + dusk lux-crossing); the blip is arrival-only. Splitting keeps
  each automation single-purpose. The two are mutually exclusive per arrival: `auto_light_allowed`
  is either on (→ `presence_on` lights up) or off (→ `presence_blip` flashes).
- **No feedback loop.** The blip fires only when `auto_light_allowed` is `off`, i.e. illuminance
  ≥ 50 from ambient daylight (the genuinely bright case). A ~1s blip cannot trip `presence_on`'s
  `below: 50 for: 30s` lux trigger, and the room returns to its bright ambient reading after.
- **No cooldown initially.** With the FP300 tuning fixed and `absence_off` requiring 1 min empty
  before presence re-arms, repeated blips should be rare. Add a `for:` debounce only if chatty.

## Feature 2 — Hold non-critical notifications while away

### Behavior

- **Critical tier = `pierce`.** `pierce` notifications (severe air quality, UPS low-battery,
  unexpected-occupancy security tripwire) always deliver immediately, away or not — unchanged.
- **Everything else, while away** (outside the home geofence): parked, not pushed.
- **Resolve-while-away cancels.** A recovery ("all clear") arriving while away deletes the parked
  alert instead of being delivered — so a condition that self-resolves before return is never seen.
- **On arrival home:** still-parked alerts are delivered as **one summary digest**
  ("While you were out (N)" + bulleted messages), then cleared.

### Queue mechanism — persistent notifications

Each held alert is parked as `persistent_notification.create(notification_id="hold_<tag>", ...)`.
Persistent notifications are real entities with `title`/`message` attributes, so the flush can
template over them; tag-keyed dismissal is one service call; zero new helpers.

Rejected alternatives: `input_text` JSON (255-char cap, can't hold multiple); per-tag boolean/text
helpers (rigid, new helper per alert family).

### Changes to `script.bedroom_notify`

Add two optional fields: `recovery` (bool, default false) and reuse existing `pierce`. New computed
variable:

```yaml
away: "{{ states('person.daniel') not in ['home', 'unknown', 'unavailable'] }}"
```

`away` **fails open**: true only when the geofence *explicitly* places the operator elsewhere
(`not_home`/another zone). On `unknown`/`unavailable` (tracker glitch) it's false → over-notify
rather than silently swallow. (Contrast the unexpected-occupancy tripwire, which treats unknown as
away — opposite safe default because the cost of being wrong is opposite.)

Routing at the top of the sequence (before the existing push path):

```
if pierce OR not away:
    → existing normal path (phone push + optional watch + pierce persistent_notification)  [unchanged]
else:   # away AND non-critical
    if recovery:
        → persistent_notification.dismiss(notification_id="hold_<tag>")   # cancel held alert
        → stop
    else:
        → persistent_notification.create(notification_id="hold_<tag>", title=title, message=message)
        → stop
```

Tag-keyed: a re-fired alert (same tag) updates the parked copy in place; a recovery (same tag,
`recovery: true`) deletes it.

### Marking recoveries

Add `recovery: true` to the existing recovery `bedroom_notify` call-sites:

- `bedroom_sensor_offline_alert` — the "back online" branch (the sensor-off-then-on example)
- `bedroom_threshold_alert` — the moderate-category "back to normal" recovery
- `ups_power_event` — the "power restored" edge

Alerts with no recovery signal (battery, humidity, away "left on", update digest, calibration
reminder, bedtime prompt) stay parked and flush on return — correct, nothing resolves them.

### New automation — `bedroom_flush_held_notifications` (automations.yaml)

```yaml
trigger:  person.daniel  to: "home"
condition: at least one persistent_notification entity whose object_id starts with "hold_"
action:
  - build a bulleted summary from those entities' message attributes (templated)
  - notify.mobile_app_pixel_9_pro:  title "While you were out (N)", message = bullets
  - dismiss each held persistent_notification
```

Digest example:

```
While you were out (2)
• CO₂ was high
• FP300 battery low
```

Phone-only digest; loses per-alert action buttons (e.g. "Boost fan") and individual tags — accepted,
since the vast majority of held alerts are informational. Tap into HA to act on anything actionable.

### Edge cases (accepted)

| Case | Behavior |
|---|---|
| HA restarts while away (e.g. a deploy) | Persistent notifications are in-memory → held queue lost. Accepted: held items are non-critical; deploy-while-away overlap is rare. Known limitation. |
| Alert delivered *before* leaving, resolves while away | No `hold_<tag>` exists; recovery dismissed silently. No late "all clear" — fine for non-critical. |
| `pierce` alert while away | Unchanged: push + watch + its own (non-`hold_`) persistent_notification. Flush scans `hold_*` only — no collision. |
| Arrive with nothing held | Flush condition false → no "you're back" spam. |

## Testing / verification

- **Deploy:** `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"` (recreates HA
  ~120s via `common_config_changed`).
- **Blip:** with room bright (illuminance ≥ 50, `auto_light_allowed` off) and lights off, trip
  FP300 presence → observe one soft warm flash; lights return off. Verify no flash when
  `manual_off` on, when away, or when `auto_light_allowed` on (normal turn-on instead).
- **Hold:** set `person.daniel` away; fire a non-critical alert (e.g. trip an offline sensor) →
  confirm no phone push and a `hold_<tag>` persistent notification appears in HA. Fire its recovery
  → confirm the `hold_<tag>` disappears and no push. Set `person.daniel` home with one held item →
  confirm the "While you were out" digest pushes and the held notification clears.
- **Critical bypass:** while away, fire a `pierce` alert → confirm immediate push (not held).
- **Verification gotchas (from CLAUDE.md):** query automations by alias-slug not id; the HA
  recorder is stale right after a restart — confirm liveness via container `StartedAt` /
  `last_triggered`, not recorder timestamps.

## Out of scope

- Surviving HA restart for the held queue (durable file/store) — deferred.
- Per-alert action buttons in the digest — chose summary over individual replay.
- Extending the hold to DND/sleep quieting — that layer is separate and unchanged.
