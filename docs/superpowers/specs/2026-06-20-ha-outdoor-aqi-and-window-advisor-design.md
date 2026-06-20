# HA: outdoor air quality + weather data → AQI alerts + "open the window?" advisor

**Date:** 2026-06-20
**Status:** Approved — ready for implementation plan
**Scope:** `ansible/roles/containers/home-assistant/` — new `files/rest.yaml`, new
`files/custom_templates/ventilation.jinja` (+ `tests/test_ventilation_macros.py`), edits to
`templates/configuration.yaml.j2`, `files/automations.yaml`, `templates/ui-lovelace.yaml.j2`.
No secrets, no `containers/` edits, no fan-curve changes. Four loosely-coupled pieces (data source,
outdoor AQI alerts, window advisor, dashboard) that can be implemented incrementally.

## Problem

The bedroom suite alerts on *indoor* air (CO₂/PM2.5/VOC/NOx via the AirGradient) but has no
*outdoor* reference, so it can't answer the two questions that follow from "my air is stale":
**is it safe to ventilate** (wildfire smoke?) and **is opening the window actually better than the
fan** (cooler + cleaner outside?). Outdoor weather already exists (`weather.forecast_home`, Met.no)
but nothing consumes it. We want: (1) an outdoor air-quality data source, (2) outdoor-AQI threshold
alerts reusing the existing alert engine, (3) an "open the window?" advisor comparing indoor vs
outdoor, and (4) dashboard surfacing of both.

Out of scope by decision: feeding outdoor/forecast temp into the **fan curve** (keeps us out of the
tested `fan.jinja` macros).

## Provider decision — Open-Meteo Air Quality

Open-Meteo over OpenWeatherMap (whose key, `weather_api_key`, exists in SOPS and powers the homepage
widget) and over AirNow/WAQI:

- **No API key** → nothing in SOPS/git to manage; no `no_log` secret reference in the data source;
  no coupling of HA's air data to the same credential the homepage widget depends on.
- **US AQI 0–500** (EPA-category granular) vs OWM's coarse 1–5 index.
- **PM2.5/PM10 in µg/m³** — directly comparable to the AirGradient indoor reading (verified: indoor
  `sensor.bedroom_airgradient_one_temperature` and `weather.forecast_home` both report °F; the
  Open-Meteo `current.pm2_5` field is µg/m³, same unit as the indoor PM2.5 sensor).
- Modeled to exact coordinates; no nearest-station sparsity.

Architecture is a git-managed YAML `rest:` sensor against the public air-quality endpoint — **not**
the config-flow integration / HACS route, which would live in un-reproducible `.storage` (the same
reason automations/templates/scripts are copy'd files here, and why Adaptive Lighting's `.storage`
dependency is a known wart).

API confirmed live (2026-06-20):
```
GET https://air-quality-api.open-meteo.com/v1/air-quality
    ?latitude=..&longitude=..&current=pm2_5,pm10,us_aqi,ozone&timezone=America/Chicago
→ { "current": { "time":"…", "pm2_5":4.8, "pm10":4.9, "us_aqi":32, "ozone":96.0 }, … }
```

## Piece 1 — Data source (`files/rest.yaml`, copy'd; `rest: !include rest.yaml`)

