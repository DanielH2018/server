"""The parameterised shell entry point every hook is registered through (#3278).

Six per-hook shims came in three variants that differed only in flags: a quiet
`uv run --no-project` for the observability hooks, a `cd` + `exec` for the bridge, and the
same with an `ask` on failure for the deny guards. `run-hook.sh <name> [--project]
[--ask-on-cd[=<guards>]]` is the one file, and the interpreter pin is written once.

`test_hook_shim_fail_open.py` owns the per-shim census and skips this file deliberately: a
shim's posture is a property of the file, where the runner's is a property of the flags a
registration passes it. So every check here is a REJECT/ACCEPT pair over one INVOCATION —
REJECT is a run that cannot reach the `.py` (a missing `cd` target, a missing sibling), ACCEPT
is one that can.

The runner is unreferenced on purpose while this lands. `.claude/settings.json` names each hook
by an absolute path into the PRIMARY checkout, so a settings file that switched to `run-hook.sh`
in the same commit would register a script a behind primary checkout does not have — `/bin/sh`
exits 127 and the matching tool calls run with the guard skipped, which is the incident
`hooklib/hook_registration_lines.py` exists for. The switch is the second half of #3278.

Run: uv run pytest .claude/hooks/tests/test_run_hook.py
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lib.proc_testing import run

HOOKS = Path(__file__).resolve().parent.parent
REPO = HOOKS.parent.parent
RUNNER = HOOKS / "run-hook.sh"

# The flag sets the six shims collapse into. Named rather than counted, so a variant that
# stops being exercised fails here instead of thinning the coverage in silence.
VARIANTS = {
    "quiet": [],
    "project": ["--project"],
    "ask": ["--project", "--ask-on-cd"],
}

# The host facts run-hook.sh hard-codes. A GitHub runner has none of them: no primary checkout,
# no uv at this install path, and setup-python's patch release rather than the hosts' pin, which
# `--no-python-downloads` will not fetch. The quiet variant sends uv's stderr to /dev/null, so a
# missing one reads as a hook that printed nothing rather than as an error.
_PROJECT_DIR = "/home/ubuntu/server"
_UV = "/home/ubuntu/.local/bin/uv"
_PIN = re.compile(r"--python [0-9][0-9.]*")


def _variant_runner(tmp_path: Path, cd_target: str) -> Path:
    """A throwaway copy of the runner that depends on nothing of the host's.

    Its `cd` target becomes `cd_target`, its uv the one on PATH, and its interpreter pin the
    interpreter running this test: the swap `test_hook_shim_fail_open.py` made for the shims'
    `cd` target, extended to the runner's other two host facts. The copy sits in `tmp_path`,
    so the runner's `HOOKS_DIR` resolves there and only a `.py` the test writes can run.
    """
    text = RUNNER.read_text(encoding="utf-8")
    for host_fact in (f"cd {_PROJECT_DIR} ||", f"exec {_UV} run", f"\n{_UV} run"):
        assert host_fact in text, f"run-hook.sh no longer holds {host_fact!r}"
    # Checked before the swap, because the runner-side values can live under /home/ubuntu too.
    unswapped = text.replace(_PROJECT_DIR, "").replace(_UV, "")
    assert "/home/ubuntu" not in unswapped, (
        "run-hook.sh has a host path this copy keeps"
    )
    uv = shutil.which("uv")
    assert uv, "uv is not on PATH"
    text = text.replace(_PROJECT_DIR, cd_target).replace(_UV, uv)
    text, pins = _PIN.subn(f"--python {sys.executable}", text)
    assert pins == 1, f"expected one interpreter pin in run-hook.sh, found {pins}"
    path = tmp_path / "run-hook.sh"
    path.write_text(text, encoding="utf-8")
    return path


def _run(runner: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(runner), *args],
        input="",
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_runner_is_executable_in_git():
    """A hook committed 100644 is silently dead once settings.json names it.

    `test_hook_scripts_executable.py` cannot see this one: it anchors on the scripts
    settings.json REGISTERS, and the runner is deliberately unregistered until the second half
    of #3278. `uv-python.sh` shipped 100644 in #361 the same way.
    """
    out = run(
        ["git", "ls-files", "-s", "--", ".claude/hooks/run-hook.sh"],
        cwd=REPO,
        check=True,
    ).stdout
    assert out.strip(), "run-hook.sh is not tracked in git"
    assert out.split()[0] == "100755", f"git mode {out.split()[0]}, expected 100755"


def test_the_decided_marker_records_the_trade_off():
    """So a future session does not read either posture as an oversight and 'fix' it back."""
    text = RUNNER.read_text(encoding="utf-8")
    assert "# DECIDED:" in text
    assert "fail-open" in text
    for issue in ("#1014", "#2171", "#2394", "#3278"):
        assert issue in text, issue


# --- REJECT: the invocation cannot reach the `.py` ----------------------------------------


@pytest.mark.parametrize("flags", sorted(VARIANTS), ids=sorted(VARIANTS))
def test_reject_a_missing_py_sibling_reports_on_stderr(tmp_path, flags):
    """The failure that is invisible from the session side: the shim RUNS, nothing exits 127,
    and the guard is skipped behind a hook that reports no error. The copy's `cd` succeeds, so
    the flagged variants reach the file check rather than stopping at the `cd` arm."""
    runner = _variant_runner(tmp_path, str(tmp_path))
    proc = _run(runner, "no-such-hook", *VARIANTS[flags])
    assert proc.returncode == 0, proc.stderr
    assert "did not run" in proc.stderr
    assert "no-such-hook.py" in proc.stderr


def test_reject_a_missing_cd_target_reports_on_stderr(tmp_path):
    runner = _variant_runner(tmp_path, str(tmp_path / "does-not-exist"))
    proc = _run(runner, "bash-pretool", "--project")
    assert proc.returncode == 0, proc.stderr
    assert "did not run" in proc.stderr
    assert "bash-pretool" in proc.stderr


def test_reject_a_deny_guard_that_cannot_run_asks(tmp_path):
    """A bare exit 0 from a DENY guard is an allow, so `--ask-on-cd` turns it into a prompt."""
    runner = _variant_runner(tmp_path, str(tmp_path / "does-not-exist"))
    proc = _run(runner, "block-protected-edits", "--project", "--ask-on-cd")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "ask"
    assert "block-protected-edits" in out["permissionDecisionReason"]


def test_reject_a_missing_py_sibling_also_asks(tmp_path):
    """New in #3278. It used to exit non-zero through a failed `exec`, where no `ask` could
    follow; the runner tests for the file first, so the decision is still available."""
    runner = _variant_runner(tmp_path, str(tmp_path))
    proc = _run(runner, "no-such-hook", "--project", "--ask-on-cd")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"
    assert "no-such-hook.py" in out["permissionDecisionReason"]


def test_the_ask_reason_names_every_guard_that_did_not_run(tmp_path):
    """One runner stands in for several guards, so the operator can only act on the prompt if
    the reason says which ones. `bash-pretool.sh` already answered for three."""
    guards = ("block-protected-bash", "nudge-land-sh", "block-footguns")
    runner = _variant_runner(tmp_path, str(tmp_path / "does-not-exist"))
    proc = _run(runner, "bash-pretool", f"--ask-on-cd={','.join(guards)}")
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    for guard in guards:
        assert guard in reason, guard


def test_a_hook_name_carrying_a_quote_never_reaches_the_ask(tmp_path):
    """The other half of the constraint: the NAME is interpolated into the reason too.

    Unparsable JSON from a DENY guard reads to the harness as no decision, which is the
    silent allow `--ask-on-cd` exists to prevent — so a name that could break the string
    stops the runner before it emits one.
    """
    runner = _variant_runner(tmp_path, str(tmp_path / "does-not-exist"))
    proc = _run(runner, 'a" injected "b', "--project", "--ask-on-cd")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert "is not a hook name" in proc.stderr


def test_a_reason_argument_that_is_not_a_hook_name_falls_back(tmp_path):
    """The argument reaches a JSON string, so it is constrained rather than escaped. The
    near miss: a value carrying a quote must not produce unparsable output."""
    runner = _variant_runner(tmp_path, str(tmp_path / "does-not-exist"))
    proc = _run(runner, "bash-pretool", '--ask-on-cd=a" injected "b')
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"
    assert "injected" not in out["permissionDecisionReason"]
    assert "bash-pretool" in out["permissionDecisionReason"]


@pytest.mark.parametrize(
    "args,expected",
    [
        ([], "no hook name"),
        (["--no-such-flag"], "unknown flag"),
        (["one", "two"], "more than one hook name"),
        (['a"b'], "is not a hook name"),
        (["../elsewhere/x"], "is not a hook name"),
    ],
)
def test_reject_an_unusable_command_line_says_so_and_exits_zero(args, expected):
    """A runner that cannot tell which hook it is must not guess, and must not block."""
    proc = _run(RUNNER, *args)
    assert proc.returncode == 0, proc.stderr
    assert expected in proc.stderr
    assert proc.stdout == ""


# --- ACCEPT: the invocation reaches the `.py` ---------------------------------------------


def test_accept_the_quiet_variant_runs_its_hook_and_stays_silent(tmp_path):
    """The posture for SessionStart / InstructionsLoaded / Stop: never blocks, never errors."""
    (tmp_path / "probe.py").write_text("print('ran')\n", encoding="utf-8")
    runner = _variant_runner(tmp_path, str(tmp_path))
    (tmp_path / "run-hook.sh").write_text(
        runner.read_text(encoding="utf-8"), encoding="utf-8"
    )
    proc = _run(tmp_path / "run-hook.sh", "probe")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ran"
    assert "did not run" not in proc.stderr


def test_accept_the_project_variant_execs_its_hook(tmp_path):
    """`exec` is what keeps the hook's stdin JSON; the hook's own stdout is the evidence."""
    (tmp_path / "probe.py").write_text("print('ran')\n", encoding="utf-8")
    runner = _variant_runner(tmp_path, str(tmp_path))
    proc = _run(runner, "probe", "--project")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ran"
    assert "did not run" not in proc.stderr


def test_accept_a_valid_cd_target_emits_no_ask(tmp_path):
    """The near miss for the deny-guard variant: `cd` succeeds, so no prompt is raised."""
    (tmp_path / "probe.py").write_text("", encoding="utf-8")
    runner = _variant_runner(tmp_path, str(tmp_path))
    proc = _run(runner, "probe", "--project", "--ask-on-cd")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert "did not run" not in proc.stderr


def test_the_runner_runs_the_py_beside_itself_not_beside_the_cwd(tmp_path):
    """`$(dirname "$(readlink -f "$0")")` is the RESOLVED runner's directory.

    `settings.json` names it by an absolute path into the primary checkout, so a session in a
    worktree must still get the primary checkout's `.py` — resolving against the cwd would
    silently run a different checkout's guard.
    """
    (tmp_path / "probe.py").write_text(
        "print('from the runner dir')\n", encoding="utf-8"
    )
    runner = _variant_runner(tmp_path, str(tmp_path))
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "probe.py").write_text("print('from the cwd')\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(runner), "probe"],
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        cwd=decoy,
    )
    assert proc.stdout.strip() == "from the runner dir", proc.stderr
