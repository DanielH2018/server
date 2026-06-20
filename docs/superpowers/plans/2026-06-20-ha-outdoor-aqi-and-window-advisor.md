# HA Outdoor AQI + "Open the Window?" Advisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add outdoor air-quality + weather awareness to the bedroom Home Assistant suite — outdoor PM2.5/AQI data, smoke-aware threshold alerts, an "open the window?" advisor, and dashboard cards — reusing the existing alert engine and notification layer.

**Architecture:** A git-managed YAML `rest:` sensor pulls Open-Meteo air quality (no API key, coordinates read from `zone.home`). Outdoor PM2.5 `threshold` binary-sensors feed the existing `bedroom_threshold_alert` engine as two new categories. A new tested Jinja macro (`ventilation.jinja`) decides whether to advise opening a window; a new automation reads entities, calls the macro, and notifies via the existing `script.bedroom_notify`. Dashboard cards surface the existing `weather.forecast_home` plus the new outdoor sensors.

**Tech Stack:** Home Assistant (LinuxServer.io image), YAML config + HA Jinja, the RESTful integration, Ansible (deploy), pytest (macro unit tests via the repo's `jinja_harness`), the `validate-ha-config` prek hook.

## Global Constraints

- **`containers/` is read-only** — edit only under `ansible/roles/containers/home-assistant/`. Never edit the rendered files in `containers/`.
- **Copy-not-template rule:** any HA file containing `{{ }}`/`{% %}` Jinja MUST be a `files/` static file deployed by `ansible.builtin.copy` (NOT a `templates/*.j2`, which Ansible would try to render). Plain-YAML-only config (no Jinja) MAY live inline in `configuration.yaml.j2`.
- **Math lives in a tested macro:** put computed/tunable logic in `files/custom_templates/*.jinja` (plain numbers in → value out), import it from the YAML caller, and add a unit test. Don't inline new math in automations.
- **HA Jinja `round` is banker's rounding** (round-half-to-even, int at precision 0) — the `jinja_harness` already mirrors this; rely on it, don't reimplement.
- **Config-change wiring:** every templated/copied config file must feed `common_config_changed` in `tasks/main.yml`, or an edit won't recreate the container on deploy.
- **Verify "loaded", not "playbook ran":** after deploy, confirm live state via `scripts/probe.py ha …`. Query automations by their **alias-slug**, not their `id`. The recorder DB is stale right after a restart — judge liveness by container `StartedAt` / `last_triggered`, never recorder timestamps.
- **Units:** indoor (`sensor.bedroom_airgradient_one_temperature`) and outdoor (`weather.forecast_home` temperature attribute) are both **°F**; Open-Meteo `current.pm2_5` and the indoor PM2.5 sensor are both **µg/m³**. No conversions.
- **Validation command** (run from repo root): `uv run python scripts/validate_ha_config.py` (scans the role automatically, no args). Unit tests: `uv run pytest`.
- **Deploy command:** `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"` (recreates HA ~120s). The `ha-deploy` skill wraps deploy + verify.
- **Commit trailer:** end every commit message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Stay on `master`; commit explicit paths only (a second Claude session may be live).

---

### Task 1: Open-Meteo air-quality REST data source

**Files:**
- Create: `ansible/roles/containers/home-assistant/files/rest.yaml`
- Modify: `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` (add the `rest: !include rest.yaml` line near the existing `template: !include templates.yaml`, ~line 176)
- Modify: `ansible/roles/containers/home-assistant/tasks/main.yml` (new copy task + add its register var to `common_config_changed`)
- Modify: `scripts/validate_ha_config.py:35` (add `rest.yaml` to `_STATIC_FILES`)

**Interfaces:**
- Produces entities consumed by later tasks: `sensor.outdoor_pm2_5`, `sensor.outdoor_pm10`, `sensor.outdoor_us_aqi`, `sensor.outdoor_ozone` (PM values in µg/m³).

- [ ] **Step 1: Create `files/rest.yaml`**

```yaml
---
# Managed by Ansible (roles/containers/home-assistant/files/rest.yaml).
# Source of truth — copied to ./config (NOT templated: the {{ value_json }} / {{ state_attr }}
# Jinja would be mangled by Ansible's templater) and included via `rest: !include rest.yaml`.
# HA UI edits are NOT persistent. To change, edit this file and redeploy home-assistant.
#
# Open-Meteo Air Quality (free, NO API key). resource_template reads zone.home lat/long so the
# coordinates stay in HA's .storage, never in git. API updates hourly, so scan every 30 min.
# Fields: current.pm2_5 / pm10 (µg/m³, comparable to the indoor AirGradient), us_aqi (0-500), ozone.
- resource_template: >-
    https://air-quality-api.open-meteo.com/v1/air-quality?latitude={{ state_attr('zone.home','latitude') }}&longitude={{ state_attr('zone.home','longitude') }}&current=pm2_5,pm10,us_aqi,ozone&timezone=America%2FChicago
  scan_interval: 1800
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

- [ ] **Step 2: Add the include to `configuration.yaml.j2`**

Immediately after the existing `template: !include templates.yaml` line (~line 176), add:

```yaml
# Outdoor air quality (Open-Meteo, no key) — git-managed REST sensor. Copy'd (files/rest.yaml),
# not inline, because its value_templates use {{ value_json }} that Ansible would mangle. Feeds the
# outdoor PM2.5 threshold alerts (below) and the window advisor (files/automations.yaml).
rest: !include rest.yaml
```

- [ ] **Step 3: Add `rest.yaml` to the validator's static-file list**

In `scripts/validate_ha_config.py`, line 35 currently reads:

```python
_STATIC_FILES = ["automations.yaml", "scenes.yaml", "scripts.yaml", "templates.yaml"]
```

Change it to:

```python
_STATIC_FILES = ["automations.yaml", "scenes.yaml", "scripts.yaml", "templates.yaml", "rest.yaml"]
```

(Without this the validator's `assemble_config` won't copy `rest.yaml` into its temp layout, and the `!include rest.yaml` resolves to a missing file.)

- [ ] **Step 4: Add the copy task + config-change wiring in `tasks/main.yml`**

After the "Deploy template sensors from static file" task (ends ~line 76), add:

```yaml
- name: Deploy REST sensors from static file
  tags: [config]
  ansible.builtin.copy:
    src: rest.yaml
    dest: "/home/{{ sys_user }}/server/containers/{{ container_item.name }}/config/rest.yaml"
    mode: "0664"
  register: home_assistant_rest
```

Then add `(home_assistant_rest is changed)` to the `common_config_changed` expression (~line 96):

```yaml
    common_config_changed: "{{ (home_assistant_config is changed) or (home_assistant_customize is changed) or (home_assistant_dashboard is changed) or (home_assistant_automations is changed) or (home_assistant_scenes is changed) or (home_assistant_scripts is changed) or (home_assistant_templates is changed) or (home_assistant_rest is changed) or (home_assistant_custom_templates is changed) }}"
```

- [ ] **Step 5: Validate config + run unit tests**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exits 0, no errors (it now resolves `rest: !include rest.yaml` and Jinja-parses the value/resource templates).

Run: `uv run pytest`
Expected: all tests pass (the `_STATIC_FILES` change doesn't break the validator's own suite).

- [ ] **Step 6: Lint the Ansible change**

Run: `uv run ansible-lint ansible/roles/containers/home-assistant/tasks/main.yml`
Expected: passes (no new findings).

- [ ] **Step 7: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/rest.yaml \
        ansible/roles/containers/home-assistant/templates/configuration.yaml.j2 \
        ansible/roles/containers/home-assistant/tasks/main.yml \
        scripts/validate_ha_config.py
git commit -m "feat(home-assistant): add Open-Meteo outdoor air-quality REST sensors

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Outdoor PM2.5 threshold binary-sensors

**Files:**
- Modify: `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` (append to the existing inline `binary_sensor:` block, after the humidity-low sensor ~line 170)

**Interfaces:**
- Consumes: `sensor.outdoor_pm2_5` (Task 1).
- Produces: `binary_sensor.outdoor_pm2_5_high`, `binary_sensor.outdoor_pm2_5_severe` (consumed by Task 3).

- [ ] **Step 1: Add the two threshold sensors**

These are plain YAML (no Jinja), so they belong inline in `configuration.yaml.j2`, mirroring the existing 12 threshold sensors. Append inside the `binary_sensor:` list, after the `Bedroom humidity low` block (~line 170):

```yaml
  # Outdoor air quality (source = Open-Meteo sensor.outdoor_pm2_5, files/rest.yaml). Two tiers like
  # the indoor air-quality sensors: moderate (high) and the wildfire/"unhealthy" cutoff (severe).
  # Feed the airqualityoutdoor / airqualityoutdoorsevere categories of bedroom_threshold_alert AND
  # the smoke guard in the window advisor. Starting points — tune with the indoor ones (~2026-06-25).
  - platform: threshold
    name: "Outdoor PM2.5 high"        # -> binary_sensor.outdoor_pm2_5_high
    entity_id: sensor.outdoor_pm2_5
    upper: 35                          # alerts >= 40, clears <= 30 µg/m³ (US AQI "moderate"+)
    hysteresis: 5
  - platform: threshold
    name: "Outdoor PM2.5 severe"      # -> binary_sensor.outdoor_pm2_5_severe
    entity_id: sensor.outdoor_pm2_5
    upper: 100                         # alerts >= 105, clears <= 95 µg/m³ (wildfire / "unhealthy")
    hysteresis: 5
```

- [ ] **Step 2: Validate config**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exits 0 (no duplicate keys, valid YAML).

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/home-assistant/templates/configuration.yaml.j2
git commit -m "feat(home-assistant): add outdoor PM2.5 threshold binary-sensors

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Wire outdoor AQI into the threshold-alert engine

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml` (the `bedroom_threshold_alert` automation: add 4 triggers ~after line 444, add 2 `cfg` entries ~line 468)

**Interfaces:**
- Consumes: `binary_sensor.outdoor_pm2_5_high`, `binary_sensor.outdoor_pm2_5_severe` (Task 2); `script.bedroom_notify` (existing).
- Two categories are needed (NOT one): the `cfg` map carries a single `watch`/`pierce` per category, and we want moderate→watch-only, severe→pierce — mirroring the indoor `airquality`/`airqualitysevere` split. Category names must contain no underscores (the engine does `trigger.id.split('_')[0]`).

- [ ] **Step 1: Add four trigger blocks**

In `bedroom_threshold_alert`, after the `humidity_ok` trigger block (ends ~line 444, the last item before `action:`), add:

```yaml
    - platform: state
      entity_id:
        - binary_sensor.outdoor_pm2_5_high
      from: "off"
      to: "on"
      for: "00:00:30"
      id: airqualityoutdoor_bad
    - platform: state
      entity_id:
        - binary_sensor.outdoor_pm2_5_high
      from: "on"
      to: "off"
      for: "00:00:30"
      id: airqualityoutdoor_ok
    - platform: state
      entity_id:
        - binary_sensor.outdoor_pm2_5_severe
      from: "off"
      to: "on"
      for: "00:00:30"
      id: airqualityoutdoorsevere_bad
    - platform: state
      entity_id:
        - binary_sensor.outdoor_pm2_5_severe
      from: "on"
      to: "off"
      for: "00:00:30"
      id: airqualityoutdoorsevere_ok
```

- [ ] **Step 2: Add two `cfg` map entries**

In the `cfg` variable (the Jinja map ~lines 465-468), add two entries inside the map literal (after the `humidity` line, before the closing `}[category] }}`). Outdoor alerts never pulse the bedroom lights (`pulse: false`); moderate buzzes the watch, severe pierces DND (a smoke event you'd want known even asleep with a window open):

```yaml
              'airqualityoutdoor':       {'bad': '🌫️ Outdoor air', 'ok': '✅ Outdoor air', 'pulse': false, 'watch': true,  'pierce': false, 'recovery': true},
              'airqualityoutdoorsevere': {'bad': '🚨 Outdoor air HIGH', 'ok': '✅ Outdoor air', 'pulse': false, 'watch': true, 'pierce': true, 'recovery': false},
```

The resulting map literal must remain valid (each line comma-separated; the final `humidity` entry already has no trailing comma — add a comma after the `humidity` `}` and append the two new lines, leaving no trailing comma after the last). The label-strip (`replace(' high','') | replace(' severe','')`) turns "Outdoor PM2.5 high"/"…severe" into "Outdoor PM2.5"; `src` resolves to `sensor.outdoor_pm2_5` (the threshold sensor's `entity_id` attribute), so messages read "Outdoor PM2.5 is 42 µg/m³". No change to `boost_actions` — a fan boost doesn't help outdoor air, and outdoor categories aren't in its list.

- [ ] **Step 3: Validate config**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exits 0 (valid YAML + the inline Jinja `cfg` map parses).

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/automations.yaml
git commit -m "feat(home-assistant): outdoor PM2.5 alerts via the threshold engine

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `ventilation.jinja` advisor macro (TDD)

**Files:**
- Create: `ansible/roles/containers/home-assistant/files/custom_templates/ventilation.jinja`
- Create: `ansible/roles/containers/home-assistant/tests/test_ventilation_macros.py`

**Interfaces:**
- Produces: macro `ventilation_advice(indoor_temp, outdoor_temp, indoor_pm, outdoor_pm, air_stale, comfort_lo=55, comfort_hi=78, cool_delta=5, pm_safe=25)` → returns the string `'stale'`, `'cool'`, or `'none'`. Consumed by Task 5's automation.
- Consumes: the repo's `tests/jinja_harness.py` `render_macro(file, macro, *args)` (already on the pytest pythonpath, same as `test_lighting_macros.py`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_ventilation_macros.py`:

```python
"""Unit tests for the ventilation advisor macro in custom_templates/ventilation.jinja."""
from jinja_harness import render_macro

VENT = "ventilation.jinja"


def _advice(indoor_temp, outdoor_temp, indoor_pm, outdoor_pm, air_stale):
    return render_macro(VENT, "ventilation_advice",
                        indoor_temp, outdoor_temp, indoor_pm, outdoor_pm, air_stale)


def test_smoke_guard_blocks_when_outdoor_pm_unsafe():
    # Stale + comfortable, but outdoor PM2.5 over the safe cap -> never advise ventilating.
    assert _advice(80, 65, 5, 50, True) == "none"


def test_blocks_when_outdoor_dirtier_than_indoor_even_if_under_cap():
    # Both under the 25 cap, but outside (10) is dirtier than inside (5).
    assert _advice(80, 65, 5, 10, True) == "none"


def test_stale_air_when_clean_and_comfortable():
    assert _advice(75, 65, 8, 6, True) == "stale"


def test_stale_blocked_when_too_cold_outside():
    assert _advice(75, 40, 8, 6, True) == "none"


def test_stale_blocked_when_too_hot_outside():
    assert _advice(75, 90, 8, 6, True) == "none"


def test_free_cooling_when_warm_inside_and_cooler_clean_outside():
    assert _advice(82, 70, 8, 6, False) == "cool"


def test_cooling_needs_minimum_delta():
    assert _advice(80, 77, 8, 6, False) == "none"   # only 3°F cooler (< 5)


def test_cooling_needs_indoor_above_comfort():
    assert _advice(77, 60, 8, 6, False) == "none"   # 77 not > comfort_hi 78


def test_stale_outranks_cool():
    # Both stale and a cooling opportunity apply -> stale wins.
    assert _advice(82, 70, 8, 6, True) == "stale"


def test_comfort_band_edges_are_inclusive():
    assert _advice(75, 55, 8, 6, True) == "stale"   # lower edge
    assert _advice(75, 78, 8, 6, True) == "stale"   # upper edge
    assert _advice(75, 79, 8, 6, True) == "none"    # just past upper edge


def test_pm_safe_boundary():
    # ip high so the "dirtier than indoor" guard doesn't mask the cap test.
    assert _advice(75, 65, 30, 25, True) == "stale"  # 25 is not > 25 (cap is strict >)
    assert _advice(75, 65, 30, 26, True) == "none"   # 26 > 25 cap
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_ventilation_macros.py -v`
Expected: FAIL — the macro file doesn't exist yet (template-not-found / render error).

- [ ] **Step 3: Write the macro**

Create `files/custom_templates/ventilation.jinja`:

```jinja
{# Ventilation advisor — should you open a window? Pure decision math; entity reads (states/now)
   stay in the caller (bedroom_window_advisor, files/automations.yaml). Returns 'stale' (indoor air
   is stale, ventilate), 'cool' (free cooling available), or 'none' (don't suggest). Every non-'none'
   verdict REQUIRES outdoor air to be both under the safe cap AND no dirtier than indoors — the
   system can never advise ventilating into worse/unsafe air (the smoke guard). Args coerce inside
   (like lighting.jinja) so a caller may pass strings or numbers. Unavailable -> safe 'none'.
   Unit-tested in tests/test_ventilation_macros.py. Tune the comfort band / cool delta / PM cap here.
   'stale' outranks 'cool' when both apply. #}
{%- macro ventilation_advice(indoor_temp, outdoor_temp, indoor_pm, outdoor_pm, air_stale,
                             comfort_lo=55, comfort_hi=78, cool_delta=5, pm_safe=25) -%}
{%- set it = indoor_temp | float(0) -%}
{%- set ot = outdoor_temp | float(999) -%}
{%- set ip = indoor_pm | float(0) -%}
{%- set op = outdoor_pm | float(999) -%}
{%- set stale = air_stale | bool -%}
{%- if op > pm_safe or op > ip -%}none
{%- elif stale and comfort_lo <= ot <= comfort_hi -%}stale
{%- elif it > comfort_hi and (it - ot) >= cool_delta -%}cool
{%- else -%}none
{%- endif -%}
{%- endmacro -%}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_ventilation_macros.py -v`
Expected: PASS (all 11 tests).

- [ ] **Step 5: Validate config (the validator Jinja-parses the new macro)**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exits 0.

- [ ] **Step 6: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/custom_templates/ventilation.jinja \
        ansible/roles/containers/home-assistant/tests/test_ventilation_macros.py
git commit -m "feat(home-assistant): tested ventilation-advice macro

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `bedroom_window_advisor` automation

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/automations.yaml` (append a new automation at the end of the list, after `ha_heartbeat` ~line 1084)

**Interfaces:**
- Consumes: `ventilation.jinja::ventilation_advice` (Task 4); `sensor.outdoor_pm2_5` (Task 1); `sensor.bedroom_airgradient_one_temperature`, `sensor.bedroom_airgradient_one_pm2_5`, `sensor.bedroom_airgradient_one_carbon_dioxide`, `binary_sensor.bedroom_co2_high`, `binary_sensor.bedroom_voc_high`, `weather.forecast_home`, `person.daniel`, `input_boolean.bedroom_sleep_mode`, `script.bedroom_notify` (all existing).

- [ ] **Step 1: Append the automation**

Add at the end of `files/automations.yaml`:

```yaml
# Window advisor: suggest opening a window when it's worth it AND safe. Two paths, both gated on
# OUTDOOR air being safe + no dirtier than indoors (the smoke guard lives in ventilation.jinja, so
# this can never advise ventilating into bad air): (a) STALE — indoor CO2/VOC crossed high while it's
# clean + comfortable outside; (b) COOL — the room is warm and it's meaningfully cooler + clean
# outside (free cooling vs. running the fan). The decision is the tested ventilation_advice macro;
# this automation only reads entities + notifies. Routine via bedroom_notify (silent while DND/sleep,
# phone-only, away-hold-aware), coalescing tag so a persistent condition refreshes in place. The
# cooling 'above: 78' trigger must track the macro's comfort_hi default (78). No cooldown initially —
# the routine channel updates the same tag silently; add a debounce only if it proves chatty.
- id: bedroom_window_advisor
  alias: Bedroom window advisor
  description: Suggest opening a window for stale air or free cooling when outdoor air is safe.
  mode: single
  trigger:
    # Stale-air edge: indoor CO2/VOC crosses high (fires promptly, once per episode).
    - platform: state
      entity_id:
        - binary_sensor.bedroom_co2_high
        - binary_sensor.bedroom_voc_high
      from: "off"
      to: "on"
    # Free-cooling: the room crosses into "warm" (edge, 5-min stable — NOT a `for:` on a noisy temp
    # value, which would never settle). Re-checked when outdoor PM updates (~every 30 min) so a
    # clearing-outside while already warm still nudges. A time-pattern is intentionally avoided.
    - platform: numeric_state
      entity_id: sensor.bedroom_airgradient_one_temperature
      above: 78
      for: "00:05:00"
    - platform: state
      entity_id: sensor.outdoor_pm2_5
  condition:
    - condition: state
      entity_id: person.daniel
      state: "home"
    - condition: state
      entity_id: input_boolean.bedroom_sleep_mode
      state: "off"
  action:
    # The macro is evaluated ONCE here (DRY — no duplicate call in a condition gate). When the
    # verdict is 'none' (e.g. an outdoor-PM poll fired but nothing's actionable) the choose below
    # matches no branch and the automation no-ops. Single-line {% from %}{{ }} (no surrounding
    # whitespace) so verdict is exactly 'stale'/'cool'/'none' — same single-line import pattern as
    # the fan.jinja use elsewhere in this file.
    - variables:
        verdict: "{% from 'ventilation.jinja' import ventilation_advice %}{{ ventilation_advice(states('sensor.bedroom_airgradient_one_temperature'), state_attr('weather.forecast_home', 'temperature'), states('sensor.bedroom_airgradient_one_pm2_5'), states('sensor.outdoor_pm2_5'), is_state('binary_sensor.bedroom_co2_high', 'on') or is_state('binary_sensor.bedroom_voc_high', 'on')) }}"
        out_temp: "{{ state_attr('weather.forecast_home', 'temperature') | round }}"
        in_temp: "{{ states('sensor.bedroom_airgradient_one_temperature') | round }}"
        co2: "{{ states('sensor.bedroom_airgradient_one_carbon_dioxide') }}"
    - choose:
        - conditions: "{{ verdict == 'stale' }}"
          sequence:
            - service: script.bedroom_notify
              data:
                title: "🪟 Open a window?"
                message: "Air's stale (CO₂ {{ co2 }} ppm) — it's {{ out_temp }}°F & clean outside. Crack a window."
                tag: window_advice
        - conditions: "{{ verdict == 'cool' }}"
          sequence:
            - service: script.bedroom_notify
              data:
                title: "🪟 Free cooling"
                message: "It's {{ out_temp }}°F outside ({{ in_temp - out_temp }}° cooler) & clean — open up instead of the fan."
                tag: window_advice
```

- [ ] **Step 2: Validate config**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exits 0 (YAML valid; the inline `{% from 'ventilation.jinja' import … %}` templates parse).

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/automations.yaml
git commit -m "feat(home-assistant): open-the-window advisor automation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Dashboard cards (outdoor weather + AQI)

**Files:**
- Modify: `ansible/roles/containers/home-assistant/templates/ui-lovelace.yaml.j2` (add cards after the AirGradient block ~line 79, before the DREO Tower Fan block ~line 80)

**Interfaces:**
- Consumes: `weather.forecast_home` (existing); `sensor.outdoor_us_aqi`, `sensor.outdoor_pm2_5`, `sensor.outdoor_pm10`, `sensor.outdoor_ozone` (Task 1).

- [ ] **Step 1: Add the weather + outdoor-AQI cards**

Insert directly after the AirGradient `vertical-stack` block (after line 79, the `name: PM10` entry that closes the indoor air-quality card) and before the `# ── DREO Tower Fan` comment, so outdoor sits next to indoor:

```yaml
      # ── Outdoor weather + air quality (Met.no forecast + Open-Meteo AQI) ──
      - type: vertical-stack
        cards:
          - type: weather-forecast
            entity: weather.forecast_home
            name: Outdoor
            show_current: true
            show_forecast: true
            forecast_type: daily
          - type: glance
            title: Outdoor Air
            columns: 4
            entities:
              - entity: sensor.outdoor_us_aqi
                name: US AQI
              - entity: sensor.outdoor_pm2_5
                name: PM2.5
              - entity: sensor.outdoor_pm10
                name: PM10
              - entity: sensor.outdoor_ozone
                name: Ozone
```

- [ ] **Step 2: Validate config**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exits 0 (`ui-lovelace.yaml` is an entry file the validator loads).

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/home-assistant/templates/ui-lovelace.yaml.j2
git commit -m "feat(home-assistant): dashboard cards for outdoor weather + AQI

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Deploy + live verification

**Files:** none (deploy + verify only).

**Interfaces:** consumes everything above.

- [ ] **Step 1: Final pre-deploy gate**

Run: `uv run pytest && uv run python scripts/validate_ha_config.py`
Expected: all tests pass; validator exits 0.

- [ ] **Step 2: Deploy**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`
Expected: completes; HA recreated (`common_config_changed` true via the new `home_assistant_rest` register + the edited templates/files). ~120s.

- [ ] **Step 3: Confirm the container is healthy**

Run: `uv run python scripts/probe.py health home-assistant`
Expected: exits 0 (running + healthy).

- [ ] **Step 4: Confirm the REST sensors populated**

Run: `uv run python scripts/probe.py ha state sensor.outdoor_pm2_5`
Expected: a numeric value (µg/m³), NOT `unknown`/`unavailable`. (First poll happens on startup; if `unknown`, wait ~1 min and re-check — the REST resource fetches on load.) Spot-check `sensor.outdoor_us_aqi` too.

- [ ] **Step 5: Confirm the advisor automation loaded (by alias-slug, not id)**

Run: `uv run python scripts/probe.py ha automation bedroom_window_advisor`
Expected: resolves and shows the automation as loaded (`state: on`). The probe handles the alias-slug≠id trap.

- [ ] **Step 6: Confirm the new threshold sensors + alert wiring exist**

Run: `uv run python scripts/probe.py ha state binary_sensor.outdoor_pm2_5_high`
Expected: `on`/`off` (not missing). It will be `off` under normal air.

- [ ] **Step 7: Functional smoke-check of the advisor (optional, manual)**

In HA Developer Tools → Template, paste and confirm it returns a sensible verdict for current conditions:

```
{% from 'ventilation.jinja' import ventilation_advice %}
{{ ventilation_advice(states('sensor.bedroom_airgradient_one_temperature'),
   state_attr('weather.forecast_home','temperature'),
   states('sensor.bedroom_airgradient_one_pm2_5'),
   states('sensor.outdoor_pm2_5'),
   is_state('binary_sensor.bedroom_co2_high','on') or is_state('binary_sensor.bedroom_voc_high','on')) }}
```

Expected: `none` / `stale` / `cool` consistent with the live readings. (If `custom_templates` changed and HA was only YAML-reloaded rather than restarted, run Developer Tools → Actions → `homeassistant.reload_custom_templates` first — a full deploy restart already loads it.)

- [ ] **Step 8: Update role docs**

Add a short bullet to `ansible/roles/containers/home-assistant/CLAUDE.md` (the "Notable" list) documenting: the Open-Meteo `rest.yaml` data source (no key, `zone.home` coords, copy'd), the two outdoor threshold categories, the `bedroom_window_advisor` + `ventilation.jinja` macro, and the new dashboard cards. Then commit:

```bash
git add ansible/roles/containers/home-assistant/CLAUDE.md
git commit -m "docs(home-assistant): document outdoor AQI + window advisor

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Open-Meteo REST data source (no key, `zone.home` coords, copy'd, git-managed) → Task 1 ✓
- Outdoor PM2.5 threshold alerts wired into `bedroom_threshold_alert` (moderate→watch, severe→pierce) → Tasks 2–3 ✓ (corrected to two categories — the engine's one-watch/pierce-per-category structure requires it)
- "Open the window?" advisor, stale + free-cooling paths, smoke-guarded, decision in a tested macro, routed via `bedroom_notify` → Tasks 4–5 ✓
- Dashboard: `weather.forecast_home` + outdoor AQI next to indoor AirGradient → Task 6 ✓
- Testing (macro unit tests, `validate-ha-config`), deploy + confirm-loaded → Tasks 4, 7 ✓
- Out-of-scope items (fan curve, pollen/UV, ozone alert, action buttons) — correctly not implemented ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to". All steps carry literal code + exact commands.

**Type/name consistency:** `ventilation_advice(...)` signature and the `'stale'`/`'cool'`/`'none'` return values match between the macro (Task 4), its tests (Task 4), and the single call-site in the automation (Task 5 — evaluated once in the action variables, DRY). Entity ids (`sensor.outdoor_pm2_5`, `binary_sensor.outdoor_pm2_5_high`/`_severe`, category ids `airqualityoutdoor`/`airqualityoutdoorsevere`) are consistent across Tasks 1–3 and 5. `home_assistant_rest` register var consistent between the copy task and `common_config_changed` (Task 1). The cooling trigger `above: 78` matches the macro's `comfort_hi` default (78) — flagged as a coupling to keep in sync.
