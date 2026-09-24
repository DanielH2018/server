"""The operator's half of the `manual_plane` marker: clearing one role's line.

Every rule is a pair. A command that cleared everything and one that cleared nothing read the
same from the passing side alone, and this one is destructive in the direction that matters:
clearing a role nobody applied silences the page that says so.

Run: uv run pytest scripts/deploy_tools/tests/test_gitops_state.py
"""

import fcntl
import os
from pathlib import Path

import pytest


from deploy_tools import gitops_state

K3S = "abc123def4567890 ansible/k3s-bringup.yml k3s 1000.0"
COMMON = "def456abc7890123 none common 2000.0"


@pytest.fixture
def tree_lock(tmp_path: Path) -> Path:
    """The lock `run` injects, in place of `/var/lock/server-git-tree.lock`.

    A deploy or a gitops tick on this very host may hold the real one: a suite that took it
    would block that deploy, and one that ran while a tick held it would sit through the whole
    wait on every test. `main()` takes the path as an argument so no test has to patch it.

    It lives in its own directory, NOT beside the markers. The real lock is in `/var/lock`
    while the markers are in `/var/lib/gitops-deploy`, and a test that put them together
    would make the unwritable-state-directory test fail at the lock instead of at the marker
    write it is named for — passing on a path it does not exercise.
    """
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()
    return lock_dir / "tree.lock"


@pytest.fixture
def journal() -> list[tuple[str, gitops_state.ManualPlaneEntry | None, frozenset]]:
    """Every (role, dropped line, still-pending tags) a clear recorded, in place of
    the real `logger` line.

    `run` injects it into every test: a real line from a test run would read as an
    operator's clear (`cleared=true role=k3s`) to the next investigator of this host.
    """
    return []


@pytest.fixture
def run(tree_lock: Path, journal):
    """`run(state_dir, *argv)` -> the command's exit code, against the injected lock."""

    def _run(state_dir: Path, *args: str) -> int:
        return gitops_state.main(
            ["--state-dir", str(state_dir), *args],
            lock_path=str(tree_lock),
            lock_wait_s=0.05,
            journal=lambda role, dropped, remaining: journal.append(
                (role, dropped, remaining)
            ),
        )

    return _run


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    (tmp_path / "manual_plane").write_text(f"{K3S}\n{COMMON}\n")
    return tmp_path / "manual_plane"


def test_clearing_one_role_leaves_the_other(marker, run, capsys):
    assert run(marker.parent, "clear-manual-plane", "k3s") == 0
    assert marker.read_text().splitlines() == [COMMON]
    assert "k3s" in capsys.readouterr().out


def test_clearing_a_role_that_is_not_pending_exits_zero_and_says_so(
    marker, run, capsys
):
    """An operator clearing twice, or naming a role nobody recorded, has nothing to fix."""
    assert run(marker.parent, "clear-manual-plane", "renovate_agent") == 0
    assert marker.read_text().splitlines() == [K3S, COMMON]
    assert "not pending" in capsys.readouterr().out


def test_clearing_the_last_role_removes_the_marker(tmp_path, run, capsys):
    (tmp_path / "manual_plane").write_text(f"{K3S}\n")
    assert run(tmp_path, "clear-manual-plane", "k3s") == 0
    assert not (tmp_path / "manual_plane").exists()


def test_an_absent_marker_is_not_an_error(tmp_path, run, capsys):
    assert run(tmp_path, "clear-manual-plane", "k3s") == 0
    assert "not pending" in capsys.readouterr().out


# ── #2349: a narrowed apply clears only the tags it ran ────────────────────────────────


