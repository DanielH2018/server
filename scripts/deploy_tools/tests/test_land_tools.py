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
from _deploy_sh_fakes import git_free_env
from deploy_tools.land_lib import tools


def _capture(monkeypatch):
    """Record the argv and kwargs of whichever subprocess call a boundary makes.

    The whole `subprocess` MODULE is replaced rather than one function on it, so a boundary
    that reaches for `Popen` is seen by the same seam as one that reaches for `run` -- both
    spellings answer the same questions here, which checkout and which working directory.
    One patch rather than two also keeps this file inside its entry in
    ansible/tests/monkeypatch_allowlist.txt, which only ever falls.
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


def test_deploy_tags_is_the_relative_path_in_the_primary_checkout(monkeypatch):
    """Its question IS the primary checkout: `blockers` reads HEAD..origin/master there."""
    seen = _capture(monkeypatch)
    tools.run_deploy_tags(Path("/primary"), ["blockers", "origin/master"])
    assert seen["argv"][3] == "scripts/deploy_tools/deploy_tags.py"
    assert seen["cwd"] == Path("/primary")


def test_deploy_adds_target_only_for_a_remote_host(monkeypatch):
    seen = _capture(monkeypatch)
    tools.run_deploy(Path("/primary"), ["alloy"], "daniel-pi")
    assert seen["argv"] == [
        "./scripts/deploy.sh",
        "--tags",
        "alloy",
        "-e",
        "target=daniel-pi",
    ]
    tools.run_deploy(Path("/primary"), ["sonarr"], None)
    assert seen["argv"] == ["./scripts/deploy.sh", "--tags", "sonarr"]
    # deploy.sh renders from its working directory, so a wrong cwd would deploy the wrong
    # checkout's templates and report success.
    assert seen["cwd"] == Path("/primary")


def test_the_tick_runs_from_beside_land_py(monkeypatch):
    seen = _capture(monkeypatch)
    tools.run_tick()
    assert seen["argv"] == [str(tools.HERE / "gitops_tick.sh")]
    assert (tools.HERE / "land.py").exists() or (tools.HERE / "land.sh").exists()


def test_helpers_whose_code_must_match_are_imported_from_beside_land_py():
    """Issue #851: a helper this script passes new flags to must be the same release."""
    assert Path(tools.await_ci.__file__).resolve().parent == tools.HERE
    assert Path(tools.land_tags.__file__).resolve().parent == tools.HERE
    gate_file = sys.modules[tools.health_gate.__module__].__file__
    assert gate_file and Path(gate_file).resolve().parent == tools.HERE


def test_read_state_is_empty_for_a_missing_or_blank_marker(tmp_path):
    assert tools.read_state(tmp_path, "hold_sha") == ""
    (tmp_path / "hold_sha").write_text("  \n")
    assert tools.read_state(tmp_path, "hold_sha") == ""
    (tmp_path / "hold_sha").write_text("abc\n")
    assert tools.read_state(tmp_path, "hold_sha") == "abc"


def test_the_syslog_tag_is_the_one_the_board_expects(monkeypatch):
    seen = _capture(monkeypatch)
    tools.syslog("event=landing pr=1")
    assert seen["argv"][:3] == ["logger", "-t", "landing-annotation"]


def _fake_fuser_then_ps(monkeypatch, fuser_stdout: str, ps_stdout: str = ""):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if argv[0] == "fuser":
            return subprocess.CompletedProcess(argv, 0, fuser_stdout, "")
        return subprocess.CompletedProcess(argv, 0, ps_stdout, "")

    monkeypatch.setattr(tools.subprocess, "run", run)
    return calls


def test_lock_holder_names_the_pid_alongside_etimes_and_command(monkeypatch):
    """bash's printed `lock held by pid $pid (etimes, command): ...` line named the pid
    separately from the etimes+command ps read; this single string is the only value
    callers get, so the pid must be folded in here rather than dropped (#1085 item 3)."""
    _fake_fuser_then_ps(monkeypatch, "12345", "42 ansible-playbook --tags sonarr")
    assert (
        tools.lock_holder()
        == "pid 12345 (etimes, command): 42 ansible-playbook --tags sonarr"
    )


