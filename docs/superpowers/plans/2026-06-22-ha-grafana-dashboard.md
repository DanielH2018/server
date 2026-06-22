# Home Assistant Grafana Dashboard + Datasource-uid Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a curated Grafana dashboard for the live HA → Prometheus `hass_*` metrics (trends + operational health), and a deterministic guard that every provisioned dashboard's datasource references resolve to a provisioned datasource.

**Architecture:** Two independent deliverables. (1) A standalone validator `scripts/validate_grafana_dashboards.py` + pytest + a `validate-grafana-dashboards` prek hook, mirroring the existing `validate_compose_templates.py` / `validate_ha_config.py` triad — pure validation, no deploy. (2) A hand-authored dashboard JSON dropped into a new `files/dashboards/HomeAssistant/` subdir, which the existing file-provider turns into a "HomeAssistant" Grafana folder with zero Ansible task changes; the guard then validates it, and a `--tags grafana` deploy + live PromQL probes confirm data.

**Tech Stack:** Python 3 (stdlib `json`, `pyyaml`), `jinja2`-free YAML parse, `uv run pytest`, Grafana file-provisioning (schemaVersion 39), Ansible, `prek`.

## Global Constraints

- **Dashboard file:** `ansible/roles/containers/grafana/files/dashboards/HomeAssistant/home-assistant.json`; Grafana folder `HomeAssistant` (from the subdir name); dashboard `uid` = `home-assistant-overview`; `title` = `Home Assistant — Overview`; `schemaVersion` 39; `version` 1.
- **Every datasource ref pins** `{"type": "prometheus", "uid": "EGdsQqhVk"}` (the provisioned Prometheus uid). No `${...}` template-variable datasources.
- **Metric names are used verbatim** — HA mangles units: `µg/m³` → `u0xb5g_per_mu0xb3`, no-unit → `_None`. e.g. `hass_sensor_pm25_u0xb5g_per_mu0xb3`, `hass_sensor_aqi_None`.
- **Legends** use `{{friendly_name}}`.
- **Guard valid-set is parsed, not hardcoded** — datasource uids AND names come from `datasources.yml.j2`, plus Grafana built-ins `-- Grafana --`, `-- Mixed --`, `-- Dashboard --`, `grafana`.
- **The guard must stay GREEN on the real role** — all 14 existing boards + the new one resolve. No deploy for the guard (validation code only).
- **YAGNI:** no template-variable entity-pickers, no Grafana alerting rules, no Loki/log panels, no edits to `fetch_grafana_dashboards.py` / `export_grafana_dashboards.py`.
- **No new Python dependencies** — `pyyaml` is already a repo dep (used by `validate_ha_config.py`).

---

### Task 1: Datasource-uid guard (`validate_grafana_dashboards.py` + tests + prek hook)

**Files:**
- Create: `scripts/validate_grafana_dashboards.py`
- Create: `scripts/test_validate_grafana_dashboards.py`
- Modify: `prek.toml` (add a `validate-grafana-dashboards` hook after the `validate-ha-config` hook)
- Modify: `ansible/roles/containers/grafana/CLAUDE.md` (one line documenting the guard)