def test_a_narrowed_clear_keeps_a_tag_a_later_range_added(
    tmp_path, run, capsys, journal
):
    """The sequence the narrowing introduced.

    `land.sh` prints `--tags kubeconfig` and the clear beside it. Before the operator runs
    the clear, a second PR changes `k3s/tasks/coredns.yml` and the next tick unions the row
    to `coredns,kubeconfig`. A whole-line clear drops `coredns` with it, leaving that change
    merged, unapplied and recorded nowhere. Under the old role-tag command the operator's
    `--tags k3s` run would have applied it, so this gap is the narrowing's own.
    """
    (tmp_path / "manual_plane").write_text(f"{K3S}\n")
    (tmp_path / "manual_plane_tags").write_text("k3s coredns,kubeconfig\n")
    assert run(tmp_path, "clear-manual-plane", "k3s", "--applied", "kubeconfig") == 0
    assert (tmp_path / "manual_plane").read_text().splitlines() == [K3S]
    assert (tmp_path / "manual_plane_tags").read_text().strip() == "k3s coredns"
    assert "STILL pending for coredns" in capsys.readouterr().out
    ((_role, dropped, remaining),) = journal
    assert dropped is None and remaining == frozenset({"coredns"})


def test_a_narrowed_clear_covering_the_whole_row_takes_the_line(tmp_path, run, capsys):
    """The accepting half: nothing left to apply means the role stops being pending."""
    (tmp_path / "manual_plane").write_text(f"{K3S}\n")
    (tmp_path / "manual_plane_tags").write_text("k3s kubeconfig\n")
    assert run(tmp_path, "clear-manual-plane", "k3s", "--applied", "kubeconfig") == 0
    assert not (tmp_path / "manual_plane").exists()
    assert not (tmp_path / "manual_plane_tags").exists()
    assert "cleared k3s" in capsys.readouterr().out


def test_a_bare_clear_still_takes_the_whole_line_and_row(tmp_path, run):
    """Omitting `--applied` means a whole-role apply, which covers whatever the row gained.

    The bare command is what an operator types from memory and what every surface printed
    before this flag existed, so it keeps its old meaning.
    """
    (tmp_path / "manual_plane").write_text(f"{K3S}\n")
    (tmp_path / "manual_plane_tags").write_text("k3s coredns,kubeconfig\n")
    assert run(tmp_path, "clear-manual-plane", "k3s") == 0
    assert not (tmp_path / "manual_plane").exists()
    assert not (tmp_path / "manual_plane_tags").exists()


def test_a_narrowed_clear_on_a_row_a_later_refusal_collapsed_keeps_the_line(
    tmp_path, run, capsys, journal
):
    """Every printer names `--applied` only while it holds a non-empty row.

    So `--applied kubeconfig` meeting an empty row means the row changed after the command
    was printed: PR-B changed an untagged k3s file, the derivation refused, and the row
    collapsed to "the whole role". Clearing there leaves PR-B merged, unapplied and recorded
    nowhere.
    """
    (tmp_path / "manual_plane").write_text(f"{K3S}\n")
    (tmp_path / "manual_plane_tags").write_text("k3s -\n")
    assert run(tmp_path, "clear-manual-plane", "k3s", "--applied", "kubeconfig") == 0
    assert (tmp_path / "manual_plane").read_text().splitlines() == [K3S]
    assert (tmp_path / "manual_plane_tags").read_text().strip() == "k3s -"
    out = capsys.readouterr().out
    assert "kept k3s" in out and "clear-manual-plane k3s`" in out
    ((_role, dropped, remaining),) = journal
    assert dropped is None and remaining == frozenset({"k3s"})


def test_a_narrowed_clear_on_a_line_with_no_row_keeps_it(tmp_path, run):
    """A missing row is the same unknown: a line older than the sidecar, or a garbled row."""
    (tmp_path / "manual_plane").write_text(f"{K3S}\n")
    assert run(tmp_path, "clear-manual-plane", "k3s", "--applied", "kubeconfig") == 0
    assert (tmp_path / "manual_plane").read_text().splitlines() == [K3S]


def test_applied_naming_the_role_tag_is_a_whole_role_clear(tmp_path, run):
    """The accepting half for an empty row: the whole-role tag covers it, however spelled."""
    (tmp_path / "manual_plane").write_text(f"{K3S}\n")
    (tmp_path / "manual_plane_tags").write_text("k3s -\n")
    assert run(tmp_path, "clear-manual-plane", "k3s", "--applied", "k3s") == 0
    assert not (tmp_path / "manual_plane").exists()


