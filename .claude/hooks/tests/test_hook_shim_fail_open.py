"""The `cd /home/ubuntu/server || …` arm in every hook shim that changes into the repo.

Issue #1014: each shim is `cd /home/ubuntu/server || exit 0` followed by an `exec` into its
paired `.py` guard. All three ways the shim can fail were fail-open (exit 0, normal permission
flow), which is defensible for a permission hook — a broken guard must not brick every tool
call. But two of the three failure paths already write a line to stderr on their own (`uv`
missing, the `.py` missing), and the `cd` arm did not, so it disarmed the guard with nothing
to notice. Issue #2171: for the four DENY/ASK guards a silent exit 0 is still an allow, so
their `cd` arm now emits an `ask` decision naming the shim. The bridge keeps
exit 0 with no stdout: the bridge's events have no `ask` to emit. Issue #2394 merged five PreToolUse:Bash shims into `bash-pretool.sh`,
three of them deny guards and two of them silent, so that one shim carries the `ask` and its
reason names all three guards. This test proves per shim:

  1. the `# DECIDED:` marker recording the trade-off is present (so a future session does
     not read either posture as an oversight and "fix" it the other way),
  2. the `cd` arm, when it fails, writes a line to stderr — matching the other two arms, and
  3. what it puts on stdout: the `ask` JSON for a deny guard, nothing for the rest.

Every check here is a REJECT/ACCEPT pair: REJECT is a `cd` target that does not exist (the
failure these issues are about — must produce a stderr line and still exit 0), ACCEPT is a
`cd` target that exists (must NOT produce that line, and must still reach the `exec`, proven
by python's own "can't open file" error appearing instead once it fails to find the `.py`
next to a throwaway copy of the shim).

Issue #3278 moved every registration onto `run-hook.sh <name> <flags>`, so the posture a
session gets is the flags `settings.json` passes the runner. The last section runs each
registered command as written, which is what ties a deny guard to `--ask-on-cd`. The per-hook
shims stay, unregistered, for a session whose `settings.json` predates that switch: its
commands still name them by path into the primary checkout, and a deleted shim exits 127 there
with the guard skipped. The tests above keep them honest until they go.

Run: uv run pytest .claude/hooks/tests/test_hook_shim_fail_open.py
"""

import json
import shlex
import subprocess
from pathlib import Path

import pytest
from test_run_hook import _variant_runner

HOOKS = Path(__file__).resolve().parent.parent

_CD_GUARD = "cd /home/ubuntu/server || "

# The shims whose `cd` arm asks since #2171: every PreToolUse guard whose only decisions are
# `deny` and `ask`. Only these carry the `# DECIDED:` block, which records the trade-off once
# for the whole class. Three of the four became arms of `bash-pretool.sh` in #2394, so that one
# shim now answers for all three — and its `ask` reason names each of them, because an operator
# reading one line needs to know which guards did not run.
DENY_GUARD_SHIMS = frozenset(
    {
        "bash-pretool.sh",
        "block-protected-edits.sh",
    }
)

# The guards `bash-pretool.sh` stands in front of. Named rather than counted: the shim can only
# ask once for all of them, so the one thing its reason string has to keep true is which ones.
MERGED_DENY_GUARDS = ("block-protected-bash", "nudge-land-sh", "block-footguns")


# `run-hook.sh` holds the same `cd` guard and is NOT a per-hook shim: its posture is decided by
# the flags a registration passes it, so `bash run-hook.sh` with no arguments — what every
# parametrized test below runs — exercises none of them. `test_run_hook.py` covers it per
# variant, and `test_the_shim_census_is_non_vacuous` asserts this exclusion is for cause.
_PARAMETERISED_RUNNER = "run-hook.sh"


def _cd_guarded_shims() -> list[str]:
    """Every per-hook shim that changes into the repo before doing its work.

    DERIVED, not listed. A hardcoded list with a `len(...) == 4` non-vacuity anchor cannot
    notice a shim that is added and never listed, so several guards could keep disarming
    silently while this file reported full coverage.
    """
    return sorted(
        p.name
        for p in HOOKS.glob("*.sh")
        if _CD_GUARD in p.read_text(encoding="utf-8")
        and p.name != _PARAMETERISED_RUNNER
    )


# Shims that `exec` into a paired `.py`. The ACCEPT half below only means something for these:
# it proves execution reached the exec, and a shim with no exec has none to reach.
def _exec_shims() -> list[str]:
    return [
        name
        for name in _cd_guarded_shims()
        if "\nexec " in (HOOKS / name).read_text(encoding="utf-8")
    ]


SHIM_NAMES = _cd_guarded_shims()
EXEC_SHIM_NAMES = _exec_shims()


