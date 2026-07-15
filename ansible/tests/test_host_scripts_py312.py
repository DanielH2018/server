"""Guard: host-run production scripts must parse under the deploy host's Python floor.

`gitops_deploy.py` and `renovate_notify.py` are executed by the host's `/usr/bin/python3`
(systemd `ExecStart=/usr/bin/python3 /opt/.../<script>.py`), which on the Ubuntu 24.04 hosts is
3.12 — NOT the 3.14 that CI/uv pins (`pyproject` `requires-python = ">=3.14"`, which also sets
ruff's inferred target) nor the `python:3.14-alpine` containers the monitor-bridge check.py scripts
run in. So 3.13/3.14-only syntax (e.g. PEP 758 unparenthesized `except A, B:`) sails past ruff and
CI's own `ast.parse` yet is a hard `SyntaxError` on the host — silently bricking the deployer on its
next `initial_setup.yml --tags gitops_deploy` (a re-run then goes green while every tick crashes
before main()). `ast.parse(..., feature_version=(3, 12))` restricts the grammar to the host floor,
so the drift fails here at commit time instead. Add any new /usr/bin/python3-run script below.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# The Ubuntu 24.04 deploy hosts run these under /usr/bin/python3 (systemd ExecStart), currently 3.12.
HOST_PY_FLOOR = (3, 12)
_REPO = Path(__file__).resolve().parents[2]
HOST_RUN_SCRIPTS = [
    "ansible/roles/setup/gitops_deploy/files/gitops_deploy.py",
    "ansible/roles/setup/renovate_notify/files/renovate_notify.py",
]


@pytest.mark.parametrize("rel", HOST_RUN_SCRIPTS)
def test_host_script_parses_under_host_python_floor(rel: str) -> None:
    src = (_REPO / rel).read_text()
    try:
        ast.parse(src, feature_version=HOST_PY_FLOOR)
    except SyntaxError as e:
        floor = f"{HOST_PY_FLOOR[0]}.{HOST_PY_FLOOR[1]}"
        pytest.fail(
            f"{rel} uses syntax newer than the deploy host's Python {floor} "
            f"(it runs under /usr/bin/python3, not CI's 3.14): {e}"
        )
