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
