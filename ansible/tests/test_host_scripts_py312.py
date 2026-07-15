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
import re
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


# A repo .py invoked by a bare host `/usr/bin/python3` (or `/usr/bin/env python3`) in a systemd unit's
# ExecStart — NOT `uv run` (which pins the 3.14 env). The interpreter isn't always first on the line
# (gitops-deploy wraps it in `flock`), so match it anywhere and capture the script path after it.
_EXECSTART_HOST_PY = re.compile(
    r"^ExecStart=.*?(?:/usr/bin/python3|/usr/bin/env\s+python3)\s+(\S+\.py)\b",
    re.MULTILINE,
)


def _host_python_scripts_in_units() -> set[str]:
    """Basenames of every repo .py a systemd unit template runs under a bare /usr/bin/python3."""
    found: set[str] = set()
    for unit in _REPO.glob("ansible/roles/**/*.service.j2"):
        found.update(
            path.rsplit("/", 1)[-1]
            for path in _EXECSTART_HOST_PY.findall(unit.read_text())
        )
    return found


def test_host_run_scripts_list_is_complete() -> None:
    # The parse-guard above only covers the scripts hand-listed in HOST_RUN_SCRIPTS. This closes the
    # drift one level up (the same lockstep pattern as test_prek_pytest_files_cover_testpaths /
    # test_renovate_managers): a future setup role adding `ExecStart=/usr/bin/python3 …/<x>.py` would
    # otherwise silently escape the 3.12 floor-check — the exact class that bricked the deployer on
    # 2026-07-15 (a 3.14-only `except A, B:` that passed ruff/CI but SyntaxErrors on the host).
    found = _host_python_scripts_in_units()
    # Sanity: a broken glob/regex finding nothing would make the coverage assert vacuously pass.
    assert {"gitops_deploy.py", "renovate_notify.py"} <= found, (
        f"expected the known host-run scripts among the unit templates; found {sorted(found)}"
    )
    covered = {Path(rel).name for rel in HOST_RUN_SCRIPTS}
    missing = found - covered
    assert not missing, (
        f"systemd unit(s) run these under a bare /usr/bin/python3, but they're absent from "
        f"HOST_RUN_SCRIPTS so the 3.12 parse-guard skips them: {sorted(missing)}. "
        f"Add each script's repo path to HOST_RUN_SCRIPTS."
    )