**Interfaces:**
- Consumes: nothing from other tasks. Reads `ansible/roles/containers/grafana/files/dashboards/**/*.json` and `templates/provisioning/datasources.yml.j2`.
- Produces (used by Task 2's verification, and by the prek hook):
  - `provisioned_datasource_ids(datasources_template: Path = DATASOURCES_TEMPLATE) -> set[str]`
  - `datasource_refs_in(obj) -> list[tuple[str, str | None]]` — `(uid, nearest_panel_title)` for every datasource ref.
  - `validate(dashboards_dir: Path = DASHBOARDS_DIR, datasources_template: Path = DATASOURCES_TEMPLATE) -> list[str]`
  - `main() -> int`
  - Module constants `REPO_ROOT`, `GRAFANA_ROLE`, `DASHBOARDS_DIR`, `DATASOURCES_TEMPLATE`, `BUILTIN_DATASOURCE_UIDS`.

- [ ] **Step 1: Write the failing tests**

Create `scripts/test_validate_grafana_dashboards.py`:

```python
"""Tests for scripts/validate_grafana_dashboards.py — the datasource-uid guard."""
import json

from validate_grafana_dashboards import (
    DASHBOARDS_DIR,
    DATASOURCES_TEMPLATE,
    datasource_refs_in,
    provisioned_datasource_ids,
    validate,
)

# A minimal datasources.yml.j2 stand-in: the two real provisioned datasources.
_DATASOURCES = """\
apiVersion: 1
datasources:
  - name: Prometheus
    uid: EGdsQqhVk
    type: prometheus
  - name: loki
    uid: bf4q19tuivta8e
    type: loki
"""


def _write_datasources(tmp_path):
    p = tmp_path / "datasources.yml.j2"
    p.write_text(_DATASOURCES)
    return p


def _write_dashboard(dashboards_dir, name, obj):
    dashboards_dir.mkdir(parents=True, exist_ok=True)
    (dashboards_dir / name).write_text(json.dumps(obj))


def test_provisioned_ids_parses_uids_and_names(tmp_path):
    ids = provisioned_datasource_ids(_write_datasources(tmp_path))
    assert {"EGdsQqhVk", "bf4q19tuivta8e", "Prometheus", "loki"} <= ids


def test_object_form_uid_resolves_clean(tmp_path):
    dd = tmp_path / "dashboards"
    _write_dashboard(dd, "ok.json", {
        "uid": "my-board", "title": "OK",
        "panels": [{"title": "P", "datasource": {"type": "prometheus", "uid": "EGdsQqhVk"},
                    "targets": [{"datasource": {"type": "prometheus", "uid": "EGdsQqhVk"}}]}],
    })
    assert validate(dd, _write_datasources(tmp_path)) == []


def test_unknown_uid_flagged(tmp_path):
    dd = tmp_path / "dashboards"
    _write_dashboard(dd, "bad.json", {
        "uid": "my-board", "title": "Bad",
        "panels": [{"title": "Broken Panel",
                    "datasource": {"type": "prometheus", "uid": "IH0jqv6nz"}}],
    })
    errors = validate(dd, _write_datasources(tmp_path))
    assert len(errors) == 1
    assert "IH0jqv6nz" in errors[0]
    assert "Broken Panel" in errors[0]  # nearest-panel-title context


def test_legacy_string_form_handled(tmp_path):
    dd = tmp_path / "dashboards"
    _write_dashboard(dd, "legacy.json", {
        "uid": "b", "title": "Legacy",
        "panels": [{"title": "Good", "datasource": "EGdsQqhVk"},
                   {"title": "Stale", "datasource": "nope-uid"}],
    })
    errors = validate(dd, _write_datasources(tmp_path))
    assert len(errors) == 1
    assert "nope-uid" in errors[0]


def test_builtin_datasources_ignored(tmp_path):
    dd = tmp_path / "dashboards"
    _write_dashboard(dd, "builtins.json", {
        "uid": "b", "title": "Builtins",
        "panels": [
            {"title": "Anno", "datasource": {"type": "datasource", "uid": "-- Grafana --"}},
            {"title": "Mixed", "datasource": {"type": "datasource", "uid": "-- Mixed --"}},
            {"title": "Dash", "datasource": {"type": "datasource", "uid": "-- Dashboard --"}},
            {"title": "G", "datasource": {"type": "grafana", "uid": "grafana"}},
        ],
    })
    assert validate(dd, _write_datasources(tmp_path)) == []


def test_dashboard_own_uid_never_flagged(tmp_path):
    # The dashboard's own top-level uid is NOT a datasource ref and must never be flagged,
    # even when it is not a provisioned datasource uid (the sadlil-loki-apps-dashboard case).
    dd = tmp_path / "dashboards"
    _write_dashboard(dd, "ownuid.json", {
        "uid": "sadlil-loki-apps-dashboard", "title": "Own uid",
        "panels": [{"title": "P", "datasource": {"type": "loki", "uid": "bf4q19tuivta8e"}}],
    })
    assert validate(dd, _write_datasources(tmp_path)) == []


def test_invalid_json_reported(tmp_path):
    dd = tmp_path / "dashboards"
    dd.mkdir(parents=True)
    (dd / "broken.json").write_text("{not valid json")
    errors = validate(dd, _write_datasources(tmp_path))
    assert len(errors) == 1 and "broken.json" in errors[0]


def test_refs_collects_object_and_string_forms():
    refs = datasource_refs_in({
        "panels": [{"title": "T", "datasource": {"uid": "X"},
                    "targets": [{"datasource": "Y"}]}]
    })
    pairs = set(refs)
    assert ("X", "T") in pairs
    assert ("Y", "T") in pairs


def test_real_role_passes():
    # Regression guard: every existing provisioned board (and the new one, once added)
    # resolves against the real datasources template. The headline check.
    assert validate(DASHBOARDS_DIR, DATASOURCES_TEMPLATE) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest scripts/test_validate_grafana_dashboards.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'validate_grafana_dashboards'`.

- [ ] **Step 3: Write the validator**

Create `scripts/validate_grafana_dashboards.py`:

```python
#!/usr/bin/env python3
"""Validate that every provisioned Grafana dashboard's datasource references resolve to a
datasource declared in datasources.yml.j2.

A panel pointing at a wrong/empty datasource uid renders a silent "No data" with no error —
exactly the stale-uid class the grafana role CLAUDE.md warns about (the lingering IH0jqv6nz
uid). This guard is deterministic over all provisioned dashboards.

Run directly (`python3 scripts/validate_grafana_dashboards.py`) or via the
`validate-grafana-dashboards` prek hook. Exits non-zero on any unresolved datasource uid.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAFANA_ROLE = REPO_ROOT / "ansible/roles/containers/grafana"
DASHBOARDS_DIR = GRAFANA_ROLE / "files/dashboards"
DATASOURCES_TEMPLATE = GRAFANA_ROLE / "templates/provisioning/datasources.yml.j2"

# Grafana built-in pseudo-datasources — always valid, never provisioned.
BUILTIN_DATASOURCE_UIDS = {"-- Grafana --", "-- Mixed --", "-- Dashboard --", "grafana"}


def provisioned_datasource_ids(datasources_template: Path = DATASOURCES_TEMPLATE) -> set[str]:
    """uids AND names of every provisioned datasource. datasources.yml.j2 carries no Jinja
    (it is pure YAML despite the .j2 extension), so we parse it directly. Including names as
    well as uids means a legacy name-form datasource ref ("datasource": "Prometheus") also
    resolves — a valid Grafana reference, not a bug."""
    data = yaml.safe_load(datasources_template.read_text()) or {}
    ids: set[str] = set()
    for ds in data.get("datasources", []) or []:
        for key in ("uid", "name"):
            value = ds.get(key)
            if isinstance(value, str):
                ids.add(value)
    return ids


def _uid_from_ref(ref) -> list[str]:
    """The uid(s) a `datasource` value references: object form {"uid": "X"} or legacy bare
    string "X". null / anything else → no ref."""
    if isinstance(ref, str):
        return [ref]
    if isinstance(ref, dict):
        uid = ref.get("uid")
        return [uid] if isinstance(uid, str) else []
    return []


def datasource_refs_in(obj) -> list[tuple[str, str | None]]:
    """Every datasource ref in a loaded dashboard, as (uid, nearest_panel_title). Walks
    recursively; a uid is collected only as the value of (or nested under) a `datasource`
    key — so a dashboard's own top-level `uid` is never collected. `title` is the nearest
    enclosing object's title, for error context."""
    refs: list[tuple[str, str | None]] = []

    def visit(node, title):
        if isinstance(node, dict):
            t = node.get("title")
            if isinstance(t, str):
                title = t
            for key, value in node.items():
                if key == "datasource":
                    for uid in _uid_from_ref(value):
                        refs.append((uid, title))
                visit(value, title)
        elif isinstance(node, list):
            for item in node:
                visit(item, title)

    visit(obj, None)
    return refs


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return path.name


def validate(dashboards_dir: Path = DASHBOARDS_DIR,
             datasources_template: Path = DATASOURCES_TEMPLATE) -> list[str]:
    """Return a list of error strings ([] = clean): every dashboard JSON whose datasource
    refs all resolve to a provisioned datasource (or a built-in) passes."""
    valid = provisioned_datasource_ids(datasources_template) | BUILTIN_DATASOURCE_UIDS
    errors: list[str] = []
    for path in sorted(dashboards_dir.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{_display(path)}: invalid JSON: {exc}")
            continue
        seen: set[tuple[str, str | None]] = set()
        for uid, title in datasource_refs_in(data):
            if uid in valid or (uid, title) in seen:
                continue
            seen.add((uid, title))
            where = f" (panel {title!r})" if title else ""
            errors.append(f"{_display(path)}: datasource uid {uid!r} is not provisioned{where}")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        print("Grafana dashboard datasource validation FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("Grafana dashboard datasources OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest scripts/test_validate_grafana_dashboards.py -v`
Expected: PASS (all 9 tests, including `test_real_role_passes`).

If `test_real_role_passes` FAILS, an existing board has an unresolved uid — report it; do **not** weaken the guard to make it pass. (The grafana CLAUDE.md says all refs are pinned, so this should be clean; a failure is a real finding.)

- [ ] **Step 5: Run the validator directly**

Run: `uv run python scripts/validate_grafana_dashboards.py`
Expected: exit 0, prints `Grafana dashboard datasources OK`.

- [ ] **Step 6: Add the prek hook**

In `prek.toml`, immediately after the `validate-ha-config` hook block (ends at its `files = ...` line), add:

```toml
# Validate that every provisioned Grafana dashboard's datasource references resolve to a
# datasource declared in datasources.yml.j2 (uid or name) or a Grafana built-in. Catches the
# silent "No data" / stale-uid class (e.g. a lingering hand-imported uid) before deploy.
[[repos.hooks]]
id = "validate-grafana-dashboards"
name = "Validate Grafana dashboard datasource uids"
entry = "uv run python scripts/validate_grafana_dashboards.py"
language = "system"
pass_filenames = false
files = "^(ansible/roles/containers/grafana/files/dashboards/.*\\.json|ansible/roles/containers/grafana/templates/provisioning/datasources\\.yml\\.j2|scripts/validate_grafana_dashboards\\.py)$"
```

- [ ] **Step 7: Verify the hook runs**

Run: `prek run validate-grafana-dashboards --all-files`
Expected: the hook executes and `Passed`.

- [ ] **Step 8: Document the guard in the role CLAUDE.md**

In `ansible/roles/containers/grafana/CLAUDE.md`, under the `## Editing` section (after the two generator-script bullets), add a bullet:

```markdown
- **Datasource-uid guard:** `scripts/validate_grafana_dashboards.py` (prek hook
  `validate-grafana-dashboards`, + `scripts/test_validate_grafana_dashboards.py`) asserts every
  `files/dashboards/**/*.json` datasource ref resolves to a uid/name declared in
  `datasources.yml.j2` (or a Grafana built-in). A wrong/empty uid → silent "No data"; this
  catches it before deploy. The valid set is parsed from the template, so adding a datasource
  there is enough — no edit to the guard.
```

- [ ] **Step 9: Run the full scripts suite**

Run: `uv run pytest scripts -q`
Expected: all PASS (no regression in the other validators).

- [ ] **Step 10: Commit**

```bash
git add scripts/validate_grafana_dashboards.py scripts/test_validate_grafana_dashboards.py prek.toml ansible/roles/containers/grafana/CLAUDE.md
git commit -m "feat(grafana): guard that dashboard datasource uids resolve to a provisioned datasource"
```

---

### Task 2: The Home Assistant dashboard JSON

**Files:**
- Create: `ansible/roles/containers/grafana/files/dashboards/HomeAssistant/home-assistant.json`
- Modify: `ansible/roles/containers/grafana/CLAUDE.md` (note the new board)

**Interfaces:**
- Consumes: the Task 1 guard (`scripts/validate_grafana_dashboards.py`) validates this file; the provisioned Prometheus uid `EGdsQqhVk`.
- Produces: a provisioned dashboard (no code interface).

**Authoring note:** Grafana fills every panel default on load (see `Terraria/player-stats.json`),
so each panel needs only `id` / `type` / `title` / `datasource` / `gridPos` / `targets`, plus an
optional `fieldConfig.defaults` for `unit`/`thresholds`. Keep panels minimal — do NOT hand-write
verbose `options`/`fieldConfig` blocks. `allowUiUpdates: true` means visuals are tunable in the UI
later (and re-captured by `export_grafana_dashboards.py`).

**Grid layout rules (24-column grid):**
- A `row` panel is `{"type": "row", ...}` with `gridPos` `{"h": 1, "w": 24, "x": 0, "y": <Y>}` and `"collapsed": false`.
- Data panels are `h: 8`. Place two-up at `w: 12` (`x: 0` then `x: 12`), or three-up at `w: 8` (`x: 0/8/16`). Tables that need width use `w: 24` or `w: 12`.
- `id` is a unique integer per panel (1, 2, 3, …). `y` increases down the dashboard; rows sit at the top of their band.

- [ ] **Step 1: Create the dashboard skeleton**

Create `ansible/roles/containers/grafana/files/dashboards/HomeAssistant/home-assistant.json` with this exact skeleton (panels filled in the next steps):

```json
{
  "uid": "home-assistant-overview",
  "title": "Home Assistant — Overview",
  "tags": ["home-assistant"],
  "timezone": "browser",
  "schemaVersion": 39,
  "version": 1,
  "refresh": "1m",
  "time": { "from": "now-24h", "to": "now" },
  "panels": []
}
```

- [ ] **Step 2: Fill the panels array — Row 1 (Overview & Health)**

Replace `"panels": []` with the rows below, concatenated in order. These are the worked panel
shapes — one per panel TYPE used in the whole dashboard (stat, table, timeseries, gauge,
state-timeline, row). Reuse these shapes for every panel in the inventory.

Row 1:

```json
    { "id": 1, "type": "row", "title": "Overview & Health", "collapsed": false,
      "gridPos": { "h": 1, "w": 24, "x": 0, "y": 0 } },
    { "id": 2, "type": "stat", "title": "Entities available",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 8, "w": 6, "x": 0, "y": 1 },
      "targets": [
        { "refId": "A", "expr": "count(hass_entity_available == 1)", "legendFormat": "available",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } },
        { "refId": "B", "expr": "count(hass_entity_available == 0) or vector(0)", "legendFormat": "unavailable",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ] },
    { "id": 3, "type": "table", "title": "Unavailable entities",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 8, "w": 9, "x": 6, "y": 1 },
      "transformations": [
        { "id": "organize", "options": { "excludeByName": {
          "Time": true, "Value": true, "__name__": true, "job": true, "instance": true } } }
      ],
      "targets": [
        { "refId": "A", "format": "table", "instant": true, "expr": "hass_entity_available == 0",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ] },
    { "id": 4, "type": "table", "title": "Battery levels",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 8, "w": 9, "x": 15, "y": 1 },
      "fieldConfig": { "defaults": { "unit": "percent", "thresholds": { "mode": "absolute", "steps": [
        { "color": "red", "value": null }, { "color": "yellow", "value": 20 }, { "color": "green", "value": 40 } ] } },
        "overrides": [] },
      "transformations": [
        { "id": "organize", "options": { "excludeByName": {
          "Time": true, "__name__": true, "job": true, "instance": true, "domain": true } } },
        { "id": "sortBy", "options": { "fields": {}, "sort": [ { "field": "Value", "desc": false } ] } }
      ],
      "targets": [
        { "refId": "A", "format": "table", "instant": true, "expr": "hass_sensor_battery_percent",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ] },
    { "id": 5, "type": "timeseries", "title": "HA activity (state changes/sec)",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 9 },
      "targets": [
        { "refId": "A", "expr": "sum(rate(hass_state_change_total[$__rate_interval]))", "legendFormat": "state changes/s",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ] },
    { "id": 6, "type": "timeseries", "title": "Busiest automations (triggers/sec)",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 9 },
      "targets": [
        { "refId": "A", "expr": "topk(10, rate(hass_automation_triggered_count_total[$__rate_interval]))",
          "legendFormat": "{{friendly_name}}",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ] }
```

- [ ] **Step 3: Append Row 2 (Climate & Air Quality)**

```json
    { "id": 10, "type": "row", "title": "Climate & Air Quality", "collapsed": false,
      "gridPos": { "h": 1, "w": 24, "x": 0, "y": 17 } },
    { "id": 11, "type": "timeseries", "title": "Temperature",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "fieldConfig": { "defaults": { "unit": "celsius" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 18 },
      "targets": [ { "refId": "A", "expr": "hass_sensor_temperature_celsius", "legendFormat": "{{friendly_name}}",
        "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } } ] },
    { "id": 12, "type": "timeseries", "title": "Humidity",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "fieldConfig": { "defaults": { "unit": "percent" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 18 },
      "targets": [ { "refId": "A", "expr": "hass_sensor_humidity_percent", "legendFormat": "{{friendly_name}}",
        "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } } ] },
    { "id": 13, "type": "timeseries", "title": "CO₂",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "fieldConfig": { "defaults": { "unit": "ppm", "thresholds": { "mode": "absolute", "steps": [
        { "color": "green", "value": null }, { "color": "yellow", "value": 800 }, { "color": "red", "value": 1200 } ] } },
        "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 26 },
      "targets": [ { "refId": "A", "expr": "hass_sensor_carbon_dioxide_ppm", "legendFormat": "{{friendly_name}}",
        "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } } ] },
    { "id": 14, "type": "timeseries", "title": "Illuminance",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "fieldConfig": { "defaults": { "unit": "lux" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 26 },
      "targets": [ { "refId": "A", "expr": "hass_sensor_illuminance_lx", "legendFormat": "{{friendly_name}}",
        "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } } ] },
    { "id": 15, "type": "timeseries", "title": "Particulates (µg/m³)",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 34 },
      "targets": [
        { "refId": "A", "expr": "hass_sensor_pm25_u0xb5g_per_mu0xb3", "legendFormat": "PM2.5 {{friendly_name}}",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } },
        { "refId": "B", "expr": "hass_sensor_pm10_u0xb5g_per_mu0xb3", "legendFormat": "PM10 {{friendly_name}}",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } },
        { "refId": "C", "expr": "hass_sensor_pm1_u0xb5g_per_mu0xb3", "legendFormat": "PM1 {{friendly_name}}",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ] },
    { "id": 16, "type": "gauge", "title": "AQI",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 34 },
      "targets": [ { "refId": "A", "expr": "hass_sensor_aqi_None", "legendFormat": "{{friendly_name}}",
        "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } } ] }
```

- [ ] **Step 4: Append Row 3 (Room Control)**

```json
    { "id": 20, "type": "row", "title": "Room Control", "collapsed": false,
      "gridPos": { "h": 1, "w": 24, "x": 0, "y": 42 } },
    { "id": 21, "type": "timeseries", "title": "Light brightness",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "fieldConfig": { "defaults": { "unit": "percent", "min": 0, "max": 100 }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 43 },
      "targets": [
        { "refId": "A", "expr": "hass_light_brightness_percent", "legendFormat": "{{friendly_name}}",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } },
        { "refId": "B", "expr": "hass_switch_attr_brightness_pct", "legendFormat": "{{friendly_name}}",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ] },
    { "id": 22, "type": "timeseries", "title": "Fan speed",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "fieldConfig": { "defaults": { "unit": "percent", "min": 0, "max": 100 }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 43 },
      "targets": [ { "refId": "A", "expr": "hass_fan_speed_percent", "legendFormat": "{{friendly_name}}",
        "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } } ] },
    { "id": 23, "type": "state-timeline", "title": "Switch / fan on-off",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 51 },
      "targets": [
        { "refId": "A", "expr": "hass_switch_state", "legendFormat": "{{friendly_name}}",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } },
        { "refId": "B", "expr": "hass_fan_state", "legendFormat": "{{friendly_name}}",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ] }
```

- [ ] **Step 5: Append Row 4 (Power & Energy)**

```json
    { "id": 30, "type": "row", "title": "Power & Energy", "collapsed": false,
      "gridPos": { "h": 1, "w": 24, "x": 0, "y": 59 } },
    { "id": 31, "type": "timeseries", "title": "Power draw",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "fieldConfig": { "defaults": { "unit": "watt" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 60 },
      "targets": [ { "refId": "A", "expr": "hass_sensor_power_w", "legendFormat": "{{friendly_name}}",
        "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } } ] },
    { "id": 32, "type": "stat", "title": "Energy",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "fieldConfig": { "defaults": { "unit": "kwatth" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 6, "x": 12, "y": 60 },
      "targets": [ { "refId": "A", "expr": "hass_sensor_energy_kwh", "legendFormat": "{{friendly_name}}",
        "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } } ] },
    { "id": 33, "type": "timeseries", "title": "Voltage",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "fieldConfig": { "defaults": { "unit": "volt" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 6, "x": 18, "y": 60 },
      "targets": [ { "refId": "A", "expr": "hass_sensor_voltage_v", "legendFormat": "{{friendly_name}}",
        "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } } ] }
```

- [ ] **Step 6: Append Row 5 (Presence)**

```json
    { "id": 40, "type": "row", "title": "Presence", "collapsed": false,
      "gridPos": { "h": 1, "w": 24, "x": 0, "y": 68 } },
    { "id": 41, "type": "state-timeline", "title": "Presence",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 8, "w": 18, "x": 0, "y": 69 },
      "targets": [
        { "refId": "A", "expr": "hass_person_state", "legendFormat": "{{friendly_name}}",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } },
        { "refId": "B", "expr": "hass_device_tracker_state", "legendFormat": "{{friendly_name}}",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ] },
    { "id": 42, "type": "stat", "title": "People home",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 8, "w": 6, "x": 18, "y": 69 },
      "targets": [ { "refId": "A", "expr": "count(hass_person_state == 1) or vector(0)", "legendFormat": "home",
        "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } } ] }
```

(Final `panels` array = Row 1 + Row 2 + Row 3 + Row 4 + Row 5, comma-separated, inside the `[ ]`.)

- [ ] **Step 7: Verify the JSON parses**

Run: `python3 -c "import json; d=json.load(open('ansible/roles/containers/grafana/files/dashboards/HomeAssistant/home-assistant.json')); print(d['uid'], '|', len(d['panels']), 'objects')"`
Expected: `home-assistant-overview | 24 objects` (5 `row` objects + 19 data panels — Grafana counts rows in the `panels` array).

- [ ] **Step 8: Run the datasource-uid guard (from Task 1) over the new board**

Run: `uv run python scripts/validate_grafana_dashboards.py`
Expected: exit 0, `Grafana dashboard datasources OK` (the new board's uids all resolve).

Run: `uv run pytest scripts/test_validate_grafana_dashboards.py::test_real_role_passes -v`
Expected: PASS (the regression guard now also covers the new board).

- [ ] **Step 9: Live-probe each row's headline query (before deploy — confirms metric names)**

Run each and confirm a non-empty result (`result` array length > 0):

```bash
uv run python scripts/probe.py metric 'count(hass_entity_available == 1)'
uv run python scripts/probe.py metric 'hass_sensor_temperature_celsius'
uv run python scripts/probe.py metric 'hass_sensor_pm25_u0xb5g_per_mu0xb3'
uv run python scripts/probe.py metric 'hass_light_brightness_percent'
uv run python scripts/probe.py metric 'hass_sensor_power_w'
uv run python scripts/probe.py metric 'hass_person_state'
```

Expected: each returns `"status":"success"` with a non-empty `result`. A typo'd metric name returns an empty result → fix the panel's `expr` before deploying.

- [ ] **Step 10: Deploy the grafana role**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "grafana"`
Expected: PLAY RECAP `failed=0`. The `grafana_cfg_dashboards` copy is `changed`, so the container recreates.

- [ ] **Step 11: Gate on health**

Run: `uv run python scripts/probe.py health grafana`
Expected: exit 0 (running + healthy).

- [ ] **Step 12: Confirm the dashboard provisioned without error**

Run: `docker logs grafana 2>&1 | grep -iE "provision|home-assistant-overview|dashboard" | tail -20`
Expected: provisioning lines present, and NO `level=error` mentioning the dashboard file or
`home-assistant.json`. (Grafana logs a parse/schema error here if the board is malformed.)

- [ ] **Step 13: Note the board in the role CLAUDE.md**

In `ansible/roles/containers/grafana/CLAUDE.md`, in the dashboards bullet that lists the custom
boards (the `**Custom boards**` line), add `HomeAssistant/home-assistant.json` to the list of
custom boards (it is exported from the live DB like the others).

- [ ] **Step 14: Commit**

```bash
git add ansible/roles/containers/grafana/files/dashboards/HomeAssistant/home-assistant.json ansible/roles/containers/grafana/CLAUDE.md
git commit -m "feat(grafana): add Home Assistant overview dashboard (climate/health/power/presence)"
```

---

## Notes for the executor

- **Order matters:** Task 1 (guard) before Task 2 (dashboard) — the guard then validates the new
  board, and `test_real_role_passes` extends to cover it for free.
- **Task 1 = no deploy** (validation code, runs in the prek hook). **Task 2 = deploy** the grafana
  role + live verification.
- If `test_real_role_passes` fails in Task 1, an EXISTING board has a stale uid — that's a real
  finding to report, not a reason to loosen the guard.
- After both tasks land: update memory `ha-deferred-followups` — the Grafana dashboard was the
  lone remaining deferral, so the file should be removed (and its `MEMORY.md` index line dropped),
  or rewritten to note all HA → Prometheus follow-ups are complete. (Controller bookkeeping, not a
  code step.)
- If executed via subagent-driven-development: the implementer reports only; the controller runs
  the gate and commits explicit paths. Task 2's deploy + live probes are controller actions
  (they touch the live host), not the implementer's.
```
