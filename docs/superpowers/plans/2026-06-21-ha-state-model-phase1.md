# HA State Model — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a mostly-derived, drift-gated representation of the bedroom Home Assistant control plane (cells/actuators + who writes/reads them), plus CI guardrail checks and a live `probe.py ha-state` debug view.

**Architecture:** A new pure-Python module `scripts/ha_state_model.py` reuses `validate_ha_config.py`'s `HAConfigLoader`/`assemble_config` to parse the real automations/scripts/config, recursively extracts every write (service call → target entity), and `generate`s two committed artifacts (`derived_state.yml`, `STATE.md`). A set of checks (run by the existing `validate-ha-config` prek/CI slot) enforces freshness, entity-reference resolution, a 3-boolean override-writer tripwire, and engine structural-completeness; two more invariants (single-writer, override-consistency) run in **report** mode to measure the Phase 2 gap. `probe.py ha-state` reads the derived cell list and annotates it with live values.

**Tech Stack:** Python 3 (stdlib + `PyYAML` + `jinja2`, already deps), pytest (via `uv run pytest`), prek, SOPS (for the live `refresh`/probe token).

## Global Constraints

- **Derive everything derivable; the ONLY hand-declared state-model file is `state/expected_override_writers.yml`** (3 booleans). The role `CLAUDE.md` prose is the separate irreducible "why".
- **One validator, one hook:** extend the existing `validate-ha-config` prek/CI slot; do NOT add a second hook.
- **Generated artifacts are deterministic** (sorted keys/lists) so the freshness `git diff` gate is stable.
- **Tests are hermetic** — no live HA, no Docker, no network. Live-only paths (`refresh`, `ha-state`) are tested with injected stubs.
- **`containers/` is never edited** (Ansible-managed); all role edits are under `ansible/roles/containers/home-assistant/`.
- Generated artifacts live in `ansible/roles/containers/home-assistant/state/`.
- Hard checks: freshness, entity-resolution, override-writer tripwire, structural-completeness, alias-slug collision. Report-only: single-writer, override-consistency.
- Tests are collected via `pyproject.toml` `testpaths` which already includes `scripts`; module is imported directly (`import ha_state_model as hsm`).
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Work on `master` (no feature branch unless asked).

---

## File Structure

- `scripts/ha_state_model.py` — **new.** Extractor (pure), config introspection, `generate`/`refresh`, the check functions, and a `main()` CLI (`generate`/`refresh`/`check`).
- `scripts/test_ha_state_model.py` — **new.** Hermetic unit tests.
- `scripts/probe.py` — **modify.** Add the `ha-state` subcommand (parser + formatting; live I/O injected for tests).
- `scripts/validate_ha_config.py` — **modify.** `validate()` also runs `ha_state_model.check_errors(ROLE_DIR)` so the existing hook runs both.
- `ansible/roles/containers/home-assistant/state/derived_state.yml` — **new, generated, committed.**
- `ansible/roles/containers/home-assistant/state/STATE.md` — **new, generated, committed.**
- `ansible/roles/containers/home-assistant/state/external_entities.yml` — **new, generated (from live HA), committed.**
- `ansible/roles/containers/home-assistant/state/expected_override_writers.yml` — **new, hand-maintained tripwire.**
- `ansible/roles/containers/home-assistant/CLAUDE.md` — **modify.** Pointer to `STATE.md` (the `:70` entity_id typo in an earlier draft does not exist — verified; no fix needed).

---

### Task 1: Extractor core — service-call walking + target resolution

**Files:**
- Create: `scripts/ha_state_model.py`
- Test: `scripts/test_ha_state_model.py`

**Interfaces:**
- Produces:
  - `slugify(name: str) -> str`
  - `call_service(call: dict) -> str | None` — the `service:`/`action:` value
  - `call_targets(call: dict) -> list[str]` — entity_ids the call targets (scalar+list, `target`/`entity_id`/`data.entity_id`); templated ids returned verbatim (contain `{{`/`{%`)
  - `iter_service_calls(node) -> Iterator[dict]` — every service-call dict anywhere in an action tree (recurses choose/if/repeat/parallel/sequence generically)

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_ha_state_model.py
"""Hermetic tests for the HA state-model extractor + checks (no live HA / Docker / network)."""
import ha_state_model as hsm


def test_call_service_handles_service_and_action_keys():
    assert hsm.call_service({"service": "light.turn_on"}) == "light.turn_on"
    assert hsm.call_service({"action": "fan.set_percentage"}) == "fan.set_percentage"
    assert hsm.call_service({"condition": "state"}) is None


def test_call_targets_scalar_list_and_legacy_forms():
    assert hsm.call_targets({"service": "x.y", "target": {"entity_id": "light.a"}}) == ["light.a"]
    assert hsm.call_targets(
        {"service": "x.y", "target": {"entity_id": ["light.a", "light.b"]}}
    ) == ["light.a", "light.b"]
    # legacy top-level + data.entity_id forms
    assert hsm.call_targets({"service": "x.y", "entity_id": "switch.a"}) == ["switch.a"]
    assert hsm.call_targets({"service": "x.y", "data": {"entity_id": "scene.a"}}) == ["scene.a"]


def test_call_targets_keeps_templated_ids_verbatim():
    assert hsm.call_targets(
        {"service": "x.y", "target": {"entity_id": "{{ repeat.item }}"}}
    ) == ["{{ repeat.item }}"]


def test_iter_service_calls_recurses_choose_if_repeat():
    action = [
        {"choose": [
            {"conditions": [{"condition": "state"}],
             "sequence": [{"service": "input_boolean.turn_on",
                           "target": {"entity_id": "input_boolean.x"}}]}],
         "default": [
            {"if": [{"condition": "state"}],
             "then": [{"service": "timer.start", "target": {"entity_id": "timer.t"}}],
             "else": [{"repeat": {"sequence": [
                 {"service": "light.turn_off", "target": {"entity_id": "light.l"}}]}}]}]},
    ]
    svcs = {hsm.call_service(c) for c in hsm.iter_service_calls(action)}
    assert svcs == {"input_boolean.turn_on", "timer.start", "light.turn_off"}


def test_slugify_matches_ha_basic_rules():
    assert hsm.slugify("Bedroom Tap Dial control") == "bedroom_tap_dial_control"
    assert hsm.slugify("UPS power event!") == "ups_power_event"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/test_ha_state_model.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ha_state_model'`.

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/ha_state_model.py
#!/usr/bin/env python3
"""Derived state model for the Home Assistant bedroom control plane.

Reuses validate_ha_config's loader to parse the real automations/scripts/config, extracts
every write (service call -> target entity), and generates derived_state.yml + STATE.md. Also
runs the guardrail checks consumed by the validate-ha-config prek/CI hook. No live HA / Docker
for any of that — `refresh` (snapshot integration entities) is the only live path.
"""
from __future__ import annotations

import re
from collections.abc import Iterator

_TEMPLATE_MARKERS = ("{{", "{%")


def slugify(name: str) -> str:
    """HA-style slug: lowercase, non-alphanumeric runs -> single underscore, trimmed."""
    s = re.sub(r"[^a-z0-9]+", "_", str(name).lower())
    return s.strip("_")


