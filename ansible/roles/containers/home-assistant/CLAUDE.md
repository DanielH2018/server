# home-assistant — Home automation platform

LinuxServer.io Home Assistant. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/homeassistant:latest` (LSIO is x86-64-maintained;
  only the 32-bit ARM variant was deprecated — fine for daniel-server)
- **Host:** daniel-server · **Port:** 8123 · **Networks:** apps + ups · **Authelia:** no
  (`ups` = isolation net to the `nut` sidecar's upsd:3493 for the NUT integration;
  `apps` stays networks[0] so the Traefik label binds to it)
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **Auth: HA's own login, NOT Authelia.** `use_authelia: false` is deliberate —
  Authelia forward-auth breaks the HA companion mobile app, webhooks, and long-lived
  API tokens (none can complete the portal login flow). The route still gets Traefik
  TLS + CrowdSec + per-router rate-limiting; harden the gate inside HA (strong
  password + TOTP). If you ever want Authelia on the *web UI only*, you'd need
  per-path bypass rules for `/api/`, `/auth/`, and the webhook paths.
- **HACS preinstalled** via `DOCKER_MODS=linuxserver/mods:homeassistant-hacs`
  (LSIO Docker mod that drops the Home Assistant Community Store into `/config`).
- **`configuration.yaml` is templated** from `configuration.yaml.j2` to `./config`.
  It sets `use_x_forwarded_for: true` + `trusted_proxies: 172.16.0.0/12` so HA honors
  Traefik's `X-Forwarded-For` (without it HA rejects the proxied request with
  "400 Bad Request"). The template task is wired to `common_config_changed`, so editing
  it recreates the container on the next deploy. **Note:** HA may rewrite parts of its
  own config via the UI, but this file is the Ansible source of truth and is
  overwritten on deploy — keep UI-managed config (integrations, etc.) in the areas HA
  stores separately (`.storage/`, the recorder DB…), which are NOT templated.
- **Automations + scenes ARE templated (since 2026-06-18).** `files/automations.yaml` and
  `files/scenes.yaml` are static files deployed by `ansible.builtin.copy` (NOT `template` —
  HA automation YAML uses `{{ }}` Jinja that Ansible would try to render and fail; `copy`
  ships them verbatim, no `{% raw %}` needed). Git is the source of truth; HA UI
  automation/scene edits are overwritten on deploy. Both feed `common_config_changed`, so an
  edit recreates HA (~120s). First automation: Hue Tap Dial (RDM002) drives the
  `light.bedroom_lights` group (dial = brightness, button 1 = smart toggle, buttons 2-3 =
  scenes, button 4 = natural-state reset → `script.bedroom_apply_natural`, see below). Presence
  (FP300) + an `input_boolean` manual-off override + a weekday/weekend morning reset live in the
  same file; `bedroom_presence_on` and the morning reset BOTH call `script.bedroom_apply_natural`.
  Presence-on's lux gate is window-aware: `in morning window OR illuminance < 50` — wake regardless
  of ambient light during the 15-min window, gate on darkness afterwards. That window template
  duplicates the dispatcher's morning exception (in `files/scripts.yaml`) — keep the two in sync.
- **Adaptive Lighting is a HACS dependency (since 2026-06-18).** `configuration.yaml` declares
  `adaptive_lighting:` for the bedroom group; the integration code installs via HACS into
  `custom_components/adaptive_lighting/` (Kopia-backed, not templated — like `dreo`). Install it
  via HACS BEFORE deploying, or HA logs "integration not found" and skips the block. The deploy's
  full restart loads a newly added custom component (a YAML "Quick Reload" does not).
- **`files/scripts.yaml` — the "natural lighting state" dispatcher (templated via `copy`, like
  automations/scenes; wired via `script: !include scripts.yaml`; feeds `common_config_changed`).**
  `script.bedroom_apply_natural` sets the bedroom group to what it would be with no manual
  intervention RIGHT NOW: an ordered `choose:` of time-based **exceptions** (brightness overrides
  on AL's natural color) with **full Adaptive Lighting (color + brightness) as `default:`**. The
  morning wake (06:00 Mon–Fri / 07:00 Sat–Sun, 1%→50% over 15 min) is the first exception, encoded
  as `brightness = 1+(50-1)·elapsed/900` over `transition = 900-elapsed` — so `elapsed=0` equals
  the wake's start and pressing button 4 mid-window *resumes* the ramp. **Both Tap Dial button 4
  and the `bedroom_morning_reset` automation call this dispatcher** (single source of truth — no
  duplicated ramp math). Color temp ALWAYS comes from AL; exceptions override brightness only.
  Helper `script.bedroom_set_natural_brightness(brightness_pct, transition)` holds the AL
  release + color-apply boilerplate so a new exception is just a `(condition, brightness,
  transition)` triple dropped above `default:` — see the worked example comment in the file.
- **Air-quality alerts (since 2026-06-18).** `configuration.yaml` adds four built-in `threshold`
  binary-sensors over the bedroom AirGradient ONE (CO2/PM2.5/VOC/NOx); the `threshold` platform's
  native hysteresis (on > upper+hyst, off < upper−hyst) IS the "alert once + recovery, no bounce"
  lifecycle. One generic automation `bedroom_air_quality_alert` (files/automations.yaml) triggers
  on any of them flipping — **anchored on `off`↔`on` (not `unknown`)** so an HA restart while air
  is bad doesn't re-alert and an unavailable source can't false-alert — notifies
  `notify.mobile_app_pixel_9_pro` (same `tag` for bad + recovery so they coalesce on the phone),
  and **only if `light.bedroom_lights` is already on** calls `script.bedroom_alert_pulse`
  (snapshot → red flash → restore the snapshot, so a manual scene / morning ramp / AL all return
  intact). The message is derived from the triggering sensor's attributes (no per-pollutant map),
  so a new pollutant = one more threshold sensor in `configuration.yaml.j2` + add its
  `binary_sensor` to BOTH trigger lists. Thresholds are starting points — tune the VOC/NOx *index*
  ones to the observed baseline.
- **Sensor-offline alerts (since 2026-06-18).** `bedroom_sensor_offline_alert` (files/automations.yaml,
  a structural twin of the air-quality alert) notifies `notify.mobile_app_pixel_9_pro` when a
  bedroom-automation dependency goes `unavailable` for 5 min, with a coalescing-tag recovery notice.
  Watched (one representative entity per device — Z2M flips all of a device's entities together):
  `sensor.bedroom_airgradient_one_carbon_dioxide`, `binary_sensor.aqara_fp300_presence`,
  `sensor.0x001788010f0ccda4_battery` (Tap Dial), `fan.tower_fan`. **Required dependency: Z2M
  availability must be ON** (enabled 2026-06-18 in the zigbee2mqtt role) — without it the battery
  Zigbee devices (FP300, Tap Dial) never go `unavailable` and this automation can't see them fail.
  Two reusable gotchas: (1) the 5-min `for:` rides out HA/Z2M restarts + the ~120s deploy recreate;
  (2) an entity's `friendly_name` attribute is EMPTY while `unavailable`, so the human name is read
  from the AVAILABLE side of the transition (`from_state` for offline, `to_state` for recovery,
  `default(entity_id)` fallback). Battery-Zigbee offline detection is inherently coarse (~the Z2M
  passive timeout, 60 min), not minutes — a sleeping radio can't be pinged. Adding a watched device
  = add its entity to BOTH trigger lists.
- **Low-battery alerts (since 2026-06-18).** The LOWER-bound mirror of the air-quality engine:
  `configuration.yaml.j2` adds two `threshold` binary-sensors over the battery Zigbee devices
  (`sensor.aqara_fp300_battery` → `binary_sensor.bedroom_fp300_battery_low`,
  `sensor.0x001788010f0ccda4_battery` Tap Dial → `binary_sensor.bedroom_tap_dial_battery_low`),
  each `lower: 20, hysteresis: 5` (alerts ≤15%, clears ≥25% — tunable). `bedroom_battery_low_alert`
  (files/automations.yaml) is a near-exact twin of `bedroom_air_quality_alert`: off↔on triggers,
  message derived from the triggering sensor's source/friendly-name, notify-only (no light pulse),
  coalescing tag. Anchored on off↔on so a battery going `unavailable` (device offline, owned by
  `bedroom_sensor_offline_alert`) can't false battery-alert. A `lower` threshold inverts the
  hysteresis: on (low) at ≤lower−hyst, off at ≥lower+hyst; battery drain is monotonic so no
  flapping. **These three threshold-driven alert automations (air-quality, battery, and the planned
  humidity one) are the same skeleton** — once humidity lands, consider unifying into one
  `bedroom_threshold_alert` engine (category→title/icon map, pulse gated to air-quality).
- **Temperature → fan control (since 2026-06-18).** `script.bedroom_apply_fan` (in
  `files/scripts.yaml`) drives `fan.tower_fan` (DREO, 9 levels) from
  `sensor.bedroom_airgradient_one_temperature` (°F): off <72 / Low 72–74 / Medium 74–76 / High ≥76,
  mapped to fan **levels 2/4/6 (≈22/44/67%)**, with a **0.5°F hysteresis deadband** (steps down only
  0.5° below a boundary) and a **22:00–06:00 Medium night cap**. Works in fan LEVELS, not raw %,
  because the DREO integration `math.ceil()`s a requested % up to the next level (a `67%` request
  lands on level 7 ≈ 77%) — send `(L−0.5)/9·100`% to hit level L; speeds tune via one `levels` list.
  Same script-computes / caller-gates split as the lights:
  `bedroom_fan_temperature` (triggers on temp change + 22:00 + 06:00) gates on
  `input_boolean.bedroom_fan_manual` then calls the script. `bedroom_fan_manual_detect` sets that
  override on a real manual change. **`parent_id is none` alone self-trips** here — `dreo` is
  `cloud_push` and its setters only `_send_command` (no optimistic state), so our OWN command's
  value arrives via a parent-less websocket echo that looks manual. Fix: `bedroom_apply_fan` writes
  the level it's about to command to `input_number.bedroom_fan_expected_level` first, and the
  detector flags only when `parent_id is none AND (preset change OR new fan level != expected)` — so
  our echo (level == expected) is ignored, a real manual/remote change is caught. The RF remote is
  caught too (the fan reports app/panel/remote changes to the DREO cloud).
  **Tap Dial button 3 = reset the fan to automatic** (clear `bedroom_fan_manual` + apply, night-cap
  aware); the morning reset clears it too. The `bedroom_relax` scene remains defined but is no longer
  bound to the dial. Adding a pollutant-style fan band = edit the band ladder in the script only.
- **YAML dashboard + entity customization (templated).** `configuration.yaml` registers a YAML
  dashboard via `lovelace: dashboards:` (NOT the legacy top-level `mode: yaml` — deprecated,
  removed in HA 2026.8) pointing at `config/ui-lovelace.yaml` (`templates/ui-lovelace.yaml.j2`),
  shown in the sidebar as "Bedroom". `homeassistant: customize: !include customize.yaml` holds
  friendly-name/icon overrides (`templates/customize.yaml.j2`). Both feed `common_config_changed`,
  so an edit recreates HA (~120s). Built-in cards only — no Lovelace `resources:`/`resource_mode:`.
  **The landing dashboard is NOT YAML-configurable:** HA opens its auto-generated areas "Overview"
  unless "Bedroom" is set as default in the UI (Settings → Dashboards → ⋮ → "Set as default for
  everyone" → persists in `.storage/core.config` `default_panel`, Kopia-backed). **Fast loop for
  dashboard-only tweaks:** edit the rendered file and Developer Tools → YAML → **Reload Lovelace**
  (no HA restart). Dashboard entity IDs are exact as of 2026-06-17 (UPS / DREO Tower Fan / Aqara FP300).
- **All persistent state is `./config` → `/config`** (Kopia-backed): the SQLite
  recorder DB, `.storage/`, secrets, automations, and the templated `configuration.yaml`.
- **Bridge networking, not host.** Cloud/API-based integrations work fine. **Local
  device discovery** (mDNS/SSDP, Bluetooth, Zigbee/Z-Wave USB dongles) generally needs
  `network_mode: host` and/or `devices:` passthrough — which is incompatible with the
  Traefik-label + bridge-network setup here. Switching to host mode is a separate,
  larger change; revisit only if you add local hardware.

## Editing
- Compose: `templates/docker-compose.yml.j2` · HA cfg: `templates/configuration.yaml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`