def test_the_printed_clear_for_k3s_and_common_leaves_no_line_behind(tmp_path, run):
    """What an operator pastes after applying both roles must clear both.

    `common`'s row is always empty, because no playbook applies it and nothing narrows it.
    A shared `clear-manual-plane <role> --applied <tags>` sent the operator to run it with
    `--applied` for `common` too, which keeps that line by design.
    """
    # Importable only once `gitops_state` has put the deployer's `files/` on sys.path.
    import deploy_remediation

    (tmp_path / "manual_plane").write_text(f"{K3S}\n{COMMON}\n")
    (tmp_path / "manual_plane_tags").write_text("common -\nk3s kubeconfig\n")
    text = deploy_remediation.manual_plane_remediation(
        {"k3s", "common"}, {"k3s": frozenset({"kubeconfig"})}
    )
    clear = text.rsplit("`", 2)[-2]
    commands = [c.split("gitops_state.py ", 1)[1].split() for c in clear.split(" && ")]
    assert len(commands) == 2, clear
    for argv in commands:
        assert run(tmp_path, *argv) == 0
    assert not (tmp_path / "manual_plane").exists(), clear


def test_a_state_directory_this_user_cannot_write_says_who_owns_it(marker, run, capsys):
    """The state directory is 0750 and owned by the deployer's user, so the wrong shell gets
    a PermissionError.

    A traceback there reads as a broken script rather than as "run this as the deploy user".
    """
    marker.parent.chmod(0o500)
    try:
        assert run(marker.parent, "clear-manual-plane", "k3s") == 1
        err = capsys.readouterr().err
        assert "cannot write" in err
    finally:
        marker.parent.chmod(0o700)


def test_a_held_tree_lock_refuses_and_changes_nothing(marker, tree_lock, run, capsys):
    """The accepting half is every other test here, which runs against a free lock.

    A tick's `record_manual_plane` and this command are both read-modify-write over the whole
    file, so an interleaved pair drops one of their two changes. The tick runs under this lock
    already; refusing is what keeps the operator's half out of the gap.
    """
    with gitops_state.tree_lock(str(tree_lock)):
        assert run(marker.parent, "clear-manual-plane", "k3s") == 1
    assert marker.read_text().splitlines() == [K3S, COMMON], "nothing was rewritten"
    assert "is held" in capsys.readouterr().err


def test_an_unopenable_lock_names_the_lock_and_not_the_state_directory(
    marker, tree_lock, capsys
):
    """The two unwritable paths are different faults with different fixes.

    `/var/lock` and `/var/lib/gitops-deploy` are different directories, so attributing an
    EACCES on the lock to the state directory would send an operator to `sudo -u ubuntu` over
    a lock that no user can open.
    """
    tree_lock.parent.chmod(0o500)
    try:
        rc = gitops_state.main(
            ["--state-dir", str(marker.parent), "clear-manual-plane", "k3s"],
            lock_path=str(tree_lock),
            lock_wait_s=0.05,
        )
    finally:
        tree_lock.parent.chmod(0o700)
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot open the tree lock" in err
    assert str(tree_lock) in err
    assert marker.read_text().splitlines() == [K3S, COMMON], "nothing was rewritten"


def test_the_lock_is_released_for_the_next_run(marker, run, capsys):
    """A refusal must not leave the lock held, and neither must a successful clear."""
    assert run(marker.parent, "clear-manual-plane", "k3s") == 0
    assert run(marker.parent, "clear-manual-plane", "common") == 0
    assert not marker.exists()


def test_the_role_is_resolved_through_the_deployers_own_tag_map(marker):
    """The marker's key is `setup_role_tag(role)`, so this must not match on the bare name.

    Today every role the marker can hold is tagged by its own name, which is what makes the
    two indistinguishable — and exactly why the resolution belongs here rather than in a
    reader's head.
    """
    assert gitops_state.marker_key("k3s") == "k3s"


