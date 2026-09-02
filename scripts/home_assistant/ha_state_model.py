#!/usr/bin/env python3
"""Derived state model for the Home Assistant bedroom control plane.

Reuses validate_ha_config's loader to parse the real automations/scripts/config, extracts
every write (service call -> target entity), and generates derived_state.yml + STATE.md. The
guardrail checks consumed by the validate-ha-config prek/CI hook live in the sibling
`ha_state_checks.py`, built on top of the model this module extracts. No live HA / Docker for
any of that — `refresh` (snapshot integration entities) is the only live path.
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

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
# `scripts/` alone is not enough for `from diagnostics import probe_ha`. Every module in
# scripts/diagnostics/ imports its siblings BARE (`import probe_core`), which resolves only
# when that directory is itself on sys.path — true when probe.py is invoked directly, false
# when we import it as a namespace-package member. Without this, `refresh` dies with
# `ModuleNotFoundError: No module named 'probe_core'`. See cmd_refresh's docstring: this is
# the THIRD time that one live path broke on an import, and the first two fixes both moved
# the failing name rather than the sys.path that resolves it.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "diagnostics"))

_TEMPLATE_MARKERS = ("{{", "{%")


def slugify(name: str) -> str:
    """HA-style slug: lowercase, non-alphanumeric runs -> single underscore, trimmed."""
    s = re.sub(r"[^a-z0-9]+", "_", str(name).lower())
    return s.strip("_")


def call_service(call: dict) -> str | None:
    """The service id of a service-call step.

    Handles both the `service:` (this repo) and the newer `action:` spelling; returns None for
    non-call dicts.
    """
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
    """Yield every service-call dict anywhere under `node`.

    Recurses universally, so all of choose/if/then/else/repeat/parallel/sequence are covered without
    special-casing — a step is a 'call' iff it has a `service`/`action` key whose value is a
    `domain.service` string (a block-style `action:` is a list, so it is not mistaken for a call).
    """
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
    """The state-machine name of an automation:

    `automation.<slug(alias)>` (HA derives the entity_id from the alias, not the id; fall back to
    the id when alias is absent).
    """
    return "automation." + slugify(auto.get("alias") or auto.get("id") or "unknown")


def extract_writes(automations, scripts, scene_map):
    """Return (writes, dynamic_writes).

    writes[entity] = sorted writer names; dynamic_writes [writer] = sorted templated target strings
    that couldn't be resolved to an entity.
    """
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

    return (
        {k: sorted(v) for k, v in writes.items()},
        {k: sorted(v) for k, v in dynamic.items()},
    )


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
    """Each threshold binary_sensor -> {entity, name, bound, source}.

    The derived entity id is binary_sensor.<slug(name)> (how HA names a platform sensor from its
    `name`).
    """
    out = []
    for s in _threshold_sensors(config):
        name = s.get("name", "")
        bound = "upper" if "upper" in s else "lower"
        out.append(
            {
                "entity": f"binary_sensor.{slugify(name)}",
                "name": name,
                "bound": bound,
                "source": s.get("entity_id"),
            }
        )
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
    """`scene.<scene_id>` for every `scene.create` call — transient scenes built at runtime (e.g.

    bedroom_pre_alert from script.bedroom_alert_pulse) that are legitimately referenced by a later
    `scene.turn_on` but exist in no scenes.yaml entry and no live snapshot.
    """
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

_GENERATED_BANNER = "# GENERATED by scripts/home_assistant/ha_state_model.py — DO NOT EDIT. Run `generate`.\n"


class _IndentDumper(yaml.SafeDumper):
    """SafeDumper that indents sequence items under their parent key, so the generated YAML
    satisfies ansible-lint/yamllint's `indent-sequences` (these files live under ansible/)."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, indentless=False)


def _dump_yaml(data) -> str:
    """Deterministic, ansible-lint-clean YAML dump used for every generated state-model file
    (derived_state.yml, external_entities.yml, expected_override_writers seed)."""
    return yaml.dump(
        data, Dumper=_IndentDumper, sort_keys=True, default_flow_style=False
    )


def _actuator_lights(config: dict) -> set[str]:
    out: set[str] = set()
    for inst in config.get("adaptive_lighting") or []:
        for light in (inst or {}).get("lights", []) or []:
            out.add(light)
    return out


def build_model(config: dict) -> dict:
    scenes = config.get("scene") or []
    cells = extract_cells(config)
    writes, dynamic = extract_writes(
        config.get("automation"), config.get("script"), scene_entity_map(scenes)
    )
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
    lines = [
        "<!-- GENERATED by scripts/home_assistant/ha_state_model.py — DO NOT EDIT. Run `generate`. -->",
        "# Bedroom HA — Derived State Model",
        "",
        "Generated from the real automations/scripts/config. The *why* (runtime traps, "
        "feedback loops) lives in this role's `CLAUDE.md`.",
        "",
        "> Writer lists are **entity_id-static-only**: they attribute writes by the literal "
        "`entity_id` of each service call. A write that targets a cell/actuator by `device_id`, "
        "`area_id`, `label_id`, or a templated `{{ }}` entity is NOT attributed here (the real "
        "config uses none today). The override-writer tripwire's guarantee holds for "
        "entity_id-targeted writes — which is every write in this config.",
        "",
        "## Cells (coordination state)",
        "",
        "| Cell | Entity | Purpose |",
        "|---|---|---|",
    ]
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
EXTERNAL_SERVICES_YAML = STATE_DIR / "external_services.yml"


