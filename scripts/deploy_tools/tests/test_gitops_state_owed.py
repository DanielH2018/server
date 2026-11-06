"""The `clear-owed` verb: which classes it takes, and the command every surface prints.

`clear-owed <class> <subject>` is the one verb (#3544); the per-class verbs it replaced were
retired in #3550. The `manual_plane` clear's own
rules are in test_gitops_state.py, and the shared `tree_lock`, `journal` and `run` fixtures in
conftest.py.

Run: uv run pytest scripts/deploy_tools/tests/test_gitops_state_owed.py
"""

import json
from pathlib import Path

import pytest

from deploy_tools import gitops_state
from gitops_ledger import OWED_CLASSES, OWED_HOLD_PLANE
from gitops_markers import owed_clear_cmd


def _k3s(*tags: str) -> str:
    """One `manual_plane` line for k3s owing `tags`, as the deployer writes it."""
    obj = {"class": "manual_plane", "subject": "k3s", "origin": "abc123def4567890"}
    return json.dumps(
        {**obj, "at": 1000, "playbook": "ansible/k3s-bringup.yml", "tags": sorted(tags)}
    )


K3S = _k3s()
COMMON = json.dumps(
    {
        "class": "manual_plane",
        "subject": "common",
        "origin": "def456abc7890123",
        "at": 2000,
        "playbook": "none",
        "tags": [],
    }
)


# ── clear-owed: one verb for every class an operator may clear (#3544) ──────────────


@pytest.fixture
def owed(tmp_path: Path) -> Path:
    (tmp_path / "owed.jsonl").write_text(f"{K3S}\n{COMMON}\n")
    return tmp_path / "owed.jsonl"


def test_the_retired_per_class_verb_is_refused(owed, run, capsys):
    """Every printer names `clear-owed` (#3547), so the per-class verbs are gone (#3550)."""
    before = owed.read_text()
    with pytest.raises(SystemExit):
        run(owed.parent, "clear-manual-plane", "k3s")
    assert "invalid choice" in capsys.readouterr().err
    assert owed.read_text() == before


@pytest.mark.parametrize(
    ("cls", "subject", "tags", "ledger", "kept"),
    [
        ("manual_plane", "k3s", (), (K3S, COMMON), [COMMON]),
        (
            "manual_plane",
            "k3s",
            ("kubeconfig",),
            (_k3s("kubeconfig"), COMMON),
            [COMMON],
        ),
    ],
)
def test_the_printed_clear_command_runs_and_drops_its_line(
    tmp_path, run, cls, subject, tags, ledger, kept
):
    """Every surface prints `owed_clear_cmd`, so its argv must be one this parser accepts (#3547)."""
    path = tmp_path / "owed.jsonl"
    path.write_text("\n".join(ledger) + "\n")
    printed = owed_clear_cmd(cls, subject, tags)
    words = printed.split()
    argv = words[words.index("scripts/deploy_tools/gitops_state.py") + 1 :]
    assert run(tmp_path, *argv) == 0
    assert path.read_text().splitlines() == kept


@pytest.mark.parametrize("cls", [OWED_HOLD_PLANE, "k8s_deferred", "k8s_unapplied"])
def test_clear_owed_refuses_any_other_class_and_changes_nothing(owed, run, capsys, cls):
    """A hold clears only once an apply covers each plane, and no tick writes a k8s class."""
    before = owed.read_text()
    with pytest.raises(SystemExit):
        run(owed.parent, "clear-owed", cls, "ansible/deploy.yml")
    assert "invalid choice" in capsys.readouterr().err
    assert owed.read_text() == before


def test_every_ledger_class_but_hold_plane_is_clearable():
    """A class added to the ledger fails here until someone rules on a hand clear for it."""
    assert set(gitops_state.CLEARABLE_CLASSES) == OWED_CLASSES - {OWED_HOLD_PLANE}


def test_clear_owed_takes_applied_for_manual_plane(tmp_path, run):
    (tmp_path / "owed.jsonl").write_text(_k3s("k3s-a", "k3s-b") + "\n")
    assert run(tmp_path, "clear-owed", "manual_plane", "k3s", "--applied", "k3s-a") == 0
    pending = gitops_state.DeployerState(str(tmp_path)).manual_plane_tags_pending()
    assert pending == {"k3s": frozenset({"k3s-b"})}
