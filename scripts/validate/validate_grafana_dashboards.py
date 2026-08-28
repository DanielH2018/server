#!/usr/bin/env python3
"""Validate that every provisioned Grafana dashboard's datasource references resolve to a
datasource declared in datasources.yml.j2.

A panel pointing at a wrong/empty datasource uid renders a silent "No data" with no error —
exactly the stale-uid class the grafana role CLAUDE.md warns about (the lingering IH0jqv6nz
uid). This guard is deterministic over all provisioned dashboards.

Run directly (`python3 scripts/validate/validate_grafana_dashboards.py`) or via the
`validate-grafana-dashboards` prek hook. Exits non-zero on any unresolved datasource uid.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import re

REPO_ROOT = Path(__file__).resolve().parents[2]
GRAFANA_ROLE = REPO_ROOT / "ansible/roles/k8s/claude-otel"
DASHBOARDS_DIR = GRAFANA_ROLE / "files/dashboards"
DATASOURCES_TEMPLATE = (
    REPO_ROOT / "ansible/roles/k8s/claude-otel/templates/grafana.yaml.j2"
)

# Grafana built-in pseudo-datasources — always valid, never provisioned.
BUILTIN_DATASOURCE_UIDS = {"-- Grafana --", "-- Mixed --", "-- Dashboard --", "grafana"}


def provisioned_datasource_ids(
    datasources_template: Path = DATASOURCES_TEMPLATE,
) -> set[str]:
    """uids AND names of every provisioned datasource. Since the Docker grafana role's
    deploy machinery retired (2026-08-14) the live declaration is the cluster grafana's
    provisioning ConfigMap (claude-otel grafana.yaml.j2). That file carries Jinja, so the
    datasource entries are extracted by line rather than yaml-parsed whole; including
    names as well as uids means a legacy name-form ref ("datasource": "Prometheus") also
    resolves — a valid Grafana reference, not a bug."""
    ids: set[str] = set()
    in_datasources = False
    for line in datasources_template.read_text().splitlines():
        stripped = line.strip()
        if stripped == "datasources:":
            in_datasources = True
            continue
        if in_datasources:
            m = re.match(r"-?\s*(uid|name):\s*(\S+)$", stripped.lstrip("- "))
            if m and "{{" not in m.group(2):
                ids.add(m.group(2).strip("\"'"))
    return ids


def _uid_from_ref(ref) -> list[str]:
    """The uid(s) a `datasource` value references: object form {"uid": "X"} or legacy bare
    string "X". null / anything else → no ref.

    A `${DS_*}` template-variable string is intentionally NOT special-cased: every board in
    this repo bakes a concrete uid into its panel refs, and the plan forbids template-var
    datasources in new boards, so such a ref would (correctly) be reported as unresolved
    rather than silently accepted."""
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


def duplicate_dashboard_uids(boards: dict[str, str]) -> list[str]:
    """Error strings for every dashboard uid claimed by more than one file.

    Grafana does NOT resolve a duplicate uid by picking one. Its file provisioner detects the
    collision and then disables database writes for the WHOLE provider:

        msg="the same UID is used more than once" uid=longhorn-storage times=2
        msg="dashboards provisioning provider has no database write permissions because of
             duplicates" provider=homelab
        msg="Not saving new dashboard due to restricted database access" ...

    So one duplicated pair silently freezes every board in the estate at whatever version was
    last written — new panels never appear, and the pod stays 1/1 Running with a green health
    probe throughout. That is exactly the failure this repo already recorded as "Grafana pod
    health cannot see dead panels", one layer further up: here nothing renders wrong, the
    updates simply never land.

    It bit on 2026-08-28. `export_grafana_dashboards.py` names files `slug(title).json`, so
    running it against boards that had hand-written filenames wrote a second copy of eight
    dashboards beside the originals rather than replacing them.
    """
    errors = []
    for uid, paths in sorted(boards.items()):
        if len(paths) > 1:
            errors.append(
                f"dashboard uid {uid!r} is claimed by {len(paths)} files ({', '.join(paths)}) "
                f"— Grafana disables provisioning writes for EVERY board when it sees a "
                f"duplicate uid, so this freezes the whole estate. Delete all but one."
            )
    return errors


def validate(
    dashboards_dir: Path = DASHBOARDS_DIR,
    datasources_template: Path = DATASOURCES_TEMPLATE,
) -> list[str]:
    """Return a list of error strings ([] = clean): every dashboard JSON whose datasource
    refs all resolve to a provisioned datasource (or a built-in), and whose uid no other
    dashboard claims, passes."""
    valid = provisioned_datasource_ids(datasources_template) | BUILTIN_DATASOURCE_UIDS
    errors: list[str] = []
    boards: dict[str, list[str]] = {}
    for path in sorted(dashboards_dir.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{_display(path)}: invalid JSON: {exc}")
            continue
        uid = data.get("uid")
        if isinstance(uid, str) and uid:
            boards.setdefault(uid, []).append(_display(path))
        seen: set[tuple[str, str | None]] = set()
        for uid_ref, title in datasource_refs_in(data):
            if uid_ref in valid or (uid_ref, title) in seen:
                continue
            seen.add((uid_ref, title))
            where = f" (panel {title!r})" if title else ""
            errors.append(
                f"{_display(path)}: datasource uid {uid_ref!r} is not provisioned{where}"
            )
    return errors + duplicate_dashboard_uids(boards)


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
