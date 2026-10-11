"""The install-layout test's stale-copy check can go red (#4441).

`test_the_reader_imports_from_the_installed_layout` seeds the pre-#4416 flat files, runs the
role's removal, then globs for survivors. Its seeds must be a literal list the role's tasks do
not supply: seeded from the removal task's own loop, a name dropped from that loop is neither
seeded nor removed, and the glob passes on a removal that leaves a stale copy behind.

These tests copy the role, `scripts/lib` and the host libraries into tmp_path, where
`lib.repo_paths` resolves REPO to the copy, mutate the copied removal loop, and run that test
there in a subprocess. A control run on the unmutated copy must pass first, so a red run is
the mutation's doing and not a broken copy.

Run: uv run pytest ansible/roles/k8s/configarr/tests/test_configarr_lib_install_can_fail.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from lib.repo_paths import REPO

_ROLE = Path("ansible/roles/k8s/configarr")
_INSTALL_TEST = _ROLE / "tests" / "test_configarr_lib_install.py"
_LAYOUT_TEST = f"{_INSTALL_TEST}::test_the_reader_imports_from_the_installed_layout"
_REMOVAL = "Remove the configarr health modules that moved into configarr_lib"
_COPIED = (
    _ROLE,
    Path("scripts/lib"),
    Path("ansible/filter_plugins"),
    Path("ansible/roles/setup/common/files"),
    # longhorn_groups, a filter plugin make_ansible_env loads, imports longhorn_lib from here.
    Path("ansible/roles/setup/k3s/files"),
)


def _repo_copy(root: Path, removal_loop: list[str] | None) -> Path:
    """Copy what the layout test reads under `root`, replacing the removal loop if given."""
    for rel in _COPIED:
        shutil.copytree(
            REPO / rel, root / rel, ignore=shutil.ignore_patterns("__pycache__")
        )
    if removal_loop is not None:
        tasks_file = root / _ROLE / "tasks" / "main.yml"
        tasks = yaml.safe_load(tasks_file.read_text())
        removal = next(t for t in tasks if t.get("name") == _REMOVAL)
        # Named members, so a renamed task or a reshaped loop fails here, not as a vacuous
        # mutation.
        assert sorted(removal["loop"]) == [
            "configarr_health_logic.py",
            "configarr_status.py",
        ]
        removal["loop"] = removal_loop
        tasks_file.write_text(yaml.safe_dump(tasks, sort_keys=False))
    (root / "pytest.ini").write_text("[pytest]\n")
    return root


def _run_layout_test(root: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(root / "scripts")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", _LAYOUT_TEST],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _assert_mutation_turns_it_red(tmp_path: Path, removal_loop: list[str]) -> None:
    control = _run_layout_test(_repo_copy(tmp_path / "control", None))
    assert control.returncode == 0, (
        f"the unmutated copy must pass first:\n{control.stdout}{control.stderr}"
    )
    mutated = _run_layout_test(_repo_copy(tmp_path / "mutated", removal_loop))
    assert mutated.returncode == 1 and "1 failed" in mutated.stdout, (
        f"removal loop {removal_loop} left the layout test green, so its stale-copy check "
        f"compares against the list it seeded from:\n{mutated.stdout}{mutated.stderr}"
    )


def test_dropping_configarr_status_from_the_removal_loop_turns_the_layout_test_red(
    tmp_path,
):
    _assert_mutation_turns_it_red(tmp_path, ["configarr_health_logic.py"])


def test_the_layout_test_seeds_stale_copies_even_when_the_removal_loop_is_empty(
    tmp_path,
):
    # A removal that removes nothing leaves every seeded flat copy, so a seed list independent
    # of the loop must surface them; one derived from the loop seeds nothing and stays green.
    _assert_mutation_turns_it_red(tmp_path, [])
