#!/usr/bin/env python3
"""No role copies a test file to a host, which is what lets the deployer ignore them.

`deploy_logic._is_test_only_path` skips test-suite paths before every plane prefix, so a
test-only push produces an empty ChangeSet and takes gitops_deploy.py's ff-merge branch. That
is only safe while the invariant here holds: a file the deployer ignores must also be a file
no host ever receives. If a role started shipping its own test suite, the deployer would
fast-forward a change to deployed code without deploying it — silent, and green from every
repo-side check.

`test_monitor_bridge_modules.py` already pins the one role that could plausibly ship one; this
is the tree-wide version.

Run: uv run pytest ansible/tests/repo/test_no_role_ships_a_test_file.py
"""

import re

from _helpers import ANSIBLE

# `src:` and `copy:`/`template:` bodies naming a file, and the ConfigMap `lookup()` form the
# k8s roles use to inline a script. A comment mentioning a test file is not a ship.
_SHIP = re.compile(
    r"""(?:src|dest)\s*:\s*['"]?\S*?(?P<a>[\w.-]+\.py)|lookup\(\s*['"]file['"]\s*,\s*['"]?\S*?(?P<b>[\w.-]+\.py)""",
)


def _shipped_python_names() -> list[tuple[str, str]]:
    """(file, python basename) for every Python file a role's tasks or templates ship."""
    # pathlib.glob has no brace expansion, so the three directories are walked separately.
    # Writing them as one `{tasks,templates,handlers}` pattern matches nothing and silently
    # empties the corpus, which is what the guard above exists to catch.
    found = []
    candidates = [
        path
        for directory in ("tasks", "templates", "handlers")
        for path in ANSIBLE.glob(f"roles/*/*/{directory}/**/*")
    ]
    for path in sorted(candidates):
        if not path.is_file():
            continue
        for i, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            body = line.split("#", 1)[0]
            for match in _SHIP.finditer(body):
                name = match.group("a") or match.group("b")
                if name:
                    found.append((f"{path.relative_to(ANSIBLE)}:{i}", name))
    return found


def test_the_scan_finds_python_files_being_shipped() -> None:
    """Guard the guard.

    A pattern that stopped matching would pass the check below vacuously, which is the failure this
    repo has paid for twice.
    """
    shipped = _shipped_python_names()
    assert len(shipped) >= 5, (
        f"only found {len(shipped)} shipped .py files — the scan broke. Roles copy "
        "gitops_deploy.py, deploy_logic.py, host_lib.py and the bridge modules at minimum."
    )


def test_no_role_ships_a_test_file() -> None:
    """The rule `_is_test_only_path` depends on."""
    offenders = [
        f"{where} ships {name}"
        for where, name in _shipped_python_names()
        if name == "conftest.py" or name.startswith("test_")
    ]
    assert not offenders, (
        "a role ships a test file, so deploy_logic._is_test_only_path would ignore a change to "
        "deployed code — the deployer fast-forwards it and never deploys it: "
        + "; ".join(offenders)
    )
