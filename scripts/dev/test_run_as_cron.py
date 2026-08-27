#!/usr/bin/env python3
"""Tests for the cron-environment reproduction wrapper.

These invoke the script rather than importing anything: the whole point of
`run_as_cron.sh` is the environment it builds for a child process, and that is only
observable by running it. A test that asserted on the script's text would pass while
the environment it constructs was wrong — the argparse-only failure this repo has
already paid for once.

Every check here is paired: an assertion that the wrapper flags the bad case, and an
assertion that it does NOT flag the good one. A guard that fires on everything is
indistinguishable from a guard that fires on nothing.

Run: uv run pytest scripts/dev/test_run_as_cron.sh.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "run_as_cron.sh"

EMPTY_SUCCESS_EXIT = 66


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_script_is_executable() -> None:
    # A wrapper nobody can invoke is a wrapper nobody will use.
    assert SCRIPT.exists(), f"{SCRIPT} is missing"
    assert SCRIPT.stat().st_mode & 0o111, f"{SCRIPT} is not executable"


def test_stdout_passes_through_and_the_exit_code_is_the_command_s() -> None:
    result = run("--", "echo hello")
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_a_failing_command_s_exit_code_survives() -> None:
    result = run("--", "exit 3")
    assert result.returncode == 3


def test_kubeconfig_is_absent_from_the_child_environment() -> None:
    # The documented second trap: an interactive shell exports KUBECONFIG, cron does not,
    # so k3s's kubectl silently falls back to a root-owned 0640 file and returns nothing.
    result = run("--", "echo [${KUBECONFIG-unset}]")
    assert result.returncode == 0
    assert "[unset]" in result.stdout


def test_path_is_cron_s_and_excludes_usr_local_bin() -> None:
    # The documented first trap. /usr/local/bin is where kubectl lives on this host.
    result = run("--", "echo $PATH")
    assert result.returncode == 0
    assert result.stdout.strip() == "/usr/bin:/bin"
    assert "/usr/local/bin" not in result.stdout


def test_the_child_runs_under_sh_not_the_interactive_shell() -> None:
    result = run("--", "echo $0")
    assert result.returncode == 0
    assert result.stdout.strip() == "/bin/sh"


def test_a_shell_builtin_still_runs() -> None:
    # Regression: an `exec "$@"` implementation failed every builtin with a 127 that read
    # exactly like the PATH trap. A crontab line is a shell line, so builtins must work.
    result = run("--", "cd / && echo ok")
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_expect_output_fails_a_silent_success() -> None:
    result = run("--expect-output", "--", "true")
    assert result.returncode == EMPTY_SUCCESS_EXIT
    assert "wrote nothing to stdout" in result.stderr


def test_expect_output_passes_a_command_that_produced_output() -> None:
    # The positive control for the check above. Without this, an --expect-output that
    # rejected everything would look identical to one that works.
    result = run("--expect-output", "--", "echo found-something")
    assert result.returncode == 0
    assert "found-something" in result.stdout


def test_expect_output_does_not_count_stderr_as_output() -> None:
    # The trap's real signature is a stderr warning plus an EMPTY item list. Counting
    # that warning as output would pass exactly the case this flag exists to catch.
    result = run("--expect-output", "--", "echo warning >&2")
    assert result.returncode == EMPTY_SUCCESS_EXIT


def test_no_command_is_a_usage_error_not_a_silent_pass() -> None:
    result = run()
    assert result.returncode == 2
    assert "no command given" in result.stderr
