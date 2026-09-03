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

# The four shims issue #1014 names. Named explicitly rather than globbed, so a rename or a
# fifth deny shim added later does not silently drop out of coverage.
SHIM_NAMES = [
    "block-protected-bash.sh",
    "block-footguns.sh",
    "nudge-land-sh.sh",
    "block-protected-edits.sh",
]


def test_shim_names_is_non_vacuous():
    # Guards SHIM_NAMES itself: if the list above were ever emptied by a bad edit, every
    # parametrized test below would pass by iterating zero times.
    assert len(SHIM_NAMES) == 4


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


@pytest.mark.parametrize("hook_name", SHIM_NAMES)
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
    assert "guard did not run" in proc.stderr
    assert hook_name in proc.stderr


@pytest.mark.parametrize("hook_name", SHIM_NAMES)
def test_accept_a_valid_cd_target_stays_silent_on_that_line(tmp_path, hook_name):
    """The near miss: `cd` succeeds, so the new stderr line must not fire, and execution
    must still reach the `exec` (proven by python's own error once the paired `.py` is not
    found beside this throwaway copy of the shim)."""
    existing = tmp_path  # a real, existing directory
    proc = _run(tmp_path, hook_name, str(existing))
    assert "guard did not run" not in proc.stderr
    # Reached exec: some interpreter-level error about the missing .py, not a clean no-op.
    assert proc.returncode != 0
