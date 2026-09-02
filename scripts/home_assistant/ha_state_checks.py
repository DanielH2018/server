#!/usr/bin/env python3
"""Guardrail checks over the HA state model built by `ha_state_model.py`.

A "check" here is a pure function of an already-loaded config (plus, for a few, the derived
`writes` map or a live/committed snapshot) that returns a list of human-readable error strings —
never raises, never touches the filesystem beyond a small `state/*.yml` declaration file, and
never renders anything. Each one catches a different silent-failure mode the raw YAML would pass
through uncaught: an unresolved entity or service (a typo), a mediator called with an
out-of-vocabulary reason, an override cell or actuator with an undeclared writer, a half-wired
threshold category, a duplicate automation alias, a `system_log_event` trigger with no
`fire_event: true`, or a committed `derived_state.yml`/`STATE.md` gone stale. `check_errors`
aggregates all of them into the one list `validate_ha_config.py` fails CI on.

They sit apart from extraction (`ha_state_model.py`) because extraction answers "what does this
config do" — writers, cells, actuators — while a check answers "is that model internally
consistent, and does it match the outside world (the live entity/service snapshot, the declared
writer lists)". Extraction has one shape (config -> model) and stays correct by construction;
each check instead encodes one specific invariant someone added after it broke silently once, so
they grow independently of each other and of the model they check.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

import yaml

from ha_state_model import (
    DERIVED_YAML,
    EXTERNAL_SERVICES_YAML,
    EXTERNAL_YAML,
    ROLE_DIR,
    STATE_DIR,
    STATE_MD,
    _all_service_calls,
    _is_templated,
    automation_writer,
    build_model,
    call_service,
    config_entities,
    config_services,
    extract_thresholds,
    load_role,
    override_consistency_report,
    render_derived_yaml,
    render_state_md,
)

# Domains the resolution check is responsible for (entity references we author + control). Other
# domains (notify, persistent_notification, tts, media_player, device_tracker, weather, zone, sun,
# person, sensor) come from integrations and are only checked if present in `known`.
_MANAGED_DOMAINS = (
    "input_boolean",
    "input_number",
    "input_datetime",
    "timer",
    "switch",
    "light",
    "fan",
    "scene",
    "binary_sensor",
)


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
    """Write targets and every structural `entity_id:` field across automations and scripts.

    Covers triggers/conditions/actions. Templated values are dropped (can't be resolved
    statically). NON-GOAL (deliberate): entity ids inside `{{ }}` template bodies
    (states('sensor.x')) are NOT extracted — that is regex-fragile and would make this
    the flaky part of the gate.
    """
    refs: set[str] = set()
    for auto in config.get("automation") or []:
        refs |= set(_walk_entity_id_fields(auto))
    for body in (config.get("script") or {}).values():
        refs |= set(_walk_entity_id_fields(body))
    return {r for r in refs if not _is_templated(r)}


def resolution_errors(config: dict, known: set[str]) -> list[str]:
    """List errors for each managed-domain entity referenced but absent from `known`.

    Absence means a typo or a stale external snapshot — run `refresh`.
    """
    errs = []
    for ref in sorted(referenced_entities(config)):
        if ref.split(".")[0] in _MANAGED_DOMAINS and ref not in known:
            errs.append(
                f"unresolved entity reference: {ref} "
                f"(typo, or run `ha_state_model.py refresh` if it is a new device)"
            )
    return errs


def referenced_services(config: dict) -> set[str]:
    """Every literal service id called in automations + scripts.

    Skips templated names (no dot -> call_service already returns None; a `domain.{{ }}` form is
    caught by the _is_templated guard).
    """
    return {
        svc
        for call in _all_service_calls(config)
        if (svc := call_service(call)) and not _is_templated(svc)
    }


def service_resolution_errors(config: dict, known_services: set[str]) -> list[str]:
    """A called service absent from `known_services` (= a typo or a stale snapshot — run `refresh`).

    Unlike resolution_errors (entities), this checks EVERY domain unconditionally — there is no
    _MANAGED_DOMAINS gate. The live /api/services snapshot is complete, so an un-enumerable domain
    like `notify` no longer has to be exempted; that is exactly how `notify.<typo>` is caught.
    """
    errs = []
    for svc in sorted(referenced_services(config)):
        if svc not in known_services:
            errs.append(
                f"unresolved service call: {svc} "
                f"(typo, or run `ha_state_model.py refresh` if it is a new integration)"
            )
    return errs


# Valid `reason` vocabulary per actuator mediator. Declared here (NOT regex-derived from
# light_decision's Jinja / bedroom_fan_set's choose:) — a drifted constant fails SAFE (a newly
# added valid reason false-fails loudly until added here), never silently passes. Mirror any
# change to lighting.jinja's light_decision / files/scripts/fan.yaml's bedroom_fan_set.
MEDIATOR_REASONS = {
    "script.bedroom_lights_set": {
        "presence",
        "natural",
        "wake",
        "wake_fallback",
        "off",
    },
    "script.bedroom_fan_set": {"auto", "boost", "off"},
}


def mediator_reason_errors(config: dict) -> list[str]:
    """HARD: every actuator-mediator call passes a `reason` in that mediator's vocabulary.

    The `reason` must be a STRING. Catches a missing data:/reason, a typo, and the unquoted
    `reason: off` -> YAML `false` -> silent no-op trap (the config is loaded YAML-1.1, so `off`
    is already a bool here).
    """
    errs = []
    for call in _all_service_calls(config):
        svc = call_service(call)
        if svc not in MEDIATOR_REASONS:
            continue
        reason = (call.get("data") or {}).get("reason")
        if not isinstance(reason, str) or reason not in MEDIATOR_REASONS[svc]:
            errs.append(
                f"{svc}: invalid reason {reason!r} — must be a quoted string in "
                f"{sorted(MEDIATOR_REASONS[svc])} (unquoted off/on becomes a YAML bool)"
            )
    return errs


def load_external_entities() -> set[str]:
    if not EXTERNAL_YAML.is_file():
        return set()
    return set(yaml.safe_load(EXTERNAL_YAML.read_text()).get("entities", []))


def load_external_services() -> set[str]:
    if not EXTERNAL_SERVICES_YAML.is_file():
        return set()
    return set(yaml.safe_load(EXTERNAL_SERVICES_YAML.read_text()).get("services", []))


EXPECTED_OVERRIDE_WRITERS = STATE_DIR / "expected_override_writers.yml"
OVERRIDE_CELLS = (
    "input_boolean.bedroom_manual_off",
    "input_boolean.bedroom_fan_manual",
    "input_boolean.bedroom_sleep_mode",
)
SANCTIONED_YAML = STATE_DIR / "sanctioned_writers.yml"


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
            errs.append(
                f"{cell}: undeclared writer {extra} — add it to "
                f"state/expected_override_writers.yml (shared coordination state)"
            )
        for missing in sorted(want - got):
            errs.append(
                f"{cell}: declared writer {missing} no longer writes it — remove it"
            )
    return errs


def _threshold_automation(config: dict) -> dict | None:
    for auto in config.get("automation") or []:
        if auto.get("id") == "bedroom_threshold_alert":
            return auto
    return None


def _trigger_entity_directions(trig: dict):
    """Yield (entity_id, to_value) for a state trigger.

    The real bedroom_threshold_alert groups each category's sensors into ONE bad + ONE ok trigger
    with a LIST entity_id, so this must handle both list and scalar forms (a scalar-only collector
    leaves trig_entities empty and false-flags every declared threshold as unwired).
    """
    ent = trig.get("entity_id")
    to_val = trig.get("to")
    ids = [ent] if isinstance(ent, str) else (ent if isinstance(ent, list) else [])
    for e in ids:
        if isinstance(e, str):
            yield e, to_val


def threshold_pairing_errors(config: dict) -> list[str]:
    """HARD: every threshold sensor is declared and wired in both directions.

    Three parts: every `<cat>_bad` trigger id has a `<cat>_ok`; every declared threshold sensor
    is wired into the automation in BOTH directions (on via a _bad list, off via a _ok list);
    and no triggered threshold-looking sensor is undeclared. Catches a half-added metric
    (declared but not wired, or wired in only one direction) and a half-added category (a _bad
    with no _ok).
    """
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
        errs.append(
            f"declared threshold {ent} is not wired into bedroom_threshold_alert triggers"
        )
    for ent in sorted(declared & trig_entities):
        missing = {"on", "off"} - entity_directions.get(ent, set())
        if missing:
            errs.append(
                f"declared threshold {ent} is wired but missing the "
                f"{'/'.join(sorted(missing))} trigger direction"
            )
    for ent in sorted(trig_entities - declared):
        if ent.startswith("binary_sensor."):
            errs.append(
                f"threshold trigger {ent} has no matching declared threshold sensor"
            )
    return errs


def _threshold_cfg_text(auto: dict) -> str:
    """Raw text of bedroom_threshold_alert's inline Jinja `cfg` routing map, or '' if absent.

    Used only for a literal key-presence check — never parsed as a dict.
    """
    for step in auto.get("action", []) or []:
        if isinstance(step, dict):
            cfg = (step.get("variables") or {}).get("cfg")
            if isinstance(cfg, str):
                return cfg
    return ""


def threshold_cfg_coverage_errors(config: dict) -> list[str]:
    """HARD: every category with _bad/_ok triggers has a key in the inline `cfg` routing map.

    A category wired into triggers but missing from cfg KeyErrors at runtime on its first crossing
    (the pairing check can't see it — it only checks trigger pairing, not the cfg map). Non-brittle:
    checks the category name appears as a quoted key literal ('cat' or "cat") — no dict parse, and a
    prefix like 'airquality' can't satisfy 'airqualitysevere' because the closing quote anchors it.
    Skipped when there's no cfg block (a structurally different automation).
    """
    auto = _threshold_automation(config)
    if not auto:
        return []
    cfg_text = _threshold_cfg_text(auto)
    if not cfg_text:
        return []
    cats = set()
    for trig in auto.get("trigger", []) or []:
        tid = trig.get("id", "")
        if tid.endswith("_bad"):
            cats.add(tid[:-4])
        elif tid.endswith("_ok"):
            cats.add(tid[:-3])
    errs = []
    for cat in sorted(cats):
        if f"'{cat}'" not in cfg_text and f'"{cat}"' not in cfg_text:
            errs.append(
                f"threshold category '{cat}' has triggers but no key in the inline cfg map "
                f"of bedroom_threshold_alert — it would KeyError at runtime on the first "
                f"crossing; add a '{cat}' entry to the cfg dict"
            )
    return errs


def alias_collision_errors(config: dict) -> list[str]:
    """List errors for automations whose alias-derived entity_id slug collides.

    Two automations with different `id`s can still generate the same `automation.*`
    entity_id when their aliases slug to the same value, silently shadowing one of them.
    """
    seen: dict[str, str] = {}
    errs = []
    for auto in config.get("automation") or []:
        name = automation_writer(auto)
        alias = auto.get("alias") or auto.get("id")
        if name in seen:
            errs.append(
                f"alias-slug collision: {name!r} from {seen[name]!r} and {alias!r}"
            )
        seen[name] = alias
    return errs


def load_sanctioned_writers() -> dict:
    if not SANCTIONED_YAML.is_file():
        return {}
    return yaml.safe_load(SANCTIONED_YAML.read_text()) or {}


def single_writer_errors(writes: dict, sanctioned: dict) -> list[str]:
    """HARD and symmetric: each sanctioned actuator's writer set equals module ∪ exemptions.

    Derived from the config, and checked both ways. An
    unsanctioned writer fails; a sanctioned entry that no longer writes the actuator fails too (a
    stale entry silently widens the allowed set — remove it). Mirrors override_writer_errors.
    """
    errs = []
    for actuator, spec in sorted(sanctioned.items()):
        allowed = set(spec.get("module", [])) | set(spec.get("exemptions", []))
        got = set(writes.get(actuator, []))
        for writer in sorted(got - allowed):
            errs.append(
                f"{actuator}: unsanctioned writer {writer} — route it through the mediator "
                f"(script.bedroom_lights_set / bedroom_fan_set) or declare it in "
                f"state/sanctioned_writers.yml"
            )
        for stale in sorted(allowed - got):
            errs.append(
                f"{actuator}: sanctioned writer {stale} no longer writes it — "
                f"remove it from state/sanctioned_writers.yml"
            )
    return errs


def system_log_fire_event_errors(config: dict) -> list[str]:
    """HARD: an automation triggering on `system_log_event` needs `system_log: fire_event: true`.

    That setting goes in configuration.yaml. default_config enables system_log WITHOUT it, so
    the event never fires by default and the trigger never matches — the automation is silently
    dead. A structured-data check, no Jinja or string parsing. (Found the hard way via the
    ha_runtime_error_alert live-fire; this turns it into a pre-deploy gate.)
    """
    offenders = []
    for auto in config.get("automation") or []:
        trig = auto.get("trigger") or auto.get("triggers") or []
        if isinstance(trig, dict):
            trig = [trig]
        for t in trig:
            if not isinstance(t, dict):
                continue
            et = t.get("event_type")
            ets = [et] if isinstance(et, str) else (et if isinstance(et, list) else [])
            if "system_log_event" in ets:
                offenders.append(auto.get("id") or auto.get("alias") or "<unknown>")
                break
    if not offenders:
        return []
    if ((config.get("system_log") or {}).get("fire_event")) is True:
        return []
    return [
        f"automation(s) {sorted(set(offenders))} trigger on system_log_event but "
        f"configuration.yaml does not set `system_log: fire_event: true` — system_log does not "
        f"fire that event by default, so the trigger never matches (silently dead). Add a "
        f"top-level `system_log:` block with `fire_event: true`."
    ]


def freshness_errors(role_dir: Path = ROLE_DIR) -> list[str]:
    """List errors for any generated file that no longer matches the derived model.

    Rebuilds the model from `role_dir` and compares it against the committed
    `DERIVED_YAML` and `STATE_MD`, both of which only `ha_state_model.py generate`
    should update.
    """
    model = build_model(load_role(role_dir))
    errs = []
    for path, want in (
        (DERIVED_YAML, render_derived_yaml(model)),
        (STATE_MD, render_state_md(model)),
    ):
        have = path.read_text() if path.is_file() else ""
        if have != want:
            errs.append(
                f"{path.name} is stale — run `ha_state_model.py generate` and commit"
            )
    return errs


def check_errors(role_dir: Path = ROLE_DIR) -> list[str]:
    """All HARD checks, aggregated. Report-mode invariants are printed (stderr), not failed on."""
    config = load_role(role_dir)
    model = build_model(config)
    known = (
        config_entities(config, config.get("scene") or []) | load_external_entities()
    )
    known_services = config_services(config) | load_external_services()
    errs: list[str] = []
    errs += freshness_errors(role_dir)
    errs += resolution_errors(config, known)
    errs += service_resolution_errors(config, known_services)
    errs += mediator_reason_errors(config)
    errs += override_writer_errors(model["writes"], load_expected_override_writers())
    errs += threshold_pairing_errors(config)
    errs += threshold_cfg_coverage_errors(config)
    errs += alias_collision_errors(config)
    errs += single_writer_errors(model["writes"], load_sanctioned_writers())
    errs += system_log_fire_event_errors(config)
    for line in override_consistency_report(model["writes"]):
        print(f"[state-model report] {line}", file=sys.stderr)
    return errs
