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
  morning wake ramp tied to your real alarm, and a tap-on dim nightlight (Button 3) for night trips.
  Once you're in sleep mode the room stays dark on its own — presence won't auto-light you until
  morning.
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
| Pixel 9 Pro + Pixel Watch 3 | HA companion apps | `person.daniel`, `device_tracker.{pixel_9_pro,pixel_watch_3}`, `sensor.pixel_9_pro_{do_not_disturb_sensor,sleep_duration}`, `sensor.pixel_watch_3_next_alarm`, `binary_sensor.pixel_watch_3_bedtime_mode`, `notify.mobile_app_pixel_9_pro`, `notify.mobile_app_pixel_watch_3` |
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
| `templates/configuration.yaml.j2` | `default_config`, helpers, Adaptive Lighting, 16 `threshold` sensors, `template: !include`, `recorder:` excludes, http/trusted-proxy, Lovelace | `template` (Ansible-rendered) |
| `files/automations.yaml` | the 31 automations | `copy` (verbatim — HA Jinja) |
| `files/scripts.yaml` | the 16 scripts | `copy` |
| `files/scenes.yaml` | `bedroom_bright` / `bedroom_nightlight` | `copy` |
| `files/templates.yaml` | `sensor.bedroom_wake_start` template sensor | `copy` |
| `files/custom_templates/fan.jinja` | shared `pct_to_level` / `level_to_pct` fan macros | `copy` |
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
recovery, no bounce". Sixteen, feeding `bedroom_threshold_alert`:
- Air quality indoor (moderate): `bedroom_{co2,pm2_5,voc,nox}_high`
- Air quality indoor (severe, pierces DND): `bedroom_{co2,pm2_5,voc,nox}_severe`
- Air quality outdoor: `outdoor_pm2_5_high` (moderate, watch) / `outdoor_pm2_5_severe` (wildfire tier, pierces DND)
- Temperature: `bedroom_temp_high` / `_low` — the away safety net (`critical_away`: pushes even while you're out)
- Battery: `bedroom_{fp300,tap_dial}_battery_low`
- Humidity: `bedroom_humidity_high` / `_low`

---

## 6. Scripts (the reusable building blocks)

> The **core** building blocks below; the suite has **16** scripts in total.
> [`CLAUDE.md`](CLAUDE.md) is the authoritative, exhaustive per-feature reference — this section
> is a curated walkthrough, not a full inventory (keeping it one keeps it from drifting).

- **`bedroom_apply_natural`** — sets the lights to their "natural" state *right now*: an ordered
  `choose:` of time-based exceptions over **full Adaptive Lighting** (the default). Exceptions, in
  order: (1) **night nightlight** (`scene.bedroom_nightlight`) when sleep mode is on OR 00:00–05:00;
  (2) **morning wake ramp** (a gradual 1%→100% sunrise over a 45-min window, `alarm−15`→`alarm+30`,
  that keeps climbing to full brightness so the hand-off to Adaptive Lighting has no sudden pop). Uses
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

> The **core** automations below; the suite has **31** in total (the cast/display, heartbeat,
> runtime-error, color-track, AL-startup-suppress, air-purifier-presence, wake-ramp, held-notification
> flush, and others are documented per-feature in [`CLAUDE.md`](CLAUDE.md), the authoritative reference).

**Presence & lighting**
- `bedroom_presence_on` — FP300 occupied (+ home, + dark-or-in-wake-window) → `apply_natural`.
- `bedroom_absence_off` — room empty 5 min → lights off (5 min, not 1, for FP300 false-absence de-flap).
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
- `zigbee_bridge_offline` — the Zigbee2MQTT bridge (whole mesh) offline 2 min → notify (watch); a
  faster root-cause signal than the per-device offline alert, via `binary_sensor.zigbee2mqtt_bridge_connection_state`. Recovery when it returns.
- `bedroom_notification_action` — handles taps on notification buttons (`BEDROOM_*` actions).
- `update_available_digest` — Sunday 10:00, a digest of pending device/integration updates.

**Maintenance & power**
- `bedroom_co2_calibration_reminder` — quarterly (1st of Jan/Apr/Jul/Oct) notify to recalibrate the
  AirGradient CO₂ sensor against the outdoor ~400 ppm baseline (notify-only; no one-tap calibrate).
- `ups_power_event` — UPS outage / low-battery / restored, off the raw NUT flags
  (`sensor.apc_ups_status_data`); low-battery pierces DND (the server may shut down).

---

## 8. Operator controls

**Tap Dial (RDM002):**

| Control | Press / Rotate | Hold |
|---------|----------------|------|
| **Button 1 — Power** | toggle: on → natural (Adaptive Lighting, ungated); off → off + stay-off | reset to auto (clear overrides, re-sync lights lux-gated + fan) |
| **Button 2 — Brightness** | Relax / cozy scene (warm ~30%) | Bright scene (full) |
| **Button 3 — Sleep** | sleep toggle: in sleep mode + lights on → all off (room dark, fan stays quiet); otherwise → nightlight (warm ~3%) | Bedtime routine (30-min fade to nightlight) |
| **Button 4 — Fan** | fan → auto (clear manual override, re-apply curve) | toggle **fan-dial mode** (5-min window: the dial then steps the fan ±1 level; max fan still reachable by dialing to L9) |
| **Dial** | brightness ±12% | — |

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
| Wake ramp curve (knees / final brightness / short-night softening) | `wake_brightness` macro in `custom_templates/lighting.jinja` (`mid`/`knee`/`full`); window length in `in_wake_window` |
| Nightlight window | `bedroom_apply_natural` first exception (`sleep_mode` or 00:00–05:00) |
| Bedtime fade length (30 min) | `transition:` on the `scene.turn_on` in `bedroom_bedtime` (`scripts.yaml`) |
| Away timings (10 / 30 min) | `bedroom_away` trigger `for:` |
| Bedtime prompt time (22:00) | `bedroom_bedtime_prompt` trigger |
| FP300 presence drop-out (desk-sitting) | Z2M **device settings** (not git): `motion_sensitivity`, `absence_delay_timer`, `presence_detection_options` — set via `mosquitto_pub -t 'zigbee2mqtt/Aqara FP300/set'` (current: mmwave / high / 60 s) |
| Which alerts are critical / watch | `cfg` map in `bedroom_threshold_alert`; flags on `bedroom_notify` calls |

After any edit, redeploy (§ top). Device-level settings (Z2M names, FP300 tuning) live in Z2M's
`./data` (Kopia-backed, not git) — re-apply after a re-pair.

---

## 11. Google Nest Hub Max — voice control + dashboard casting

Two **independent**, self-hosted (no Nabu Casa) integrations with a Nest Hub Max:

- **Voice control** ("Hey Google, turn on the bedroom lights") via HA's **manual
  `google_assistant`** integration → your own Google Cloud project pointed at HA's existing
  public HTTPS endpoint. Authelia is already off for HA, so no per-path bypass is needed.
- **Dashboard casting** (show a Lovelace view on the Hub Max screen) via the local **`cast`**
  integration. **Why `known_hosts`:** HA is bridge-networked, so it can't discover Cast devices
  over mDNS — `known_hosts` names the Hub Max by IP and skips discovery entirely. Outbound
  container→LAN traffic works through bridge+NAT, so **no host-networking change is needed.**

### 11a. Prerequisite — give the Hub Max a fixed IP
`cast` `known_hosts` needs a stable address. Reserve a DHCP lease for the Hub Max's MAC (home
router, or — if Pi-hole serves DHCP — a `dhcp-host=<MAC>,<IP>` line in
`pihole/templates/dnsmasq.yml.j2`). Find the MAC in the Google Home app (device → Settings →
Technical information) or the router's lease table. Record the IP for `known_hosts`.

