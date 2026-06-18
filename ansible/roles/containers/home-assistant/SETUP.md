# Bedroom Home Assistant — Setup & Reference

A human-readable guide to the bedroom automation suite: what it does, how to set it up from
scratch, how to operate it day-to-day, and where to tune it. For implementation gotchas while
*editing* the config, see [`CLAUDE.md`](CLAUDE.md). Per-feature design rationale lives in
`docs/superpowers/specs/2026-06-18-ha-*`.

Everything is Ansible-managed — **git is the source of truth; HA UI edits are overwritten on
deploy.** Apply changes with:

```bash
uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"
```

---

## 1. What it does (at a glance)

- **Lighting** follows room presence (Aqara FP300) with an Adaptive-Lighting sun curve, a gentle
  morning wake ramp tied to your real alarm, and a dim nightlight for night trips.
- **The fan** (DREO tower) tracks temperature on a smooth curve, with quieter caps at night / in
  sleep mode.
- **A bedtime routine** sets the room up for sleep; **home/away** turns everything off when you
  leave; an **occupancy tripwire** alerts if someone's in the bedroom while you're out.
- **Alerts** (air quality, battery, humidity, sensor-offline) all flow through one notification
  layer that respects Do-Not-Disturb and can pierce it for genuinely critical conditions, mirror to
  your watch, and offer one-tap action buttons.

---

## 2. Hardware & integrations

| Device / source | HA integration | Key entities |
|---|---|---|
| 3× Hue color bulbs (Lamp, Left Light, Right Light) | Zigbee2MQTT | grouped as `light.bedroom_lights` |
| Hue Tap Dial (RDM002) | Zigbee2MQTT (raw topic `zigbee2mqtt/Tap Dial`) | `sensor.0x001788010f0ccda4_battery` |
| Aqara FP300 presence sensor | Zigbee2MQTT | `binary_sensor.aqara_fp300_presence` / `_pir_detection`, `sensor.aqara_fp300_{illuminance,temperature,humidity,battery,target_distance}` |
| AirGradient ONE air monitor | AirGradient (local) | `sensor.bedroom_airgradient_one_{carbon_dioxide,pm2_5,voc_index,nox_index,temperature,humidity}` |
| DREO tower fan | `dreo` (HACS, cloud) | `fan.tower_fan` |
| APC UPS | NUT | (power monitoring) |
| Pixel 9 Pro + Pixel Watch 3 | HA companion apps | `person.daniel`, `device_tracker.{pixel_9_pro,pixel_watch_3}`, `sensor.pixel_9_pro_{do_not_disturb_sensor,sleep_duration}`, `sensor.pixel_watch_3_next_alarm`, `binary_sensor.pixel_watch_3_bedtime_mode`, `notify.mobile_app_pixel_9_pro`, `notify.pixel_watch_3` |
| Adaptive Lighting | HACS | `switch.bedroom_adaptive_lighting_bedroom` (master), `switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom` |

---

## 3. One-time setup (recreating from scratch)

These are **not** captured by `deploy.yml` — they're device/app/UI state:

1. **HACS integrations** — install **Adaptive Lighting** and **DREO** via HACS *before* deploying
   (a full HA restart loads them; the deploy does that). `configuration.yaml` declares the
   `adaptive_lighting:` block.
2. **Zigbee** — pair the bulbs, Tap Dial, and FP300 in the Z2M UI; name them (Lamp / Left Light /
   Right Light / Tap Dial / Aqara FP300). Z2M `availability:` is enabled in the role.
3. **Light group** — create the **Bedroom Lights** group (HA Settings → Helpers / group) over the 3
   bulb entities → `light.bedroom_lights`.
4. **Phone companion app** (Manage sensors → enable, grant the prompted permission):
   `is_charging`, `do_not_disturb_sensor`, `sleep_confidence` / `sleep_segment` / `sleep_duration`
   (Activity Recognition), `next_alarm`. On the watch: `bedtime_mode`, `next_alarm`.
5. **Critical notification channel** — after the first `pierce` alert creates the **"Bedroom
   critical"** channel, set it to **Override Do Not Disturb** (Android → Settings → Apps → Home
   Assistant → Notifications → that channel). High importance alone does *not* pierce DND.
6. **Set wake alarms in the phone's Clock** (the watch alarm surfaces via
   `sensor.pixel_watch_3_next_alarm`, which the wake uses).
7. **Default dashboard** — set "Bedroom" as default (Settings → Dashboards → ⋮) — not YAML-settable.

