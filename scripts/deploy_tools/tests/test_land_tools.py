"""The real boundary implementations build the right process, in the right checkout.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tools.py
"""

import subprocess
import sys
from pathlib import Path

from deploy_tools.land_lib import tools


def _capture(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=list(argv), **kw)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tools.subprocess, "run", run)
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
