#!/usr/bin/env python3
"""Derived state model for the Home Assistant bedroom control plane.

Reuses validate_ha_config's loader to parse the real automations/scripts/config, extracts
every write (service call -> target entity), and generates derived_state.yml + STATE.md. Also
runs the guardrail checks consumed by the validate-ha-config prek/CI hook. No live HA / Docker
for any of that — `refresh` (snapshot integration entities) is the only live path.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

import yaml

from validate_ha_config import ROLE_DIR, HAConfigLoader, assemble_config

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


def _all_service_calls(config: dict):
    """Every service call in the config's automations + scripts."""
    for auto in config.get("automation") or []:
        yield from iter_service_calls(auto.get("action", []))
    for body in (config.get("script") or {}).values():
        yield from iter_service_calls((body or {}).get("sequence", []))


def created_scenes(config: dict) -> set[str]:
    """`scene.<scene_id>` for every `scene.create` call — transient scenes built at runtime
    (e.g. bedroom_pre_alert from script.bedroom_alert_pulse) that are legitimately referenced
    by a later `scene.turn_on` but exist in no scenes.yaml entry and no live snapshot."""
    out: set[str] = set()
    for call in _all_service_calls(config):
        if call_service(call) == "scene.create":
            sid = (call.get("data") or {}).get("scene_id")
            if sid:
                out.add(f"scene.{sid}")
    return out


def config_entities(config: dict, scenes: list) -> set[str]:
    """Every entity id derivable from the repo config — helpers, scenes (static + runtime-created),
    threshold sensors, template sensors. The resolution check unions this with the live
    external-entity snapshot."""
    ents = {c["entity"] for c in extract_cells(config).values()}
    ents |= {t["entity"] for t in extract_thresholds(config)}
    ents |= set(scene_entity_map(scenes).keys())
    ents |= created_scenes(config)
    ents |= _template_sensor_entities(config)
    return ents


STATE_DIR = ROLE_DIR / "state"
DERIVED_YAML = STATE_DIR / "derived_state.yml"
STATE_MD = STATE_DIR / "STATE.md"

_GENERATED_BANNER = "# GENERATED by scripts/ha_state_model.py — DO NOT EDIT. Run `generate`.\n"


class _IndentDumper(yaml.SafeDumper):
    """SafeDumper that indents sequence items under their parent key, so the generated YAML
    satisfies ansible-lint/yamllint's `indent-sequences` (these files live under ansible/)."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, indentless=False)


def _dump_yaml(data) -> str:
    """Deterministic, ansible-lint-clean YAML dump used for every generated state-model file
    (derived_state.yml, external_entities.yml, expected_override_writers seed)."""
    return yaml.dump(data, Dumper=_IndentDumper, sort_keys=True, default_flow_style=False)


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
    return _GENERATED_BANNER + _dump_yaml(model)


def render_state_md(model: dict) -> str:
    lines = ["<!-- GENERATED by scripts/ha_state_model.py — DO NOT EDIT. Run `generate`. -->",
             "# Bedroom HA — Derived State Model", "",
             "Generated from the real automations/scripts/config. The *why* (runtime traps, "
             "feedback loops) lives in this role's `CLAUDE.md`.", "",
             "> Writer lists are **entity_id-static-only**: they attribute writes by the literal "
             "`entity_id` of each service call. A write that targets a cell/actuator by `device_id`, "
             "`area_id`, `label_id`, or a templated `{{ }}` entity is NOT attributed here (the real "
             "config uses none today). The override-writer tripwire's guarantee holds for "
             "entity_id-targeted writes — which is every write in this config.", "",
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
    EXTERNAL_YAML.write_text(_GENERATED_BANNER + _dump_yaml({"entities": external}))
    print(f"snapshotted {len(external)} external entities")
    return 0


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


def _trigger_entity_directions(trig: dict):
    """Yield (entity_id, to_value) for a state trigger. The real bedroom_threshold_alert groups
    each category's sensors into ONE bad + ONE ok trigger with a LIST entity_id, so this must
    handle both list and scalar forms (a scalar-only collector leaves trig_entities empty and
    false-flags every declared threshold as unwired)."""
    ent = trig.get("entity_id")
    to_val = trig.get("to")
    ids = [ent] if isinstance(ent, str) else (ent if isinstance(ent, list) else [])
    for e in ids:
        if isinstance(e, str):
            yield e, to_val


def threshold_pairing_errors(config: dict) -> list[str]:
    """HARD: every `<cat>_bad` trigger id has a `<cat>_ok`; every declared threshold sensor is
    wired into the automation in BOTH directions (on via a _bad list, off via a _ok list); and no
    triggered threshold-looking sensor is undeclared. Catches a half-added metric (declared but
    not wired, or wired in only one direction) and a half-added category (a _bad with no _ok)."""
    auto = _threshold_automation(config)
    if not auto:
        return []
    errs = []
    cats = defaultdict(set)
    trig_entities: set[str] = set()
    entity_directions: dict[str, set] = defaultdict(set)
    for trig in auto.get("trigger", []) or []:
        tid = trig.get("id", "")
        if tid.endswith("_bad"):
            cats[tid[:-4]].add("bad")
        elif tid.endswith("_ok"):
            cats[tid[:-3]].add("ok")
        for ent, to_val in _trigger_entity_directions(trig):
            trig_entities.add(ent)
            if to_val is not None:
                entity_directions[ent].add(to_val)
    for cat, sides in sorted(cats.items()):
        if sides != {"bad", "ok"}:
            missing = ({"bad", "ok"} - sides).pop()
            errs.append(f"threshold category '{cat}' is missing its _{missing} trigger")
    declared = {t["entity"] for t in extract_thresholds(config)}
    for ent in sorted(declared - trig_entities):
        errs.append(f"declared threshold {ent} is not wired into bedroom_threshold_alert triggers")
    for ent in sorted(declared & trig_entities):
        missing = {"on", "off"} - entity_directions.get(ent, set())
        if missing:
            errs.append(f"declared threshold {ent} is wired but missing the "
                        f"{'/'.join(sorted(missing))} trigger direction")
    for ent in sorted(trig_entities - declared):
        if ent.startswith("binary_sensor."):
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


def cmd_generate(role_dir: Path = ROLE_DIR) -> int:
    model = build_model(load_role(role_dir))
    (role_dir / "state").mkdir(exist_ok=True)
    DERIVED_YAML.write_text(render_derived_yaml(model))
    STATE_MD.write_text(render_state_md(model))
    print(f"generated {DERIVED_YAML.name} + {STATE_MD.name}")
    return 0


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
