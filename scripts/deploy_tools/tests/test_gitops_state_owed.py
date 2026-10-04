"""The owed-ledger clears beyond `manual_plane`: the two k8s classes and `clear-owed`.

`clear-owed <class> <subject>` is the one verb (#3544); `clear-k8s-deferred`,
`clear-k8s-unapplied` and `clear-manual-plane` are its aliases. The `manual_plane` clear's own
rules are in test_gitops_state.py, and the shared `tree_lock`, `journal` and `run` fixtures in
conftest.py.

Run: uv run pytest scripts/deploy_tools/tests/test_gitops_state_owed.py
"""

import json
from pathlib import Path

import pytest

from deploy_tools import gitops_state
from gitops_ledger import OWED_CLASSES, OWED_HOLD_PLANE


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


# ── clear-k8s-deferred: the same shape one plane over ─────────────────────────────


_LINE = '{{"at": {}, "class": "k8s_deferred", "origin": "{}", "subject": "{}"}}'
SONARR = _LINE.format(1000, "abc123def4567890", "sonarr")
RADARR = _LINE.format(2000, "def456abc7890123", "radarr")


@pytest.fixture
def deferred(tmp_path: Path) -> Path:
    (tmp_path / "owed.jsonl").write_text(f"{SONARR}\n{RADARR}\n")
    return tmp_path / "owed.jsonl"


def test_clearing_one_deferred_bump_leaves_the_other(deferred, run, capsys, journal):
    assert run(deferred.parent, "clear-k8s-deferred", "sonarr") == 0
    assert deferred.read_text().splitlines() == [RADARR]
    assert "sonarr" in capsys.readouterr().out
    service, dropped, _ = journal[0]
    assert (service, dropped.origin) == ("sonarr", "abc123def4567890")
    assert run(deferred.parent, "clear-k8s-deferred", "radarr") == 0
    assert not deferred.exists(), "clearing the last bump removes the ledger"


def test_clearing_a_bump_that_is_not_deferred_exits_zero_and_says_so(
    deferred, run, capsys, journal
):
    """The rejecting half: a command that cleared everything reads the same from here."""
    assert run(deferred.parent, "clear-k8s-deferred", "jellyfin") == 0
    assert deferred.read_text().splitlines() == [SONARR, RADARR]
    assert "not pending" in capsys.readouterr().out
    assert journal[0][1] is None, "nothing was dropped, so the line says so"


# ── clear-k8s-unapplied: the class nothing pages on ────────────────────────────────

AUTHELIA = _LINE.replace("k8s_deferred", "k8s_unapplied").format(
    1000, "a" * 40, "authelia"
)


def test_clearing_an_unapplied_role_leaves_the_deferred_class_alone(
    tmp_path, run, capsys, journal
):
    """Two classes of one ledger (#3392): a clear aimed at one leaves the other's line."""
    (tmp_path / "owed.jsonl").write_text(f"{AUTHELIA}\n{SONARR}\n")
    assert run(tmp_path, "clear-k8s-unapplied", "authelia") == 0
    assert (tmp_path / "owed.jsonl").read_text().splitlines() == [SONARR]
    assert "authelia" in capsys.readouterr().out


def test_clearing_an_unapplied_role_that_is_not_pending_exits_zero_and_says_so(
    tmp_path, run, capsys, journal
):
    """The rejecting half, and the reason it matters here: the ordinary way out of this
    class is the tick's own discharge, so a hand clear usually finds nothing."""
    (tmp_path / "owed.jsonl").write_text(f"{AUTHELIA}\n")
    assert run(tmp_path, "clear-k8s-unapplied", "jellyfin") == 0
    assert (tmp_path / "owed.jsonl").read_text().splitlines() == [AUTHELIA]
    assert "not pending" in capsys.readouterr().out
    assert journal[0][1] is None, "nothing was dropped, so the line says so"


# ── clear-owed: one verb for every class an operator may clear (#3544) ──────────────


@pytest.mark.parametrize(
    ("alias", "cls", "subject", "ledger", "kept"),
    [
        ("clear-manual-plane", "manual_plane", "k3s", (K3S, COMMON), [COMMON]),
        ("clear-k8s-deferred", "k8s_deferred", "sonarr", (SONARR, RADARR), [RADARR]),
        (
            "clear-k8s-unapplied",
            "k8s_unapplied",
            "authelia",
            (AUTHELIA, SONARR),
            [SONARR],
        ),
    ],
)
def test_clear_owed_and_its_alias_drop_the_same_line(
    tmp_path, run, journal, alias, cls, subject, ledger, kept
):
    owed = tmp_path / "owed.jsonl"
    for argv in (["clear-owed", cls, subject], [alias, subject]):
        owed.write_text("\n".join(ledger) + "\n")
        assert run(tmp_path, *argv) == 0
        assert owed.read_text().splitlines() == kept, argv
    assert [j[0] for j in journal] == [subject, subject]
    assert [j[1] is not None for j in journal] == [True, True]


def test_clear_owed_refuses_hold_plane_and_changes_nothing(deferred, run, capsys):
    """A hold clears only once an apply covers each plane, so no hand verb reaches it."""
    before = deferred.read_text()
    with pytest.raises(SystemExit):
        run(deferred.parent, "clear-owed", OWED_HOLD_PLANE, "ansible/deploy.yml")
    assert "invalid choice" in capsys.readouterr().err
    assert deferred.read_text() == before


def test_every_ledger_class_but_hold_plane_is_clearable():
    """A class added to the ledger fails here until someone rules on a hand clear for it."""
    assert set(gitops_state.CLEARABLE_CLASSES) == OWED_CLASSES - {OWED_HOLD_PLANE}


def test_applied_is_refused_for_a_k8s_class(deferred, run, capsys):
    """Only a manual_plane line carries tags; ignoring the flag would hide a whole-line clear."""
    before = deferred.read_text()
    with pytest.raises(SystemExit):
        run(deferred.parent, "clear-owed", "k8s_deferred", "sonarr", "--applied", "x")
    assert "manual_plane only" in capsys.readouterr().err
    assert deferred.read_text() == before


def test_clear_owed_takes_applied_for_manual_plane(tmp_path, run):
    (tmp_path / "owed.jsonl").write_text(_k3s("k3s-a", "k3s-b") + "\n")
    assert run(tmp_path, "clear-owed", "manual_plane", "k3s", "--applied", "k3s-a") == 0
    pending = gitops_state.DeployerState(str(tmp_path)).manual_plane_tags_pending()
    assert pending == {"k3s": frozenset({"k3s-b"})}


def test_the_journal_event_keeps_each_alias_name():
    """`journalctl -t gitops-state` queries written against the old verbs still match."""
    assert {
        gitops_state.journal_event(c) for c in gitops_state.CLEARABLE_CLASSES
    } == set(gitops_state.ALIAS_CLASSES)
