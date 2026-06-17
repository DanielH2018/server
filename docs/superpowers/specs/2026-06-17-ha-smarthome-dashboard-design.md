# Home Assistant Smart Home dashboard + entity customization (YAML/IaC)

**Date:** 2026-06-17
**Status:** Approved (design) — pending implementation plan
**Host:** daniel-server · **Role:** `home-assistant`

## Goal

Add a repo-managed (Ansible-templated) **YAML-mode Lovelace dashboard** and **entity
customization** for the three paired devices — APC UPS (NUT), DREO Tower Fan (DR-HTF024S,
hass-dreo), and Aqara FP300 presence sensor (Zigbee2MQTT). No automations.

## Approach

Register a new YAML-mode dashboard `smart-home` **alongside** the existing default
("Overview") dashboard. Because `lovelace:` is given a `dashboards:` block but **no top-level
`mode:`**, the default dashboard stays storage-mode (UI-editable) while only `smart-home` is
YAML/file-defined. Only built-in cards are used (gauge/tile/entities/glance/stacks), so no
Lovelace `resources:` are required. Everything is templated by the `home-assistant` role and
applied on deploy (which recreates HA).

## Entity inventory (verified from `.storage/core.entity_registry`)

- **APC UPS:** `sensor.apc_ups_battery_charge`, `_load`, `_battery_runtime`, `_input_voltage`,
  `_status`, `_nominal_real_power` (+18 more, not surfaced).
- **DREO Tower Fan:** `fan.tower_fan`, `sensor.tower_fan_temperature`,
  `switch.tower_fan_display_auto_off`, `switch.tower_fan_panel_sound`.
- **Aqara FP300:** `binary_sensor.aqara_fp300_presence`, `_pir_detection`,
  `sensor.aqara_fp300_illuminance`, `_temperature`, `_humidity`, `_battery`, `_target_distance`
  (+~25 `number./select./switch.` tuning entities, not surfaced).

## Files

### Modify `roles/containers/home-assistant/templates/configuration.yaml.j2`
Add (keeping existing `default_config:` + `http:`):
```yaml
homeassistant:
  customize: !include customize.yaml
lovelace:
  dashboards:
    smart-home:
      mode: yaml
      title: Smart Home
      icon: mdi:home-automation
      show_in_sidebar: true
      filename: dashboards/smart-home.yaml
```

### Create `templates/customize.yaml.j2`
Friendly-name + icon overrides for the dashboard entities (neutral names, no room
assumptions). ~8 entities: UPS battery/load/runtime/input-voltage, FP300 presence/illuminance,
fan. `customize:` applies because the entities aren't UI-renamed.

### Create `templates/dashboards/smart-home.yaml.j2`
One `Overview` view (masonry), three vertical-stack device cards:
- **UPS:** horizontal-stack of two gauges (battery %, load %) + an entities card
  (status, runtime, input voltage, nominal power).
- **Tower Fan:** `tile` card for `fan.tower_fan` with `features: [{type: fan-speed}]`
  + entities card (temperature, display-auto-off, panel-sound).
- **FP300:** `glance` (presence, PIR, battery) + entities card (illuminance, temperature,
  humidity, target distance).

### Modify `roles/containers/home-assistant/tasks/main.yml`
- Add `{{ container_item.name }}/config/dashboards` to `common_dirs_to_create`.
- Template `customize.yaml` → `config/customize.yaml` (register `home_assistant_customize`).
- Template `dashboards/smart-home.yaml` → `config/dashboards/smart-home.yaml`
  (register `home_assistant_dashboard`).
- `common_config_changed` OR-s all three registers
  (`home_assistant_config`, `home_assistant_customize`, `home_assistant_dashboard`) so a deploy
  recreates HA and applies any change.

### Update `roles/containers/home-assistant/CLAUDE.md`
Note the new YAML dashboard + customize include, and the fast-iteration path
(Developer Tools → YAML → **Reload Lovelace**) to apply dashboard-only edits without the ~120s
HA restart.

## Apply / iterate

- Deploy recreates HA (~120s start_period) → dashboard + customizations live.
- Dashboard-only tweaks can be hot-reloaded from the UI without a restart once the file is on disk.

## Validation

- `scripts/validate_compose_templates.py` is unaffected (no compose change), but render the new
  templates and assert valid YAML (a Jinja/YAML slip would make HA drop the dashboard).
- `uv run python scripts/probe.py health home-assistant` after deploy (running + healthy).
- In the UI: the **Smart Home** dashboard appears in the sidebar with all three cards populated
  (no "entity not found").

## Out of scope (YAGNI)

- Automations (presence → fan), custom HACS cards, UPS history/graph cards, room naming,
  the FP300's 25+ tuning entities, and any change to the default Overview dashboard.
