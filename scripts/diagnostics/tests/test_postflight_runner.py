#!/usr/bin/env python3
"""Tests for postflight.py's runner shell: `main()`, the curl transport and the parser.

The per-check tests are in test_postflight.py. What lives here reaches no service and
decrypts nothing, so it needs none of that module's autouse stubs.

Run: uv run pytest scripts/diagnostics/tests/test_postflight_runner.py
"""

import os
import socket
import subprocess
import sys

import pytest

# `scripts/diagnostics` is deliberately absent from `pythonpath` in pyproject.toml, so this
# module puts its own parent directory on `sys.path` — the insert every sibling here carries.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import postflight


def only_checks(monkeypatch, checks):
    """Run `main()` over `checks` alone, so a test isn't at the mercy of the real registry."""
    monkeypatch.setattr(postflight, "CHECKS", checks)


def stub_curl(monkeypatch, run):
    """Replace the subprocess `get()` shells out to."""
    monkeypatch.setattr(subprocess, "run", run)


def stub_secret(monkeypatch):
    """Every secret decrypts to a placeholder, for a check that reads one before it SKIPs."""
    monkeypatch.setattr(postflight, "secret", lambda name: (f"<{name}>", ""))


# The three tests below pass `main([])` rather than `main()`, and the empty list is
# load-bearing: with no argument argparse falls back to `sys.argv[1:]`, which under pytest is
# PYTEST'S OWN flags. Any invocation carrying a flag postflight does not define exits 2 before
# the check under test runs. That made `pytest_shard.py --record` impossible for the whole
# repo — it runs the suite as `-n0 -vv --durations=0`, so these three failed, and
# `record_weights` refuses to write weights from a suite that did not pass.
def test_a_workload_with_no_service_skips_not_fails(monkeypatch):
    def absent(name):
        raise postflight.Skip(f"{name} has no ClusterIP (does the Service exist?)")

    stub_secret(monkeypatch)
    monkeypatch.setattr(postflight, "service_ip", absent)
    only_checks(
        monkeypatch, [("9.3", "sonarr", lambda: postflight.check_arr_key("sonarr"))]
    )
    assert postflight.main([]) == 0


def test_one_failure_exits_nonzero(monkeypatch):
    only_checks(monkeypatch, [("9.1", "x", lambda: (postflight.FAIL, "broken"))])
    assert postflight.main([]) == 1


def test_check_raising_does_not_abort_the_run(monkeypatch):
    """One check blowing up must not hide the checks after it."""

    def boom():
        raise ValueError("bad json")

    only_checks(
        monkeypatch, [("9.1", "x", boom), ("9.2", "y", lambda: (postflight.OK, "fine"))]
    )
    assert postflight.main([]) == 1


def test_get_parses_status_and_body(monkeypatch):
    class Result:
        returncode = 0
        stdout = '{"a": 1}\n200'
        stderr = ""

    stub_curl(monkeypatch, lambda *a, **kw: Result())
    assert postflight.get("http://x") == (200, '{"a": 1}')


def test_get_reports_curl_failure_as_status_zero(monkeypatch):
    class Result:
        returncode = 7
        stdout = ""
        stderr = "connection refused"

    stub_curl(monkeypatch, lambda *a, **kw: Result())
    assert postflight.get("http://x") == (0, "connection refused")


def test_get_reports_a_curl_that_never_returns_as_status_zero(monkeypatch):
    """The subprocess timeout is what turns a hung curl into a failed check (issue #2156)."""

    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    stub_curl(monkeypatch, hang)
    status, body = postflight.get("http://x", timeout=3)
    assert status == 0
    assert "8s" in body


def test_get_bounds_the_subprocess_beyond_curls_own_max_time(monkeypatch):
    seen = {}

    class Result:
        returncode = 0
        stdout = "\n200"
        stderr = ""

    def fake_run(argv, **kw):
        seen["timeout"] = kw.get("timeout")
        seen["max_time"] = argv[argv.index("--max-time") + 1]
        return Result()

    stub_curl(monkeypatch, fake_run)
    postflight.get("http://x", timeout=3)
    assert seen["max_time"] == "3"
    assert seen["timeout"] is not None and seen["timeout"] > 3


def test_credentials_never_reach_argv(monkeypatch):
    """The auth header goes in on stdin — a secret in argv would land in `ps`."""
    seen = {}

    class Result:
        returncode = 0
        stdout = "\n200"
        stderr = ""

    def fake_run(argv, input=None, **kw):
        seen["argv"] = argv
        seen["input"] = input
        return Result()

    stub_curl(monkeypatch, fake_run)
    postflight.get("http://x", 'header = "X-Api-Key: hunter2"\n')
    assert "hunter2" not in " ".join(seen["argv"])
    assert "hunter2" in seen["input"]


# ── `--help` must not run the sweep (#1685) ──────────────────────────────────────────
# The rejecting half of the pair: before the parser existed, `--help` ran every check —
# several SOPS decrypts and ~15 authenticated requests to production — and exited 0, which
# reads as a passing `--help`. The accepting half is below it: the no-argument invocation is
# still what the parser accepts, so it cannot have swallowed the interface.


def test_help_exits_zero_without_running_a_single_check(capsys, monkeypatch):
    # Pin the colour setting rather than inheriting it. Python 3.14's argparse colourises help
    # when FORCE_COLOR is set, TTY or not, and it wraps `usage: ` and the program name in
    # SEPARATE escape runs — so the phrase below survives in CI (which sets neither variable)
    # and is split on any host whose shell exports FORCE_COLOR. Stating the dependency here is
    # what stops this passing remotely and failing locally (#1727).
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    with pytest.raises(SystemExit) as exc:
        postflight.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: postflight.py" in out
    # A check that ran would have printed its own `[OK  ] §9.x ...` report line first. The
    # help text itself cites §9, so the status bracket is what distinguishes them.
    assert not [status for status in ("[OK", "[FAIL", "[SKIP") if status in out]


def test_no_arguments_is_still_the_whole_interface():
    """The parse must not exit, or the no-argument sweep would stop working."""
    assert vars(postflight.build_parser().parse_args([])) == {}


def test_a_host_in_no_known_cluster_skips_its_kubectl_checks(monkeypatch, capsys):
    """#2069: the Pi is a node of neither cluster, so a kubectl-reaching check SKIPs, not FAILs.

    A raise here would count as the check's own verdict — `MissingKubectl` on the Pi, or
    `WrongCluster` on a node the table does not list — and fail the run.
    """
    stub_secret(monkeypatch)
    monkeypatch.setattr(socket, "gethostname", lambda: "daniel-pi")
    only_checks(
        monkeypatch, [("9.3", "sonarr", lambda: postflight.check_arr_key("sonarr"))]
    )
    assert postflight.main([]) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert line.startswith("[SKIP]")
    assert "daniel-pi is a node of no known cluster" in line
