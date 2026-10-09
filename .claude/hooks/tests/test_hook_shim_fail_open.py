"""Every registered hook's `cd /home/ubuntu/server || …` arm, run as settings.json writes it.

Issue #1014: a hook that cannot change into the repo was fail-open (exit 0, normal permission
flow), which is defensible for a permission hook — a broken guard must not brick every tool
call — but the `cd` arm disarmed the guard with nothing to notice. Issue #2171: for the DENY/ASK
guards a silent exit 0 is still an allow, so their `cd` arm emits an `ask` decision naming the
hook. Issue #2394 merged five PreToolUse:Bash guards into `bash-pretool`, so its `ask` reason
names every deny guard it stands in front of.

Since #3278 every registration runs `run-hook.sh <name> <flags>`, and the posture a session
gets is the flags `settings.json` passes the runner. This file runs each registered command as
written against a runner whose `cd` target does not exist: the deny guards must ask, and every
other hook must stay silent. `test_run_hook.py` covers the flags one at a time, and pins the
runner's `# DECIDED:` marker. The six per-hook `.sh` shims these tests once covered were deleted
in #3304.

The deny guards also carry `gen_hook_settings.GUARD_SUFFIX` after the runner call, because a
session whose `$CLAUDE_PROJECT_DIR` was deleted has no runner left to ask from (#3887). The
last tests here run those commands under `/bin/sh`, the way the harness does.

Run: uv run pytest .claude/hooks/tests/test_hook_shim_fail_open.py
"""

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
from gen_hook_settings import GUARD_SUFFIX
from test_run_hook import _variant_runner

from lib.proc_testing import write_exec

HOOKS = Path(__file__).resolve().parent.parent

# The guards `bash-pretool` stands in front of. Named rather than counted: it can only ask once
# for all of them, so the one thing its reason string has to keep true is which ones.
MERGED_DENY_GUARDS = ("block-protected-bash", "block-footguns")


def _commands() -> dict[str, str]:
    """`{hook name: command}` for every hook `settings.json` registers, as written."""
    settings = json.loads((HOOKS.parent / "settings.json").read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for groups in settings["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                out[shlex.split(hook["command"])[1]] = hook["command"]
    return out


def _registered() -> dict[str, list[str]]:
    """`{hook name: runner flags}`, with a guard's missing-runner suffix cut off."""
    out: dict[str, list[str]] = {}
    for command in COMMANDS.values():
        runner, name, *flags = shlex.split(command.split(" || ", 1)[0])
        assert runner.endswith("/run-hook.sh"), command
        out[name] = flags
    return out


COMMANDS = _commands()
REGISTERED = _registered()
# The hooks whose `cd` arm must ask: the PreToolUse guards whose only decisions are deny and
# ask. Named, so a registration that drops `--ask-on-cd` fails here rather than going quiet.
ASKING = frozenset({"bash-pretool", "block-protected-edits"})


def test_the_registered_census_is_non_vacuous():
    assert set(REGISTERED) == {
        "auto-mode-bridge",
        "bash-pretool",
        "block-protected-edits",
        "fanout-stop",
        "log-instructions",
        "session-health",
    }
    asking = {
        name
        for name, flags in REGISTERED.items()
        if any(f.split("=")[0] == "--ask-on-cd" for f in flags)
    }
    assert asking == ASKING


@pytest.mark.parametrize("name", sorted(ASKING))
def test_reject_a_registered_deny_guard_that_cannot_cd_asks(tmp_path, name):
    runner = _variant_runner(tmp_path, str(tmp_path / "does-not-exist"))
    proc = subprocess.run(
        ["bash", str(runner), name, *REGISTERED[name]],
        input="",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"
    assert name in out["permissionDecisionReason"]
    if name == "bash-pretool":
        for guard in MERGED_DENY_GUARDS:
            assert guard in out["permissionDecisionReason"], guard


@pytest.mark.parametrize("name", sorted(set(REGISTERED) - ASKING))
def test_accept_a_registered_non_deny_hook_that_cannot_cd_stays_silent(tmp_path, name):
    """The near miss: an `ask` from a pass-through hook would be a prompt the design never
    makes. The quiet hooks never `cd`, so for them this is a run that reaches no `.py`."""
    runner = _variant_runner(tmp_path, str(tmp_path / "does-not-exist"))
    proc = subprocess.run(
        ["bash", str(runner), name, *REGISTERED[name]],
        input="",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def _run_as_the_harness(command: str, project_dir: Path, rc: int = 0):
    """Run a registered command under `/bin/sh -c`, the shell Claude Code runs hooks in."""
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(project_dir), "STUB_RC": str(rc)},
    )


def _stub_checkout(tmp_path: Path) -> Path:
    """A checkout whose runner exits `$STUB_RC`, standing in for a live session's."""
    write_exec(
        tmp_path / "live" / ".claude" / "hooks" / "run-hook.sh",
        '#!/bin/sh\nexit "$STUB_RC"\n',
    )
    return tmp_path / "live"


def test_only_the_deny_guards_carry_the_missing_runner_suffix():
    """`fanout-stop` must not: exit 2 on Stop keeps the session working, forever."""
    marker = GUARD_SUFFIX.split("{", 1)[0]
    suffixed = {name for name, command in COMMANDS.items() if marker in command}
    assert suffixed == ASKING
    assert " || " not in COMMANDS["fanout-stop"]


@pytest.mark.parametrize("name", sorted(ASKING))
def test_reject_a_deny_guard_whose_project_dir_is_gone_exits_2(tmp_path, name):
    """The #3887 verify-by: bare, `/bin/sh` exits 127 here, which the harness lets through."""
    proc = _run_as_the_harness(COMMANDS[name], tmp_path / "gone")
    assert proc.returncode == 2, proc.stderr
    assert f"run-hook.sh {name}:" in proc.stderr
    assert "exit 127" in proc.stderr
    if name == "bash-pretool":
        for guard in MERGED_DENY_GUARDS:
            assert guard in proc.stderr, guard


@pytest.mark.parametrize("name", sorted(ASKING))
@pytest.mark.parametrize("rc", [0, 2, 127])
def test_accept_a_deny_guard_with_a_live_runner_passes_its_exit_through(
    tmp_path, name, rc
):
    """An allow stays an allow and a deny a deny. A missing `uv`, 127 from the runner's own
    `exec`, stays the fail-open that `run-hook.sh`'s `# DECIDED:` documents."""
    proc = _run_as_the_harness(COMMANDS[name], _stub_checkout(tmp_path), rc)
    assert proc.returncode == rc, proc.stderr
    assert proc.stderr == ""