def test_the_shim_census_is_non_vacuous():
    # Assert the NAMES, not a count. A glob returns an empty set the moment the files move or
    # are renamed, and every parametrized test below would then pass by iterating zero times —
    # the failure mode the repo-root CLAUDE.md describes.
    assert set(SHIM_NAMES) == {
        "auto-mode-bridge.sh",
        "bash-pretool.sh",
        "block-protected-edits.sh",
    }
    assert DENY_GUARD_SHIMS <= set(SHIM_NAMES)
    # The exclusion is for cause, not a typo that quietly drops a shim: the runner exists and
    # does carry the guard, so it was skipped for being parameterised rather than for missing.
    runner = HOOKS / _PARAMETERISED_RUNNER
    assert runner.is_file(), runner
    assert _CD_GUARD in runner.read_text(encoding="utf-8")
    # Every cd-guarded shim execs into a paired .py, so the ACCEPT half below covers all of
    # them. A future shim that does not exec drops out of EXEC_SHIM_NAMES and fails here.
    assert set(SHIM_NAMES) == set(EXEC_SHIM_NAMES)


def _variant(hook_path: Path, cd_target: str) -> str:
    """The shim's text with its `cd` target swapped for `cd_target`."""
    text = hook_path.read_text(encoding="utf-8")
    original = "cd /home/ubuntu/server || "
    assert original in text, f"{hook_path.name}: expected cd-guard line not found"
    return text.replace("cd /home/ubuntu/server ", f"cd {cd_target} ", 1)


def _run(tmp_path: Path, hook_name: str, cd_target: str) -> subprocess.CompletedProcess:
    variant_path = tmp_path / hook_name
    variant_path.write_text(_variant(HOOKS / hook_name, cd_target), encoding="utf-8")
    return subprocess.run(
        ["bash", str(variant_path)],
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("hook_name", sorted(DENY_GUARD_SHIMS))
def test_decided_marker_documents_the_trade_off(hook_name):
    text = (HOOKS / hook_name).read_text(encoding="utf-8")
    assert "# DECIDED:" in text
    assert "fail-open" in text
    assert "#1014" in text
    assert "#2171" in text


def test_the_merged_shim_names_every_guard_that_did_not_run(tmp_path):
    """One shim stands in for several guards, so the posture is the `ask` — and the operator
    can only act on it if the reason says which guards it covers."""
    proc = _run(tmp_path, "bash-pretool.sh", str(tmp_path / "does-not-exist"))
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    for guard in MERGED_DENY_GUARDS:
        assert guard in reason, guard


@pytest.mark.parametrize("hook_name", SHIM_NAMES)
def test_reject_a_missing_cd_target_now_reports_on_stderr(tmp_path, hook_name):
    """When `cd` fails, something must say so."""
    missing = tmp_path / "does-not-exist"
    proc = _run(tmp_path, hook_name, str(missing))
    assert proc.returncode == 0, proc.stderr
    # "did not run", not "guard did not run": each shim names the thing that did not happen
    # (guard / classifier / bridge / lint), which is what an operator reading one line needs.
    assert "did not run" in proc.stderr
    assert hook_name in proc.stderr


@pytest.mark.parametrize("hook_name", sorted(DENY_GUARD_SHIMS))
def test_reject_a_deny_guard_that_cannot_run_asks(tmp_path, hook_name):
    """A bare exit 0 from a DENY guard is an allow. The `ask` names the shim, because
    four of these can fire on one call and the operator needs to know which one did not run."""
    proc = _run(tmp_path, hook_name, str(tmp_path / "does-not-exist"))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "ask"
    assert hook_name in out["permissionDecisionReason"]


@pytest.mark.parametrize("hook_name", sorted(set(SHIM_NAMES) - DENY_GUARD_SHIMS))
def test_reject_a_non_deny_shim_that_cannot_run_stays_silent(tmp_path, hook_name):
    """The near miss: the bridge keeps no stdout on a failed `cd` — an `ask` from it would be
    a prompt where the design is a pass-through."""
    proc = _run(tmp_path, hook_name, str(tmp_path / "does-not-exist"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


@pytest.mark.parametrize("hook_name", EXEC_SHIM_NAMES)
def test_accept_a_valid_cd_target_stays_silent_on_that_line(tmp_path, hook_name):
    """The near miss: `cd` succeeds, so the new stderr line must not fire, and execution
    must still reach the `exec` (proven by python's own error once the paired `.py` is not
    found beside this throwaway copy of the shim)."""
    existing = tmp_path  # a real, existing directory
    proc = _run(tmp_path, hook_name, str(existing))
    assert "did not run" not in proc.stderr
    # Reached exec: some interpreter-level error about the missing .py, not a clean no-op.
    assert proc.returncode != 0


# --- the registered commands, each run through the runner as settings.json writes it -------


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