def test_lock_holder_is_empty_when_nobody_holds_the_lock(monkeypatch):
    _fake_fuser_then_ps(monkeypatch, "")
    assert tools.lock_holder() == ""


# -- the in-flock waits the wrappers report on their own stderr -------------------------
#
# `retry_while_locked` books a wait only when an attempt EXITS 75. deploy.sh waits inside
# `flock -w` and returns 0, and gitops_tick.sh joins a tick already in flight and returns 0,
# so both waits were invisible to the ledger. Each rule below has both halves: a line that
# books and a line that does not.


def test_the_deploy_acquire_line_books_its_seconds_and_its_holder():
    assert tools.in_flock_wait(
        "deploy: lock acquired after 412s (holder was: pid 8 (etimes, command): 9 ansible)\n"
    ) == (412, "pid 8 (etimes, command): 9 ansible")


def test_an_uncontended_acquire_line_books_no_holder():
    assert tools.in_flock_wait("deploy: lock acquired after 0s\n") == (0, "")


def test_the_joined_tick_line_books_the_wait_and_not_the_run_before_it():
    """`N` is how long the tick had already run; only `M` is time THIS landing waited.

    Booking `N` would push `lock` above `tick`, breaking the sub-part invariant the ledger
    docstring states.
    """
    assert tools.in_flock_wait(
        "gitops_tick: joined a tick already 300s in flight; waited 47s for it\n"
    ) == (47, "")


def test_a_self_started_tick_wait_line_books_nothing():
    """The half that must NOT book: those seconds are the tick's own work.

    When the unit's own `flock -w` gives up, gitops_tick.sh exits 3 and
    `retry_while_locked` books the wait already. Booking this line too would double it.
    """
    assert tools.in_flock_wait("gitops_tick: waited 240s\n") is None


def test_an_ordinary_stderr_line_books_nothing():
    assert tools.in_flock_wait("TASK [k8s/sonarr : render manifests] ****\n") is None


def test_a_quote_in_the_holder_cannot_break_the_logfmt_field():
    """`annotation_line` wraps the holder in `holder="..."`, so a quote splits the row."""
    booked = tools.in_flock_wait(
        'deploy: lock acquired after 9s (holder was: sh -c "x")\n'
    )
    assert booked == (9, "sh -c x")


def test_a_long_holder_is_capped_the_way_lock_holder_caps_its_own():
    line = f"deploy: lock acquired after 1s (holder was: {'a' * 500})\n"
    booked = tools.in_flock_wait(line)
    assert booked is not None and len(booked[1]) == tools.HOLDER_MAX


def test_every_stderr_line_is_echoed_through_unchanged(tmp_path, capsys):
    """The landing log must read exactly as it did when the child owned the handle.

    The payload carries a `%`, a double quote and a bare `\\r` progress line, which are what
    text-mode translation or a format string would mangle.
    """
    payload = 'TASK [x] ***\nok: 50% "done"\rok: 100% "done"\nno trailing newline'
    written = tmp_path / "stderr.txt"
    written.write_text(payload)
    rc = tools.stream_stderr(
        ["sh", "-c", 'cat "$0" >&2; exit 7', str(written)], tmp_path, None
    )
    assert rc == 7
    assert capsys.readouterr().err == payload


def test_a_watched_stderr_line_reaches_the_observer(tmp_path, capsys):
    booked: list[tuple[int, str]] = []
    tools.stream_stderr(
        [
            "sh",
            "-c",
            "printf 'TASK [x]\\ndeploy: lock acquired after 5s\\n' >&2",
        ],
        tmp_path,
        lambda s, h: booked.append((s, h)),
    )
    assert booked == [(5, "")]
    assert "TASK [x]" in capsys.readouterr().err


def test_a_joined_line_books_its_wait_even_when_the_in_flight_seconds_are_missing():
    """The in-flight seconds are NOT booked, so an unreadable one must not cost the one that is.

    gitops_tick.sh derives that number from /proc/uptime and renders `already s in flight` if
    the read comes back empty. A parser that required it to be a number stopped matching and
    silently restored `lock=0`.
    """
    assert tools.in_flock_wait(
        "gitops_tick: joined a tick already s in flight; waited 47s for it\n"
    ) == (47, "")


