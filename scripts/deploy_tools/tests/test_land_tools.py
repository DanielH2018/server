"""The real boundary implementations build the right process, in the right checkout.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tools.py
"""

import fcntl
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from lib.git_testing import scrubbed_env
from lib.git_testing import commit, init_repo
from lib.proc_testing import run
from deploy_tools.land_lib import tools


def _capture(monkeypatch):
    """Record the argv and kwargs of whichever subprocess call a boundary makes.

    The whole `subprocess` MODULE is replaced rather than one function on it, so a boundary
    that reaches for `Popen` is seen by the same seam as one that reaches for `run` -- both
    spellings answer the same questions here, which checkout and which working directory.
    One patch rather than two also keeps this file inside its entry in
    tests/repo/monkeypatch_allowlist.txt, which only ever falls.
    """
    seen = {}

    def run(argv, **kw):
        seen.update(argv=list(argv), **kw)
        return subprocess.CompletedProcess(argv, 0, "", "")

    class Popen:
        returncode = 0
        stderr = ()

        def __init__(self, argv, **kw):
            seen.update(argv=list(argv), **kw)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    fake = SimpleNamespace(**vars(subprocess))
    fake.run = run
    fake.Popen = Popen
    monkeypatch.setattr(tools, "subprocess", fake)
    return seen


def test_helpers_whose_code_must_match_are_imported_from_beside_land_py():
    """A helper this script passes new flags to must be the same release."""
    assert Path(tools.await_ci.__file__).resolve().parent == tools.HERE
    assert Path(tools.land_tags.__file__).resolve().parent == tools.HERE / "land_lib"
    gate_file = sys.modules[tools.health_gate.__module__].__file__
    assert gate_file and Path(gate_file).resolve().parent == tools.HERE / "land_lib"


def test_the_syslog_tag_is_the_one_the_board_expects(monkeypatch):
    seen = _capture(monkeypatch)
    tools.syslog("event=landing pr=1")
    assert seen["argv"][:3] == ["logger", "-t", "landing-annotation"]


def _commit(repo: Path, name: str) -> str:
    """Commit `name` into `repo` and return the new HEAD, with GIT_* scrubbed."""
    return commit(repo, name, **{name: name})


def _snapshot_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """A two-commit repo and a tree-lock path inside tmp_path; (repo, first, second, lock).

    The lock path is handed to the test that holds it while a snapshot is taken. It points
    inside tmp_path, never at the production file a live tree job holds.
    """
    repo = init_repo(tmp_path / "repo")
    first = _commit(repo, "one")
    second = _commit(repo, "two")
    return repo, first, second, str(tmp_path / "tree.lock")


def test_the_gate_snapshot_is_a_worktree_of_the_named_commit(tmp_path):
    """CLEAN half: the gate renders the commit that was deployed, not the checkout's HEAD."""
    repo, first, second, _lock = _snapshot_repo(tmp_path)
    with tools.gate_snapshot(repo, first) as snap:
        assert snap is not None
        head = run(
            ["git", "rev-parse", "HEAD"],
            cwd=snap,
            env=scrubbed_env(),
            check=True,
        ).stdout.strip()
        assert head == first and head != second
        assert os.environ[tools.UV_PROJECT_ENVIRONMENT] == str(repo / ".venv")
        assert (snap / "one").exists()
    assert not snap.exists()
    assert tools.UV_PROJECT_ENVIRONMENT not in os.environ
    # And it deregistered itself, or the next `git worktree add` in this repo trips over it.
    listed = run(
        ["git", "worktree", "list"],
        cwd=repo,
        env=scrubbed_env(),
        check=True,
    ).stdout
    assert str(snap) not in listed


def test_a_snapshot_of_an_unresolvable_commit_is_none(tmp_path):
    """REJECTING half: it answers None rather than raising; the CALLER decides what that means.

    `health_verdict.gate_from_the_deployed_tree` is where None is turned into a verdict, and
    None must not read as permission to gate the primary.
    """
    repo, _first, _second, _lock = _snapshot_repo(tmp_path)
    with tools.gate_snapshot(repo, "deadbeefdeadbeefdeadbeef") as snap:
        assert snap is None
    assert tools.UV_PROJECT_ENVIRONMENT not in os.environ


