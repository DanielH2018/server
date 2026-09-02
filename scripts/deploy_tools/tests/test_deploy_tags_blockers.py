"""`deploy_tags.py blockers` must refuse a range the tick will never cross, and only that.

The deployer refuses to fast-forward past a `_BROAD_MANUAL_PREFIXES` change -- the bring-up
playbooks, which only run by hand. Anything deployed after that tick is then refused as stale
(deploy.sh exit 4). That outcome is decided the moment the commit is pushed, so it is
checkable BEFORE waiting on CI -- which is the whole point of the subcommand.

Both halves matter. A checker that flagged every range would stop every landing; one that
flagged none would restore the six wasted minutes it exists to save.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_tags_blockers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deploy_tags  # noqa: E402 — needs the path insert above


def _run(paths, monkeypatch) -> int:
    monkeypatch.setattr(
        deploy_tags, "_incoming_paths", lambda ref, cwd=None: list(paths)
    )

    class Args:
        ref = "origin/master"

    return deploy_tags._cmd_blockers(Args())


def test_a_deployer_change_no_longer_blocks(monkeypatch):
    """The #570 blocker was another session's edit to gitops_deploy.py. Since 2026-09-01 the
    deployer applies its own role like any other setup role, so the tick crosses it and a
    landing behind it must NOT be told to stop -- three landings were, that day alone."""
    rc = _run(["ansible/roles/setup/gitops_deploy/files/gitops_deploy.py"], monkeypatch)
    assert rc == 0


def test_the_bringup_playbooks_block(monkeypatch):
    for path in (
        "ansible/bootstrap.yml",
        "ansible/k3s-bringup.yml",
        "ansible/initial_setup.yml",
    ):
        assert _run([path], monkeypatch) == 3, f"{path} should block"


def test_an_ordinary_service_change_does_not_block(monkeypatch):
    """The reject half. Without it, a checker that returned 3 unconditionally would pass
    every test above while stopping every landing."""
    rc = _run(["ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"], monkeypatch)
    assert rc == 0


def test_a_broad_but_applicable_change_does_not_block(monkeypatch):
    """Broad is not the same as broad-MANUAL. The deployer applies a shared-template or
    inventory change itself, so it must not stop a landing -- only the manual subset does."""
    rc = _run(["ansible/inventory/group_vars/all.yml"], monkeypatch)
    assert rc == 0


def test_an_empty_range_does_not_block(monkeypatch):
    assert _run([], monkeypatch) == 0


def test_the_culprit_is_named(monkeypatch, capsys):
    """A verdict with no path leaves the operator to re-derive what stopped them."""
    _run(
        [
            "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
            "ansible/bootstrap.yml",
        ],
        monkeypatch,
    )
    err = capsys.readouterr().err
    assert "ansible/bootstrap.yml" in err
    assert "ansible/roles/k8s/sonarr" not in err, "named a path that is not a blocker"
