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
    # gitops_deploy.py imports this at runtime under the SAME /usr/bin/python3 (sys.path.insert of its
    # own dir), so it needs the 3.12 floor-check too — but it never appears on an ExecStart line, so
    # the unit scan can't find it. The import-resolution check below re-derives this requirement.
    "ansible/roles/setup/gitops_deploy/files/deploy_logic.py",
    "ansible/roles/setup/renovate_notify/files/renovate_notify.py",
    "ansible/roles/setup/renovate_notify/files/notify_logic.py",
    # Cross-role shared lib: deployed INTO each script's /opt dir (a runtime sibling both `from
    # host_lib import`), so it loads under the same host /usr/bin/python3 — but its source lives in a
    # different role's files/, so the _first_party_imports sibling scan below can't derive it. Listed
    # by hand for the 3.12 parse-check; keep it 3.12-clean.
    "ansible/roles/setup/common/files/host_lib.py",
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


def _first_party_imports(script: Path) -> set[str]:
    """Basenames of sibling modules `script` imports from its own dir. Each host-run script does a
    `sys.path.insert(0, <own dir>)` and imports its logic module, which then loads under the SAME
    host /usr/bin/python3 — so a 3.14-only construct there SyntaxErrors at import time just as it
    would in the entry script, yet the module never appears on an ExecStart line for the unit scan
    to catch. Only first-party siblings (a .py of that name exists alongside the script) are returned;
    stdlib/third-party imports have no matching sibling and are ignored."""
    tree = ast.parse(script.read_text())
    siblings = {p.stem for p in script.parent.glob("*.py")}
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module in siblings
        ):
            found.add(f"{node.module}.py")
        elif isinstance(node, ast.Import):
            found.update(f"{a.name}.py" for a in node.names if a.name in siblings)
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

    # The ExecStart scan can't see a module that's IMPORTED rather than exec'd — but a first-party
    # sibling loads under the same host /usr/bin/python3 and would SyntaxError at import time on a
    # 3.14-only construct just the same. Re-derive that requirement from each listed script's imports
    # so a future sibling (or a new import in deploy_logic.py / notify_logic.py) can't silently escape.
    imported: set[str] = set()
    for rel in HOST_RUN_SCRIPTS:
        imported |= _first_party_imports(_REPO / rel)
    missing_imports = imported - covered
    assert not missing_imports, (
        f"a host-run /usr/bin/python3 script imports these first-party modules (same interpreter), "
        f"but they're absent from HOST_RUN_SCRIPTS so the 3.12 parse-guard skips them: "
        f"{sorted(missing_imports)}. Add each module's repo path to HOST_RUN_SCRIPTS."
    )
