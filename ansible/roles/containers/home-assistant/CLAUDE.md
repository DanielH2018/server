# home-assistant — Home automation platform

LinuxServer.io Home Assistant. See repo-root `CLAUDE.md` for shared conventions, and
[`SETUP.md`](SETUP.md) for a human-readable setup / operation / tuning guide to the bedroom suite
(this file is the editing-gotchas reference).

## At a glance
- **Image:** `lscr.io/linuxserver/homeassistant:<X.Y.Z-lsNN>` — **pinned + Renovate-managed**
  (`watchtower.enable=false`), NOT `:latest`. HA is stateful with monthly, occasionally-breaking
  releases, so it belongs in the critical/stateful tier (like jellyfin/the *arr stack) — bump via
  Renovate PRs (the `/linuxserver/` regex tracks the tag), not watchtower's watch-all `:latest` pool.
  (LSIO is x86-64-maintained; only the 32-bit ARM variant was deprecated — fine for daniel-server.)
- **Host:** daniel-server · **Port:** 8123 · **Networks:** apps + ups · **Authelia:** no
  (`ups` = isolation net to the `nut` sidecar's upsd:3493 for the NUT integration;
  `apps` stays networks[0] so the Traefik label binds to it)
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **Auth: HA's own login, NOT Authelia.** `use_authelia: false` is deliberate —
  Authelia forward-auth breaks the HA companion mobile app, webhooks, and long-lived
  API tokens (none can complete the portal login flow). The route still gets Traefik
  TLS + CrowdSec + per-router rate-limiting; harden the gate inside HA. If you ever want
  Authelia on the *web UI only*, you'd need per-path bypass rules for `/api/`, `/auth/`,
  and the webhook paths.
  - **`ip_ban_enabled: true` + `login_attempts_threshold: 5`** (in `configuration.yaml`'s
    `http:`) auto-ban an IP after 5 failed logins (→ `config/ip_bans.yaml`; delete a line to
    unban). Bans the REAL client IP because the CF→Traefik→HA chain forwards X-Forwarded-For
    (Traefik `forwardedHeaders.trustedIPs=cloudflare_ips` + HA `use_x_forwarded_for`). Only
    failed PASSWORD logins count — tokens/app/webhooks unaffected.
  - **TOTP/MFA: enrolled (2026-06-18).** This route is internet-facing (Cloudflare-proxied
    `home-assistant.<domain>`), so MFA is the compensating control for Authelia-off; `ip_ban` is
    defense-in-depth on top. If MFA is ever reset/lost, re-enrol: HA → Profile → Multi-factor
    Authentication → TOTP (and keep the recovery code from enrolment).
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
- **Automations + scenes + scripts + template sensors + shared Jinja macros ARE copy'd (since 2026-06-18).**
  `files/automations.yaml`, `files/scenes.yaml`, `files/scripts.yaml`, `files/templates.yaml`, and
  `files/custom_templates/fan.jinja`
  are static files deployed by `ansible.builtin.copy` (NOT `template` — they use HA `{{ }}` Jinja
  that Ansible's templater would try to render and fail; `copy` ships them verbatim, no `{% raw %}`
  needed). **This is why HA Jinja lives in copy'd files, never inline in `configuration.yaml.j2`**
  (which IS Ansible-templated) — `template: !include templates.yaml` pulls the template sensors in.
  Git is the source of truth; HA UI edits are overwritten on deploy. Both feed `common_config_changed`, so an
  edit recreates HA (~120s). First automation: Hue Tap Dial (RDM002) drives the
  `light.bedroom_lights` group (dial = brightness ±12%; B1 = Power: press = smart toggle [on → `bedroom_apply_natural`
  ungated, off → off + manual-off], hold = reset-to-auto [clear overrides, re-sync lux-gated
  via `bedroom_apply_natural_gated` + fan]; B2 = Brightness: press = `scene.bedroom_relax`,
  hold = `scene.bedroom_bright`; B3 = Sleep: press = sleep TOGGLE [in sleep mode + lights on → lights
  off (stay in sleep mode, fan quiet); else → `scene.bedroom_nightlight` + clear manual-off], hold =
  `script.bedroom_bedtime` (15-min fade); B4 = Fan: press = auto [clear fan-manual + `bedroom_apply_fan`
  + cancel fan-dial mode], hold = toggle fan-dial mode [`timer.bedroom_fan_dial`, 5-min sliding window:
  the dial then steps the fan ±1 level via `script.bedroom_fan_nudge`; auto-reverts to light dial on
  expiry — replaces the old hold-to-boost-100%, max fan still reachable by dialing to L9]). Manual taps
  are ungated by design — the lux gate lives on the presence
  path + the reset hold. **Tap Dial gotchas (RDM002, verified live):** match button actions on the
  `*_press_release`/`*_hold_release` events — a tap fires `button_N_press`→`button_N_press_release`,
  but a HOLD fires `button_N_press` (!) then repeats `button_N_hold` then `button_N_hold_release`, so
  matching `*_press`/`*_hold` double-fires the tap before every hold AND runs holds ~3×; the release
  events are mutually exclusive (exactly one per gesture). The two LIGHT buttons (B1, B2) call
  `script.bedroom_exit_sleep` FIRST (clears `sleep_mode` + AL sleep mode) — using the normal lights
  releases the night state (the daytime sleep-exit the morning reset otherwise owns; closes the "very
  red" trap where a stuck `sleep_mode` made B1's `apply_natural` serve the amber nightlight). The FAN
  button (B4) is **fan-only** — it clears the `sleep_mode` flag (un-caps `apply_fan` from its L2 sleep cap)
  but does NOT touch AL sleep or the lights. Two reasons it stays off the lights: clearing AL sleep makes
  AL **self-on the lights asynchronously** (a flash that beat a prompt `light.turn_off`), and the FP300
  illuminance is dominated by the bedroom lights THEMSELVES (~640 lux on / ~48 off), so anything that
  turns the lights off makes the in-room sensor read "dark" and `presence_on` re-lights them (a feedback
  loop the fan button must not get tangled in — see the lux-gate note below).
  **Fan-dial mode (since 2026-06-20):** B4 HOLD toggles `timer.bedroom_fan_dial` (5-min sliding
  window) — while it's `active` the dial steps the fan ±1 level (`script.bedroom_fan_nudge`) instead
  of the lights; it auto-reverts to the light dial on expiry, and a B4 tap cancels it. The timer's
  `active` state IS the mode (no `input_boolean`, so it's off after any HA restart — deliberately
  sidesteps the stale-override-restore trap below). The nudge drives off the
  `input_number.bedroom_fan_expected_level` accumulator (not the laggy DREO cloud %) so rapid turns
  accumulate, engages `bedroom_fan_manual`, and ignores the night/sleep caps (you're in control until
  a B4 tap / the morning reset clears the override). `fan_nudge_level` clamp math is a tested macro.
  The
  stuck state itself recurs because the LSIO HA's unclean shutdown restores a STALE `input_boolean` snapshot
  on restart — every deploy can resurrect an overnight override until the 09:00/alarm morning reset clears it. The
  dial emits `dial_rotate_<dir>_<slow|fast|step>` (caught by the substring match) alongside harmless
  `brightness_step_*` no-ops. Presence
  (FP300) + an `input_boolean` manual-off override + an alarm-driven morning reset live in the
  same file; `bedroom_presence_on` and the morning reset BOTH call `script.bedroom_apply_natural`.
  The lux gate is window-aware (`in morning window OR illuminance < 50` — wake regardless of ambient
  light during the 15-min window, gate on darkness afterwards) and lives in ONE place:
  `binary_sensor.bedroom_auto_light_allowed` (templates.yaml). `bedroom_presence_on` (its darkness
  condition) and `bedroom_apply_natural_gated` reference that sensor — tune the 50-lux threshold / window
  there, once. The window reads `sensor.bedroom_wake_start` (the shared dynamic-wake source — see below),
  the SAME sensor the dispatcher's morning exception uses, so the two are inherently in sync (no duplicated
  formula). **Feedback-loop caveat (tuning):** `sensor.aqara_fp300_illuminance` is dominated by the
  bedroom lights themselves (~640 lux with them on, ~48 off), so the gate is partly circular — turning the
  lights off makes the room read "dark," which can have `presence_on` re-light it ~30 s later. The 50-lux
  threshold sits right in this room's lights-off ambient (~48), so it's borderline; pick a value clearly
  below the lights-off daytime ambient if you want to stop daytime auto-lighting (this is why the fan
  button stays out of light control entirely).
