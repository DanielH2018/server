# HA DND-aware notification routing + severe air-quality tier

**Date:** 2026-06-18
**Status:** Approved — implementing
**Area:** Home Assistant bedroom (`ansible/roles/containers/home-assistant`)

## Problem

Respect "do not disturb" / sleep: routine alerts (battery, humidity, moderate air quality,
sensor-offline) should arrive **silently** while the operator is in DND or sleep mode, while a
genuinely urgent condition can still **pierce** with sound. The only condition urgent enough to wake
through DND is **air quality at especially elevated levels** — a tier above the current "bad"
threshold. Watch routing (glanceable wrist buzz) and DND-pierce (wake with sound) are **decoupled**.

## Goals / decisions

- **Two independent flags per alert:**
  - `watch` — also mirror to the Pixel Watch (glanceable). Unchanged from the earlier work:
    sensor-offline + any air-quality bad.
  - `pierce` — sounds even while quiet (high-importance channel that can bypass DND). **Only
    air-quality severe.**
- **"Quiet"** = phone DND on (`sensor.pixel_9_pro_do_not_disturb_sensor` not `off`) **OR**
  `input_boolean.bedroom_sleep_mode` on (composes with bedtime, like the nightlight).
- **Soften = silent delivery** (low-importance channel), not hold/queue (YAGNI — no delayed-delivery
  queue).
- **Severe air-quality is escalation-aware** (a second threshold sensor per pollutant, not a value
  check at alert time), so a gradual overnight CO₂ climb gives a routine nudge at the normal line
  then a critical wake-up only if it crosses the severe line. Cutoffs (tunable, join the
  ~2026-06-25 pass): CO₂ 2000, PM2.5 100, VOC 400, NOx 200.

## Architecture

### Component 1 — 4 "severe" air-quality `threshold` sensors (`configuration.yaml.j2`)

Added to the `binary_sensor:` list: `bedroom_co2_severe` (upper 2000, hyst 100),
`bedroom_pm2_5_severe` (upper 100, hyst 5), `bedroom_voc_severe` (upper 400, hyst 25),
`bedroom_nox_severe` (upper 200, hyst 10). Same sources as the moderate ones.

### Component 2 — `script.bedroom_notify` (new, `scripts.yaml`)

The single notification-routing layer every bedroom alert calls.

```
fields: title, message, tag, watch (default false), pierce (default false)
quiet      = DND-sensor not in [off,unknown,unavailable]  OR  bedroom_sleep_mode on
importance = 'high' if pierce else ('low' if quiet else 'default')
channel    = 'Bedroom critical' if pierce else 'Bedroom alerts'
-> notify.mobile_app_pixel_9_pro (title/message, data: {tag, channel, importance})
-> if watch: notify.pixel_watch_3 (title/message, data: {tag})
```

`mode: parallel`. Flags defaulted via `| default(false)` so a caller may omit them. **One-time phone
setup:** to actually pierce DND, mark the "Bedroom critical" channel as a DND exception in Android
(Settings → Apps → Home Assistant → Notifications) after the first critical alert creates it —
high-importance alone is not enough.

### Component 3 — `bedroom_threshold_alert` refactor (`automations.yaml`)

- **`cfg` map** gains `watch` / `pierce` / `recovery` per category and a new **`airqualitysevere`**
  category:
  | category | bad title | pulse | watch | pierce | recovery |
  |---|---|---|---|---|---|
  | airquality | ⚠️ Air quality | yes | yes | no | yes |
  | airqualitysevere | 🚨 Air quality HIGH | yes | yes | **yes** | **no** |
  | battery | 🔋 Battery low | no | no | no | yes |
  | humidity | 💧 Humidity | no | no | no | yes |
  (`recovery:false` on severe avoids a redundant "severe cleared" notice — the moderate recovery
  covers "back to normal".)
- **+2 trigger blocks** (`airqualitysevere_bad` / `_ok`) over the 4 severe sensors (`for: 00:00:30`).
- **Bad branch:** `script.bedroom_notify(title=cfg.bad, message=<bad>, tag, watch=cfg.watch,
  pierce=cfg.pierce)`; then `if cfg.pulse and lights on → script.bedroom_alert_pulse`.
- **Recovery branch:** `if cfg.recovery → script.bedroom_notify(title=cfg.ok, message=<ok>, tag)`
  (always routine — no watch/pierce).
- **Removes** the inline `notify.mobile_app_pixel_9_pro` + duplicated `notify.pixel_watch_3` blocks
  (now inside `bedroom_notify`). Category parsing (`trigger.id.split('_')[0]`) unchanged — the new
  category name `airqualitysevere` has no underscore.

### Component 4 — route the remaining alerts through `bedroom_notify`

- `bedroom_sensor_offline_alert`: offline → `bedroom_notify(watch=true)` (routine for DND — no
  pierce); recovery → `bedroom_notify()` (routine). Removes its inline phone+watch notifies.
- `bedroom_away`: "left on" → `bedroom_notify()` (routine). One notify path across the system.

## Data flow

alert automation → `script.bedroom_notify(watch, pierce)` → phone (channel/importance from
pierce+quiet) [+ watch if watch]. Severe air-quality crossing → `airqualitysevere_bad` → notify
`pierce=true` → sounds through DND.

## Error handling / edge cases

- **DND sensor unavailable:** treated as not-quiet (only an explicit DND state counts) — a routine
  alert then sounds normally; acceptable.
- **Escalation:** moderate (routine, silent if quiet) then severe (pierce) as a pollutant climbs;
  on the way down only the moderate recovery notifies (severe `recovery:false`).
- **`pierce` outside quiet:** still high importance (sounds) — fine, it's the urgent tier.
- **Missing flags:** `| default(false)` in `bedroom_notify` keeps a bare call routine/phone-only.

## Testing (manual — repo has no HA unit harness)

- Before deploy: HA Developer Tools → YAML → Check Configuration.
- After deploy: confirm the 4 `*_severe` binary_sensors exist (off at normal levels) and
  `script.bedroom_notify` loads. Functional: set the phone to DND (or `bedroom_sleep_mode` on) and
  force a battery/humidity crossing → confirm it arrives silently; force a severe-CO₂ crossing
  (lower the severe threshold or set the source) → confirm it sounds + hits the watch. Confirm the
  "Bedroom critical" channel appears in Android notification settings (then mark it a DND exception).

## Files touched

- `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` — 4 severe sensors
- `ansible/roles/containers/home-assistant/files/scripts.yaml` — `bedroom_notify`
- `ansible/roles/containers/home-assistant/files/automations.yaml` — threshold engine + sensor-offline + away
- `ansible/roles/containers/home-assistant/CLAUDE.md` — document routing + severe tier
- `ansible/PLANS.md` — move the item to done

HA-only deploy; all edits feed `common_config_changed`.

## Future / out of scope

- Hold/queue-and-deliver-later for routine alerts (vs. silent delivery) — not built.
- Actionable notification buttons (separate backlog item) — would add `data.actions` here.
- UPS critical routing (the backlog mentioned it) — UPS alerting lives in the homelab monitoring, not HA.
