#!/usr/bin/env python3
"""Tests for the SessionStart health-banner hook.

The hook must: stay silent when all-green, surface down scrape targets when they exist, and
degrade a failed probe to a skip. A bug outside the probes exits non-zero, which
`run-hook.sh` reports. We exercise the pure helpers directly and stub `_run` so the suite needs no live
Prometheus.

Run: uv run pytest .claude/hooks
"""

import functools
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook_common import parse_payload

from lib.proc_testing import fake_bin, path_with, write_exec

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session-health.py"
)
_spec = importlib.util.spec_from_file_location("session_health", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_a_crash_in_main_exits_non_zero_so_run_hook_can_report_it(tmp_path):
    """`run-hook.sh` reports a crash only on a non-zero exit, so main's raise must escape.

    The copy sits beside a stub `_hook_common` whose `read_payload` raises, the first call
    `main` makes, so nothing on the host is read.
    """
    copy = tmp_path / "session-health.py"
    shutil.copy(_HOOK, copy)
    (tmp_path / "_hook_common.py").write_text(
        "def primary_checkout():\n    return None\n\n"
        "def tagged(hook, text):\n    return text\n\n"
        "def read_payload():\n    raise RuntimeError('payload read broke')\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(copy)],
        input="{}",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode != 0
    assert proc.stderr.strip().splitlines()[-1] == "RuntimeError: payload read broke"


def test_banner_empty_when_no_problems():
    assert _mod.format_banner() == []


def test_banner_lists_problems_and_triage():
    out = _mod.format_banner(targets=["  ✗ target jellyfin [x] down"])
    assert out[0] == "Homelab health check found issues:"
    assert "jellyfin" in out[1]
    assert "triage" in out[-1]


def test_a_branch_only_banner_prints_no_triage_line():
    out = _mod.format_banner(master_moved=["  ⚠ this branch is 3 commits behind"])
    assert out == [
        "Homelab health check found issues:",
        "  ⚠ this branch is 3 commits behind",
    ]


# The scrape-target section moved to hooklib/service_lines.py — its tests are in the sibling
# test_hooklib_service_lines.py, driven through that module's seams.


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
    checkout's `git status` (dirty while anyone edits it by hand), this machine's worktrees, GitHub, and `~/.claude/fanout`. They go in as
    main()'s keyword seams rather than as patches, because the monkeypatch ratchet
    (tests/_ratchet.py) caps a test module at zero patches on a first-party module.
    """
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    payload = json.loads(stdin)
    return functools.partial(
        _mod.main,
        read_payload=lambda: parse_payload(payload),
        target_problems=lambda: targets or [],
        master_moved_problems=lambda: master_moved or [],
        dirty_primary_problems=lambda: parked or [],
        other_live_sessions=lambda cwd: sessions or [],
        unlabelled_worktree_lines=lambda cwd: [],
        stale_worktree_lines=lambda repo: worktrees or [],
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
        monkeypatch, '{"source":"startup"}', sessions=["  • other-branch — k8s/roles/x"]
    )
    assert run_main() == 0
    out = capsys.readouterr().out
    assert "Other Claude sessions" in out and "other-branch" in out


def test_main_stays_silent_when_no_other_session_is_live(monkeypatch, capsys):
    run_main = _run_main(monkeypatch, '{"source":"startup"}', sessions=[])
    assert run_main() == 0
    assert capsys.readouterr().out == ""


def test_main_is_clean_when_the_parked_checkout_probe_is_quiet(monkeypatch, capsys):
    """main() prints nothing when dirty_primary_problems() finds no dirty checkout or park."""
    run_main = _run_main(monkeypatch, '{"source":"startup"}', parked=[])
    assert run_main() == 0
    assert capsys.readouterr().out == ""


def test_main_is_flagged_when_the_parked_checkout_probe_reports(monkeypatch, capsys):
    """main() prints the banner when dirty_primary_problems() finds a dirty primary checkout.

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


def test_a_parked_checkout_only_banner_names_probe_landing(monkeypatch, capsys):
    """A banner holding only the parked-checkout line prints `probe.py landing`."""
    run_main = _run_main(
        monkeypatch,
        '{"source":"startup"}',
        parked=["  ✗ homelab-checkout-sync has not fast-forwarded for 40 min ..."],
    )
    assert run_main() == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"][
        "additionalContext"
    ]
    triage = context.splitlines()[-1]
    assert "`uv run python scripts/diagnostics/probe.py landing`" in triage


def test_a_target_only_banner_omits_landing_and_joins_commands_with_or():
    triage = _mod.format_banner(targets=["  ✗ target x [y] down"])[-1]
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
            "WARN: 1 merged worktree(s) can be removed:",
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


def test_the_banner_requires_the_interface_the_library_does():
    from hooklib import worktree_lines
    from lib._claude_worktree import REQUIRED_INTERFACE

    assert worktree_lines.PRUNER_INTERFACE == REQUIRED_INTERFACE


def _hook_output(capsys):
    out = json.loads(capsys.readouterr().out)
    return out["hookSpecificOutput"]["additionalContext"], out.get("systemMessage", "")


def test_main_prints_the_health_lines_before_the_session_list(monkeypatch, capsys):
    run_main = _run_main(
        monkeypatch,
        '{"source":"startup"}',
        targets=["  ✗ target loki [loki:3100] down"],
        sessions=["  • other-branch — k8s/roles/x"],
        worktrees=["WARN: 1 merged worktree(s) can be removed:"],
    )
    assert run_main() == 0
    context, operator = _hook_output(capsys)
    assert context.index("loki") < context.index("other-branch")
    assert "merged worktree" in operator


def test_main_cuts_the_agent_context_to_its_budget_and_keeps_the_health_lines(
    monkeypatch, capsys
):
    sessions = [f"  • worktree-fanout-{n} — {'k8s/roles/x/' * 4}" for n in range(60)]
    run_main = _run_main(
        monkeypatch,
        '{"source":"startup"}',
        targets=["  ✗ target loki [loki:3100] down"],
        sessions=sessions,
        worktrees=["WARN: 1 merged worktree(s) can be removed:"],
    )
    assert run_main() == 0
    context, operator = _hook_output(capsys)
    assert len(context.encode()) <= _mod.BANNER_BUDGET_BYTES
    assert "loki" in context
    assert context.splitlines()[-1].endswith("cut at the banner's 2000-byte budget")
    assert "merged worktree" in operator
    assert context.startswith("[session-health] ")
    assert operator.startswith("[session-health] ")


# `other_live_sessions` imports lib.worktrees off a hand-built sys.path. That path must point
# at scripts/, which holds lib/, and a wrong path fails silently: the except returns
# [], which reads identically to "no other sessions are running". These two pin the import and
# the fail-loud, because a silent [] is what hid the bug.
@pytest.fixture
def _fence_worktree_survey(fenced_calls, tmp_path, monkeypatch):
    """Keep the hook off the host's worktree pruner and its survey cache.

    A scratch `CLAUDE_CONFIG_DIR` holds a stand-in pruner, and a scratch `XDG_CACHE_HOME`
    holds a fresh, clean survey, so the hook starts no refresh and writes nothing outside
    `tmp_path`.

    A `systemctl` stub answers the live-session scan's fan-out unit queries `inactive`, the
    state of a collected batch unit, so the host's own fan-out units stay unasked.
    """
    write_exec(tmp_path / "claude" / "hooks" / "prune-worktrees.py", "#!/bin/sh\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    survey = tmp_path / "cache" / "session-health" / "worktree-survey.txt"
    survey.parent.mkdir(parents=True)
    survey.write_text("exit 0\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    stub_dir = fake_bin(
        tmp_path / "systemctl-stub", systemctl="#!/bin/sh\necho inactive\n"
    )
    monkeypatch.setenv("PATH", path_with(stub_dir))
    return fenced_calls


def test_the_hook_can_import_the_worktree_library_when_run_as_a_subprocess(
    _fence_worktree_survey,
):
    """Accept case, and it MUST be a subprocess with a clean PYTHONPATH.

    Calling `other_live_sessions()` in-process proves nothing here: pyproject's pytest
    `pythonpath` lists `scripts`, so `lib.worktrees` imports under the suite no matter
    what path the hook inserts. An in-process version of this test passed with the original bug
    reintroduced — verified, not assumed. SessionStart runs this file as a plain script with no
    such path, which is the only condition under which the insert is load-bearing. This is the
    repo rule about verifying a moved entry point by RUNNING it rather than by running the suite.

    Deliberately a real subprocess reaching real external binaries: `curl` (via
    `uv run probe.py targets`). The `_fence_external_binaries` conftest fixture
    stubs them so this runs without side effects, and the calls file below is the proof that
    the fence held. `_fence_worktree_survey` keeps the hook off the host's pruner and cache.
    """
    fenced_calls = _fence_worktree_survey
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
    # target_problems() runs unconditionally on every call to main(), so its binary is
    # always exercised — unlike gh, which only fires per worktree on disk and so isn't
    # asserted here. A stub silently dropped from PATH is
    # indistinguishable from a passing run without this: the real binaries would just run
    # underneath it.
    recorded = fenced_calls.read_text()
    assert "curl" in recorded, (
        "the real `curl` ran instead of the stub — the PATH fence did not hold.\n"
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
    # `-S`: without it the venv's `.pth` supplies this checkout's `scripts/`, which the copy
    # lacks, so the other-session section would read the real worktrees.
    proc = subprocess.run(
        [sys.executable, "-S", str(_hook_copy_with_a_broken_hooklib(tmp_path))],
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