def test_a_held_tree_lock_does_not_cost_the_gate_its_snapshot(tmp_path):
    """No lock is taken at all, because a detached add of a pinned SHA touches no working tree.

    The first cut took it non-blocking and degraded to the primary on a refusal, which handed
    the gap back on the likeliest path: the retired `gitops-deploy.service` held this lock for
    its whole unit run, so the tick a landing raced was exactly when the gate read `skipped`.
    """
    repo, first, _second, lock = _snapshot_repo(tmp_path)
    held = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o666)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tools.gate_snapshot(repo, first) as snap:
            assert snap is not None
    finally:
        os.close(held)


def test_a_sigterm_during_the_gate_still_removes_the_worktree(tmp_path):
    """A default SIGTERM skips every `finally` and leaves a registered worktree behind.

    The handler is installed for the life of the snapshot and restored after it, so the signal
    arrives as a KeyboardInterrupt the `finally` can run under -- and pytest's own handlers are
    the ones in place again afterwards.
    """
    repo, first, _second, _lock = _snapshot_repo(tmp_path)
    before = signal.getsignal(signal.SIGTERM)
    kept: list[Path] = []

    def snapshot_then_sigterm() -> None:
        with tools.gate_snapshot(repo, first) as snap:
            assert snap is not None
            kept.append(snap)
            os.kill(os.getpid(), signal.SIGTERM)

    with pytest.raises(KeyboardInterrupt):
        snapshot_then_sigterm()
    assert not kept[0].exists()
    assert signal.getsignal(signal.SIGTERM) is before
    listed = run(
        ["git", "worktree", "list"],
        cwd=repo,
        env=scrubbed_env(),
        check=True,
    ).stdout
    assert str(kept[0]) not in listed


def test_the_gate_snapshot_lands_in_the_pinned_root_its_docstring_names(
    tmp_path, monkeypatch
):
    """The leak the docstring tells an operator to `rm -rf` has to be where it says it is.

    `tempfile.mkdtemp` with no `dir=` honours TMPDIR, which on this host is `/tmp/user/1000`,
    so a SIGKILLed landing left `land-gate-*` where the recovery command matches nothing -- and
    the `worktree prune` after it then deregisters nothing either, the leaked directory still
    being there. The constant is the oracle for the prose: the recovery command is built from
    GATE_TMP_ROOT, so moving the snapshot without the text (or the reverse) fails here.
    """
    repo, first, _second, _lock = _snapshot_repo(tmp_path)
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    monkeypatch.setenv(tools.GATE_TMP_ROOT_ENV, str(pinned))
    with tools.gate_snapshot(repo, first) as snap:
        assert snap is not None
        assert snap.parent.parent == pinned
    assert f"rm -rf {tools.GATE_TMP_ROOT}/land-gate-*" in (
        tools.gate_snapshot.__doc__ or ""
    )


def _wait_args(timeout_s, token):
    """The (interval, walk_descendants) `await_ci_verdict` hands `await_ci.wait`."""
    seen = []

    def wait(sha, budget, interval, walk_descendants):
        seen.append((interval, walk_descendants))
        return 0, f"{sha[:8]}: CI green"

    tools.await_ci_verdict("abc123def", timeout_s, wait=wait, token=lambda: token)
    return seen


def test_an_anonymous_ci_wait_backs_off_and_walks_no_descendants(capsys):
    """#6148: anonymous reads get 60/hour per IP, shared by every anonymous reader there."""
    (interval, walk) = _wait_args(1200, None)[0]
    assert interval >= 60
    assert walk is False
    assert "gh auth login" in capsys.readouterr().err


def test_an_authenticated_ci_wait_keeps_the_20s_poll_and_the_walk(capsys):
    assert _wait_args(1200, "ghp_token") == [(20, True)]
    assert capsys.readouterr().err == ""
