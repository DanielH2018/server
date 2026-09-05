#!/usr/bin/env python3
"""Run every registered template/manifest validator in one process.

The prek hooks (``validate-compose-templates``, ``validate-config-templates``,
``validate-grafana-dashboards``, ``validate-k8s-manifests``, ``validate-shell-templates``)
call each ``scripts/validate/*.py`` module directly and stay that way — prek scopes each
hook to its own ``files`` glob, so a change to one template only pays for that one
validator. This script is for running the same five together outside of prek, e.g. before
a broad change: ``uv run python scripts/validate/run_all.py`` (``--only``/``--skip`` take a
comma-separated list of the names ``--list`` prints).

Each validator's ``main() -> int`` already prints its own report and returns an exit code;
this script just calls them in sequence and reports which ones failed.
"""

import argparse
import sys
from pathlib import Path as _Path

# `scripts/` on sys.path for the `validate.<module>` and `lib.cli_registry` imports below — a
# directly-invoked script gets only its own directory, and pyproject's `pythonpath` is a
# pytest-only setting (see CLAUDE.md's "Python & Tests").
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.cli_registry import Registry
from validate import (
    compose_templates,
    config_templates,
    grafana_dashboards,
    k8s_manifests,
    shell_templates,
)

REGISTRY = Registry("validate")
REGISTRY.add(
    "compose",
    compose_templates.main,
    "docker-compose.yml.j2 renders to valid YAML",
    module="compose_templates",
)
REGISTRY.add(
    "config",
    config_templates.main,
    "config templates (non-manifest, e.g. local.ini) render to valid YAML",
    module="config_templates",
)
REGISTRY.add(
    "grafana",
    grafana_dashboards.main,
    "Grafana dashboard datasource references resolve",
    module="grafana_dashboards",
)
REGISTRY.add(
    "k8s",
    k8s_manifests.main,
    "k8s manifests render, parse and schema-check clean",
    module="k8s_manifests",
)
REGISTRY.add(
    "shell",
    shell_templates.main,
    "shell templates render clean and pass bash -n + shellcheck",
    module="shell_templates",
)

# `refresh_crd_schemas.py` also defines a `main()`, so a blind `package_entry_points(validate)`
# census finds SIX modules — it is not a validator: it refreshes the vendored CRD schemas
# k8s_manifests.py checks against, and has no prek hook of its own. Named explicitly here
# rather than derived from the census, so a real sixth validator can't ship unregistered
# behind this exclusion (the completeness test asserts this set, not the raw census).
EXPECTED_MODULES = frozenset(
    {
        "compose_templates",
        "config_templates",
        "grafana_dashboards",
        "k8s_manifests",
        "shell_templates",
    }
)


def _name_set(value):
    return frozenset(n for n in (value or "").replace(" ", "").split(",") if n)


def _build_parser():
    p = argparse.ArgumentParser(
        prog="run_all.py", description="run the repo's template/manifest validators"
    )
    p.add_argument(
        "--list", action="store_true", help="print the registered validators and exit"
    )
    p.add_argument(
        "--only", help="comma-separated validator names to run (default: all)"
    )
    p.add_argument("--skip", help="comma-separated validator names to skip")
    return p


def main(argv=None):
    ns = _build_parser().parse_args(argv)
    if ns.list:
        print("\n".join(REGISTRY.render_list()))
        return 0
    only = _name_set(ns.only)
    skip = _name_set(ns.skip)
    unknown = REGISTRY.unknown(only | skip)
    if unknown:
        print(f"unknown validator name(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    selected = REGISTRY.selected(only=only, skip=skip)
    if not selected:
        print("no validators selected", file=sys.stderr)
        return 2
    failed = []
    for entry in selected:
        print(f"--- {entry.name}: {entry.description} ---")
        if entry.func():
            failed.append(entry.name)
    if failed:
        print(f"FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"all {len(selected)} validator(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
