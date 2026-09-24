"""The `cd /home/ubuntu/server || …` arm in every hook shim that changes into the repo.

Issue #1014: each shim is `cd /home/ubuntu/server || exit 0` followed by an `exec` into its
paired `.py` guard. All three ways the shim can fail were fail-open (exit 0, normal permission
flow), which is defensible for a permission hook — a broken guard must not brick every tool
call. But two of the three failure paths already write a line to stderr on their own (`uv`
missing, the `.py` missing), and the `cd` arm did not, so it disarmed the guard with nothing
to notice. Issue #2171: for the four DENY/ASK guards a silent exit 0 is still an allow, so
their `cd` arm now emits an `ask` decision naming the shim. The linter and the bridge keep
exit 0 with no stdout: a missed approval is a prompt, not a bypass, and the bridge's events
have no `ask` to emit. Issue #2394 merged five PreToolUse:Bash shims into `bash-pretool.sh`,
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

Run: uv run pytest .claude/hooks/tests/test_hook_shim_fail_open.py
"""

import json
import subprocess
from pathlib import Path

import pytest

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
        # Transition shims (#2465): unregistered, kept for sessions that started before
        # bash-pretool.sh and still run them by path. They keep their own `ask` until deleted.
        "block-footguns.sh",
        "block-protected-bash.sh",
        "nudge-land-sh.sh",
    }
)

# The five pre-#2394 shims, kept as `# gen-hooks: library` files so a session started before
# the dispatcher does not find its deny guards missing. Delete them, and this set, together.
TRANSITION_SHIMS = frozenset(
    {
        "auto-approve-readonly.sh",
        "block-footguns.sh",
        "block-protected-bash.sh",
        "inject-nested-docs.sh",
        "nudge-land-sh.sh",
    }
)

# The guards `bash-pretool.sh` stands in front of. Named rather than counted: the shim can only
# ask once for all of them, so the one thing its reason string has to keep true is which ones.
MERGED_DENY_GUARDS = ("block-protected-bash", "nudge-land-sh", "block-footguns")


def _cd_guarded_shims() -> list[str]:
    """Every hook shim that changes into the repo before doing its work.

    DERIVED, not listed. The four names above were hardcoded with `len(...) == 4` as the
    non-vacuity anchor, and a count cannot notice a shim that was added and never listed:
    auto-mode-bridge, auto-approve-readonly, auto-approve-remote-ssh and ansible-lint all grew
    the same `cd ... || exit 0` and none of them got issue #1014's stderr line, so four guards
    kept disarming silently while this file reported full coverage of "the four shims".
    """
    return sorted(
        p.name for p in HOOKS.glob("*.sh") if _CD_GUARD in p.read_text(encoding="utf-8")
    )


# Shims that `exec` into a paired `.py`. The ACCEPT half below only means something for these:
# it proves execution reached the exec, and ansible-lint.sh has no exec to reach.
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
    # the failure mode the repo-root CLAUDE.md describes, and the one the old `len() == 4`
    # anchor could not see because it pinned the size of a hand-written list instead.
    assert (
        set(SHIM_NAMES)
        == {
            "ansible-lint.sh",
            "auto-mode-bridge.sh",
            "bash-pretool.sh",
            "block-protected-edits.sh",
        }
        | TRANSITION_SHIMS
    )
    assert DENY_GUARD_SHIMS <= set(SHIM_NAMES)
    # ansible-lint.sh is the only one that does not exec into a paired .py.
    assert set(SHIM_NAMES) - set(EXEC_SHIM_NAMES) == {"ansible-lint.sh"}


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
    """#2394: one shim replaced three deny guards and two silent ones. The silent pair cost a
    prompt and a re-read, so the merged posture is the `ask` — and the operator can only act
    on it if the reason says which guards it covers."""
    proc = _run(tmp_path, "bash-pretool.sh", str(tmp_path / "does-not-exist"))
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    for guard in MERGED_DENY_GUARDS:
        assert guard in reason, guard


@pytest.mark.parametrize("hook_name", SHIM_NAMES)
def test_reject_a_missing_cd_target_now_reports_on_stderr(tmp_path, hook_name):
    """The failure this issue is about: `cd` fails, and until now nothing said so."""
    missing = tmp_path / "does-not-exist"
    proc = _run(tmp_path, hook_name, str(missing))
    assert proc.returncode == 0, proc.stderr
    # "did not run", not "guard did not run": each shim names the thing that did not happen
    # (guard / classifier / bridge / lint), which is what an operator reading one line needs.
    assert "did not run" in proc.stderr
    assert hook_name in proc.stderr


@pytest.mark.parametrize("hook_name", sorted(DENY_GUARD_SHIMS))
def test_reject_a_deny_guard_that_cannot_run_asks(tmp_path, hook_name):
    """#2171: a bare exit 0 from a DENY guard is an allow. The `ask` names the shim, because
    four of these can fire on one call and the operator needs to know which one did not run."""
    proc = _run(tmp_path, hook_name, str(tmp_path / "does-not-exist"))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "ask"
    assert hook_name in out["permissionDecisionReason"]


@pytest.mark.parametrize("hook_name", sorted(set(SHIM_NAMES) - DENY_GUARD_SHIMS))
def test_reject_a_non_deny_shim_that_cannot_run_stays_silent(tmp_path, hook_name):
    """The near miss: the linter and the bridge keep no stdout on a failed `cd` — an `ask`
    from either would be a prompt where the design is a pass-through."""
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