- **Too-bright arrival blip (since 2026-06-19).** `automation.bedroom_presence_blip_too_bright` is a
  sibling of `bedroom_presence_on`: same arrival edge (`binary_sensor.aqara_fp300_presence` -> on),
  but the lux gate is **inverted** (`binary_sensor.bedroom_auto_light_allowed` == off) plus
  `manual_off` off, `person home`, and lights currently off. When you walk in but it's too bright to
  auto-light, it calls `script.bedroom_blip` (off -> 15% warm 2700K ~1s -> off) so you get an
  acknowledgement instead of silence. `bedroom_blip` is the inverse of `bedroom_alert_pulse` — it
  needs NO `scene.create` snapshot because it only runs with the lights already off, so a plain
  `turn_off` restores the known state. No feedback loop: it fires only at illuminance >= 50 (bright
  ambient), and a ~1s blip can't satisfy `presence_on`'s `below: 50 for: 30s`. No cooldown initially;
  add a trigger `for:` debounce if presence flapping makes it chatty.
  **Tap Dial button-1 HOLD blips too (since 2026-06-20).** The "reset to auto" branch in
  `bedroom_tap_dial_control` calls the SAME `script.bedroom_blip` when its lux-gated apply
  (`script.bedroom_apply_natural_gated`) leaves the lights off — i.e. `bedroom_auto_light_allowed` ==
  off (too bright) AND the lights were already off before the reset (a `was_off` snapshot taken before
  the apply, so a reset that turns an already-on light off doesn't double-up the visible feedback with
  a blip). Same too-bright acknowledgement as the arrival blip, but button-driven (not lux-driven), so
  there is no feedback-loop concern at all.
  **`script.bedroom_blip` is the SINGLE source of truth for the blip flash** (off -> 15% warm 2700K ->
  off). Both the arrival automation AND this Button 1 HOLD branch reference that one script — never
  re-roll an inline flash — so the acknowledgement is identical by construction and can't drift. Any
  future "the lights stayed off on purpose, acknowledge it" feedback should call `script.bedroom_blip`
  too (it requires the lights already off — no snapshot; each caller must enforce that precondition).
  **Verification gotcha:** an automation's `entity_id` derives from its `alias` (slugified) at
  first creation, NOT its `id` — so `bedroom_fan_temperature` (id) is
  `automation.bedroom_fan_temperature_control` (alias) in the state machine / recorder DB. Query by
  the alias-slug, not the id, when checking whether an automation loaded.
  **FP300 presence tuning (2026-06-18, "lights off while sitting at the desk" fix):** the FP300 was
  dropping `presence` ~2 min while the operator sat still (187 flips/24h, 16 false-absences 1–5 min
  that crossed `bedroom_absence_off`'s 1-min timeout). Fixed via Z2M **device settings** (NOT
  templated — set with `mosquitto_pub -t 'zigbee2mqtt/Aqara FP300/set' -m '{...}'`; re-apply after a
  re-pair): `presence_detection_options: mmwave` (radar-only — holds a stationary person; PIR sees
  only motion), `motion_sensitivity: high`, `absence_delay_timer: 60` (sec; was 10, range 10–300 —
  the hold-vs-prompt knob). `bedroom_absence_off` stays at 1 min; bump it if drops persist.
  **FP300 fan false-HOLD (2026-06-18, the mirror-image fix):** the above hold-harder tuning
  over-corrected — the high-sensitivity mmwave radar read the **running tower fan's** moving air as a
  permanent occupant, so `presence` stuck `true` in an empty room (15+ min observed) and
  `bedroom_absence_off` never fired → lights stayed on. Confirmed by experiment: fan OFF → `presence`
  cleared to `false` 72s later (= the 60s `absence_delay_timer`). Fixed via another Z2M **device
  setting** (same `mosquitto_pub .../Aqara FP300/set`, NOT templated, re-apply after a re-pair):
  `ai_interference_source_selfidentification: ON` — Aqara's purpose-built interference rejection;
  keeps `motion_sensitivity: high` so the desk-sitting fix survives. The dog is NOT a factor (an
  mmwave radar does see a pet as presence, but the room was confirmed pet-free during the incident).
