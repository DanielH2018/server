#!/usr/bin/env python3
"""Derived state model for the Home Assistant bedroom control plane.

Reuses validate_ha_config's loader to parse the real automations/scripts/config, extracts
every write (service call -> target entity), and generates derived_state.yml + STATE.md. Also
runs the guardrail checks consumed by the validate-ha-config prek/CI hook. No live HA / Docker
for any of that — `refresh` (snapshot integration entities) is the only live path.
"""
from __future__ import annotations

import re
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


def config_entities(config: dict, scenes: list) -> set[str]:
    """Every entity id derivable from the repo config — helpers, scenes, threshold sensors,
    template sensors. The resolution check unions this with the live external-entity snapshot."""
    ents = {c["entity"] for c in extract_cells(config).values()}
    ents |= {t["entity"] for t in extract_thresholds(config)}
    ents |= set(scene_entity_map(scenes).keys())
    ents |= _template_sensor_entities(config)
    return ents
