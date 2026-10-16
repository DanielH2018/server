"""`runbook_gates.py`, the one entry point over the six runbooks' gate modules (#4344).

Run: uv run pytest scripts/deploy_tools/tests/test_runbook_gates_dispatch.py
"""

import importlib

import pytest
from deploy_tools import runbook_gates
from deploy_tools.runbook_gates_lib import gate_runner

from lib.cli_registry import package_entry_points
from lib.proc_testing import run
from lib.repo_paths import REPO

RUNBOOKS = frozenset(
    {
        "etcd-restore",
        "k3s-upgrade",
        "longhorn-dr",
        "longhorn-upgrade",
        "metallb-upgrade",
        "pinned-rotation",
    }
)


def test_list_names_every_runbook(capsys):
    assert runbook_gates.main(["--list"]) == 0
    listed = {line.split()[0] for line in capsys.readouterr().out.splitlines()}
    assert listed == RUNBOOKS


def test_every_gate_module_in_the_package_is_registered():
    # A sixth runbook module that defines `main` but was never added to the registry fails here.
    package = importlib.import_module("deploy_tools.runbook_gates_lib")
    runbook_gates.REGISTRY.assert_complete(
        package_entry_points(package, prefixes=("main",))
    )


@pytest.mark.parametrize("argv", [[], ["no-such-runbook"], ["--gate", "3"]])
def test_a_missing_or_unknown_runbook_is_usage(argv, capsys):
    assert runbook_gates.main(argv) == gate_runner.EX_USAGE
    assert "etcd-restore" in capsys.readouterr().err


def test_the_rest_of_argv_reaches_the_runbook(monkeypatch, tmp_path):
    # `--gate 3` belongs to etcd-restore, so the dispatcher must hand it on untouched. Gate 3
    # refuses a name carrying `/` before it asks the cluster, so it exits 3. Had the flag been
    # dropped, gate 1 would run first and fail on the absent drill directory with exit 1.
    monkeypatch.setenv("ETCD_DRILL_STATE_DIR", str(tmp_path / "absent"))
    assert runbook_gates.main(["etcd-restore", "--gate", "3", "a/b.zip"]) == 3


@pytest.mark.parametrize("runbook", sorted(RUNBOOKS))
def test_each_runbook_answers_help_from_outside_the_repo(runbook, tmp_path):
    proc = run(
        [
            "uv",
            "run",
            "--project",
            str(REPO),
            "python",
            str(REPO / "scripts/deploy_tools/runbook_gates.py"),
            runbook,
            "--help",
        ],
        cwd=tmp_path,
        # A nested `uv run` resolves the dev group before the child starts, which a cold
        # cache makes minutes rather than seconds.
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Exit codes:" in proc.stdout