# ── the journal line a clear leaves (issue #2022) ─────────────────────────────────────────
def test_a_clear_journals_the_role_and_the_line_it_dropped_and_a_no_op_says_so(
    marker, run, journal
):
    """`k3s` was cleared by hand on 2026-09-18 with its apply still owed, and nothing said
    who, when, or which merged SHA that silenced: the marker's own truncation is the only
    write the command made. The journal call is the evidence that write does not leave.
    """
    assert run(marker.parent, "clear-manual-plane", "k3s") == 0
    ((role, dropped, _remaining),) = journal
    assert role == "k3s"
    assert (dropped.origin, dropped.playbook, dropped.at) == (
        "abc123def4567890",
        "ansible/k3s-bringup.yml",
        1000.0,
    )
    assert run(marker.parent, "clear-manual-plane", "k3s") == 0
    assert journal[1] == ("k3s", None, frozenset()), (
        "a second run drops nothing and still says so"
    )


def test_a_refused_clear_journals_nothing(marker, tree_lock, run, journal):
    """The rejecting half: a line claiming a clear that never happened is worse than none."""
    with gitops_state.tree_lock(str(tree_lock)):
        assert run(marker.parent, "clear-manual-plane", "k3s") == 1
    assert journal == []


def test_the_journal_line_is_logfmt_under_the_gitops_state_tag(monkeypatch):
    """`journalctl -t gitops-state` is the verify-by, and the fields are what an
    investigator matches against an apply: the role, who, from where, and the dropped
    line's origin and playbook."""
    monkeypatch.setenv("SUDO_USER", "daniel")
    argvs: list[list[str]] = []
    entry = gitops_state.ManualPlaneEntry(
        "abc123def4567890", "ansible/k3s-bringup.yml", "k3s", 1000.0
    )
    gitops_state.journal_clear("k3s", entry, run=lambda argv, **kw: argvs.append(argv))
    (argv,) = argvs
    assert argv[:3] == ["logger", "-t", "gitops-state"]
    fields = argv[3].split()
    for field in (
        "event=clear-manual-plane",
        "role=k3s",
        "cleared=true",
        "user=daniel",
        f"cwd={os.getcwd()}",
        "origin=abc123def4567890",
        "playbook=ansible/k3s-bringup.yml",
        "pending_since=1000",
    ):
        assert field in fields
    gitops_state.journal_clear("k3s", None, run=lambda argv, **kw: argvs.append(argv))
    assert "cleared=false" in argvs[1][3].split()
    assert "origin=" not in argvs[1][3]


def test_a_failing_logger_does_not_change_the_clears_exit_code(marker, tree_lock):
    """The clear already happened by the time the journal line is written."""

    def no_logger(argv, **kw):
        raise FileNotFoundError("logger")

    rc = gitops_state.main(
        ["--state-dir", str(marker.parent), "clear-manual-plane", "k3s"],
        lock_path=str(tree_lock),
        lock_wait_s=0.05,
        journal=lambda role, dropped, remaining: gitops_state.journal_clear(
            role, dropped, remaining, run=no_logger
        ),
    )
    assert rc == 0
    assert marker.read_text().splitlines() == [COMMON]


# ── clear-contention (issue #1847) ────────────────────────────────────────────────────────
def test_clear_contention_removes_the_marker(tmp_path, run, capsys):
    (tmp_path / "contention_since").write_text(f"{'a' * 40} sonarr 1.0 2.0 3\n")
    assert run(tmp_path, "clear-contention") == 0
    assert not (tmp_path / "contention_since").exists()
    assert "cleared" in capsys.readouterr().out


def test_clear_contention_with_no_marker_exits_zero_and_says_so(tmp_path, run, capsys):
    assert run(tmp_path, "clear-contention") == 0
    assert "nothing to clear" in capsys.readouterr().out


def test_clear_contention_refuses_while_the_tree_lock_is_held(
    tmp_path, tree_lock, run, capsys
):
    (tmp_path / "contention_since").write_text(f"{'a' * 40} sonarr 1.0 2.0 3\n")
    fd = os.open(tree_lock, os.O_RDONLY | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert run(tmp_path, "clear-contention") == 1
    finally:
        os.close(fd)
    assert (tmp_path / "contention_since").exists()
    assert "is held" in capsys.readouterr().err
