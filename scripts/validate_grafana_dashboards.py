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
