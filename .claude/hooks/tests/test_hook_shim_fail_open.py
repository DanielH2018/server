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

Run: uv run pytest .claude/hooks/tests/test_hook_shim_fail_open.py
"""

import json
import shlex
import subprocess
from pathlib import Path

import pytest
from test_run_hook import _variant_runner

HOOKS = Path(__file__).resolve().parent.parent

# The guards `bash-pretool` stands in front of. Named rather than counted: it can only ask once
# for all of them, so the one thing its reason string has to keep true is which ones.
MERGED_DENY_GUARDS = ("block-protected-bash", "block-footguns")


def _registered() -> dict[str, list[str]]:
    """`{hook name: runner flags}` for every hook `settings.json` registers."""
    settings = json.loads((HOOKS.parent / "settings.json").read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for groups in settings["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                runner, name, *flags = shlex.split(hook["command"])
                assert runner.endswith("/run-hook.sh"), hook["command"]
                out[name] = flags
    return out


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
