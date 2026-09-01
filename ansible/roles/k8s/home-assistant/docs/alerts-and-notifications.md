# home-assistant — alerts and notifications

Split out of the role's `CLAUDE.md` on 2026-08-15; the content is unchanged.
The threshold-alert engine, the `bedroom_notify` routing script, and every automation
that reports rather than actuates.

- **Threshold alerts — unified engine (since 2026-06-18).** `configuration.yaml` defines sixteen
  built-in `threshold` binary-sensors; the platform's native hysteresis (on past bound±hyst) IS the
  "alert once + recovery, no bounce" lifecycle. ALL feed ONE automation `bedroom_threshold_alert`
  (files/automations/alerts.yaml) in four **categories** — air quality (CO2/PM2.5/VOC/NOx, `upper`),
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
  alerts and the at-home path are unchanged. **`critical_away` (since 2026-06-24) also bypasses the
  hold** — it pushes WHILE away (at normal importance; unlike `pierce` it does NOT sound through DND
  at home) for damage-class alerts whose whole purpose is to reach you when nobody's home. Only the
  `temperature` threshold category sets it: the extreme-temperature alert is the away safety net for
  an AC/heating failure, so holding-then-digesting it (you'd see "92 °F" only after getting home,
  too late) defeats the point. A `critical_away` recovery pushes its all-clear too (no `hold_<tag>`
  to cancel). On arrival (`automation.bedroom_flush_held_notifications`,
  `person.daniel -> home`), all still-held `hold_*` notifications are delivered as ONE "While you were
  out (N)" digest (bulleted messages; phone-only, so per-alert action buttons like Boost fan are lost —
  tap into HA to act) and then dismissed; arriving with nothing held is silent. Recovery call-sites
  carrying `recovery: true`: threshold-ok, sensor-online, UPS-restored, zigbee-bridge-online.
  **Known limitation:** persistent notifications are in-memory, so an HA restart (e.g. a deploy) while
  away loses the held queue — accepted, since held items are non-critical and the overlap is rare (the
  one damage-class away alert, extreme temperature, is `critical_away` so it's pushed not held — it's
  not in the fragile queue).
  `match` filtering is start-anchored, so `hold_` never catches the pierce path's bare-`tag`
  persistent notifications.
