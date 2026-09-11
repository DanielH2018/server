"""The operator's half of the `manual_plane` marker: clearing one role's line.

Every rule is a pair. A command that cleared everything and one that cleared nothing read the
same from the passing side alone, and this one is destructive in the direction that matters:
clearing a role nobody applied silences the page that says so.

Run: uv run pytest scripts/deploy_tools/tests/test_gitops_state.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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
def run(tree_lock: Path):
    """`run(state_dir, *argv)` -> the command's exit code, against the injected lock."""

    def _run(state_dir: Path, *args: str) -> int:
        return gitops_state.main(
            ["--state-dir", str(state_dir), *args],
            lock_path=str(tree_lock),
            lock_wait_s=0.05,
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