### 11b. Voice control — Google-side (one-time clickops; not captured by `deploy.yml`)
Google's console UI shifts often — the HA docs are authoritative:
<https://www.home-assistant.io/integrations/google_assistant/>. **This is the smart-home /
cloud-to-cloud path** (HomeGraph device control) — NOT "Conversational Actions" (those were sunset
June 2023, <https://developers.google.com/assistant/ca-sunset>; smart-home Actions are explicitly
unaffected). Google has since **moved smart-home dev off the legacy Actions console** to the Google
Home Developer Console — use that. Repo-specific values:

1. **Create a project** at <https://console.home.google.com/projects> (the **Google Home Developer
   Console**) → "Create project" → note the **Project ID**.
2. **Add a Cloud-to-cloud integration → fulfillment URL:**
   `https://home-assistant.daniel-hunter.com/api/google_assistant`
3. **Account linking** (OAuth; HA *is* the auth provider):
   - Authorization URL: `https://home-assistant.daniel-hunter.com/auth/authorize`
   - Token URL: `https://home-assistant.daniel-hunter.com/auth/token`
   - Client ID: `https://oauth-redirect.googleusercontent.com/r/<PROJECT_ID>`
   - Client secret: any non-empty string (HA ignores it); Scopes: `email` (dummy)