def test_a_kicked_tick_passes_no_wait(monkeypatch):
    """The landing deploys the merge commit itself, so it starts the tick and moves on."""
    seen = _capture(monkeypatch)
    tools.run_tick(wait=False)
    assert seen["argv"] == [str(tools.HERE / "gitops_tick.sh"), "--no-wait"]


def test_a_deploy_of_a_named_commit_passes_at(monkeypatch):
    """Both halves in one pair with the test below: `--at` appears only when asked for."""
    seen = _capture(monkeypatch)
    tools.run_deploy(Path("/primary"), ["sonarr"], None, at="c0ffee")
    assert seen["argv"] == [
        "./scripts/deploy.sh",
        "--tags",
        "sonarr",
        "--at",
        "c0ffee",
    ]


def test_a_deploy_of_the_primary_checkout_passes_no_at(monkeypatch):
    seen = _capture(monkeypatch)
    tools.run_deploy(Path("/primary"), ["sonarr"], "daniel-pi")
    assert seen["argv"] == [
        "./scripts/deploy.sh",
        "--tags",
        "sonarr",
        "-e",
        "target=daniel-pi",
    ]


def _commit(repo: Path, name: str) -> str:
    """Commit `name` into `repo` and return the new HEAD, with GIT_* scrubbed."""
    env = git_free_env(
        GIT_AUTHOR_NAME="t",
        GIT_COMMITTER_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )

    def run(*args: str) -> str:
        return subprocess.run(
            args, cwd=repo, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    (repo / name).write_text(name)
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", name, "--no-gpg-sign")
    return run("git", "rev-parse", "HEAD")


def _snapshot_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """A two-commit repo and a tree-lock path inside tmp_path; (repo, first, second, lock).

    The lock path is handed to the test that holds it while a snapshot is taken. It points
    inside tmp_path, never at the production file a live gitops tick holds.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ("git", "init", "-q", "-b", "master"),
        cwd=repo,
        env=git_free_env(),
        check=True,
        capture_output=True,
    )
    first = _commit(repo, "one")
    second = _commit(repo, "two")
    return repo, first, second, str(tmp_path / "tree.lock")


def test_the_gate_snapshot_is_a_worktree_of_the_named_commit(tmp_path):
    """CLEAN half: the gate renders the commit that was deployed, not the checkout's HEAD."""
    repo, first, second, _lock = _snapshot_repo(tmp_path)
    with tools.gate_snapshot(repo, first) as snap:
        assert snap is not None
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=snap,
            env=git_free_env(),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == first and head != second
        assert os.environ[tools.UV_PROJECT_ENVIRONMENT] == str(repo / ".venv")
        assert (snap / "one").exists()
    assert not snap.exists()
    assert tools.UV_PROJECT_ENVIRONMENT not in os.environ
    # And it deregistered itself, or the next `git worktree add` in this repo trips over it.
    listed = subprocess.run(
        ("git", "worktree", "list"),
        cwd=repo,
        env=git_free_env(),
        check=True,
        capture_output=True,
        text=True,
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
    the gap back on the likeliest path: `gitops-deploy.service` holds this lock for its whole
    unit run, so the tick a landing races is exactly when the gate would have read `skipped`.
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
    with pytest.raises(KeyboardInterrupt):
        with tools.gate_snapshot(repo, first) as snap:
            kept = snap
            os.kill(os.getpid(), signal.SIGTERM)
    assert not kept.exists()
    assert signal.getsignal(signal.SIGTERM) is before
    listed = subprocess.run(
        ("git", "worktree", "list"),
        cwd=repo,
        env=git_free_env(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(kept) not in listed


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
    assert f"rm -rf {tools.GATE_TMP_ROOT}/land-gate-*" in tools.gate_snapshot.__doc__


def test_the_watched_tick_inherits_the_working_directory(monkeypatch):
    """`cwd=None`, not `HERE`.

    The SCRIPT comes from beside land.py (issue #851), but pinning the working directory is
    the re-aiming tools.py's own docstring warns about -- deploy.sh renders from its cwd and
    deploy_tags reads ranges relative to it.
    """
    seen = _capture(monkeypatch)
    tools.run_tick(observe=lambda *_: None)
    assert seen["argv"] == [str(tools.HERE / "gitops_tick.sh")]
    assert seen["cwd"] is None
