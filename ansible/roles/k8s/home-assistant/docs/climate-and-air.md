# home-assistant — climate, air quality and the dashboard

Split out of the role's `CLAUDE.md` on 2026-08-15; the content is unchanged.
Fan control, the YAML dashboard and entity customization, and the outdoor-AQI window
advisor.

- **Temperature → fan control (since 2026-06-18; smoothed 2026-06-18).** `script.bedroom_apply_fan`
  (in `files/scripts.yaml`) drives `fan.tower_fan` (DREO, 9 levels) from
  `sensor.bedroom_airgradient_one_temperature` (°F) on a **smooth ~0.8-level-per-°F curve**: off below
  ~72°F, then `ideal = (t − 71)/1.3` → `round` clamped 1–9 (72→L1 … ~82→L9). A **~0.7-level hysteresis
  deadband** (`want` only steps when temp wants ≥0.7 level away from current; turning on jumps to the
  ideal) prevents flapping. **Level caps:** max **L4** during 22:00–06:00; in sleep mode an
  outdoor-temp seasonal **floor** (L2/L3/L4) up to an **L5 ceiling** (see the Bedtime/sleep bullet).
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
  **Tap Dial button 4 = reset the fan to automatic** (clear `bedroom_fan_manual` + apply, night-cap
  aware); the morning reset clears it too. **Button 3 (fan character): tap toggles oscillation
  (sweep↔fixed via `fan.oscillate` — orthogonal to speed, does not touch the override); hold
  force-stops the fan (engages `bedroom_fan_manual` first so the temp curve won't restart it, then
  `bedroom_fan_set` off).** Tune the fan curve (start offset / slope / caps) in
  `bedroom_apply_fan` only.
  **Manual fan survives a restart (since 2026-06-21).** A hand-set fan speed used to be undone by a
  deploy/restart: HA's unclean shutdown (SIGKILL) can drop the `bedroom_fan_manual` +
  `bedroom_fan_expected_level` helpers when they changed within ~15 min of the restart (the
  `restore_state` dump cycle), so they restore STALE (override off, old level) and
  `bedroom_fan_temperature` re-applies the auto level on the first post-boot temp report (observed live:
  a hand-set L3 bumped to L5 after a deploy — the documented [[ha-stale-override-restore-on-deploy]]
  trap, in the "lost my change" direction). Two coordinated fixes: (1) `bedroom_fan_temperature` now
  skips a temp **state** trigger whose `from_state` is `unknown`/`unavailable` (the boot re-report), so
  the restart can't drive the fan and the reconcile gets a clean window; (2) `bedroom_fan_startup_reconcile`
  (on `homeassistant` start) captures the fan's restored level FIRST (the DREO cloud keeps the physical
  speed across the restart), then once temp is known compares it to the **hysteresis-free** auto ideal
  (`fan_target_level` with `cur_level=0`) — under auto control the fan tracks within ~1 level of ideal,
  so a fan >1 level off was hand-set: it re-engages the override, re-arms `expected_level`, and
  re-asserts the level. Only when home + fan on + temp known; the `>1` tolerance means a legit auto level
  never trips it. Does NOT recover a manual speed within 1 level of the auto ideal (accepted).
- **YAML dashboard + entity customization (templated).** `configuration.yaml` registers a YAML
  dashboard via `lovelace: dashboards:` (NOT the legacy top-level `mode: yaml` — deprecated,
  removed in HA 2026.8) pointing at `config/ui-lovelace.yaml` (`templates/ui-lovelace.yaml.j2`),
  shown in the sidebar as "Bedroom". `homeassistant: customize: !include customize.yaml` holds
  friendly-name/icon overrides (`templates/customize.yaml.j2`). Both feed `common_config_changed`,
  so an edit recreates HA (~120s). Built-in cards only — no Lovelace `resources:`/`resource_mode:`.
  **The landing dashboard is NOT YAML-configurable:** HA opens its auto-generated areas "Overview"
  unless "Bedroom" is set as default in the UI (Settings → Dashboards → ⋮ → "Set as default for
  everyone" → persists in `.storage/core.config` `default_panel`, on the config PVC). **Fast loop for
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
  `outdoor_pm10 > 50` (`pm10_safe`) — the two ABSOLUTE air-quality caps. There is deliberately no
  relative "outdoor dirtier than indoors" term: it kept vetoing CO₂/cooling ventilation on
  safe-but-moderate outdoor air past a HEPA-scrubbed indoor (the recurring regression, dropped
  2026-07-01 after a relative margin then a relative floor both leaked). Objectively-safe air (under
  the caps) is fine to bring in — so it can never advise ventilating into unsafe air. Notify is
  routine via `script.bedroom_notify` (`tag: window_advice`). Macro math unit-tested in
  `tests/test_ventilation_macros.py`; the HA `round` returns an int at precision 0
  (`forgiving_round`), so the "N° cooler" message renders cleanly. Dashboard:
  `weather.forecast_home` card + an outdoor-AQI glance (US AQI/PM2.5/PM10/ozone) next to the
  indoor AirGradient card in `ui-lovelace.yaml.j2`.