A new copy'd file (like `automations.yaml`/`templates.yaml`), included from
`configuration.yaml.j2` with `rest: !include rest.yaml`. **Must be copy'd, not inline** — its
`value_template`s use `{{ value_json.* }}`, which Ansible's templater would mangle in the
`.j2` (the project's copy-not-template rule).

One REST resource, multiple sensors:

```yaml
# files/rest.yaml
- resource_template: >-
    https://air-quality-api.open-meteo.com/v1/air-quality?latitude={{ state_attr('zone.home','latitude') }}&longitude={{ state_attr('zone.home','longitude') }}&current=pm2_5,pm10,us_aqi,ozone&timezone=America%2FChicago
  scan_interval: 1800          # API is hourly; polling faster is pointless + impolite
  sensor:
    - name: "Outdoor PM2.5"
      unique_id: outdoor_pm2_5
      value_template: "{{ value_json.current.pm2_5 }}"
      unit_of_measurement: "µg/m³"
      device_class: pm25
      state_class: measurement
    - name: "Outdoor PM10"
      unique_id: outdoor_pm10
      value_template: "{{ value_json.current.pm10 }}"
      unit_of_measurement: "µg/m³"
      device_class: pm10
      state_class: measurement
    - name: "Outdoor US AQI"
      unique_id: outdoor_us_aqi
      value_template: "{{ value_json.current.us_aqi }}"
      device_class: aqi
      state_class: measurement
    - name: "Outdoor Ozone"
      unique_id: outdoor_ozone
      value_template: "{{ value_json.current.ozone }}"
      unit_of_measurement: "µg/m³"
      device_class: ozone
      state_class: measurement
```

- `resource_template` reading `zone.home` lat/long keeps coordinates out of git (they live in HA's
  `.storage` core config).
- Resulting entities: `sensor.outdoor_pm2_5`, `sensor.outdoor_pm10`, `sensor.outdoor_us_aqi`,
  `sensor.outdoor_ozone`.
- Wire the include in `configuration.yaml.j2` next to the existing `template: !include templates.yaml`.
  The copy task feeds `common_config_changed` like the other copy'd files, so an edit recreates HA.

## Piece 2 — Outdoor AQI alerts

Add `threshold` binary-sensors **inline** in `configuration.yaml.j2`'s existing `binary_sensor:`
block (plain YAML, no Jinja — safe inline, mirrors the existing 12). PM2.5-driven (the
wildfire-smoke signal); ozone optional, deferred unless wanted.

```yaml
- platform: threshold
  name: "Outdoor PM2.5 high"        # -> binary_sensor.outdoor_pm2_5_high
  entity_id: sensor.outdoor_pm2_5
  upper: 35                          # alerts >= 40, clears <= 30  (US AQI "moderate"+ region)
  hysteresis: 5
- platform: threshold
  name: "Outdoor PM2.5 severe"      # -> binary_sensor.outdoor_pm2_5_severe
  entity_id: sensor.outdoor_pm2_5
  upper: 100                         # alerts >= 105, clears <= 95   (wildfire / "unhealthy")
  hysteresis: 5
```

Wire into the existing `bedroom_threshold_alert` automation as a **new category**
`airqualityoutdoor`, per the documented recipe (two trigger blocks + one `cfg` entry):

- Moderate `outdoor_pm2_5_high` → `watch: true` (wrist buzz, like indoor air quality), with a
  recovery notice.
- Severe `outdoor_pm2_5_severe` → `pierce: true` (the one outdoor edge worth piercing DND — the
  hazard is sleeping with a window open during a smoke event; severe skips the recovery notice, as
  the other severe tiers do).
- Debounce consistent with the air-quality categories (30s).

Thresholds are starting points (tune in the same ~2026-06-25 pass as the indoor ones).

## Piece 3 — "Open the window?" advisor

New automation `bedroom_window_advisor` (`files/automations.yaml`) + new tested macro
`custom_templates/ventilation.jinja`. Two trigger paths, one shared verdict computed by the macro.
Both paths require **outdoor PM2.5 safe** — the system can never advise ventilating into bad air.

### Decision macro — `ventilation.jinja` (pure math, unit-tested)

Per the project rule: entity/`now()` reads stay in the YAML caller; the macro takes plain numbers
and returns a verdict. Proposed signature and starting constants (all tuneable here, in one place):

```jinja
{# returns one of: 'stale', 'cool', 'none' #}
{% macro ventilation_advice(indoor_temp, outdoor_temp, indoor_pm, outdoor_pm,
                            air_stale, comfort_lo=55, comfort_hi=78,
                            cool_delta=5, pm_safe=25) %}
{# guard: never advise opening into worse/unsafe outdoor air #}
{%- if outdoor_pm > pm_safe or outdoor_pm > indoor_pm -%}none
{%- elif air_stale and comfort_lo <= outdoor_temp <= comfort_hi -%}stale
{%- elif indoor_temp > comfort_hi and (indoor_temp - outdoor_temp) >= cool_delta -%}cool
{%- else -%}none
{%- endif -%}
{% endmacro %}
```

- `air_stale` = `binary_sensor.bedroom_co2_high` **or** `bedroom_voc_high` is `on` (reuses existing
  indoor threshold sensors — the same edges the indoor alert fires on).
- `outdoor_pm > indoor_pm` guard: never suggest opening when outside PM2.5 is worse than inside, even
  if below the absolute `pm_safe` cap.
- `'stale'` wins over `'cool'` when both apply (stale air is the more actionable nudge).

### Automation — `bedroom_window_advisor`

```
trigger:
  - indoor air went stale: binary_sensor.bedroom_co2_high OR bedroom_voc_high -> on
  - free-cooling poll: state change on sensor.outdoor_pm2_5 / weather.forecast_home / indoor temp
condition:
  - person.daniel == home
  - input_boolean.bedroom_sleep_mode == off        # no "open a window" at 3am
  - ventilation_advice(...) != 'none'              # macro verdict gates the whole thing
action:
  - choose on the verdict:
      'stale' -> bedroom_notify(... "Air's stale (CO₂ N ppm) — it's <out>°F & clean outside, crack a window")
      'cool'  -> bedroom_notify(... "It's <out>°F (<delta>° cooler) & clean outside — open up instead of the fan")
```

- Inputs: indoor temp `sensor.bedroom_airgradient_one_temperature`, outdoor temp
  `state_attr('weather.forecast_home','temperature')`, indoor PM `sensor.bedroom_airgradient_one_pm2_5`,
  outdoor PM `sensor.outdoor_pm2_5`, `air_stale` from the two indoor binary_sensors.
- **Routing:** routine via `script.bedroom_notify` — silent while DND/sleep, phone-only,
  away-hold-aware. Coalescing `tag: window_advice` so it updates in place rather than spamming.
- **Cooldown:** `mode: single` + the free-cooling poll trigger carries a `for:` debounce (e.g. 10m)
  so a temp hovering at the boundary doesn't re-fire each poll. The coalescing tag also limits
  visible spam. (Add an explicit cooldown helper later only if observed chatty.)
- **No action buttons in v1** (YAGNI) — the advice is informational; you act by opening a window.
- **Unit safety:** indoor and outdoor temp are both °F (verified); macro is unit-agnostic but callers
  must pass consistent units.

## Piece 4 — Dashboard

`templates/ui-lovelace.yaml.j2`: add a `weather` forecast card for `weather.forecast_home` and an
outdoor-AQI `glance`/`entities` card (`sensor.outdoor_us_aqi`, `sensor.outdoor_pm2_5`,
`sensor.outdoor_ozone`), placed adjacent to the existing indoor AirGradient card so indoor-vs-outdoor
reads at a glance. Built-in cards only (no Lovelace resources). Fast iteration: edit the rendered
file + Developer Tools → Reload Lovelace (no HA restart) per the role's notes.

## Testing / verification

- **Macro unit tests** — `tests/test_ventilation_macros.py` (like `test_fan_macros.py`), driven by
  the harness in `tests/jinja_harness.py`: cover the smoke guard (`outdoor_pm > pm_safe` → `none`),
  the `outdoor_pm > indoor_pm` guard, stale-vs-cool precedence, the comfort band edges, and the
  cool-delta boundary. Runs under `uv run pytest` / the prek `pytest` hook / CI.
- **Config validation** — the `validate-ha-config` prek hook checks the new `rest.yaml` +
  `ventilation.jinja` syntax, duplicate keys, and `!include` targets pre-deploy.
- **Deploy** — `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"` (recreates HA
  ~120s via `common_config_changed`); use the `ha-deploy` flow.
- **Confirm-loaded (not just "playbook ran"):**
  - `sensor.outdoor_pm2_5` etc. populate (not `unknown`/`unavailable`) after the first poll — check
    via `probe.py ha state sensor.outdoor_pm2_5`.
  - `bedroom_window_advisor` loaded — query by **alias-slug**, not id (the documented trap).
  - The new `airqualityoutdoor` triggers exist on `bedroom_threshold_alert` and didn't break the
    existing category logic.
  - Recorder is stale right after restart — verify liveness via container `StartedAt` /
    `last_triggered`, not recorder timestamps.

## Out of scope

- Fan-curve pre-cool from outdoor/forecast temp (explicitly declined — protects the tested fan macros).
- Pollen/dust/UV sensors from Open-Meteo (available; add later if wanted).
- Ozone threshold alert (PM2.5 is the primary ventilation-safety signal; add a second `cfg` entry
  later if desired).
- Actionable advisor buttons (e.g. "Opened it" snooze) — chose informational-only for v1.
- Switching the homepage widget or HA weather off OpenWeatherMap/Met.no — both stay as-is.
