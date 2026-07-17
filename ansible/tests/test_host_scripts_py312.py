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
    # autofix-bridge's fake-remux host cron + its pure core, run by a plain crontab `/usr/bin/python3`
    # (not a systemd unit, so the ExecStart scan can't derive them). fake_remux_scan.py `from host_lib
    # import`s the shared helper listed below. Keep all three 3.12-clean.
    "ansible/roles/containers/autofix-bridge/files/fake_remux_scan.py",
    "ansible/roles/containers/autofix-bridge/files/fake_remux_logic.py",
    "ansible/roles/containers/autofix-bridge/files/fake_remux_replace.py",
    "ansible/roles/containers/autofix-bridge/files/fake_remux_replace_logic.py",
    # configarr sync host cron + its pure core (crontab /usr/bin/python3, not a systemd unit).
    # configarr_sync.py `from host_lib import`s the shared helper listed below. Keep all 3.12-clean.
    "ansible/roles/containers/configarr/files/configarr_sync.py",
    "ansible/roles/containers/configarr/files/configarr_status.py",
    # Cross-role shared lib: deployed INTO each script's /opt dir (a runtime sibling both `from
    # host_lib import`), so it loads under the same host /usr/bin/python3 — but its source lives in a
    # different role's files/, so the _first_party_imports sibling scan below can't derive it. Listed
    # by hand for the 3.12 parse-check; keep it 3.12-clean.
    "ansible/roles/setup/common/files/host_lib.py",
    # .claude/hooks/*.sh wrappers run these under a BARE python3 (for hook latency — not `uv run`), so
    # they face the same host 3.12 floor. The ExecStart scan can't see a .sh-wrapped invocation;
    # _host_python_scripts_in_hooks() below re-derives the requirement. session-health.py had a live
    # 3.14-only `except A, B, C:` that SyntaxErrored on the host and failed silently (stderr→/dev/null).
    ".claude/hooks/session-health.py",
    ".claude/hooks/log-instructions.py",
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


def _host_python_scripts_in_hooks() -> set[str]:
    """Basenames of every repo .py a .claude/hooks/*.sh wrapper runs under a BARE python3 (the host
    interpreter, chosen for hook latency — NOT `uv run`, which would pin CI's 3.14). session-health.sh
    is the live case: its wrapped session-health.py had a 3.14-only `except A, B, C:` that SyntaxErrored
    on the host's 3.12, and the wrapper sends stderr to /dev/null + exits 0, so the health banner failed
    SILENTLY. The _EXECSTART scan only reads systemd units, so a .sh-wrapped hook escapes it — this
    closes that gap the same way. Skips comment lines and `uv run` (3.14-pinned) invocations."""
    found: set[str] = set()
    for wrapper in _REPO.glob(".claude/hooks/*.sh"):
        for line in wrapper.read_text().splitlines():
            stripped = line.strip()
            if (
                stripped.startswith("#")
                or "uv run" in stripped
                or "python3" not in stripped
            ):
                continue
            found.update(re.findall(r"([\w.-]+\.py)\b", stripped))
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


# The cross-role shared lib dir: host_lib.py lives here and is copied INTO each host-run script's
# /opt dir, so it loads under the same host /usr/bin/python3 but is NOT a sibling of any script —
# _first_party_imports can't derive it. Resolving a listed script's imports against this dir makes
# its coverage self-enforcing instead of relying on the hand-listed HOST_RUN_SCRIPTS entry.
_SHARED_LIB_DIR = _REPO / "ansible/roles/setup/common/files"


def _cross_role_shared_imports(script: Path) -> set[str]:
    """Basenames of modules `script` imports that live in the cross-role shared lib dir. Unlike a
    first-party sibling (same dir as the script) these can't be derived from the script's own
    directory, so without this a host-run script importing host_lib.py would rely solely on its
    hand-listed HOST_RUN_SCRIPTS entry — drop that line and the 3.12 floor-check silently stops
    covering it, re-opening the exact 3.14-syntax brick this file guards against."""
    if not _SHARED_LIB_DIR.is_dir():
        return set()
    shared = {p.stem for p in _SHARED_LIB_DIR.glob("*.py")}
    tree = ast.parse(script.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module in shared
        ):
            found.add(f"{node.module}.py")
        elif isinstance(node, ast.Import):
            found.update(f"{a.name}.py" for a in node.names if a.name in shared)
    return found


def test_host_run_scripts_list_is_complete() -> None:
    # The parse-guard above only covers the scripts hand-listed in HOST_RUN_SCRIPTS. This closes the
    # drift one level up (the same lockstep pattern as test_prek_pytest_files_cover_testpaths /
    # test_renovate_managers): a future setup role adding `ExecStart=/usr/bin/python3 …/<x>.py` would
    # otherwise silently escape the 3.12 floor-check — the exact class that bricked the deployer on
    # 2026-07-15 (a 3.14-only `except A, B:` that passed ruff/CI but SyntaxErrors on the host).
    found = _host_python_scripts_in_units() | _host_python_scripts_in_hooks()
    # Sanity: a broken glob/regex finding nothing would make the coverage assert vacuously pass.
    assert {"gitops_deploy.py", "renovate_notify.py", "session-health.py"} <= found, (
        f"expected the known host-run scripts among the unit/hook templates; found {sorted(found)}"
    )
    covered = {Path(rel).name for rel in HOST_RUN_SCRIPTS}
    missing = found - covered
    assert not missing, (
        f"a systemd unit or .claude/hooks/*.sh wrapper runs these under a bare host python3, but "
        f"they're absent from HOST_RUN_SCRIPTS so the 3.12 parse-guard skips them: {sorted(missing)}. "
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

    # A cross-role shared lib (host_lib.py) is copied into a script's /opt dir and imported there, so
    # it loads under the same host python3 but isn't a sibling the scan above can derive — sanity
    # that the resolver actually sees it (a broken glob would make the assert vacuously pass), then
    # require every such import be covered, so its HOST_RUN_SCRIPTS entry can't be silently dropped.
    shared_imports: set[str] = set()
    for rel in HOST_RUN_SCRIPTS:
        shared_imports |= _cross_role_shared_imports(_REPO / rel)
    assert "host_lib.py" in shared_imports, (
        f"expected a host-run script to import the cross-role host_lib; found {sorted(shared_imports)}"
    )
    missing_shared = shared_imports - covered
    assert not missing_shared, (
        f"a host-run /usr/bin/python3 script imports these cross-role shared modules (copied into "
        f"its /opt dir, same interpreter), but they're absent from HOST_RUN_SCRIPTS so the 3.12 "
        f"parse-guard skips them: {sorted(missing_shared)}. Add each module's repo path to "
        f"HOST_RUN_SCRIPTS."
    )