---

## 4. Files in this role

| File | What it holds | Deployed via |
|---|---|---|
| `templates/configuration.yaml.j2` | `default_config`, helpers, Adaptive Lighting, 12 `threshold` sensors, `template: !include`, http/trusted-proxy, Lovelace | `template` (Ansible-rendered) |
| `files/automations.yaml` | the 15 automations | `copy` (verbatim — HA Jinja) |
| `files/scripts.yaml` | the 6 scripts | `copy` |
| `files/scenes.yaml` | `bedroom_bright` / `bedroom_relax` / `bedroom_nightlight` | `copy` |
| `files/templates.yaml` | `sensor.bedroom_wake_start` template sensor | `copy` |
| `templates/customize.yaml.j2` | entity friendly-name / icon overrides | `template` |
| `templates/ui-lovelace.yaml.j2` | the Bedroom dashboard | `template` |

> **HA Jinja (`{{ }}`) only goes in the copy'd `files/` — never inline in `configuration.yaml.j2`**,
> which Ansible renders (even a brace pair in a comment breaks the deploy). That's why template
> sensors live in `files/templates.yaml` and are pulled in with `template: !include templates.yaml`.

---

## 5. Helpers & sensors

**Helpers** (`configuration.yaml.j2`):
- `input_boolean.bedroom_manual_off` — set when you turn lights off via the dial; suppresses
  presence auto-on. Cleared on manual-on or the morning reset.
- `input_boolean.bedroom_fan_manual` — set when the fan is changed by hand/remote; suppresses
  temperature fan control. Cleared by Tap Dial button 3 or the morning reset.
- `input_boolean.bedroom_sleep_mode` — set by the bedtime routine; caps the fan to Low and switches
  the nightlight window on. Cleared by the morning reset.
- `input_number.bedroom_fan_expected_level` — internal: the fan level the script last commanded, so
  the manual-fan detector can tell our own cloud echo from a real manual change.

**Template sensor** (`files/templates.yaml`):
- `sensor.bedroom_wake_start` — the watch alarm minus 15 min, available only for *morning* alarms
  (03:00–11:00). The single source of truth for the wake-ramp window.

**`threshold` binary-sensors** (`configuration.yaml.j2`) — native hysteresis = "alert once +
recovery, no bounce". Twelve, feeding `bedroom_threshold_alert`:
- Air quality (moderate): `bedroom_{co2,pm2_5,voc,nox}_high`
- Air quality (severe, pierces DND): `bedroom_{co2,pm2_5,voc,nox}_severe`
- Battery: `bedroom_{fp300,tap_dial}_battery_low`
- Humidity: `bedroom_humidity_high` / `_low`

---

## 6. Scripts (the reusable building blocks)

- **`bedroom_apply_natural`** — sets the lights to their "natural" state *right now*: an ordered
  `choose:` of time-based exceptions over **full Adaptive Lighting** (the default). Exceptions, in
  order: (1) **night nightlight** (`scene.bedroom_nightlight`) when sleep mode is on OR 00:00–05:00;
  (2) **morning wake ramp** (1%→peak over 15 min ending at the alarm). Uses
  `bedroom_set_natural_brightness` (a helper that releases AL, applies its natural color, then sets
  a brightness). Called by presence-on, the morning reset, Tap Dial button 4, and arrive-home.
- **`bedroom_apply_fan`** — smooth temperature→fan curve (~1 DREO level/°F, off below ~72°F, up to
  L9 by ~80°F) with a ~0.7-level hysteresis deadband and level caps (L4 at night, L2 in sleep mode).
- **`bedroom_bedtime`** — the sleep routine: sleep mode on + Adaptive Lighting sleep mode +
  `scene.bedroom_nightlight` + re-apply the (now quiet-capped) fan.
- **`bedroom_notify`** — the one notification layer everything calls. Fields:
  `title, message, tag, watch, pierce`. Picks Android channel/importance from `pierce` + the live
  "quiet" state (DND on **or** sleep mode); mirrors to the watch if `watch`.
- **`bedroom_alert_pulse`** — snapshot → red flash → restore, for a bad-air-quality pulse when the
  lights are already on.

---

## 7. Automations (what each does)

**Presence & lighting**
- `bedroom_presence_on` — FP300 occupied (+ home, + dark-or-in-wake-window) → `apply_natural`.
- `bedroom_absence_off` — room empty 1 min → lights off.
- `bedroom_morning_reset` — at `sensor.bedroom_wake_start` (the alarm) + a 09:00 fallback: clear the
  overnight overrides, re-apply the fan, and (if present, alarm trigger) run the wake ramp + a
  "you slept N h" note (gentler ramp if you slept < 6 h).

