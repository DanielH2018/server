#!/usr/bin/env python3
"""Tests for the SessionStart health-banner hook.

The hook must: stay silent when all-green, surface down scrape targets when they exist, and
never raise. We exercise the pure helpers directly and stub `_run` so the suite needs no live
Prometheus.

Run: uv run pytest .claude/hooks
"""

import functools
import importlib.util
import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session-health.py"
)
_spec = importlib.util.spec_from_file_location("session_health", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _result(stdout, returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def test_banner_empty_when_no_problems():
    assert _mod.format_banner([]) == ""


def test_banner_lists_problems_and_triage():
    out = _mod.format_banner(["  ✗ jellyfin — unhealthy (x)"])
    assert "issues detected" in out
    assert "jellyfin" in out
    assert "triage" in out  # always points the reader at the probe commands


# The scrape-target and stale-release sections moved to hooklib/service_lines.py — their tests
# are in the sibling test_hooklib_service_lines.py, driven through that module's seams.


# `master_moved_problems` is tested in the sibling test_session_health_master_moved.py --
# it moved there when this file hit its line cap.
def _run_main(
    monkeypatch,
    stdin,
    *,
    targets=None,
    master_moved=None,
    parked=None,
    env=None,
    sessions=None,
    worktrees=None,
    remote_fanout=None,
):
    """Wire up main()'s dependencies; returns a callable, `_run_main(...)()`, rather than
    calling main() itself, so a test can override one more seam as a keyword argument.

    Every probe is a fake, because each real one reads live state that would make a main()
    assertion depend on the host: this checkout's distance from origin/master, the PRIMARY
    checkout's `git status` (genuinely dirty while docs-refresh runs the suite over the pages
    it just staged), this machine's worktrees, GitHub, and `~/.claude/fanout`. They go in as
    main()'s keyword seams rather than as patches, because the monkeypatch ratchet
    (ansible/tests/_ratchet.py) caps a test module at zero patches on a first-party module.
    """
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    payload = json.loads(stdin)
    return functools.partial(
        _mod.main,
        read_payload=lambda: payload,
        target_problems=lambda: targets or [],
        master_moved_problems=lambda: master_moved or [],
        parked_deployer_problems=lambda: parked or [],
        other_live_sessions=lambda cwd: sessions or [],
        stale_worktree_lines=lambda: worktrees or [],
        remote_fanout_lines=lambda: remote_fanout or [],
    )


def test_main_silent_on_compact(monkeypatch, capsys):
    run_main = _run_main(
        monkeypatch,
        '{"source":"compact"}',
        targets=["  ✗ target loki [loki:3100] down"],
        env={"SESSION_HEALTH_VERBOSE": "1"},
    )
    assert run_main() == 0
    assert capsys.readouterr().out == ""  # no re-banner mid-session


def test_main_silent_when_green(monkeypatch, capsys):
    run_main = _run_main(monkeypatch, '{"source":"startup"}')
    assert run_main() == 0
    assert capsys.readouterr().out == ""


def test_main_lists_other_sessions_even_when_green(monkeypatch, capsys):
    # another session's open work is information this session needs whether or not the
    # homelab itself is healthy, so it is not gated on the health banner
    run_main = _run_main(
        monkeypatch, '{"source":"startup"}', sessions=["  • other-branch — roles/k8s/x"]
    )
    assert run_main() == 0
    out = capsys.readouterr().out
    assert "Other Claude sessions" in out and "other-branch" in out


def test_main_stays_silent_when_no_other_session_is_live(monkeypatch, capsys):
    run_main = _run_main(monkeypatch, '{"source":"startup"}', sessions=[])
    assert run_main() == 0
    assert capsys.readouterr().out == ""


def test_main_is_clean_when_the_parked_deployer_probe_is_quiet(monkeypatch, capsys):
    """main() prints nothing when parked_deployer_problems() finds no dirty checkout or park."""
    run_main = _run_main(monkeypatch, '{"source":"startup"}', parked=[])
    assert run_main() == 0
    assert capsys.readouterr().out == ""


def test_main_is_flagged_when_the_parked_deployer_probe_reports(monkeypatch, capsys):
    """main() prints the banner when parked_deployer_problems() finds a dirty primary checkout.

    Paired with the _is_clean case above: this is the RED proof that isolating this seam in
    _run_main did not just neuter the assert-silence tests — a genuine report from this probe
    still reaches the banner.
    """
    run_main = _run_main(
        monkeypatch,
        '{"source":"startup"}',
        parked=["  ✗ primary checkout /home/ubuntu/server is dirty — ..."],
    )
    assert run_main() == 0
    assert "primary checkout" in capsys.readouterr().out


def test_a_deployer_only_banner_names_probe_landing(monkeypatch, capsys):
    """A banner whose only line is the deployer's prints `probe.py landing` in full (#3936)."""
    run_main = _run_main(
        monkeypatch,
        '{"source":"startup"}',
        parked=["  ✗ the GitOps deployer has not fast-forwarded for 40 min ..."],
    )
    assert run_main() == 0
    triage = capsys.readouterr().out.splitlines()[-1]
    assert "`uv run python scripts/diagnostics/probe.py landing`" in triage


def test_a_target_only_banner_omits_landing_and_joins_commands_with_or():
    triage = _mod.format_banner(["  ✗ target x [y] down"]).splitlines()[-1]
    assert "landing" not in triage
    assert "|" not in triage
    assert (
        "`uv run python scripts/diagnostics/probe.py targets` or "
        "`uv run python scripts/diagnostics/probe.py health <svc>`"
    ) in triage


def test_main_survives_a_broken_session_scan(monkeypatch, capsys):
    # the scan shells out to git in other checkouts; it must never block session start
    def boom(cwd):
        raise OSError("git exploded")

    run_main = _run_main(monkeypatch, '{"source":"startup"}')
    assert run_main(other_live_sessions=boom) == 0
    assert capsys.readouterr().out == ""


def test_main_prints_banner_on_problem(monkeypatch, capsys):
    run_main = _run_main(
        monkeypatch,
        '{"source":"startup"}',
        targets=["  ✗ target loki [loki:3100] down"],
    )
    assert run_main() == 0
    assert "loki" in capsys.readouterr().out


def test_main_actually_calls_the_target_check(monkeypatch, capsys):
    # `_run_main` stubs target_problems to a fixed-return lambda, which a main() that never
    # called it would pass anyway. Override it here so the assertion proves the call happened.
    called = {"targets": False}

    def tp():
        called["targets"] = True
        return ["  ✗ target loki [loki:3100] down"]

    run_main = _run_main(monkeypatch, '{"source":"startup"}')
    assert run_main(target_problems=tp) == 0
    assert called["targets"] is True
    assert "loki" in capsys.readouterr().out


def test_main_prints_master_moved_line(monkeypatch, capsys):
    run_main = _run_main(
        monkeypatch,
        '{"source":"startup"}',
        master_moved=[
            "  ⚠ this branch is 3 commits behind origin/master (as of the last fetch)"
        ],
    )
    assert run_main() == 0
    assert "3 commits behind origin/master" in capsys.readouterr().out


def test_main_prints_the_removable_worktree_lines(monkeypatch, capsys):
    run_main = _run_main(
        monkeypatch,
        '{"source":"startup"}',
        worktrees=[
            "\U0001f9f9 1 merged worktree(s) can be removed:",
            "  old-thing — x",
        ],
    )
    assert run_main() == 0
    assert "old-thing" in capsys.readouterr().out


def test_main_prints_remote_fanout_lines(monkeypatch, capsys):
    run_main = _run_main(
        monkeypatch,
        '{"source":"startup"}',
        remote_fanout=["  • daniel-server worktree-fanout-1345 — #1345 (run r1)"],
    )
    assert run_main() == 0
    assert "daniel-server" in capsys.readouterr().out


def test_stale_worktree_lines_reports_removable():
    brief = (
        "\U0001f9f9 1 merged worktree(s) can be removed:\n"
        "  old-thing — worktree-old-thing merged, clean, unlocked\n"
        "  → uv run python scripts/dev/prune_worktrees.py --prune\n"
    )
    lines = _mod.stale_worktree_lines(lambda *a, **k: _result(brief))
    assert any("old-thing" in line for line in lines)


def test_stale_worktree_lines_silent_when_clean():
    assert _mod.stale_worktree_lines(lambda *a, **k: _result("")) == []


def test_stale_worktree_lines_swallows_a_timeout():
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="prune_worktrees.py", timeout=30)

    assert _mod.stale_worktree_lines(boom) == []


# settings.json kills the hook at its `timeout`, and a kill discards whatever Python still
# buffers — the whole banner, parked-deployer warning included. The worktree read is
# the slow step (it reaches GitHub), so it is bounded inside that budget and runs after a flush.


def test_the_worktree_read_is_bounded_well_inside_the_hook_budget():
    settings = json.loads(
        Path(_HOOK).parent.parent.joinpath("settings.json").read_text(encoding="utf-8")
    )
    budgets = [
        hook["timeout"]
        for group in settings["hooks"]["SessionStart"]
        for hook in group["hooks"]
        if hook["command"].endswith("run-hook.sh session-health")
    ]
    assert budgets, "settings.json registers no session-health SessionStart hook"
    assert _mod.WORKTREE_TIMEOUT_S <= min(budgets) / 2


class _FlushRecorder(io.StringIO):
    """A stdout that snapshots everything written so far each time it is flushed."""

    def __init__(self):
        super().__init__()
        self.flushed = []

    def flush(self):
        self.flushed.append(self.getvalue())
        super().flush()


def test_main_flushes_the_banner_before_the_worktree_read(monkeypatch):
    out = _FlushRecorder()
    monkeypatch.setattr(sys, "stdout", out)
    run_main = _run_main(
        monkeypatch,
        '{"source":"startup"}',
        targets=["  ✗ target loki [loki:3100] down"],
        worktrees=["  old-thing — worktree-old-thing merged"],
    )
    assert run_main() == 0
    assert any("loki" in s and "old-thing" not in s for s in out.flushed)


# `other_live_sessions` imports lib.worktrees off a hand-built sys.path. That path must point
# at scripts/, which holds lib/, and a wrong path fails silently: the except returns
# [], which reads identically to "no other sessions are running". These two pin the import and
# the fail-loud, because a silent [] is what hid the bug.
def test_the_hook_can_import_the_worktree_library_when_run_as_a_subprocess(
    fenced_calls,
):
    """Accept case, and it MUST be a subprocess with a clean PYTHONPATH.

    Calling `other_live_sessions()` in-process proves nothing here: pyproject's pytest
    `pythonpath` lists `scripts`, so `lib.worktrees` imports under the suite no matter
    what path the hook inserts. An in-process version of this test passed with the original bug
    reintroduced — verified, not assumed. SessionStart runs this file as a plain script with no
    such path, which is the only condition under which the insert is load-bearing. This is the
    repo rule about verifying a moved entry point by RUNNING it rather than by running the suite.

    Deliberately a real subprocess reaching real external binaries: `sops` and `curl` (via
    `uv run probe.py targets`), and `gh` (via `uv run prune_worktrees.py`, once per worktree on
    disk). The `_fence_external_binaries` conftest fixture stubs them so this runs neither slow
    nor side-effecting — see fenced_calls below for the proof it held.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, _HOOK],
        input='{"cwd": "%s"}' % _mod.REPO,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert "detection is broken" not in proc.stdout, (
        "session-health.py could not import lib.worktrees or lib.git from the paths it "
        "inserts — a module moved and an insert did not follow it. Fix the path in "
        "other_live_sessions or master_moved_problems (both report through this string).\n"
        + proc.stdout
    )
    # target_problems() runs unconditionally on every call to main(), so sops/curl/journalctl
    # are always exercised — unlike gh, which only fires per worktree on disk and so isn't
    # asserted here. A stub silently dropped from PATH is
    # indistinguishable from a passing run without this: the real binaries would just run
    # underneath it.
    recorded = fenced_calls.read_text()
    for binary in ("sops", "curl", "journalctl"):
        assert binary in recorded, (
            f"the real `{binary}` ran instead of the stub — the PATH fence did not hold.\n"
            f"recorded calls:\n{recorded}"
        )


def test_a_broken_import_is_reported_rather_than_read_as_no_sessions(monkeypatch):
    """Reject case: when the import fails the banner says so instead of going quiet.

    `sys.modules[name] = None` is what makes `from name import ...` raise ImportError even
    after the accept case above has imported it for real, because the module stays cached.
    """
    import sys

    monkeypatch.setitem(sys.modules, "lib.worktrees", None)
    lines = _mod.other_live_sessions("/nonexistent-repo-root")
    assert lines, (
        "a failed import returned no lines — indistinguishable from 'no other sessions'"
    )
    assert "other-session detection is broken" in lines[0]


_OTHER_WORKTREE_PORCELAIN = (
    "worktree /repo\nHEAD aaa\nbranch refs/heads/master\n\n"
    "worktree /repo/.claude/worktrees/other\nHEAD bbb\nbranch refs/heads/feature\n"
    "locked some-other-session\n\n"
)


def _fake_run_for_other_sessions(diff_stdout):
    def fake(cmd, *_args, **_kwargs):
        return _result(
            _OTHER_WORKTREE_PORCELAIN if cmd[:2] == ["git", "worktree"] else diff_stdout
        )

    return fake


# The per-tree dirty check delegates to `lib.git.git_dirty`, not its own `git status
# --porcelain` read. Pins "(+ uncommitted)" to follow git_dirty's answer, including a
# git_dirty failure (tree vanished mid-scan) degrading to not-dirty rather than crashing the
# section. `run` and `git_dirty` are `other_live_sessions`' own seams.
def test_other_live_sessions_dirty_marker_follows_lib_git():
    def scan(git_dirty):
        run = _fake_run_for_other_sessions("file.py\n")
        return _mod.other_live_sessions("/repo", run=run, git_dirty=git_dirty)[0]

    assert "(+ uncommitted)" in scan(lambda *a, **k: True)
    assert "(+ uncommitted)" not in scan(lambda *a, **k: False)

    def boom(*a, **k):
        raise subprocess.CalledProcessError(128, ["git", "status"])

    assert "(+ uncommitted)" not in scan(boom)


def _hook_copy_with_a_broken_hooklib(tmp_path):
    """A copy of the hook whose `hooklib.service_lines` will not compile.

    Copied rather than patched because the guard runs at MODULE scope: it has already run
    for `_mod`, so only a fresh interpreter loading a fresh file can exercise it.
    """
    hooks = tmp_path / "repo" / ".claude" / "hooks"
    (hooks / "hooklib").mkdir(parents=True)
    with open(_HOOK) as f:
        (hooks / "session-health.py").write_text(f.read())
    (hooks / "hooklib" / "worktree_lines.py").write_text("")
    (hooks / "hooklib" / "service_lines.py").write_text("def broken(:\n")
    return hooks / "session-health.py"


def test_a_hooklib_that_will_not_compile_is_named_rather_than_killing_the_banner(
    tmp_path,
):
    """A SyntaxError is not an ImportError, so an `except ImportError` alone would let one
    through.

    These modules use PEP 758 syntax, and `run-hook.sh` sends this hook's stderr to /dev/null
    and exits 0. An uncaught SyntaxError at import would take the whole banner out with nothing
    a session could see, and nothing at module scope may do that.
    """
    proc = subprocess.run(
        [sys.executable, str(_hook_copy_with_a_broken_hooklib(tmp_path))],
        input='{"source": "startup"}',
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    assert "hooklib is broken" in proc.stdout
    assert "SyntaxError" in proc.stdout
    # The rest of the banner still ran: this copy's REPO points at a tree with no
    # scripts/, so the other-session section reports its own broken import.
    assert "other-session detection is broken" in proc.stdout
