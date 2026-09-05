"""The `cd /home/ubuntu/server || exit 0` arm in the four PreToolUse deny/ask shims.

Issue #1014: each shim is `cd /home/ubuntu/server || exit 0` followed by an `exec` into its
paired `.py` guard. All three ways the shim can fail are fail-open (exit 0, normal permission
flow), which is defensible for a permission hook — a broken guard must not brick every tool
call. But two of the three failure paths already write a line to stderr on their own (`uv`
missing, the `.py` missing), and the `cd` arm did not, so it disarmed the guard with nothing
to notice. This test proves two things per shim:

  1. the `# DECIDED:` marker documenting the fail-open trade-off is present (so a future
     session does not read the silence as an oversight and "fix" it into fail-closed), and
  2. the `cd` arm, when it fails, now writes a line to stderr — matching the other two arms.

Every check here is a REJECT/ACCEPT pair: REJECT is a `cd` target that does not exist (the
failure this issue is about — must now produce a stderr line and still exit 0), ACCEPT is a
`cd` target that exists (must NOT produce that line, and must still reach the `exec`, proven
by python's own "can't open file" error appearing instead once it fails to find the `.py`
next to a throwaway copy of the shim).

Run: uv run pytest .claude/hooks/tests/test_hook_shim_fail_open.py
"""

import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent

_CD_GUARD = "cd /home/ubuntu/server || "

# The four shims issue #1014 originally fixed. Only these carry the `# DECIDED:` block, which
# records the fail-open trade-off once for the whole class.
ISSUE_1014_SHIMS = frozenset(
    {
        "block-protected-bash.sh",
        "block-footguns.sh",
        "nudge-land-sh.sh",
        "block-protected-edits.sh",
    }
)


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
    assert set(SHIM_NAMES) == {
        "ansible-lint.sh",
        "auto-approve-readonly.sh",
        "auto-approve-remote-ssh.sh",
        "auto-mode-bridge.sh",
        "block-footguns.sh",
        "block-protected-bash.sh",
        "block-protected-edits.sh",
        "nudge-land-sh.sh",
    }
    assert ISSUE_1014_SHIMS <= set(SHIM_NAMES)
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


@pytest.mark.parametrize("hook_name", sorted(ISSUE_1014_SHIMS))
def test_decided_marker_documents_the_fail_open_trade_off(hook_name):
    text = (HOOKS / hook_name).read_text(encoding="utf-8")
    assert "# DECIDED:" in text
    assert "fail-open" in text
    assert "#1014" in text


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