def parse_services(api_services: list) -> set[str]:
    """Flatten HA's GET /api/services (a list of {domain, services:

    {name: ...}}) into a flat {f"{domain}.{name}"} set.
    """
    out: set[str] = set()
    for block in api_services or []:
        domain = block.get("domain")
        if not domain:
            continue
        for name in block.get("services") or {}:
            out.add(f"{domain}.{name}")
    return out


def config_services(config: dict) -> set[str]:
    """Services the config itself defines:

    every script registers `script.<name>`. This is the freshness escape-hatch so a brand-new script
    (not yet in the committed snapshot) resolves.
    """
    return {f"script.{name}" for name in (config.get("script") or {})}


def load_external_services() -> set[str]:
    if not EXTERNAL_SERVICES_YAML.is_file():
        return set()
    return set(yaml.safe_load(EXTERNAL_SERVICES_YAML.read_text()).get("services", []))


def cmd_refresh(get_states=None, get_services=None) -> int:
    """Snapshot live external entity ids + the live service registry into external_entities.yml /
    external_services.yml. get_states/get_services are injected for tests; both default to live HA
    (needs the host age key + a running HA, reached through the cluster ingress VIP).

    Talks to HA the same way probe.py's own `ha` subcommands do — base URL + --resolve pin. It
    used to call probe.resolve_ip(probe.HA_CONTAINER), a Docker-era container lookup that stopped
    existing when HA moved to k3s, so `refresh` died with AttributeError from that cutover until
    2026-08-16. It broke the same way a second time when probe.py was split up: the helpers now
    live in probe_core (ha_base/ha_resolve) and probe_ha (ha_token/ha_get/ha_get_url), and a bare
    `probe.` prefix raised AttributeError again. It is the ONLY live path in this script, which is
    why a stale external_entities.yml silently outlived two removed sensors."""
    if get_states is None or get_services is None:
        import json

        from diagnostics import probe_core
        from diagnostics import probe_ha

        base = probe_core.ha_base()
        resolve = probe_core.ha_resolve()
        token = probe_ha.ha_token()
    if get_states is None:
        live = [
            s["entity_id"]
            for s in json.loads(
                probe_ha.ha_get(
                    probe_ha.ha_get_url(base, "states"), token, resolve=resolve
                )
            )
        ]
    else:
        live = list(get_states())
    if get_services is None:
        services = parse_services(
            json.loads(
                probe_ha.ha_get(
                    probe_ha.ha_get_url(base, "services"), token, resolve=resolve
                )
            )
        )
    else:
        services = set(get_services())
    config = load_role()
    derived = config_entities(config, config.get("scene") or [])
    external = sorted(e for e in live if e not in derived)
    external_services = sorted(services - config_services(config))
    STATE_DIR.mkdir(exist_ok=True)
    EXTERNAL_YAML.write_text(_GENERATED_BANNER + _dump_yaml({"entities": external}))
    EXTERNAL_SERVICES_YAML.write_text(
        _GENERATED_BANNER + _dump_yaml({"services": external_services})
    )
    print(
        f"snapshotted {len(external)} external entities + {len(external_services)} services"
    )
    return 0


def override_consistency_report(writes: dict) -> list[str]:
    """REPORT:

    surfaces actuators whose manual-detect override isn't engaged by every manual surface. Phase 1
    emits the lights<->manual_off relationship as a starting datapoint.
    """
    rep = []
    light_writers = set(writes.get("light.bedroom_lights", []))
    override_writers = set(writes.get("input_boolean.bedroom_manual_off", []))
    # automations that write the light but never the override are candidate gaps (advisory).
    gap = sorted(
        w
        for w in light_writers
        if w not in override_writers and w.startswith("automation.")
    )
    if gap:
        rep.append(
            "lights written without touching manual_off (review for Phase 2): "
            + ", ".join(gap)
        )
    return rep


def cmd_generate(role_dir: Path = ROLE_DIR) -> int:
    model = build_model(load_role(role_dir))
    (role_dir / "state").mkdir(exist_ok=True)
    DERIVED_YAML.write_text(render_derived_yaml(model))
    STATE_MD.write_text(render_state_md(model))
    print(f"generated {DERIVED_YAML.name} + {STATE_MD.name}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ha_state_model.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate", help="regenerate derived_state.yml + STATE.md")
    sub.add_parser(
        "refresh", help="snapshot live external entities (needs HA + SOPS key)"
    )
    sub.add_parser("check", help="run the guardrail checks (exit 1 on hard error)")
    ns = p.parse_args(argv)
    if ns.cmd == "generate":
        return cmd_generate()
    if ns.cmd == "refresh":
        return cmd_refresh()
    from ha_state_checks import check_errors

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
