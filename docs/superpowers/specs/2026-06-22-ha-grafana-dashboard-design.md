# Home Assistant Grafana Dashboard + Datasource-uid Guard — Design

**Date:** 2026-06-22
**Status:** Approved (design)

## Goal

A single curated Grafana dashboard for the live HA → Prometheus metrics (the `hass_*`
series, namespace `hass`), covering both long-term **trends** (climate, air quality, power)
and operational **health** (entity availability, batteries, automation activity). Plus a
deterministic guard that asserts every provisioned dashboard's datasource references resolve
to a provisioned datasource — closing the silent "No data" / stale-uid class.

This is the last deferred follow-up from the HA → Prometheus work (spec
`2026-06-22-ha-prometheus-design.md`, LIVE: 1169 `hass_*` series scraped, target `up`).

## Context (verified live, 2026-06-22)

- **54 distinct `hass_*` metric families** are scraped, each labelled with `entity`,
  `friendly_name`, `domain`. They cluster into climate/air-quality, room control (light/fan/
  switch), power/energy (the UPS integration), presence, and operational health
  (`hass_entity_available` over 222 entities / 28 domains, `hass_sensor_battery_percent`,
  `hass_automation_triggered_count_total` over 30 automations, `hass_state_change_total`).
- **Provisioning is code-first** (grafana role `CLAUDE.md`): dashboards are JSON under
  `files/dashboards/**`, loaded by a file-provider with `foldersFromFilesStructure: true`
  (each subdir → a Grafana folder) and `allowUiUpdates: true`. The `grafana_cfg_dashboards`
  register already recreates the container when `files/dashboards/` changes — no task edits.
- **Datasource uids are pinned literals**: Prometheus `EGdsQqhVk` (default), Loki
  `bf4q19tuivta8e`, both declared in `templates/provisioning/datasources.yml.j2`.

## Architecture

Hand-author one dashboard JSON (the documented "drop JSON, pin the uid, redeploy" path —
the UI-build-then-export path needs interactive clicking, which is not available to an agent,
and a fresh export of a hand-built board reproduces the same file). It plugs into the existing
provisioning with zero new Ansible plumbing.

- **File:** `ansible/roles/containers/grafana/files/dashboards/HomeAssistant/home-assistant.json`
- **Grafana folder:** `HomeAssistant` (created automatically from the subdir name)
- **uid:** `home-assistant-overview` · **title:** `Home Assistant — Overview`
- **schemaVersion:** 39 (the proven hand-built pattern in this repo — `traefik-custom`,
  `Terraria/player-stats` are both 39 with human-readable uids; Grafana migrates older
  schema versions up on load).
- **Datasource:** every panel pins `{"type": "prometheus", "uid": "EGdsQqhVk"}`.

## Panel inventory — 5 rows, ~18 panels

Every query targets a metric confirmed live. Legends use `{{friendly_name}}`. Metric names are
used **verbatim** — HA mangles unit suffixes (µg/m³ → `u0xb5g_per_mu0xb3`, no-unit → `_None`).

### Row 1 · Overview & Health
- **Stat** — Entities available: `count(hass_entity_available == 1)`; unavailable
  `count(hass_entity_available == bool 0)` (red when > 0).
- **Table** — Unavailable entities: `hass_entity_available == 0`, instant, columns
  `friendly_name` / `domain` (empty table = healthy).
- **Table** — Battery levels: `hass_sensor_battery_percent`, instant, sorted ascending,
  thresholds red < 20 / yellow < 40 / green ≥ 40.
- **Timeseries** — HA activity: `sum(rate(hass_state_change_total[$__rate_interval]))`.
- **Timeseries** — Busiest automations:
  `topk(10, rate(hass_automation_triggered_count_total[$__rate_interval]))`.

