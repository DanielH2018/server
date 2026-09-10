#!/usr/bin/env python3
"""Run every registered template/manifest validator in one process.

The prek hooks (``validate-compose-templates``, ``validate-config-templates``,
``validate-grafana-dashboards``, ``validate-k8s-manifests``, ``validate-setup-templates``,
``validate-shell-templates``, ``validate-unit-templates``) call each ``scripts/validate/*.py``
module directly and stay that way — prek scopes each hook to its own ``files`` glob, so a
change to one template only pays for that one validator. This script is for running the same
seven together outside of prek, e.g. before a broad change:
``uv run python scripts/validate/run_all.py`` (``--only``/``--skip`` take a comma-separated
list of the names ``--list`` prints).

DECIDED: ``run_all`` covers the ``validate`` package; a role-scoped validator runs from its own
prek hook. ``validate-ha-config`` (``prek.toml``) is the one prek validator this script does not
run, and it stays that way. It lives in ``scripts/home_assistant/`` and checks the home-assistant
role's own config tree, where every module registered here checks a template plane a broad change
crosses. Registering it would cost the thing that makes this registry trustworthy: the
completeness guard is a single-package census (``package_entry_points(validate)``), so a
cross-package entry would have to weaken to a ``module=None`` row or widen across packages, and an
unregistered validator would stop failing the guard. Settled on GitHub issue #1578; contradict it
there rather than by adding an entry. The rule generalises — a validator scoped to one role is
run by whoever changes that role, through its own hook.

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
    setup_templates,
    shell_templates,
    unit_templates,
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
    "setup",
    setup_templates.main,
    "setup-plane templates render and every variable they read resolves",
    module="setup_templates",
)
REGISTRY.add(
    "shell",
    shell_templates.main,
    "shell templates render clean and pass bash -n + shellcheck",
    module="shell_templates",
)
REGISTRY.add(
    "unit",
    unit_templates.main,
    "rendered systemd units pass systemd-analyze verify",
    module="unit_templates",
)

# Two modules in this package define a `main()` without being validators, so a blind
# `package_entry_points(validate)` census over-counts: `refresh_vendored_schemas.py`
# re-downloads the vendored schemas (traefik CRDs + Authelia's configuration schema), and
# `run_all.py` is this dispatcher
# itself. Neither has a prek hook. This set stays a literal — deriving it at import time would
# import every module in the package on every run — and the completeness test subtracts those
# two exclusions BY NAME from the census and requires the remainder to equal it. That test is
# what makes an unregistered validator fail rather than pass silently: this set read five names
# while `unit_templates` and `setup_templates` sat unregistered (GitHub issue #1505), and the
# test it faced asserted the same five, so nothing disagreed.
EXPECTED_MODULES = frozenset(
    {
        "compose_templates",
        "config_templates",
        "grafana_dashboards",
        "k8s_manifests",
        "setup_templates",
        "shell_templates",
        "unit_templates",
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
