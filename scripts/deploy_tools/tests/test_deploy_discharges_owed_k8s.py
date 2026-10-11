"""A successful `deploy.sh` drops the `k8s_unapplied` lines its release records now carry (#4087).

A landing whose tick recorded the change before the landing deployed it used to leave the line
standing until the next tick, and sessions cleared it by hand. `discharge_owed_k8s` runs the
deployer's own discharge rule at the end of the deploy instead.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_discharges_owed_k8s.py
"""

import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest

from _deploy_sh_fakes import (
    FAKE_RECAP,
    FLOCK_STUB,
    UV_WRAPPER_ARMS,
    deploy_sh_env,
    make_snapshot_repo,
    stub_bin,
)
from deploy_tools.deploy_lib import detach_run as deploy_detach
from deploy_tools.deploy_lib import playbook as deploy_playbook
from deploy_tools.deploy_lib import under_locks as deploy_under_locks
from deploy_toolbox import DeployTools
from lib.git_testing import git_out
from lib.repo_paths import REPO

ORIGIN = "a" * 40
APPLIED = "b" * 40


def _line(service: str, origin: str = ORIGIN) -> str:
    return json.dumps(
        {"at": 1000, "class": "k8s_unapplied", "origin": origin, "subject": service},
        sort_keys=True,
    )


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch) -> Path:
    """An owed ledger holding tdarr's line, with the state dir and tree lock under tmp_path."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "owed.jsonl").write_text(_line("tdarr") + "\n")
    monkeypatch.setenv(deploy_playbook.GITOPS_STATE_DIR_ENV, str(state_dir))
    monkeypatch.setenv("HOMELAB_DEPLOY_TREE_LOCK", str(tmp_path / "tree.lock"))
    return state_dir / "owed.jsonl"


def _tools(carries: bool) -> DeployTools:
    return DeployTools(
        release_commit=lambda _svc: APPLIED,
        is_ancestor=lambda _repo, origin, commit: (
            carries and (origin, commit) == (ORIGIN, APPLIED)
        ),
        digest_provable=lambda _repo, _roles: set(),
        render_proof=lambda _svc: None,
    )


def test_a_deploy_carrying_the_change_drops_its_line(ledger, tmp_path):
    """FLAGGED half: tdarr's record names a commit that descends from the line's origin."""
    deploy_playbook.discharge_owed_k8s(tmp_path, _tools(carries=True))
    assert not ledger.exists()


def test_a_deploy_not_carrying_the_change_keeps_its_line(ledger, tmp_path):
    """CLEAN half: the record predates the change, so the change is still owed."""
    deploy_playbook.discharge_owed_k8s(tmp_path, _tools(carries=False))
    assert ledger.read_text().splitlines() == [_line("tdarr")]


def test_a_tick_holding_the_tree_lock_keeps_the_line_and_says_so(
    ledger, tmp_path, capsys
):
    """The deploy already succeeded, so a busy lock is one line on stderr and no raise."""
    fd = os.open(tmp_path / "tree.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        deploy_playbook.discharge_owed_k8s(
            tmp_path, _tools(carries=True), lock_wait_s=0.05
        )
    finally:
        os.close(fd)
    assert ledger.read_text().splitlines() == [_line("tdarr")]
    assert "the next tick discharges" in capsys.readouterr().err


# ── the wiring: both success paths discharge, and no failure does ──────────────────────


@pytest.mark.parametrize(("status", "discharged"), [(0, True), (20, False)])
def test_a_detached_deploy_discharges_only_on_success(tmp_path, status, discharged):
    """`--detach` runs its playbook in `deploy_detach.deploy_and_gate`, not in `run`."""
    seen = []
    steps = deploy_detach.ChildSteps(
        run_playbook=lambda _run: status,
        annotate=lambda _run: None,
        discharge=seen.append,
        notify=lambda *_a: status,
    )
    run = deploy_under_locks.Run(
        repo_root=tmp_path, tags=["sonarr"], at_sha="", args=[]
    )
    deploy_detach.deploy_and_gate(run, tmp_path / "log", "notifier.py", steps)
    assert seen == ([tmp_path] if discharged else [])


UV_STUB = f"""#!/bin/bash
case "$*" in
  *ansible-playbook*) {FAKE_RECAP}; exit $PLAYBOOK_EXIT ;;
{UV_WRAPPER_ARMS}
  *) exit 0 ;;
esac
"""


def _deploy_with_a_carried_line(tmp_path: Path, playbook_exit: int) -> Path:
    """Run the real `deploy.sh` with a ledger line its release record carries."""
    repo = make_snapshot_repo(tmp_path / "repo")
    head = git_out(repo, "rev-parse", "HEAD")
    (tmp_path / "gitops-state").mkdir()
    ledger = tmp_path / "gitops-state" / "owed.jsonl"
    ledger.write_text(_line("sonarr", head) + "\n")
    (tmp_path / "releases").mkdir()
    (tmp_path / "releases" / "sonarr.json").write_text(json.dumps({"commit": head}))
    bin_dir = stub_bin(tmp_path, {"flock": FLOCK_STUB, "uv": UV_STUB})
    env = deploy_sh_env(tmp_path, bin_dir, PLAYBOOK_EXIT=str(playbook_exit))
    result = subprocess.run(
        [
            str(REPO / "scripts" / "deploy.sh"),
            "--tags",
            "sonarr",
            "--skip-staleness-check",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == (0 if playbook_exit == 0 else 20), result.stderr
    return ledger


def test_a_successful_deploy_sh_drops_the_line_it_carried(tmp_path):
    """FLAGGED half, through `deploy_under_locks.run`: the call after `annotate`."""
    assert not _deploy_with_a_carried_line(tmp_path, 0).exists()


def test_a_failed_deploy_sh_keeps_the_line(tmp_path):
    """CLEAN half: a playbook that failed proves nothing was applied."""
    ledger = _deploy_with_a_carried_line(tmp_path, 2)
    assert ledger.read_text().splitlines()[0].startswith('{"at": 1000')