**Fan**
- `bedroom_fan_temperature` — on temp change / 22:00 / 06:00 → `apply_fan` (gated on home +
  `fan_manual` off).
- `bedroom_fan_manual_detect` — a hand/remote fan change → set `bedroom_fan_manual`.

**Bedtime & controls**
- `bedroom_bedtime` — phone Bedtime mode on (while home) → `bedroom_bedtime` script.
- `bedroom_bedtime_prompt` — 22:00, if present + not in sleep mode + home → a "Ready for bed?" notify
  with a **Start now** button.
- `bedroom_tap_dial_control` — the Tap Dial (see §8).

**Home / away & security**
- `bedroom_away` — left home (10 min) + a 30-min failsafe → turn off lights + fan, notify what was on.
- `bedroom_arrive_home` — returned home → resume the fan, re-check lights if you're in the room.
- `bedroom_unexpected_occupancy` — FP300 presence while away > 5 min → security alert (watch + pierce).

**Alerts & notifications**
- `bedroom_threshold_alert` — the unified engine: any threshold sensor crossing → routed notify
  (air-quality also pulses the lights; severe air-quality pierces DND).
- `bedroom_sensor_offline_alert` — a watched dependency `unavailable` 5 min → notify (watch).
- `bedroom_notification_action` — handles taps on notification buttons (`BEDROOM_*` actions).
- `update_available_digest` — Sunday 10:00, a digest of pending device/integration updates.

---

## 8. Operator controls

**Tap Dial (RDM002):**
- **Rotate** — brightness ±12%.
- **Button 1 press** — smart toggle (off engages `manual_off`; on clears it).
- **Button 1 hold** — start the bedtime routine.
- **Button 2** — `scene.bedroom_bright`.
- **Button 3** — reset the fan to automatic (clear `fan_manual`, apply the temperature band).
- **Button 4** — reset the lights to their natural state (clear `manual_off`).

**Phone / watch:**
- **Bedtime mode** (watch) → runs the bedtime routine.
- **Next alarm** (phone Clock) → drives the morning wake ramp.
- **Do Not Disturb** → routine alerts go silent; critical ones pierce (if you set the channel
  override).
- **Notification buttons** — Boost fan (air quality), Turn back on (away), Start now (bedtime).

---

## 9. Notification model

| Severity | Behavior | Examples |
|---|---|---|
| **Routine** | silent while "quiet" (DND or sleep mode), normal otherwise; phone only | battery, humidity, moderate air quality, sensor-offline recovery, away |
| **Watch** | also buzzes the Pixel Watch | sensor-offline, any air-quality bad |
| **Critical (`pierce`)** | high-importance "Bedroom critical" channel — sounds through DND | **severe** air quality, the occupancy tripwire |

All set per-category in `bedroom_threshold_alert`'s `cfg` map or per-call to `bedroom_notify`.

---

## 10. Tuning guide

| Want to change… | Where |
|---|---|
| Air-quality / humidity / battery / severe thresholds | the `threshold` sensors in `configuration.yaml.j2` (`upper`/`lower`/`hysteresis`) |
| Fan curve (start temp / slope / caps) | `bedroom_apply_fan` in `scripts.yaml` (the `ideal`/`cap` lines) |
| Wake ramp peak / short-night softening | `bedroom_apply_natural` morning exception (`wake_peak`) |
| Nightlight window | `bedroom_apply_natural` first exception (`sleep_mode` or 00:00–05:00) |
| Away timings (10 / 30 min) | `bedroom_away` trigger `for:` |
| Bedtime prompt time (22:00) | `bedroom_bedtime_prompt` trigger |
| FP300 presence drop-out (desk-sitting) | Z2M **device settings** (not git): `motion_sensitivity`, `absence_delay_timer`, `presence_detection_options` — set via `mosquitto_pub -t 'zigbee2mqtt/Aqara FP300/set'` (current: mmwave / high / 60 s) |
| Which alerts are critical / watch | `cfg` map in `bedroom_threshold_alert`; flags on `bedroom_notify` calls |

After any edit, redeploy (§ top). Device-level settings (Z2M names, FP300 tuning) live in Z2M's
`./data` (Kopia-backed, not git) — re-apply after a re-pair.