def call_service(call: dict) -> str | None:
    """The service id of a service-call step. Handles both the `service:` (this repo) and the
    newer `action:` spelling; returns None for non-call dicts."""
    svc = call.get("service")
    if svc is None:
        svc = call.get("action")
    return svc if isinstance(svc, str) and "." in svc else None


def call_targets(call: dict) -> list[str]:
    """Entity ids a service call targets — across `target.entity_id`, legacy top-level
    `entity_id`, and `data.entity_id`; scalar or list. Templated ids are returned verbatim."""
    ids: list[str] = []
    for container in (call.get("target"), call, call.get("data")):
        if not isinstance(container, dict):
            continue
        ent = container.get("entity_id")
        if isinstance(ent, str):
            ids.append(ent)
        elif isinstance(ent, list):
            ids.extend(e for e in ent if isinstance(e, str))
    return ids


def iter_service_calls(node) -> Iterator[dict]:
    """Yield every service-call dict anywhere under `node`. Recurses universally, so all of
    choose/if/then/else/repeat/parallel/sequence are covered without special-casing — a step is
    a 'call' iff it has a `service`/`action` key whose value is a `domain.service` string (a
    block-style `action:` is a list, so it is not mistaken for a call)."""
    if isinstance(node, dict):
        if call_service(node) is not None:
            yield node
        for value in node.values():
            yield from iter_service_calls(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_service_calls(value)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/test_ha_state_model.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/ha_state_model.py scripts/test_ha_state_model.py
git commit -m "feat(ha-state): extractor core — service-call walking + target resolution

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Write attribution + scene resolution

**Files:**
- Modify: `scripts/ha_state_model.py`
- Test: `scripts/test_ha_state_model.py`

**Interfaces:**
- Consumes: `iter_service_calls`, `call_service`, `call_targets`, `slugify` (Task 1).
- Produces:
  - `scene_entity_map(scenes: list) -> dict[str, list[str]]` — `scene.<id>` → entities it sets
  - `automation_writer(auto: dict) -> str` — `automation.<alias-slug>` (falls back to `id`)
  - `extract_writes(automations: list, scripts: dict, scene_map: dict) -> tuple[dict[str, list[str]], dict[str, list[str]]]` — returns `(writes, dynamic_writes)` where `writes[entity_id] = sorted writer names`, `dynamic_writes[writer] = sorted templated targets`. `scene.turn_on: scene.X` is resolved to scene X's entities; other `scene.*` (create/reload) is skipped.

- [ ] **Step 1: Write the failing test**

```python
# add to scripts/test_ha_state_model.py
SCENES = [
    {"id": "bedroom_nightlight", "name": "Bedroom Nightlight",
     "entities": {"light.bedroom_lights": {"state": "on"}}},
]


def test_scene_entity_map():
    m = hsm.scene_entity_map(SCENES)
    assert m == {"scene.bedroom_nightlight": ["light.bedroom_lights"]}


def test_automation_writer_uses_alias_slug():
    assert hsm.automation_writer({"id": "x", "alias": "Bedroom away"}) == "automation.bedroom_away"
    assert hsm.automation_writer({"id": "ups_power_event"}) == "automation.ups_power_event"


def test_extract_writes_attributes_and_resolves_scenes():
    autos = [
        {"id": "a", "alias": "Bedroom away", "action": [
            {"service": "light.turn_off", "target": {"entity_id": "light.bedroom_lights"}},
            {"service": "scene.turn_on", "target": {"entity_id": "scene.bedroom_nightlight"}}]},
    ]
    scripts = {
        "bedroom_bedtime": {"sequence": [
            {"service": "input_boolean.turn_on",
             "target": {"entity_id": "input_boolean.bedroom_sleep_mode"}},
            {"service": "light.turn_on",
             "target": {"entity_id": "{{ some_var }}"}}]},
    }
    writes, dynamic = hsm.extract_writes(autos, scripts, hsm.scene_entity_map(SCENES))
    # scene.turn_on resolved to the light; direct light.turn_off also attributed
    assert writes["light.bedroom_lights"] == ["automation.bedroom_away", "script.bedroom_bedtime"]
    assert writes["input_boolean.bedroom_sleep_mode"] == ["script.bedroom_bedtime"]
    assert dynamic["script.bedroom_bedtime"] == ["{{ some_var }}"]
    # the scene entity itself is not recorded as a written entity
    assert "scene.bedroom_nightlight" not in writes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/test_ha_state_model.py -q`
Expected: FAIL — `AttributeError: module 'ha_state_model' has no attribute 'scene_entity_map'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to scripts/ha_state_model.py
from collections import defaultdict


def _is_templated(entity_id: str) -> bool:
    return any(marker in entity_id for marker in _TEMPLATE_MARKERS)


def scene_entity_map(scenes: list) -> dict[str, list[str]]:
    """Map `scene.<id>` -> the entity ids the scene sets (so `scene.turn_on` counts as a write
    to those entities)."""
    out: dict[str, list[str]] = {}
    for scene in scenes or []:
        sid = scene.get("id")
        ents = scene.get("entities") or {}
        if sid:
            out[f"scene.{sid}"] = list(ents.keys())
    return out


def automation_writer(auto: dict) -> str:
    """The state-machine name of an automation: `automation.<slug(alias)>` (HA derives the
    entity_id from the alias, not the id; fall back to the id when alias is absent)."""
    return "automation." + slugify(auto.get("alias") or auto.get("id") or "unknown")


def extract_writes(automations, scripts, scene_map):
    """Return (writes, dynamic_writes). writes[entity] = sorted writer names; dynamic_writes
    [writer] = sorted templated target strings that couldn't be resolved to an entity."""
    writes: dict[str, set] = defaultdict(set)
    dynamic: dict[str, set] = defaultdict(set)

    def record(writer, call):
        svc = call_service(call)
        for ent in call_targets(call):
            if _is_templated(ent):
                dynamic[writer].add(ent)
            elif svc == "scene.turn_on" and ent in scene_map:
                for real in scene_map[ent]:
                    writes[real].add(writer)
            elif svc and svc.startswith("scene."):
                continue  # scene.create / scene.reload — not a device-state write
            else:
                writes[ent].add(writer)

    for auto in automations or []:
        writer = automation_writer(auto)
        for call in iter_service_calls(auto.get("action", [])):
            record(writer, call)
    for name, body in (scripts or {}).items():
        writer = f"script.{name}"
        for call in iter_service_calls((body or {}).get("sequence", [])):
            record(writer, call)

    return ({k: sorted(v) for k, v in writes.items()},
            {k: sorted(v) for k, v in dynamic.items()})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/test_ha_state_model.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/ha_state_model.py scripts/test_ha_state_model.py
git commit -m "feat(ha-state): write attribution + scene-to-entity resolution

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Load the real role + introspect cells/scenes/thresholds/entities

**Files:**
- Modify: `scripts/ha_state_model.py`
- Test: `scripts/test_ha_state_model.py`

**Interfaces:**
- Consumes: `validate_ha_config.assemble_config`, `validate_ha_config.HAConfigLoader`, `validate_ha_config.ROLE_DIR`.
- Produces:
  - `load_role(role_dir=ROLE_DIR) -> dict` — the merged `configuration.yaml` tree (with `!include`d automation/script/scene/template sub-trees inlined; `!secret` → placeholder strings).
  - `extract_cells(config: dict) -> dict[str, dict]` — name → `{entity, domain, name}` for every `input_boolean`/`input_number`/`input_datetime`/`timer`.
  - `extract_thresholds(config: dict) -> list[dict]` — each threshold `binary_sensor`: `{entity, name, bound}` where bound ∈ {`upper`,`lower`}.
  - `config_entities(config: dict, scenes: list) -> set[str]` — every entity id DERIVABLE from the repo config (helpers, scenes, threshold + template sensors). Used by the resolution check so a new helper added in the same PR resolves without a live refresh.

> **Implementation note (do at Step 3, before coding):** run `uv run python -c "import sys; sys.path.insert(0,'scripts'); import ha_state_model as h; c=h.load_role(); print(sorted(c))"` once `load_role` exists, to confirm the actual top-level keys (`automation`, `script`, `scene`, `input_boolean`, `binary_sensor`, `template`, …). The repo's `configuration.yaml.j2` is copy-verbatim (no Ansible `{{ }}` — `assemble_config` would raise otherwise), so it loads cleanly. Adjust `.get` keys below to match what prints.

- [ ] **Step 1: Write the failing test**

```python
# add to scripts/test_ha_state_model.py
import validate_ha_config as vhc

CONFIG = {
    "input_boolean": {"bedroom_manual_off": {"name": "Bedroom manual off override"}},
    "input_number": {"bedroom_fan_expected_level": {"name": "Bedroom fan expected level"}},
    "timer": {"bedroom_fan_dial": {"name": "Bedroom fan-dial mode"}},
    "binary_sensor": [
        {"platform": "threshold", "name": "Bedroom CO2 high",
         "entity_id": "sensor.bedroom_airgradient_one_carbon_dioxide", "upper": 1000},
        {"platform": "threshold", "name": "Bedroom FP300 battery low",
         "entity_id": "sensor.aqara_fp300_battery", "lower": 20},
    ],
}


def test_extract_cells():
    cells = hsm.extract_cells(CONFIG)
    assert cells["bedroom_manual_off"]["entity"] == "input_boolean.bedroom_manual_off"
    assert cells["bedroom_fan_dial"]["entity"] == "timer.bedroom_fan_dial"
    assert cells["bedroom_fan_expected_level"]["domain"] == "input_number"


def test_extract_thresholds_records_bound_direction():
    th = {t["entity"]: t for t in hsm.extract_thresholds(CONFIG)}
    assert th["binary_sensor.bedroom_co2_high"]["bound"] == "upper"
    assert th["binary_sensor.bedroom_fp300_battery_low"]["bound"] == "lower"


def test_config_entities_includes_helpers_scenes_thresholds():
    ents = hsm.config_entities(CONFIG, SCENES)
    assert "input_boolean.bedroom_manual_off" in ents
    assert "timer.bedroom_fan_dial" in ents
    assert "binary_sensor.bedroom_co2_high" in ents
    assert "scene.bedroom_nightlight" in ents


def test_load_role_returns_real_automation_list():
    config = hsm.load_role()
    aliases = {a.get("alias") for a in config.get("automation", [])}
    assert "Bedroom away" in aliases          # sanity: the real role loaded
    assert isinstance(config.get("script"), dict)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/test_ha_state_model.py -q`
Expected: FAIL — `AttributeError: module 'ha_state_model' has no attribute 'extract_cells'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to scripts/ha_state_model.py
import tempfile
from pathlib import Path

import yaml

from validate_ha_config import ROLE_DIR, HAConfigLoader, assemble_config

_CELL_DOMAINS = ("input_boolean", "input_number", "input_datetime", "timer")


def load_role(role_dir: Path = ROLE_DIR) -> dict:
    """Assemble the deployed /config layout into a temp dir and return the loaded
    configuration.yaml tree (automation/script/scene/template sub-trees inlined via !include)."""
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        assemble_config(role_dir, dest)
        with (dest / "configuration.yaml").open() as fh:
            return yaml.load(fh, Loader=HAConfigLoader)


def extract_cells(config: dict) -> dict[str, dict]:
    """name -> {entity, domain, name} for every helper that is coordination state."""
    cells: dict[str, dict] = {}
    for domain in _CELL_DOMAINS:
        for name, spec in (config.get(domain) or {}).items():
            cells[name] = {
                "entity": f"{domain}.{name}",
                "domain": domain,
                "name": (spec or {}).get("name", name),
            }
    return cells


def _threshold_sensors(config: dict) -> list[dict]:
    bs = config.get("binary_sensor") or []
    return [s for s in bs if isinstance(s, dict) and s.get("platform") == "threshold"]


def extract_thresholds(config: dict) -> list[dict]:
    """Each threshold binary_sensor -> {entity, name, bound, source}. The derived entity id is
    binary_sensor.<slug(name)> (how HA names a platform sensor from its `name`)."""
    out = []
    for s in _threshold_sensors(config):
        name = s.get("name", "")
        bound = "upper" if "upper" in s else "lower"
        out.append({
            "entity": f"binary_sensor.{slugify(name)}",
            "name": name,
            "bound": bound,
            "source": s.get("entity_id"),
        })
    return out


def _template_sensor_entities(config: dict) -> set[str]:
    """Entity ids declared by the modern `template:` integration (templates.yaml)."""
    ents: set[str] = set()
    tmpl = config.get("template") or []
    blocks = tmpl if isinstance(tmpl, list) else [tmpl]
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for domain in ("sensor", "binary_sensor"):
            for item in block.get(domain, []) or []:
                uid = item.get("unique_id")
                if uid:
                    ents.add(f"{domain}.{uid}")
    return ents


def config_entities(config: dict, scenes: list) -> set[str]:
    """Every entity id derivable from the repo config — helpers, scenes, threshold sensors,
    template sensors. The resolution check unions this with the live external-entity snapshot."""
    ents = {c["entity"] for c in extract_cells(config).values()}
    ents |= {t["entity"] for t in extract_thresholds(config)}
    ents |= set(scene_entity_map(scenes).keys())
    ents |= _template_sensor_entities(config)
    return ents
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/test_ha_state_model.py -q`
Expected: PASS (12 tests). If `test_load_role_returns_real_automation_list` fails on a key name, run the implementation-note command and adjust `.get("automation")`/`.get("script")`/scene key to the actual include keys.

- [ ] **Step 5: Commit**

```bash
git add scripts/ha_state_model.py scripts/test_ha_state_model.py
git commit -m "feat(ha-state): load real role + introspect cells/thresholds/derived entities

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Build the model + `generate` the committed artifacts

**Files:**
- Modify: `scripts/ha_state_model.py`
- Create (generated): `ansible/roles/containers/home-assistant/state/derived_state.yml`, `…/state/STATE.md`
- Test: `scripts/test_ha_state_model.py`

**Interfaces:**
- Consumes: `load_role`, `extract_cells`, `extract_writes`, `scene_entity_map`, `config_entities`.
- Produces:
  - `build_model(config: dict) -> dict` — `{cells, actuators, writes, dynamic_writes}`, fully sorted/deterministic. `actuators` = the configured `adaptive_lighting` lights + `fan.tower_fan` plus any entity in `writes` whose domain is `light`/`fan`.
  - `render_derived_yaml(model: dict) -> str` and `render_state_md(model: dict) -> str` — deterministic text.
  - `STATE_DIR`, `DERIVED_YAML`, `STATE_MD` path constants.
  - `cmd_generate(role_dir=ROLE_DIR) -> int` — writes both artifacts.

- [ ] **Step 1: Write the failing test**

```python
# add to scripts/test_ha_state_model.py
def test_build_model_is_deterministic_and_sorted():
    config = {**CONFIG, "automation": [
        {"id": "a", "alias": "Bedroom away", "action": [
            {"service": "light.turn_off", "target": {"entity_id": "light.bedroom_lights"}}]}],
        "script": {}, "scene": SCENES}
    m1 = hsm.build_model(config)
    m2 = hsm.build_model(config)
    assert m1 == m2
    assert m1["writes"]["light.bedroom_lights"] == ["automation.bedroom_away"]
    assert "light.bedroom_lights" in m1["actuators"]


def test_render_derived_yaml_roundtrips():
    import yaml as y
    model = {"cells": {}, "actuators": ["light.bedroom_lights"],
             "writes": {"light.bedroom_lights": ["automation.x"]}, "dynamic_writes": {}}
    text = hsm.render_derived_yaml(model)
    assert y.safe_load(text)["writes"]["light.bedroom_lights"] == ["automation.x"]


def test_render_state_md_lists_actuator_writers():
    model = {"cells": {"bedroom_manual_off": {"entity": "input_boolean.bedroom_manual_off",
             "name": "Bedroom manual off override"}},
             "actuators": ["light.bedroom_lights"],
             "writes": {"light.bedroom_lights": ["automation.bedroom_away"]},
             "dynamic_writes": {}}
    md = hsm.render_state_md(model)
    assert "light.bedroom_lights" in md
    assert "automation.bedroom_away" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/test_ha_state_model.py -q`
Expected: FAIL — `AttributeError: ... 'build_model'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to scripts/ha_state_model.py
STATE_DIR = ROLE_DIR / "state"
DERIVED_YAML = STATE_DIR / "derived_state.yml"
STATE_MD = STATE_DIR / "STATE.md"

_GENERATED_BANNER = "# GENERATED by scripts/ha_state_model.py — DO NOT EDIT. Run `generate`.\n"


def _actuator_lights(config: dict) -> set[str]:
    out: set[str] = set()
    for inst in config.get("adaptive_lighting") or []:
        for light in (inst or {}).get("lights", []) or []:
            out.add(light)
    return out


def build_model(config: dict) -> dict:
    scenes = config.get("scene") or []
    cells = extract_cells(config)
    writes, dynamic = extract_writes(config.get("automation"), config.get("script"), scene_entity_map(scenes))
    actuators = set(_actuator_lights(config)) | {"fan.tower_fan"}
    actuators |= {e for e in writes if e.split(".")[0] in ("light", "fan")}
    return {
        "cells": dict(sorted(cells.items())),
        "actuators": sorted(actuators),
        "writes": dict(sorted(writes.items())),
        "dynamic_writes": dict(sorted(dynamic.items())),
    }


def render_derived_yaml(model: dict) -> str:
    body = yaml.safe_dump(model, sort_keys=True, default_flow_style=False)
    return _GENERATED_BANNER + body


def render_state_md(model: dict) -> str:
    lines = ["<!-- GENERATED by scripts/ha_state_model.py — DO NOT EDIT. Run `generate`. -->",
             "# Bedroom HA — Derived State Model", "",
             "Generated from the real automations/scripts/config. The *why* (runtime traps, "
             "feedback loops) lives in this role's `CLAUDE.md`.", "",
             "## Cells (coordination state)", "", "| Cell | Entity | Purpose |", "|---|---|---|"]
    for name, c in model["cells"].items():
        lines.append(f"| {name} | `{c['entity']}` | {c.get('name', '')} |")
    lines += ["", "## Actuators — writers", ""]
    for act in model["actuators"]:
        writers = ", ".join(f"`{w}`" for w in model["writes"].get(act, [])) or "_none_"
        lines.append(f"- **`{act}`** ← {writers}")
    if model["dynamic_writes"]:
        lines += ["", "## Unresolved (templated) write targets", ""]
        for writer, targets in model["dynamic_writes"].items():
            lines.append(f"- `{writer}`: {', '.join('`%s`' % t for t in targets)}")
    return "\n".join(lines) + "\n"


def cmd_generate(role_dir: Path = ROLE_DIR) -> int:
    model = build_model(load_role(role_dir))
    (role_dir / "state").mkdir(exist_ok=True)
    DERIVED_YAML.write_text(render_derived_yaml(model))
    STATE_MD.write_text(render_state_md(model))
    print(f"generated {DERIVED_YAML.name} + {STATE_MD.name}")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/test_ha_state_model.py -q`
Expected: PASS (15 tests).

- [ ] **Step 5: Generate the real artifacts and eyeball them**

Run:
```bash
uv run python -c "import sys; sys.path.insert(0,'scripts'); import ha_state_model as h; h.cmd_generate()"
sed -n '1,40p' ansible/roles/containers/home-assistant/state/STATE.md
```
Expected: `STATE.md` lists the cells table and `light.bedroom_lights` ← a long writer list (~15 writers — the Phase 2 gap, visible). `derived_state.yml` exists and is valid YAML.

- [ ] **Step 6: Commit (code + initial generated artifacts)**

```bash
git add scripts/ha_state_model.py scripts/test_ha_state_model.py \
  ansible/roles/containers/home-assistant/state/derived_state.yml \
  ansible/roles/containers/home-assistant/state/STATE.md
git commit -m "feat(ha-state): build model + generate derived_state.yml + STATE.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `refresh` (live external-entity snapshot) + entity-reference resolution check

**Files:**
- Modify: `scripts/ha_state_model.py`
- Create: `ansible/roles/containers/home-assistant/state/external_entities.yml`
- Test: `scripts/test_ha_state_model.py`

**Interfaces:**
- Consumes: `config_entities`, `extract_writes`, `load_role`; `probe.resolve_ip`, `probe.ha_get`, `probe.ha_token`, `probe.ha_get_url`, `probe.HA_CONTAINER` (for the live `refresh` only).
- Produces:
  - `EXTERNAL_YAML` path; `load_external_entities() -> set[str]`.
  - `referenced_entities(config: dict) -> set[str]` — write targets + trigger/condition `entity_id`s across automations+scripts (NOT templated reads).
  - `resolution_errors(config: dict, known: set[str]) -> list[str]` — referenced ids of a managed domain not in `known`.
  - `cmd_refresh(get_states=None) -> int` — snapshot live entity ids that are NOT config-derivable into `external_entities.yml`.

- [ ] **Step 1: Write the failing test**

```python
# add to scripts/test_ha_state_model.py
def test_referenced_entities_collects_write_and_trigger_targets():
    config = {"automation": [
        {"id": "a", "alias": "A", "trigger": [
            {"platform": "state", "entity_id": "binary_sensor.aqara_fp300_presence"}],
         "condition": [{"condition": "state", "entity_id": "person.daniel", "state": "home"}],
         "action": [{"service": "light.turn_on", "target": {"entity_id": "light.bedroom_lights"}}]}],
        "script": {}, "scene": []}
    refs = hsm.referenced_entities(config)
    assert {"binary_sensor.aqara_fp300_presence", "person.daniel", "light.bedroom_lights"} <= refs


def test_resolution_errors_flags_unknown_managed_entity():
    config = {"automation": [
        {"id": "a", "alias": "A", "action": [
            {"service": "switch.turn_on", "target": {"entity_id": "switch.typo_does_not_exist"}}]}],
        "script": {}, "scene": []}
    known = {"light.bedroom_lights"}  # switch.typo... absent
    errs = hsm.resolution_errors(config, known)
    assert any("switch.typo_does_not_exist" in e for e in errs)


def test_resolution_ignores_unmanaged_domains_and_templated():
    config = {"automation": [
        {"id": "a", "alias": "A", "action": [
            {"service": "notify.mobile_app_x", "data": {"message": "hi"}},
            {"service": "light.turn_on", "target": {"entity_id": "{{ x }}"}}]}],
        "script": {}, "scene": []}
    assert hsm.resolution_errors(config, set()) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/test_ha_state_model.py -q`
Expected: FAIL — `AttributeError: ... 'referenced_entities'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to scripts/ha_state_model.py
EXTERNAL_YAML = STATE_DIR / "external_entities.yml"

# Domains the resolution check is responsible for (entity references we author + control). Other
# domains (notify, persistent_notification, tts, media_player, device_tracker, weather, zone, sun,
# person, sensor) come from integrations and are only checked if present in `known`.
_MANAGED_DOMAINS = ("input_boolean", "input_number", "input_datetime", "timer",
                    "switch", "light", "fan", "scene", "binary_sensor")


def _walk_entity_id_fields(node) -> Iterator[str]:
    """Yield every value of an `entity_id:` key anywhere in `node` (scalar or list)."""
    if isinstance(node, dict):
        ent = node.get("entity_id")
        if isinstance(ent, str):
            yield ent
        elif isinstance(ent, list):
            yield from (e for e in ent if isinstance(e, str))
        for value in node.values():
            yield from _walk_entity_id_fields(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_entity_id_fields(value)


def referenced_entities(config: dict) -> set[str]:
    """Write targets + every `entity_id:` field (triggers/conditions/actions) across automations
    and scripts. Templated values are dropped (can't be resolved statically)."""
    refs: set[str] = set()
    for auto in config.get("automation") or []:
        refs |= set(_walk_entity_id_fields(auto))
    for body in (config.get("script") or {}).values():
        refs |= set(_walk_entity_id_fields(body))
    return {r for r in refs if not _is_templated(r)}


def resolution_errors(config: dict, known: set[str]) -> list[str]:
    """A managed-domain entity referenced but absent from `known` (= a typo or a stale external
    snapshot — run `refresh`)."""
    errs = []
    for ref in sorted(referenced_entities(config)):
        if ref.split(".")[0] in _MANAGED_DOMAINS and ref not in known:
            errs.append(f"unresolved entity reference: {ref} "
                        f"(typo, or run `ha_state_model.py refresh` if it is a new device)")
    return errs


def load_external_entities() -> set[str]:
    if not EXTERNAL_YAML.is_file():
        return set()
    return set(yaml.safe_load(EXTERNAL_YAML.read_text()).get("entities", []))


def cmd_refresh(get_states=None) -> int:
    """Snapshot live entity ids that are NOT config-derivable into external_entities.yml. Injects
    get_states for tests; defaults to the live HA GET /api/states via probe.py."""
    if get_states is None:
        import json
        import probe
        body = probe.ha_get(probe.ha_get_url(probe.resolve_ip(probe.HA_CONTAINER), "states"),
                            probe.ha_token())
        live = [s["entity_id"] for s in json.loads(body)]
    else:
        live = list(get_states())
    config = load_role()
    derived = config_entities(config, config.get("scene") or [])
    external = sorted(e for e in live if e not in derived)
    STATE_DIR.mkdir(exist_ok=True)
    EXTERNAL_YAML.write_text(_GENERATED_BANNER + yaml.safe_dump({"entities": external},
                                                               default_flow_style=False))
    print(f"snapshotted {len(external)} external entities")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/test_ha_state_model.py -q`
Expected: PASS (18 tests).

- [ ] **Step 5: Produce the real external snapshot (live HA — on daniel-server)**

Run:
```bash
uv run python -c "import sys; sys.path.insert(0,'scripts'); import ha_state_model as h; h.cmd_refresh()"
uv run python scripts/probe.py ha state switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom
```
Expected: `external_entities.yml` written with the integration entities (fan/sensors/AL switches/person/etc.); the AL sleep switch resolves. If `refresh` can't reach HA in your context, this step is done on daniel-server where the SOPS age key + HA live.

- [ ] **Step 6: Commit**

```bash
git add scripts/ha_state_model.py scripts/test_ha_state_model.py \
  ansible/roles/containers/home-assistant/state/external_entities.yml
git commit -m "feat(ha-state): live external-entity snapshot + entity-reference resolution check

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Tripwire + structural + report-mode checks

**Files:**
- Modify: `scripts/ha_state_model.py`
- Create: `ansible/roles/containers/home-assistant/state/expected_override_writers.yml`
- Test: `scripts/test_ha_state_model.py`

**Interfaces:**
- Consumes: `extract_writes`, `extract_thresholds`, `automation_writer`, `load_role`.
- Produces:
  - `OVERRIDE_CELLS = ("input_boolean.bedroom_manual_off", "input_boolean.bedroom_fan_manual", "input_boolean.bedroom_sleep_mode")`
  - `override_writer_errors(writes, expected: dict) -> list[str]` (hard)
  - `threshold_pairing_errors(config) -> list[str]` (hard) — every `<cat>_bad` trigger id in the threshold automation has a matching `<cat>_ok`, and the automation's referenced threshold sensors match the declared threshold set.
  - `alias_collision_errors(config) -> list[str]` (hard)
  - `single_writer_report(writes, sanctioned: dict) -> list[str]` and `override_consistency_report(...) -> list[str]` (report)

- [ ] **Step 1: Write the failing test**

```python
# add to scripts/test_ha_state_model.py
def test_override_writer_errors_flags_undeclared_writer():
    writes = {"input_boolean.bedroom_sleep_mode": ["script.bedroom_bedtime", "automation.new_thing"]}
    expected = {"input_boolean.bedroom_sleep_mode": ["script.bedroom_bedtime"]}
    errs = hsm.override_writer_errors(writes, expected)
    assert any("automation.new_thing" in e for e in errs)


def test_override_writer_errors_clean_when_match():
    writes = {"input_boolean.bedroom_sleep_mode": ["script.bedroom_bedtime"]}
    expected = {"input_boolean.bedroom_sleep_mode": ["script.bedroom_bedtime"]}
    assert hsm.override_writer_errors(writes, expected) == []


def test_threshold_pairing_flags_missing_ok():
    config = {"binary_sensor": [
        {"platform": "threshold", "name": "Bedroom CO2 high", "entity_id": "sensor.x", "upper": 1},
        {"platform": "threshold", "name": "Bedroom VOC high", "entity_id": "sensor.y", "upper": 1}],
        "automation": [{"id": "bedroom_threshold_alert", "alias": "Bedroom threshold alert",
            "trigger": [
                {"platform": "state", "entity_id": "binary_sensor.bedroom_co2_high",
                 "to": "on", "id": "airquality_bad"},
                {"platform": "state", "entity_id": "binary_sensor.bedroom_co2_high",
                 "to": "off", "id": "airquality_ok"},
                {"platform": "state", "entity_id": "binary_sensor.bedroom_voc_high",
                 "to": "on", "id": "airquality_bad"}],
            "action": []}], "script": {}}
    # co2+voc declared but the automation never references binary_sensor.bedroom_voc_high as a
    # trigger entity -> mismatch flagged
    errs = hsm.threshold_pairing_errors(config)
    assert any("bedroom_voc_high" in e for e in errs)


def test_alias_collision_flags_duplicate_slug():
    config = {"automation": [{"id": "a", "alias": "Bedroom away"},
                             {"id": "b", "alias": "Bedroom  away"}], "script": {}}
    assert hsm.alias_collision_errors(config) != []


def test_single_writer_report_lists_extra_writers():
    writes = {"light.bedroom_lights": ["script.bedroom_lights_set", "automation.bedroom_away"]}
    sanctioned = {"light.bedroom_lights": "script.bedroom_lights_set"}
    rep = hsm.single_writer_report(writes, sanctioned)
    assert any("automation.bedroom_away" in r for r in rep)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/test_ha_state_model.py -q`
Expected: FAIL — `AttributeError: ... 'override_writer_errors'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to scripts/ha_state_model.py
EXPECTED_OVERRIDE_WRITERS = STATE_DIR / "expected_override_writers.yml"
OVERRIDE_CELLS = ("input_boolean.bedroom_manual_off",
                  "input_boolean.bedroom_fan_manual",
                  "input_boolean.bedroom_sleep_mode")
# Phase 2 will flip these report checks to hard; the sanctioned writer per actuator:
SANCTIONED_WRITERS = {"light.bedroom_lights": "script.bedroom_lights_set",
                      "fan.tower_fan": "script.bedroom_apply_fan"}


def load_expected_override_writers() -> dict:
    if not EXPECTED_OVERRIDE_WRITERS.is_file():
        return {}
    return yaml.safe_load(EXPECTED_OVERRIDE_WRITERS.read_text()) or {}


def override_writer_errors(writes: dict, expected: dict) -> list[str]:
    """HARD: the derived writer set of each override boolean must equal the declared list."""
    errs = []
    for cell in OVERRIDE_CELLS:
        got = set(writes.get(cell, []))
        want = set(expected.get(cell, []))
        for extra in sorted(got - want):
            errs.append(f"{cell}: undeclared writer {extra} — add it to "
                        f"state/expected_override_writers.yml (shared coordination state)")
        for missing in sorted(want - got):
            errs.append(f"{cell}: declared writer {missing} no longer writes it — remove it")
    return errs


def _threshold_automation(config: dict) -> dict | None:
    for auto in config.get("automation") or []:
        if auto.get("id") == "bedroom_threshold_alert":
            return auto
    return None


def threshold_pairing_errors(config: dict) -> list[str]:
    """HARD: every `<cat>_bad` trigger id has a `<cat>_ok`, and the threshold sensors the
    automation triggers on exactly match the declared threshold binary_sensors."""
    auto = _threshold_automation(config)
    if not auto:
        return []
    errs = []
    cats = defaultdict(set)
    trig_entities = set()
    for trig in auto.get("trigger", []) or []:
        tid = trig.get("id", "")
        ent = trig.get("entity_id")
        if isinstance(ent, str):
            trig_entities.add(ent)
        if tid.endswith("_bad"):
            cats[tid[:-4]].add("bad")
        elif tid.endswith("_ok"):
            cats[tid[:-3]].add("ok")
    for cat, sides in sorted(cats.items()):
        if sides != {"bad", "ok"}:
            missing = ({"bad", "ok"} - sides).pop()
            errs.append(f"threshold category '{cat}' is missing its _{missing} trigger")
    declared = {t["entity"] for t in extract_thresholds(config)}
    for ent in sorted(declared - trig_entities):
        errs.append(f"declared threshold {ent} is not wired into bedroom_threshold_alert triggers")
    for ent in sorted(trig_entities - declared):
        if ent.startswith("binary_sensor.") and "outdoor" not in ent:
            errs.append(f"threshold trigger {ent} has no matching declared threshold sensor")
    return errs


def alias_collision_errors(config: dict) -> list[str]:
    seen: dict[str, str] = {}
    errs = []
    for auto in config.get("automation") or []:
        name = automation_writer(auto)
        alias = auto.get("alias") or auto.get("id")
        if name in seen:
            errs.append(f"alias-slug collision: {name!r} from {seen[name]!r} and {alias!r}")
        seen[name] = alias
    return errs


def single_writer_report(writes: dict, sanctioned: dict) -> list[str]:
    rep = []
    for act, owner in sanctioned.items():
        extras = [w for w in writes.get(act, []) if w != owner]
        if extras:
            rep.append(f"{act}: {len(extras)} writer(s) besides {owner}: {', '.join(extras)}")
    return rep


def override_consistency_report(writes: dict) -> list[str]:
    """REPORT: surfaces actuators whose manual-detect override isn't engaged by every manual
    surface. Phase 1 emits the lights<->manual_off relationship as a starting datapoint."""
    rep = []
    light_writers = set(writes.get("light.bedroom_lights", []))
    override_writers = set(writes.get("input_boolean.bedroom_manual_off", []))
    # automations that write the light but never the override are candidate gaps (advisory).
    gap = sorted(w for w in light_writers if w not in override_writers
                 and w.startswith("automation."))
    if gap:
        rep.append("lights written without touching manual_off (review for Phase 2): "
                   + ", ".join(gap))
    return rep
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/test_ha_state_model.py -q`
Expected: PASS (23 tests).

- [ ] **Step 5: Seed the tripwire file from the current derived writers**

Run:
```bash
uv run python - <<'PY'
import sys; sys.path.insert(0, "scripts")
import ha_state_model as h, yaml
m = h.build_model(h.load_role())
seed = {c: m["writes"].get(c, []) for c in h.OVERRIDE_CELLS}
banner = ("# HAND-MAINTAINED tripwire — the ONLY hand-declared state-model file.\n"
          "# CI fails if an automation/script writes one of these 3 override booleans without\n"
          "# being listed here (forces a conscious 'touching shared coordination state' ack).\n")
h.EXPECTED_OVERRIDE_WRITERS.write_text(banner + yaml.safe_dump(seed, default_flow_style=False))
print(open(h.EXPECTED_OVERRIDE_WRITERS).read())
PY
```
Expected: the file lists the real current writers of the 3 booleans. Eyeball it — this is the one file you maintain by hand.

- [ ] **Step 6: Commit**

```bash
git add scripts/ha_state_model.py scripts/test_ha_state_model.py \
  ansible/roles/containers/home-assistant/state/expected_override_writers.yml
git commit -m "feat(ha-state): override-writer tripwire + structural + report-mode checks

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Aggregate checks + freshness gate + wire into the existing hook

**Files:**
- Modify: `scripts/ha_state_model.py`, `scripts/validate_ha_config.py`
- Test: `scripts/test_ha_state_model.py`

**Interfaces:**
- Consumes: every check above; `render_derived_yaml`/`render_state_md`/`build_model`.
- Produces:
  - `freshness_errors(role_dir=ROLE_DIR) -> list[str]` — regenerate in-memory and compare to the committed files.
  - `check_errors(role_dir=ROLE_DIR) -> list[str]` — all hard checks aggregated (resolution + override-writer + threshold + alias + freshness). Report-mode lists are printed, not returned.
  - `main(argv=None) -> int` — `generate` / `refresh` / `check` subcommands.
  - `validate_ha_config.validate()` appends `ha_state_model.check_errors()`.

- [ ] **Step 1: Write the failing test**

```python
# add to scripts/test_ha_state_model.py
def test_freshness_errors_flag_stale_committed_file(tmp_path, monkeypatch):
    # point the artifact paths at a temp dir with deliberately-wrong content
    monkeypatch.setattr(hsm, "DERIVED_YAML", tmp_path / "derived_state.yml")
    monkeypatch.setattr(hsm, "STATE_MD", tmp_path / "STATE.md")
    (tmp_path / "derived_state.yml").write_text("stale: true\n")
    (tmp_path / "STATE.md").write_text("stale\n")
    errs = hsm.freshness_errors()
    assert any("derived_state.yml" in e for e in errs)


def test_check_errors_on_real_role_is_clean_after_generate(tmp_path):
    # After Task 4/5/6 produced fresh artifacts + snapshot, the real role must validate clean.
    errs = hsm.check_errors()
    assert errs == [], "real role failed state-model checks:\n" + "\n".join(errs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/test_ha_state_model.py::test_freshness_errors_flag_stale_committed_file -q`
Expected: FAIL — `AttributeError: ... 'freshness_errors'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to scripts/ha_state_model.py
import argparse
import sys


def freshness_errors(role_dir: Path = ROLE_DIR) -> list[str]:
    model = build_model(load_role(role_dir))
    errs = []
    for path, want in ((DERIVED_YAML, render_derived_yaml(model)),
                       (STATE_MD, render_state_md(model))):
        have = path.read_text() if path.is_file() else ""
        if have != want:
            errs.append(f"{path.name} is stale — run `ha_state_model.py generate` and commit")
    return errs


def check_errors(role_dir: Path = ROLE_DIR) -> list[str]:
    """All HARD checks, aggregated. Report-mode invariants are printed (stderr), not failed on."""
    config = load_role(role_dir)
    model = build_model(config)
    known = config_entities(config, config.get("scene") or []) | load_external_entities()
    errs: list[str] = []
    errs += freshness_errors(role_dir)
    errs += resolution_errors(config, known)
    errs += override_writer_errors(model["writes"], load_expected_override_writers())
    errs += threshold_pairing_errors(config)
    errs += alias_collision_errors(config)
    for line in (single_writer_report(model["writes"], SANCTIONED_WRITERS)
                 + override_consistency_report(model["writes"])):
        print(f"[state-model report] {line}", file=sys.stderr)
    return errs


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ha_state_model.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate", help="regenerate derived_state.yml + STATE.md")
    sub.add_parser("refresh", help="snapshot live external entities (needs HA + SOPS key)")
    sub.add_parser("check", help="run the guardrail checks (exit 1 on hard error)")
    ns = p.parse_args(argv)
    if ns.cmd == "generate":
        return cmd_generate()
    if ns.cmd == "refresh":
        return cmd_refresh()
    errs = check_errors()
    if errs:
        print("HA state-model checks FAILED:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("HA state-model OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Wire into the existing validator (one hook)**

In `scripts/validate_ha_config.py`, at the end of `validate()` (after the `jinja_errors` line, before `return errors`), add:

```python
        # State-model guardrails (freshness, entity-resolution, override tripwire, structural).
        try:
            import ha_state_model
            errors += ha_state_model.check_errors(role_dir)
        except Exception as exc:  # never let the state-model check mask a config error
            errors.append(f"state-model check crashed: {exc}")
        return errors
```

(Replace the existing bare `return errors` at the end of `validate()` with the block above.)

- [ ] **Step 5: Run the tests + the real validators**

Run:
```bash
uv run pytest scripts/test_ha_state_model.py -q
uv run python scripts/ha_state_model.py check
uv run python scripts/validate_ha_config.py
```
Expected: tests PASS (25); `ha_state_model.py check` prints any `[state-model report]` lines then `HA state-model OK`; `validate_ha_config.py` prints `Home Assistant config OK`. If `check` reports a real resolution error (e.g. a genuine entity typo in the config), fix the config — that is the tool working.

- [ ] **Step 6: Confirm the freshness gate bites, then commit**

Run:
```bash
printf '\n# tamper\n' >> ansible/roles/containers/home-assistant/state/derived_state.yml
uv run python scripts/ha_state_model.py check; echo "exit=$?"
git checkout -- ansible/roles/containers/home-assistant/state/derived_state.yml
```
Expected: `check` exits 1 with "derived_state.yml is stale"; the checkout restores it.

```bash
git add scripts/ha_state_model.py scripts/validate_ha_config.py scripts/test_ha_state_model.py
git commit -m "feat(ha-state): aggregate checks + freshness gate, wired into validate-ha-config

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: `probe.py ha-state` live view

**Files:**
- Modify: `scripts/probe.py`
- Test: `scripts/test_ha_state_model.py` (or `scripts/test_probe.py` if one exists — check first)

**Interfaces:**
- Consumes: `ha_state_model.load_role`, `ha_state_model.build_model`; probe's `ha_get_url`, `resolve_ip`, `ha_get`, `ha_token`, `HA_CONTAINER`.
- Produces (in `probe.py`):
  - `ha_state_rows(states: list[dict], model: dict) -> str` — pure formatter: per-cell current value + age, per-automation enabled + last_triggered, anomaly summary. `states` is the parsed `/api/states` list (injected → testable).
  - parser: `sub.add_parser("ha-state", …)` with `--inventory`.
  - `run_ha_state(ns)` handled in `main` like `health`/`ha`.

- [ ] **Step 1: Write the failing test**

```python
# add to scripts/test_ha_state_model.py
import probe


def test_ha_state_rows_renders_cell_values_and_anomaly():
    model = {"cells": {"bedroom_sleep_mode": {"entity": "input_boolean.bedroom_sleep_mode",
             "name": "Bedroom sleep mode"}}, "actuators": [], "writes": {}, "dynamic_writes": {}}
    states = [{"entity_id": "input_boolean.bedroom_sleep_mode", "state": "on",
               "last_changed": "2026-06-21T12:00:00+00:00"}]
    out = probe.ha_state_rows(states, model)
    assert "input_boolean.bedroom_sleep_mode" in out
    assert "on" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/test_ha_state_model.py::test_ha_state_rows_renders_cell_values_and_anomaly -q`
Expected: FAIL — `AttributeError: module 'probe' has no attribute 'ha_state_rows'`.

- [ ] **Step 3: Write minimal implementation**

In `scripts/probe.py`, add the formatter (near `format_ha_state`):

```python
def ha_state_rows(states, model):
    """Render the derived cells/automations annotated with live values from a /api/states list."""
    by_id = {s["entity_id"]: s for s in states}
    lines = ["Cells:"]
    for name, cell in model["cells"].items():
        s = by_id.get(cell["entity"])
        val = s["state"] if s else "—(absent)"
        when = s.get("last_changed", "") if s else ""
        lines.append(f"  {cell['entity']:<52} = {val:<12} {when}")
    anomalies = []
    sleep = by_id.get("input_boolean.bedroom_sleep_mode", {}).get("state")
    if sleep == "on":
        anomalies.append("sleep_mode is on (verify expected at this hour)")
    moff = by_id.get("input_boolean.bedroom_manual_off", {}).get("state")
    if moff == "on":
        anomalies.append("manual_off is on (presence will NOT auto-light)")
    if anomalies:
        lines = [f"⚠ {len(anomalies)} anomaly(ies): " + "; ".join(anomalies), ""] + lines
    return "\n".join(lines)
```

Add to `_build_parser()` (after the `ha` subparser block):

```python
    hst = sub.add_parser("ha-state", help="live view of the derived state model")
    hst.add_argument("--inventory", action="store_true",
                     help="also dump every live entity grouped by domain")
```

Add a handler and dispatch it in `main()` (alongside the `health`/`ha` branches):

```python
def run_ha_state(ns):
    import json
    import ha_state_model
    if ns.dry_run:
        print(" ".join(ha_curl_argv(ha_get_url("<ha-ip>", "states"))) + "   # + Bearer (stdin)")
        return 0
    body = ha_get(ha_get_url(resolve_ip(HA_CONTAINER), "states"), ha_token())
    states = json.loads(body)
    model = ha_state_model.build_model(ha_state_model.load_role())
    print(ha_state_rows(states, model))
    if ns.inventory:
        print("\nInventory:")
        for s in sorted(states, key=lambda x: x["entity_id"]):
            print(f"  {s['entity_id']:<55} {s['state']}")
    return 0
```

In `main()`, add before the `plan(...)` call:

```python
    if ns.cmd == "ha-state":
        return run_ha_state(ns)
```

- [ ] **Step 4: Run test + a live smoke (on daniel-server)**

Run:
```bash
uv run pytest scripts/test_ha_state_model.py::test_ha_state_rows_renders_cell_values_and_anomaly -q
uv run python scripts/probe.py ha-state
```
Expected: test PASS; the live view prints the cells with current values + any anomaly banner.

- [ ] **Step 5: Commit**

```bash
git add scripts/probe.py scripts/test_ha_state_model.py
git commit -m "feat(ha-state): probe.py ha-state live view + anomaly summary

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: CLAUDE.md pointer + full green

**Files:**
- Modify: `ansible/roles/containers/home-assistant/CLAUDE.md`

**Interfaces:** none (docs + final verification).

- [ ] **Step 1: (no entity_id typo to fix — verified)**

An earlier draft of this plan called for fixing a `CLAUDE.md:70` AL-switch entity_id typo
(`switch.bedroom_adaptive_lighting_sleep_mode_bedroom`). On verification, **no such typo exists**:
the role `CLAUDE.md` does not reference that entity_id, and the code uses the correct spelling
(`switch.adaptive_lighting_bedroom_adaptive_lighting_sleep_mode_bedroom`) in all three call sites.
The resolution check confirms 0 unresolved references on the real role. No fix needed — skip to
Step 2.

- [ ] **Step 2: Add a pointer to the generated model**

Under the role CLAUDE.md "Testing" or "Claude tooling" section, add:

```markdown
- **Derived state model** (`state/STATE.md` + `state/derived_state.yml`, generated by
  `scripts/ha_state_model.py generate`): the machine-derived map of cells/actuators and who
  writes them. Regenerated + freshness-gated by the `validate-ha-config` hook — never hand-edit.
  The single hand-maintained file is `state/expected_override_writers.yml` (the 3-boolean
  write tripwire). Live view: `scripts/probe.py ha-state`. This file (CLAUDE.md) remains the
  home of the runtime/physical *why* the model can't derive.
```

- [ ] **Step 3: Full verification — tests, checks, prek**

Run:
```bash
uv run pytest scripts -q
uv run python scripts/ha_state_model.py check
uv run python scripts/ha_state_model.py generate   # ensure committed artifacts are current
git status --short
prek run --all-files
```
Expected: all tests pass; `check` → `HA state-model OK`; `generate` leaves the tree clean (no diff — artifacts already current); `prek run` green (incl. the `validate-ha-config` hook now also running the state-model checks).

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/containers/home-assistant/CLAUDE.md
git commit -m "docs(home-assistant): fix AL sleep-switch entity_id; point to derived STATE.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review (completed during planning)

**Spec coverage:**
- Derived representation (cells/actuators/writes) → Tasks 1–4. ✅
- Generated `STATE.md` + `derived_state.yml` → Task 4. ✅
- Drift freshness gate → Task 7. ✅
- Entity-reference resolution (the AL-typo class) → Task 5. ✅
- External-entity snapshot (`refresh`) → Task 5. ✅
- 3-boolean override-writer tripwire → Task 6. ✅
- Structural completeness (threshold `_bad`/`_ok` + declared-vs-wired) → Task 6. ✅
- Alias-slug sanity → Task 6 (scoped to collision detection — the robustly-derivable subset). ✅
- Single-writer + override-consistency **report** mode → Task 6. ✅
- One validator/one hook → Task 7. ✅
- Live `probe.py ha-state` + `--inventory` → Task 8. ✅
- CLAUDE.md kept + `STATE.md` pointer → Task 9 (the `:70` typo turned out not to exist — verified). ✅
- Hermetic tests wired via `pyproject.toml testpaths` (already includes `scripts`) → every task. ✅

**Deliberate scope trims (noted, not gaps):**
- The threshold **category↔`cfg`-map** cross-check from the spec is NOT implemented (the `cfg` map is a Jinja literal; parsing it is brittle). The two robust structural checks (bad/ok pairing + declared-vs-wired threshold sets) cover the same "half-added metric" failure mode. Revisit if a clean `cfg`-key extraction proves easy.
- Recorder-`exclude` reference check deferred — low value, and `ha_heartbeat` is the only excluded helper today.

**Placeholder scan:** none — every step has runnable code/commands.

**Type consistency:** `build_model` returns `{cells, actuators, writes, dynamic_writes}`; consumed with those exact keys in Tasks 4/6/7/8. `extract_writes` returns the `(writes, dynamic)` tuple everywhere it's called. `check_errors(role_dir)`/`build_model(config)`/`load_role(role_dir)` signatures match across Tasks 3–8 and the `validate_ha_config` call site.

> **Live-path note for the executor:** `refresh` (Task 5 Step 5), the `ha-state` smoke (Task 8 Step 4), and any live verification must run **on daniel-server** (the SOPS age key + the HA container live there). The hermetic unit tests and `check`/`generate`/freshness gate run anywhere.