- **Actionable notifications (since 2026-06-18).** `bedroom_notify` takes an optional `actions` list
  (`[{action, title}]`, phone-only) → the companion app renders buttons; taps fire
  `mobile_app_notification_action`, dispatched by `automation.bedroom_notification_action` on the
  namespaced `BEDROOM_*` action id. Wired buttons: air-quality bad → **Boost fan**
  (`BEDROOM_BOOST_FAN`: fan_manual on + 100% — persists until button-4/morning reset; moves air,
  doesn't lower CO2); away "Left on" → **Turn back on** (`BEDROOM_AWAY_TURN_ON`: apply_natural +
  apply_fan, ignores home-gates — undo a false-away); and a nightly **bedtime prompt**
  (`automation.bedroom_bedtime_prompt`, alarm-anchored: fires at `sensor.bedroom_winddown_start` =
  next morning alarm − 8h, with a 22:30 no-alarm fallback; gated: present + not in sleep mode +
  home) → **Start now** (`BEDROOM_START_BEDTIME` → `script.bedroom_bedtime`). Add a button = pass `actions` to
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
- **UPS energy for the Energy dashboard (since 2026-06-21).** The NUT integration exposes `ups.load`
  as a **percentage only** — the Energy dashboard needs energy (kWh). Two-hop chain: `sensor.ups_power`
  (`files/templates.yaml`, HA Jinja) converts load% to watts via `load% / 100 * 900`, where **900 W is
  `ups.realpower.nominal` read off the NUT server for THIS unit (APC Back-UPS RS 1500MS2) — change the
  constant if the UPS is swapped**; then a Riemann-sum `integration` platform sensor `sensor.ups_energy`
  (inline `sensor:` in `configuration.yaml.j2` — no HA Jinja, so it's fine in the verbatim-copied file)
  accumulates that to kWh and auto-stamps `device_class: energy` + `state_class: total` (what the
  dashboard requires). `method: left` (load is a step function) + `max_sub_interval: "00:05:00"` so a
  steady load still accrues. Accuracy is a **coarse estimate** (Back-UPS load% is quantized) covering
  **only UPS-connected gear** (server + networking), not the whole home. **Energy-dashboard gotcha
  (UI-only, `.storage/energy`, not YAML):** the "Individual devices" panel is **gated behind a
  configured Electricity Grid source** — with no grid it never appears. With the UPS as the only meter,
  add `sensor.ups_energy` directly as **Grid consumption**; if a whole-home meter is ever added, put
  THAT on grid and demote the UPS to an individual device.
- **Unexpected-occupancy tripwire (since 2026-06-18).** `automation.bedroom_unexpected_occupancy` —
  FP300 presence `off→on` (`for: 30s`) while `person.daniel` is away (not home/unknown/unavailable)
  **and** has been away >5 min → a security alert via `bedroom_notify` (`watch: true, pierce: true`).
  Edge-triggered so a GPS glitch while you're physically present can't fire it (presence already on);
  the >5-min guard filters brief away-glitches; the fan is off while away (no airflow false-positive).
  Pure logic over two trusted sensors — pairs with the home/away work.
- **Sensor-offline alerts (since 2026-06-18).** `bedroom_sensor_offline_alert` (files/automations/alerts.yaml,
  a structural twin of the threshold engine) fires when a
  bedroom-automation dependency goes `unavailable` for 5 min, with a coalescing-tag recovery notice.
  Routed through `script.bedroom_notify` (offline: `watch:true` — wrist buzz, but routine for DND, so
  a dropout overnight doesn't wake you; recovery: routine, phone-only). The **recovery branch is
  gated on the device having been unavailable ≥5 min** (`to_state.last_changed − from_state.last_changed`)
  so the `unavailable→available` blip on every HA/Z2M restart doesn't fire a spurious "back online"
  (a real ≥5-min outage still notifies).
  Watched (one representative entity per device — Z2M flips all of a device's entities together):
  `sensor.bedroom_airgradient_one_carbon_dioxide`, `binary_sensor.aqara_fp300_presence`,
  `fan.tower_fan`. **Required dependency: Z2M
  availability must be ON** (enabled 2026-06-18 in the zigbee2mqtt role) — without it the battery
  Zigbee FP300 never goes `unavailable` and this automation can't see it fail.
  **The Tap Dial (RDM002) is deliberately NOT watched (removed 2026-06-24).** It's a passive INPUT
  device, so going quiet from disuse trips Z2M's 60-min passive timeout as a routine false positive —
  nothing depends on the dial *reporting* (unlike the FP300/fan/AirGradient, which feed live
  automations). A truly dead dial is still caught by `binary_sensor.bedroom_tap_dial_battery_low`
  (the threshold engine) and is obvious on the next press, so offline detection added only noise.
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
  and re-checks lights only if FP300-present (no forced-on). **`bedroom_presence_on` and
  `bedroom_arrive_home` both have a `light.bedroom_lights == off` guard** — they only turn lights
  off→on; if the lights are already on, manual brightness is left untouched. **Load-bearing detail:
  every on-path is gated on `person.daniel == home`** — `bedroom_fan_temperature` + `bedroom_presence_on` get a
  `person home` condition, and `bedroom_morning_reset` wraps its DIRECT `apply_fan` call in
  `if person home` (it bypasses the fan automation's gate). Miss any one and the fan/lights switch
  on in an empty house. **Overrides (`bedroom_manual_off`/`bedroom_fan_manual`) are never written by
  home/away logic** — leave-off is unconditional, arrive routes through the apply_* scripts which
  read the overrides. Known gap: an HA restart while already away misses the `from:"home"` triggers
  (no live transition); the gates still prevent away-on so it self-corrects. Prereq for the
  unexpected-occupancy tripwire backlog item.
