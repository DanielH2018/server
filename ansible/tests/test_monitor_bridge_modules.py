#!/usr/bin/env python3
"""Every runtime module in monitor-bridge's files/ must be in its ConfigMap ship list.

check.py was one 3.5k-line file until it was split. The split introduced a failure mode the
test suite structurally cannot see: pytest imports the modules from `files/` on disk, so it
goes green whatever the ship list says, while the pod only ever receives the files named in
`monitor_bridge_modules`. A module added to files/ and forgotten here therefore passes CI and
kills the bridge at import on its next roll:

    ModuleNotFoundError: No module named 'bridge_parsing'

That lands on the one workload that cannot page about its own failure — monitor-bridge IS the
alert pipeline, which is why the role sets `k8s_autodeploy: false`. The pod would crashloop and
the estate would go quiet rather than loud.

The list is deliberately explicit rather than a `files/*.py` glob, because files/ also holds
the pytest suite and conftest.py, which must not reach the image. This test is what keeps the
explicit list honest.

Run: uv run pytest ansible/tests/test_monitor_bridge_modules.py
"""

import ast

import yaml
from _helpers import REPO


ROLE = REPO / "ansible" / "roles" / "k8s" / "monitor-bridge"
FILES = ROLE / "files"
ENTRYPOINT = "check.py"


def _ship_list():
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    return list(defaults["monitor_bridge_modules"])


def _runtime_modules():
    """The .py files in files/ that are production code, not the test suite."""
    return sorted(
        p.name
        for p in FILES.glob("*.py")
        if not p.name.startswith("test_") and p.name != "conftest.py"
    )


def test_ship_list_matches_the_runtime_modules_on_disk():
    assert sorted(_ship_list()) == _runtime_modules()


def test_ship_list_carries_the_entrypoint():
    """The Deployment runs `python /app/check.py`; shipping the rest without it deploys nothing."""
    assert ENTRYPOINT in _ship_list()


def test_ship_list_excludes_the_test_suite():
    shipped = set(_ship_list())
    tests = {p.name for p in FILES.glob("test_*.py")} | {"conftest.py"}
    assert not (shipped & tests)


def test_every_module_check_py_imports_is_shipped():
    """The direct check: whatever check.py imports from its own directory must travel with it.

    Set equality above already implies this today. It is asserted separately because it is the
    property that actually breaks the pod, and it keeps holding if the runtime/test split above
    is ever loosened.
    """
    shipped = {m[: -len(".py")] for m in _ship_list()}
    local = {p.stem for p in FILES.glob("*.py")}
    tree = ast.parse((FILES / ENTRYPOINT).read_text())

    imported_locally = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module in local
        ):
            imported_locally.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in local:
                    imported_locally.add(alias.name)

    missing = imported_locally - shipped
    assert not missing, (
        f"check.py imports {sorted(missing)}, absent from monitor_bridge_modules"
    )
