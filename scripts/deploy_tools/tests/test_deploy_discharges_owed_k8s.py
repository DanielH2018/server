"""A successful `deploy.sh` drops the `k8s_unapplied` lines its release records now carry (#4087).

A landing whose tick recorded the change before the landing deployed it used to leave the line
standing until the next tick, and sessions cleared it by hand. `discharge_owed_k8s` runs the
deployer's own discharge rule at the end of the deploy instead.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_discharges_owed_k8s.py
"""

import fcntl
import json
import os
from pathlib import Path

import pytest

from deploy_tools import deploy_under_locks
from deploy_toolbox import DeployTools

ORIGIN = "a" * 40
APPLIED = "b" * 40


def _line(service: str) -> str:
    return json.dumps(
        {"at": 1000, "class": "k8s_unapplied", "origin": ORIGIN, "subject": service},
        sort_keys=True,
    )


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch) -> Path:
    """An owed ledger holding tdarr's line, with the state dir and tree lock under tmp_path."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "owed.jsonl").write_text(_line("tdarr") + "\n")
    monkeypatch.setenv(deploy_under_locks.GITOPS_STATE_DIR_ENV, str(state_dir))
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
    deploy_under_locks.discharge_owed_k8s(tmp_path, _tools(carries=True))
    assert not ledger.exists()


def test_a_deploy_not_carrying_the_change_keeps_its_line(ledger, tmp_path):
    """CLEAN half: the record predates the change, so the change is still owed."""
    deploy_under_locks.discharge_owed_k8s(tmp_path, _tools(carries=False))
    assert ledger.read_text().splitlines() == [_line("tdarr")]


def test_a_tick_holding_the_tree_lock_keeps_the_line_and_says_so(
    ledger, tmp_path, capsys
):
    """The deploy already succeeded, so a busy lock is one line on stderr and no raise."""
    fd = os.open(tmp_path / "tree.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        deploy_under_locks.discharge_owed_k8s(
            tmp_path, _tools(carries=True), lock_wait_s=0.05
        )
    finally:
        os.close(fd)
    assert ledger.read_text().splitlines() == [_line("tdarr")]
    assert "the next tick discharges" in capsys.readouterr().err
