#!/usr/bin/env python3
"""autofix-bridge's ConfigMap ship list must ship autofix.py, plus a real file for every
cross-role module it names.

autofix.py does `from bridge_common import _env, sanitize` — bridge_common.py is
monitor-bridge's file, staged into this role's ConfigMap rather than duplicated (the
host_lib.py pattern from roles/setup/common). It is not a sibling file under this role's
files/, so test_monitor_bridge_modules.py's "ship list == files on disk" check has no
analogue here: there is nothing on disk under autofix-bridge/files/ to compare against for
the cross-role entries. This test instead pins the two invariants that keep the pod
importable:

  - the ship list names the entrypoint (autofix.py) — without it the Deployment's
    `command: ["python", "/app/autofix.py"]` has nothing to run;
  - every `src` in the ship list resolves to a real file — a typo'd cross-role path renders a
    ConfigMap manifest that stages nothing for that key, and the pod dies at import with
    ModuleNotFoundError the next time it rolls, on a workload that fixes *arr issues with no
    page of its own if it silently stops.

Run: uv run pytest ansible/tests/services/test_autofix_bridge_modules.py
"""

import ast

import yaml
from _helpers import REPO

ROLE = REPO / "ansible" / "roles" / "k8s" / "autofix-bridge"
FILES = ROLE / "files"
ENTRYPOINT = "autofix.py"


def _ship_list():
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    return list(defaults["autofix_bridge_modules"])


def _resolve_src(src):
    """`{{ playbook_dir }}/...` -> the real file it renders to. playbook_dir is the ansible/
    directory deploy.yml runs from."""
    rel = src.replace("{{ playbook_dir }}/", "")
    return REPO / "ansible" / rel


def test_ship_list_carries_the_entrypoint():
    """The Deployment runs `python /app/autofix.py`; a ship list missing it deploys nothing."""
    names = {item["name"] for item in _ship_list()}
    assert ENTRYPOINT in names


def test_every_shipped_module_resolves_to_a_real_file():
    for item in _ship_list():
        path = _resolve_src(item["src"])
        assert path.is_file(), f"{item['name']}: {path} does not exist"


def test_ship_list_excludes_the_test_suite():
    names = {item["name"] for item in _ship_list()}
    assert not (names & {"test_autofix.py", "conftest.py"})


def test_autofix_py_cross_role_imports_are_shipped():
    """The direct check: a cross-role module autofix.py imports must travel with it. Scoped to
    names known to be cross-role (bridge_common) rather than every ImportFrom target, since a
    stdlib import (json, sys, ...) has no place in this ship list."""
    shipped = {
        item["name"][:-3] for item in _ship_list() if item["name"].endswith(".py")
    }
    tree = ast.parse((FILES / ENTRYPOINT).read_text())
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    }
    known_cross_role = {"bridge_common"}
    missing = (imported & known_cross_role) - shipped
    assert not missing, (
        f"autofix.py imports {sorted(missing)}, absent from autofix_bridge_modules"
    )