4. **Google Cloud Console → enable the HomeGraph API** → create a **service account**, download
   its **JSON key** → also create an **API key** (used by `google_assistant.request_sync`).
5. **Test** the Action, then in the **Google Home app** → *Works with Google* → link
   `[test] <action name>`. Re-sync anytime with "Hey Google, sync my devices" or the
   `google_assistant.request_sync` service.

> Hand off three things from the above: the **Project ID**, the **service-account JSON**, and the
> **HomeGraph API key**. They become the SOPS secrets below.

### 11c. Voice control — repo-side (Ansible) — IMPLEMENTED (2026-06-21)
**Key constraint:** `configuration.yaml.j2` is copied verbatim — it must contain **no** Ansible
`{{ }}` (the `validate-ha-config` hook rejects it). So all templated/secret values go through HA's
native `!secret` indirection backed by an Ansible-generated `secrets.yaml`.

1. **One SOPS secret** holds the whole downloaded JSON: `google_assistant_service_account`
   (`sops ansible/vars/secrets.yml`; registered in `ansible/secret_rotation.yml`, tier `assisted`).
   No `api_key` (skipped — `request_sync` works via the service account) and no `secure_devices_pin`
   (no locks/covers exposed).
2. **`templates/secrets.yaml.j2`** (Ansible-rendered → `config/secrets.yaml`, `no_log`) derives the
   `!secret` values from that one JSON with `| from_json` / `| to_json` — the `to_json` flattens the
   multi-line PEM key into one escaped scalar: `google_assistant_project_id`,
   `google_assistant_sa_client_email`, `google_assistant_sa_private_key`, plus `external_url` /
   `internal_url` (these need the `domain` var, which is why they can't live in configuration.yaml).
3. **`configuration.yaml.j2`** references them via `!secret` (curated exposure — `expose_by_default`
   off, so ONLY entities flagged `expose: true` are visible to Google):
   ```yaml
   google_assistant:
     project_id: !secret google_assistant_project_id
     service_account:
       client_email: !secret google_assistant_sa_client_email
       private_key: !secret google_assistant_sa_private_key
     report_state: true
     expose_by_default: false
     entity_config:
       light.bedroom_lights: {name: Bedroom Lights, expose: true}
       fan.tower_fan: {name: Bedroom Fan, expose: true}
   ```
   Both new config tasks are wired into `common_config_changed`. Deploy: `ha-deploy`.
4. **Finish in the Google Home app:** link the `[test] <action>` (§11b step 5), then say
   "Hey Google, sync my devices". Expose another device later = one more `entity_config` entry with
   `expose: true`, redeploy, resync.

### 11d. Dashboard casting — UI / `.storage` (the `cast` integration is NOT YAML)
Modern HA **rejects** a YAML `cast:` block ("does not support YAML setup") — Cast is a config-entry
integration. `external_url`/`internal_url` are already set (§11c step 2), so only the device list is
left, and it's a one-time UI step:
- **Settings → Devices & Services → + Add Integration → Google Cast.** HA is bridge-networked (no
  mDNS), so when prompted add the Hub by IP under **Known hosts = `10.0.0.137`** (or set it later via
  the integration's **Configure**). The Hub must keep that reserved DHCP lease (§11a).
- A `media_player.<hub>` entity then appears. Cast a view from **Developer Tools → Actions** with
  `cast.show_lovelace_view` (target that media_player, `dashboard_path: lovelace`, `view_path: 0`);
  optionally automate it.

> **HA Cast caveat (self-hosted):** needs a publicly-trusted cert (we have the Cloudflare wildcard)
> and the Hub Max to resolve + reach the `external_url`. If casting misbehaves, check that the Hub
> Max resolves `home-assistant.daniel-hunter.com`, or fall back to the LAN `internal_url`.

### 11e. Verify
- `uv run python scripts/validate_ha_config.py` passes; deploy with `ha-deploy` (gates on health).
- `scripts/probe.py ha get error_log` shows no `google_assistant` setup errors (it loads with no
  entities — it's cloud-fulfillment). After adding Cast in the UI,
  `scripts/probe.py ha get states | grep media_player` shows the Hub.
- Voice: "Hey Google, turn on the bedroom lights" toggles `light.bedroom_lights`.
- Cast: `cast.show_lovelace_view` puts the Bedroom dashboard on the Hub Max screen.