### Row 2 · Climate & Air Quality
- **Timeseries** — Temperature: `hass_sensor_temperature_celsius`.
- **Timeseries** — Humidity: `hass_sensor_humidity_percent`.
- **Timeseries** — CO₂: `hass_sensor_carbon_dioxide_ppm` (thresholds 800 / 1200 ppm).
- **Timeseries** — Illuminance: `hass_sensor_illuminance_lx`.
- **Timeseries** — Particulates: `hass_sensor_pm25_u0xb5g_per_mu0xb3`,
  `hass_sensor_pm10_u0xb5g_per_mu0xb3`, `hass_sensor_pm1_u0xb5g_per_mu0xb3`.
- **Gauge** — AQI: `hass_sensor_aqi_None`.

### Row 3 · Room Control
- **Timeseries** — Light brightness: `hass_light_brightness_percent` +
  `hass_switch_attr_brightness_pct`.
- **Timeseries** — Fan speed: `hass_fan_speed_percent`.
- **State timeline** — On/off: `hass_switch_state`, `hass_fan_state`.

### Row 4 · Power & Energy (UPS integration)
- **Timeseries** — Power draw (W): `hass_sensor_power_w`.
- **Stat** — Energy (kWh): `hass_sensor_energy_kwh` (last).
- **Timeseries** — Voltage (V): `hass_sensor_voltage_v`.

### Row 5 · Presence
- **State timeline** — `hass_person_state`, `hass_device_tracker_state`.
- **Stat** — People home: `count(hass_person_state == 1)` (`home` maps to 1).

## Datasource-uid guard — `scripts/validate_grafana_dashboards.py`

Mirrors the existing validator triad (`validate_compose_templates.py`,
`validate_ha_config.py`): a standalone script with a `validate()` returning an error list and a
`main()` exit code, a pytest companion, and a prek hook.

**Logic (deterministic, JSON-structural — not regex):**
1. Parse the provisioned uids from `datasources.yml.j2` (regex the `uid:` lines — that section
   carries no Jinja), giving `{EGdsQqhVk, bf4q19tuivta8e}`. Parsed, not hardcoded, so the guard
   tracks the template.
2. For each `files/dashboards/**/*.json`: recursively walk the parsed JSON; whenever a mapping
   has a `datasource` key, collect the referenced uid:
   - object form `{"type": ..., "uid": "X"}` → collect `X`;
   - legacy bare-string form `"datasource": "X"` → collect `X`;
   - `null` → skip.
   The dashboard's **own** top-level `uid` is never under a `datasource` key, so it is never
   collected (verified: `sadlil-loki-apps-dashboard*` are dashboard-own uids).
3. Drop built-in pseudo-datasources: `-- Grafana --`, `-- Mixed --`, `-- Dashboard --`,
   `grafana`.
4. Assert every remaining uid ∈ the provisioned set. On failure report file + offending uid
   (+ panel title when available). Exit non-zero.

**Prek hook** `validate-grafana-dashboards`: `entry = uv run python
scripts/validate_grafana_dashboards.py`, `pass_filenames = false`, `files` matching the
dashboards dir + `datasources.yml.j2` + the script itself.

## Testing & verification

- **Unit** (`scripts/test_validate_grafana_dashboards.py`):
  - object-form uid that resolves → clean;
  - unknown datasource uid → flagged;
  - legacy bare-string datasource form → handled;
  - built-in pseudo-datasources (`-- Grafana --` etc.) → ignored;
  - a dashboard's own top-level `uid` that is not a provisioned datasource → **never** flagged;
  - **regression guard:** the real role (all existing boards + the new one) passes clean.
- **Live (deploy gate):** after `uv run ansible-playbook ansible/deploy.yml --tags grafana`
  and `probe.py health grafana`, run each row's headline PromQL via `probe.py metric` to
  confirm > 0 series — catches a metric-name typo the uid guard cannot ("wired right, no data").
  Confirm the dashboard + "HomeAssistant" folder exist in Grafana.

## Out of scope (YAGNI)

- No template-variable entity-pickers — a single focused deployment; `friendly_name` legends
  suffice.
- No Grafana alerting rules — Kuma + HA own alerting.
- No Loki/log panels — this is a metrics board.
- No changes to the two generator scripts (`fetch_…`/`export_…`) — the board is hand-authored;
  a future UI edit + `export_grafana_dashboards.py` would re-capture it normally.