- **Adaptive Lighting is a HACS dependency (since 2026-06-18).** `configuration.yaml` declares
  `adaptive_lighting:` for the bedroom group; the integration code installs via HACS into
  `custom_components/adaptive_lighting/` (Kopia-backed, not templated — like `dreo`). Install it
  via HACS BEFORE deploying, or HA logs "integration not found" and skips the block. The deploy's
  full restart loads a newly added custom component (a YAML "Quick Reload" does not).
- **`files/scripts.yaml` — the "natural lighting state" dispatcher (templated via `copy`, like
  automations/scenes; wired via `script: !include scripts.yaml`; feeds `common_config_changed`).**
  `script.bedroom_apply_natural` sets the bedroom group to what it would be with no manual
  intervention RIGHT NOW: an ordered `choose:` of time-based **exceptions** (brightness overrides
  on AL's natural color) with **full Adaptive Lighting (color + brightness) as `default:`**.
  The FIRST exception is the night-time dim nightlight (`scene.bedroom_nightlight`) when
  `bedroom_sleep_mode` is on OR it's 00:00–05:00 — so a presence re-trigger doesn't blast
  you (it wins over the wake ramp; at wake time sleep_mode is cleared and hour≥5, so it's false).
  **Note (since 2026-06-20):** while `sleep_mode` is on, `bedroom_presence_on` is GATED OFF entirely
  (see its conditions), so there is NO automatic "got up overnight" nightlight — getting up leaves
  the room dark and a B3 tap brings the nightlight back. The sleep-mode arm of this exception is thus
  reached via `presence_on` only in the 00:00–05:00 *no-sleep-mode* case (up late, not yet in bed);
  it still protects the B3-tap nightlight and any other direct caller of the dispatcher.
  The morning wake (1%→`wake_peak` over the 15 min ENDING at the real alarm; peak 50%, or 30% after a
  short night — see the sleep-quality bullet) is the next exception, its window
  = `sensor.bedroom_wake_start .. +15 min` (dynamic — see the dynamic-wake bullet below), encoded
  as `brightness = 1+(wake_peak-1)·elapsed_min/15` over `transition = (15-elapsed_min)·60`s (the window
  length is written in minutes; only the `transition` converts to seconds, because that is HA's service
  unit) — so `elapsed_min=0` equals the wake's start and pressing button 4 mid-window *resumes* the ramp. **Both Tap Dial button 4
  and the `bedroom_morning_reset` automation call this dispatcher** (single source of truth — no
  duplicated ramp math). Color temp ALWAYS comes from AL; exceptions override brightness only.
  Helper `script.bedroom_set_natural_brightness(brightness_pct, transition)` holds the AL
  release + color-apply boilerplate so a new exception is just a `(condition, brightness,
  transition)` triple dropped above `default:` — see the worked example comment in the file.
- **Threshold alerts — unified engine (since 2026-06-18).** `configuration.yaml` defines twelve
  built-in `threshold` binary-sensors; the platform's native hysteresis (on past bound±hyst) IS the
  "alert once + recovery, no bounce" lifecycle. ALL feed ONE automation `bedroom_threshold_alert`
  (files/automations.yaml) in four **categories** — air quality (CO2/PM2.5/VOC/NOx, `upper`),
  **air quality SEVERE** (same 4 at a higher cutoff), battery (FP300/Tap Dial, `lower`), humidity
  (high `upper` + low `lower`). The category is encoded in each trigger `id` (`<cat>_bad`/`<cat>_ok`);
  everything else (label/value/unit, message, coalescing `tag`) is derived generically from the
  triggering sensor. Per-category differences live in a Jinja `cfg` map: `pulse` (red light flash —
  air quality only, via `script.bedroom_alert_pulse` when lights on), `watch` (wrist buzz), `pierce`
  (sound through DND — **severe air quality only**), `recovery` (send a "back to normal" notice —
  severe skips it; the moderate recovery covers it). **All notify routes through `script.bedroom_notify`**
  (DND/sleep-aware — see the notification-routing bullet). **Anchored on `off`↔`on` (not `unknown`)**
  so an HA restart while bad doesn't re-alert and an unavailable source can't false-alert (offline is
  `bedroom_sensor_offline_alert`'s job). Per-category debounce: air quality 30s, battery 1m, humidity
  5m. The label-strip is `friendly_name | replace(' high','') | replace(' low','') | replace(' severe','')`.
  **Adding a metric** = one threshold sensor + add it to its category's two trigger lists; a **new
  category** = two trigger blocks + one `cfg` entry. Thresholds (incl. the severe cutoffs CO2 2000 /
  PM2.5 100 / VOC 400 / NOx 200) are starting points — tune in the ~2026-06-25 pass.
- **Notification routing — `script.bedroom_notify` (since 2026-06-18).** The single cross-cutting
  layer EVERY bedroom alert calls (threshold engine, sensor-offline, away). Fields:
  `title, message, tag, watch, pierce` (last two default false). Computes the Android `channel` +
  `importance` from `pierce` and the live **"quiet"** state (`sensor.pixel_9_pro_do_not_disturb_sensor`
  not `off` OR `input_boolean.bedroom_sleep_mode` on): `pierce` → high-importance "Bedroom critical"
  channel (sounds, can bypass DND); else "Bedroom alerts", **low/silent while quiet**, default
  otherwise. `watch` → also `notify.mobile_app_pixel_watch_3` (the mobile-app notify service; the
  un-prefixed `notify.pixel_watch_3` does NOT exist — every `watch:true` alert raised a
  `service_not_found` Repair per calling automation until fixed 2026-06-20). **One-time phone setup:**
  mark the "Bedroom
  critical" channel as a DND exception in Android (after the first critical alert creates it) — high
  importance alone doesn't pierce DND. Only **severe air quality** sets `pierce`; sensor-offline +
  air-quality set `watch`; battery/humidity/recoveries are routine (silent while quiet, phone-only).
- **Away-aware notification hold (since 2026-06-19).** `script.bedroom_notify` parks non-critical
  alerts while you're outside the home geofence. `away = person.daniel not in [home, unknown,
  unavailable]` (fails OPEN — a tracker glitch over-notifies, the opposite safe default to the
  unexpected-occupancy tripwire). While away + NOT `pierce`: instead of pushing, it
  `persistent_notification.create`s id `hold_<tag>` (so a re-fire with the same tag updates in place),
  then `stop`s before the push path. A recovery (`recovery: true`, same tag) `dismiss`es `hold_<tag>`
  and sends nothing — so a condition that self-resolves before you return is never seen. `pierce`
  alerts and the at-home path are unchanged. On arrival (`automation.bedroom_flush_held_notifications`,
  `person.daniel -> home`), all still-held `hold_*` notifications are delivered as ONE "While you were
  out (N)" digest (bulleted messages; phone-only, so per-alert action buttons like Boost fan are lost —
  tap into HA to act) and then dismissed; arriving with nothing held is silent. Recovery call-sites
  carrying `recovery: true`: threshold-ok, sensor-online, UPS-restored, zigbee-bridge-online.
  **Known limitation:** persistent notifications are in-memory, so an HA restart (e.g. a deploy) while
  away loses the held queue — accepted, since held items are non-critical and the overlap is rare.
  `match` filtering is start-anchored, so `hold_` never catches the pierce path's bare-`tag`
  persistent notifications.
- **Actionable notifications (since 2026-06-18).** `bedroom_notify` takes an optional `actions` list
  (`[{action, title}]`, phone-only) → the companion app renders buttons; taps fire
  `mobile_app_notification_action`, dispatched by `automation.bedroom_notification_action` on the
  namespaced `BEDROOM_*` action id. Wired buttons: air-quality bad → **Boost fan**
  (`BEDROOM_BOOST_FAN`: fan_manual on + 100% — persists until button-3/morning reset; moves air,
  doesn't lower CO2); away "Left on" → **Turn back on** (`BEDROOM_AWAY_TURN_ON`: apply_natural +
  apply_fan, ignores home-gates — undo a false-away); and a nightly **bedtime prompt**
  (`automation.bedroom_bedtime_prompt`, 22:00 if present + not in sleep mode + home) → **Start now**
  (`BEDROOM_START_BEDTIME` → `script.bedroom_bedtime`). Add a button = pass `actions` to
  `bedroom_notify` + a case in the dispatcher.
- **Update-available digest (since 2026-06-18).** `automation.update_available_digest` (homelab-wide,
  no `bedroom_` prefix) — Sunday 10:00, notifies a digest of any `update.*` entity that is `on`
  (Zigbee/sensor firmware + HACS integrations — the gap Renovate doesn't cover; LSIO container HA has
  no `update.home_assistant_*`). Generic over `states.update | selectattr('state','eq','on')` so new
  devices join automatically; gated to only fire when ≥1 update is pending. **Notify-only — never
  auto-flashes.** Routine via `bedroom_notify`. Zigbee versions/names are opaque (build ints / IEEE)
  until devices are renamed in Z2M.
- **CO₂ calibration reminder (since 2026-06-18).** `automation.bedroom_co2_calibration_reminder` —
  quarterly (1st of Jan/Apr/Jul/Oct at 10:00, daily-trigger + date condition like the update digest)
  notify-only nudge to recalibrate the AirGradient's drifting SenseAir CO₂ sensor. Message carries the
  live reading + an air-out-FIRST instruction (manual calibration sets the CURRENT reading as the 400
  ppm baseline). **No one-tap calibrate button by design** — an accidental tap on stale indoor air
  would lock in a wrong baseline. Routine via `bedroom_notify`. Keeps the air-quality thresholds honest.
- **UPS power-event alert (since 2026-06-18).** `automation.ups_power_event` (homelab-wide, no
  `bedroom_` prefix) — nothing watched the UPS before. Driven off the **raw NUT flags**
  `sensor.apc_ups_status_data` (`OL`=online, `OB`=on battery, `LB`=low battery, `CHRG`=charging — more
  reliable than the friendly status string). Triggers on every `status_data` change and derives the
  edge from `from_state`/`to_state`, **requiring BOTH sides valid (not `unavailable`)** so the
  `unavailable→OL` reconnect on each HA restart can't fire a spurious "Power restored" (same startup-spam
  trap as the sensor-offline recovery; verified `last_triggered=None` post-deploy). Three edges:
  on-battery (`watch`), low-battery (`watch`+`pierce` — server may shut down), restored (routine), one
  coalescing `ups_power` tag. Routes through `bedroom_notify`.
- **Unexpected-occupancy tripwire (since 2026-06-18).** `automation.bedroom_unexpected_occupancy` —
  FP300 presence `off→on` (`for: 30s`) while `person.daniel` is away (not home/unknown/unavailable)
  **and** has been away >5 min → a security alert via `bedroom_notify` (`watch: true, pierce: true`).
  Edge-triggered so a GPS glitch while you're physically present can't fire it (presence already on);
  the >5-min guard filters brief away-glitches; the fan is off while away (no airflow false-positive).
  Pure logic over two trusted sensors — pairs with the home/away work.
- **Sensor-offline alerts (since 2026-06-18).** `bedroom_sensor_offline_alert` (files/automations.yaml,
  a structural twin of the threshold engine) fires when a
  bedroom-automation dependency goes `unavailable` for 5 min, with a coalescing-tag recovery notice.
  Routed through `script.bedroom_notify` (offline: `watch:true` — wrist buzz, but routine for DND, so
  a dropout overnight doesn't wake you; recovery: routine, phone-only). The **recovery branch is
  gated on the device having been unavailable ≥5 min** (`to_state.last_changed − from_state.last_changed`)
  so the `unavailable→available` blip on every HA/Z2M restart doesn't fire a spurious "back online"
  (a real ≥5-min outage still notifies).
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
- **Home/away automations (since 2026-06-18).** Off `person.daniel` (HA person entity over
  `device_tracker.pixel_9_pro` GPS/Wi-Fi — a different layer than the FP300's ROOM presence).
  `bedroom_away` (two triggers, both `from:"home"`: `leave` at `for:10m`, `failsafe` at `for:30m`)
  turns off `light.bedroom_lights` + `fan.tower_fan` and notifies what was on; silent if nothing
  was on. `bedroom_arrive_home` (`to:"home"`) nudges the fan back (via `script.bedroom_apply_fan`)
  and re-checks lights only if FP300-present (no forced-on). **Load-bearing detail: every on-path
  is gated on `person.daniel == home`** — `bedroom_fan_temperature` + `bedroom_presence_on` get a
  `person home` condition, and `bedroom_morning_reset` wraps its DIRECT `apply_fan` call in
  `if person home` (it bypasses the fan automation's gate). Miss any one and the fan/lights switch
  on in an empty house. **Overrides (`bedroom_manual_off`/`bedroom_fan_manual`) are never written by
  home/away logic** — leave-off is unconditional, arrive routes through the apply_* scripts which
  read the overrides. Known gap: an HA restart while already away misses the `from:"home"` triggers
  (no live transition); the gates still prevent away-on so it self-corrects. Prereq for the
  unexpected-occupancy tripwire backlog item.
- **Bedtime / sleep routine (since 2026-06-18).** `script.bedroom_bedtime` (the shared "going to
  sleep" action) engages `input_boolean.bedroom_sleep_mode` (a quiet fan cap), flips AL into sleep
  mode (`switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom`, warm/dim), **fades**
  to `scene.bedroom_nightlight` (amber 3%) over 15 min, and re-applies the fan. The fade is a
  per-call `transition: 900` on `scene.turn_on` (NOT baked into the scene), so only bedtime ramps —
  the B3-press and overnight "got up" nightlight stay instant. AL sleep mode is engaged BEFORE the
  scene so the scene is the last command on the lights, and `take_over_control: true` +
  `detect_non_ha_changes: false` keep AL from re-stomping the group mid-fade. The bulb does the
  brightness+color ramp internally (single Zigbee command, ZCL caps ~6553s), so an HA/Z2M restart
  mid-fade doesn't abort it — only a bulb power-cycle would. Triggered by `automation.bedroom_bedtime`
  off `binary_sensor.pixel_watch_3_bedtime_mode` → on (gated `person.daniel == home`), with **Tap
  Dial button-3 (Sleep) HOLD** as the manual fallback (`bedroom_tap_dial_control`). **Charging is deliberately
  NOT a trigger** (operator charges in-room). **Fan stays temperature-responsive, just quieter:**
  `bedroom_apply_fan` caps the fan to Low (level 2) when `bedroom_sleep_mode` is on — below the
  22:00–06:00 Medium (level 4) night cap, via `cap = 2 if sleep else (4 if night else 9)`; it does
  NOT freeze the fan. `bedroom_morning_reset` unwinds both sleep_mode + AL sleep mode before its fan/
  light re-applies (later moves to the watch-alarm wake). Phone bedtime/sleep sensors (DND,
  sleep_confidence, next_alarm) are now enabled in the companion app; the watch exposes
  `sensor.pixel_watch_3_next_alarm` (the real wake alarm) + `notify.mobile_app_pixel_watch_3`.
- **Dynamic morning wake (since 2026-06-18).** The wake ramp is driven by the real alarm, not a
  hardcoded time. `sensor.bedroom_wake_start` (a `device_class: timestamp` template sensor in
  `files/templates.yaml`) = `sensor.pixel_watch_3_next_alarm − 15 min`, `availability:` gated to
  MORNING alarms only (local hour 03:00–11:00) so a nap/evening alarm never arms it. It's the SINGLE
  source of truth for the wake window `[wake_start, alarm)`: `bedroom_morning_reset` time-triggers
  `at: sensor.bedroom_wake_start` (id `alarm`), and both `bedroom_apply_natural`'s morning exception
  and `bedroom_presence_on`'s window read it (the old triplicated 06:00/07:00 formula + weekday/weekend
  split are GONE). `bedroom_morning_reset` also has a `09:00` `fallback` trigger that clears the
  overnight overrides (sleep mode, AL sleep, manual-off, fan-manual) on no-alarm days WITHOUT forcing
  lights; only the `alarm` trigger runs the ramp. **The wake ramp is gated on the GEOFENCE
  (`person.daniel == home`), NOT the FP300 room sensor** (changed 2026-06-19). The room presence
  sensor was the gate originally, but with `motion_sensitivity` reverted to `high` (no setting
  separates the running fan from a person — see the FP300 fan false-HOLD note) the radar drops a
  motionless sleeper, so `presence` can read `off`/`unknown` at the exact moment you need waking
  (e.g. right after an HA restart the battery Zigbee radio hasn't reported yet). `person home` is the
  reliable "you're here to be woken" signal and still won't ramp an empty bedroom while away (an FP300
  dog/false-positive can't trigger the wake either). **Uses the WATCH alarm** (`pixel_watch_3`), not the
  phone's (unreliable). Watch caveat moot now — set alarms anywhere; only morning ones wake.
- **Sleep-quality-aware morning (since 2026-06-18).** The wake ramp adapts to how you slept: in
  `bedroom_apply_natural`'s morning exception, `wake_peak` = 30% (gentler) if
  `sensor.pixel_9_pro_sleep_duration` is `0 < x < 360` min (under 6h), else 50% — unknown/0 falls
  back to 50%. `bedroom_morning_reset`'s alarm+present block also sends a routine "you slept N h"
  note (😴 short night / ☀️ good morning), skipped if sleep_duration is 0/unknown. **Caveat:** the
  Google Sleep API finalizes `sleep_duration` around wake, so at alarm−15min it can be stale —
  best-effort (graceful fallback to a normal wake). Only the peak changes; the window/transition and
  `presence_on` are untouched.
- **Temperature → fan control (since 2026-06-18; smoothed 2026-06-18).** `script.bedroom_apply_fan`
  (in `files/scripts.yaml`) drives `fan.tower_fan` (DREO, 9 levels) from
  `sensor.bedroom_airgradient_one_temperature` (°F) on a **smooth ~0.8-level-per-°F curve**: off below
  ~72°F, then `ideal = (t − 71)/1.3` → `round` clamped 1–9 (72→L1 … ~82→L9). A **~0.7-level hysteresis
  deadband** (`want` only steps when temp wants ≥0.7 level away from current; turning on jumps to the
  ideal) prevents flapping. **Level caps:** max **L4** during 22:00–06:00, max **L2** in sleep mode.
  Works in fan LEVELS, not raw %, because the DREO integration `math.ceil()`s a requested % up to the
  next level — send `(L−0.5)/9·100`% to hit level L. That `%`<->level conversion (and the `9`-level
  count) lives once in the `pct_to_level`/`level_to_pct` macros in `files/custom_templates/fan.jinja`,
  shared with `bedroom_fan_manual_detect` so the round-trip can't drift. Tune the curve via the `71`
  start offset / slope in `bedroom_apply_fan`.
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
  aware); the morning reset clears it too. Tune the fan curve (start offset / slope / caps) in
  `bedroom_apply_fan` only.
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
  (no HA restart). Cards: APC UPS, **AirGradient ONE air quality** (CO₂ gauge +
  pollutant glance — the metrics the threshold alerts fire on), the outdoor weather + AQI cards
  (see the outdoor-AQI bullet), DREO Tower Fan, and **Bedroom lighting + controls** — a
  `light.bedroom_lights` **`tile`** card carrying inline `light-brightness` + `light-color-temp`
  feature sliders (built-in features, no HACS; tap the tile for HA's full RGB color wheel in the
  more-info dialog — the Hue bulbs are `color_temp` + `xy`) stacked above the **Bedroom Controls**
  `entities` card (AL master switch + the three override booleans) — then the Aqara FP300 glance.
  **AL caveat:** Adaptive Lighting runs `take_over_control: true`, so dragging the color-temp (or
  brightness) slider pauses AL for that light until the next reset — expected, not a bug.
- **Outdoor AQI + window advisor (since 2026-06-20).** Open-Meteo's free air-quality API feeds
  four sensors: `sensor.outdoor_pm2_5` & `sensor.outdoor_pm10` (µg/m³), `sensor.outdoor_us_aqi`,
  `sensor.outdoor_ozone` — pulled via `files/rest.yaml` (copy'd, not templated; **no API key**;
  a `resource_template` reads `zone.home` lat/lon so the coordinates never enter git;
  `scan_interval: 1800` = poll every 30 min, the API being hourly). Two outdoor threshold
  `binary_sensor`s (inline in `configuration.yaml.j2`) wire into the existing **threshold-alert
  engine** as their own categories: `airqualityoutdoor` (`binary_sensor.outdoor_pm2_5_high`,
  `upper: 35` → alerts ≥ 40, moderate → `watch`) and `airqualityoutdoorsevere`
  (`binary_sensor.outdoor_pm2_5_severe`, `upper: 100` → alerts ≥ 105, wildfire tier →
  `watch`+`pierce`). Mirrors the indoor `airquality`/`airqualitysevere` split (one `watch`/`pierce`
  per category). The **"Open the window?"** advisor is `automation.bedroom_window_advisor`
  (gated on `person.daniel` home + not sleep mode). Triggers: (a) `binary_sensor.bedroom_co2_high`/
  `bedroom_voc_high` off→on (the stale-air edge); (b) indoor temp `numeric_state above: 78` (= the
  macro's `comfort_hi`) `for: 5m`; (c) `sensor.outdoor_pm2_5` change (the ~30-min poll). It calls
  the tested `custom_templates/ventilation.jinja` `ventilation_advice()` macro ONCE (numbers in →
  `'none'`/`'stale'`/`'cool'`): **`stale`** = indoor air stale (CO₂ or VOC high) AND outside clean
  & comfortable (55–78 °F); **`cool`** = indoor > 78 °F AND outdoor ≥ 5 °F cooler (`cool_delta`) AND
  outdoor air safe; `stale` outranks `cool`; the `choose:` no-ops on `none`. **Smoke guard (load-
  bearing):** the macro returns `none` whenever `outdoor_pm > 25` (`pm_safe`) OR
  `outdoor_pm > indoor_pm`, so it can never advise ventilating into worse/unsafe air. Notify is
  routine via `script.bedroom_notify` (`tag: window_advice`). Macro math unit-tested in
  `tests/test_ventilation_macros.py`; the HA `round` returns an int at precision 0
  (`forgiving_round`), so the "N° cooler" message renders cleanly. Dashboard:
  `weather.forecast_home` card + an outdoor-AQI glance (US AQI/PM2.5/PM10/ozone) next to the
  indoor AirGradient card in `ui-lovelace.yaml.j2`.
- **All persistent state is `./config` → `/config`** (Kopia-backed): the SQLite
  recorder DB, `.storage/`, secrets, automations, and the templated `configuration.yaml`.
  **The "could not validate that the sqlite3 database was shutdown cleanly" warning on every boot is
  benign and NOT fixable via `stop_grace_period`** — a timed `docker stop` hit the full grace and
  exited 137 (SIGKILL) at both 30s and 90s, so HA under the LSIO/s6 image is effectively hung on
  shutdown (HA core / the dreo cloud_push integration never finishes stopping). SQLite WAL auto-
  recovers, so don't chase it with a longer grace (it only slows deploys). Tested + reverted 2026-06-18.
- **Bridge networking, not host.** Cloud/API-based integrations work fine. **Local
  device discovery** (mDNS/SSDP, Bluetooth, Zigbee/Z-Wave USB dongles) generally needs
  `network_mode: host` and/or `devices:` passthrough — which is incompatible with the
  Traefik-label + bridge-network setup here. Switching to host mode is a separate,
  larger change; revisit only if you add local hardware.

## Testing
- **Bedroom Jinja math is unit-tested** (`tests/`, run via `uv run pytest` / the prek `pytest`
  hook / CI — wired in `pyproject.toml` `testpaths`). The bug-prone computed logic now lives in
  pure `custom_templates/{fan,lighting}.jinja` macros (entity/time reads — `states()`/`now()` —
  stay in the YAML callers; macros take plain numbers): `fan_target_level` (curve + ±0.7-level
  hysteresis + night/sleep caps, used by `bedroom_apply_fan`), `in_wake_window` /
  `wake_brightness` / `wake_transition` (morning ramp, used by `bedroom_apply_natural`), and
  `auto_light_allowed` (lux gate, used by `templates.yaml`'s `bedroom_auto_light_allowed`).
- The harness `tests/jinja_harness.py` renders macros in a bare Jinja2 env that mirrors the handful
  of HA filter overrides the macros use — most importantly HA's `round` is **banker's** rounding
  (`forgiving_round`, round-half-to-even, int at precision 0), NOT Jinja's stock half-away-from-zero
  float; the fan level math lands on `.5` midpoints by design, so this is load-bearing.
  `test_ha_round_semantics.py` pins it. `test_fan_macros.py` carries an old-inline-vs-macro
  equivalence grid (8.8k points) as a permanent behavior-preservation guard against the curve being
  changed in only one place.
- **Adding a tunable formula:** put the math in a `custom_templates/*.jinja` macro (numbers in →
  numbers/bool out), import it from the YAML caller, and add a test — don't inline new math in the
  automations. The `custom_templates/` deploy is a whole-directory copy, so a new `.jinja` ships
  automatically.
- **Config is structurally validated pre-deploy** by the `validate-ha-config` prek hook
  (`scripts/validate_ha_config.py`, runs locally + in CI on any change under the role's
  `templates/`+`files/`). Pure Python (no Docker): it assembles the deployed `/config` layout and
  checks YAML syntax, **duplicate keys**, broken `!include` targets, and the **syntax** of every
  inline `{{ }}`/`{% %}` template + each `custom_templates/*.jinja`. It does NOT do HA *schema*
  validation (unknown keys, bad integration options) or entity-existence checks — that needs
  `hass --script check_config` in a Docker HA image (out of scope); the deploy still catches schema
  errors live.

## Claude tooling for this role
- **`home-assistant-engineer` agent** (`.claude/agents/`) — read+write HA engineer that knows
  these conventions + traps; delegate HA authoring/debugging to it.
- **Skills** (`.claude/skills/`): `ha-edit-automation` (the authoring workflow — copy-not-template,
  math-in-a-tested-macro, validate→deploy→verify), `ha-deploy` (deploy + confirm-loaded),
  `ha-verify-state` (live state via the API; the recorder + alias-slug traps), `z2m-device-setting`
  (persist a Zigbee device setting via `mosquitto_pub`).
- **`scripts/probe.py ha`** — read-only live HA state (allow-listed, no prompt), authed with the
  SOPS `claude_ha_token`: `probe.py ha state <entity>` · `ha automation <id-or-alias>` (resolves
  the alias-slug≠id trap) · `ha get <api-path>` (e.g. `error_log`). Prefer it over recorder-DB reads.

## Editing
- Compose: `templates/docker-compose.yml.j2` · HA cfg: `templates/configuration.yaml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`
