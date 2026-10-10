"""The restore drill's content assertions, run for real against the values the role deploys.

`test_longhorn_restore_drill.py` holds the floor's presence; this file runs it.
The floor exists to reject a mounted-but-empty restore; `files > 0` on the line above it already
does that, so the floor's own job is files that hold nothing. The floor has to accept
tdarr-configs, which is five JSON files, 1007 bytes in total, the whole of Tdarr's /app/configs.

Both assertions are waived for a PVC named in `k3s_longhorn_restore_drill_empty_ok_pvcs`, whose
content is legitimately empty (n8n-files), AND whose source volume is still under
`k3s_longhorn_restore_drill_empty_ok_max_actual_bytes`. The waiver is the reason the two pairs
below exist: a volume NOT on that list must still fail on an empty restore, or the exception has
widened to everything, and a volume on the list that has since filled up must fail too, or the
list is a permanent exemption rather than a claim about what the volume holds.

The guards are lifted out of the RENDERED drill by pattern rather than restated, so a reworded
check is exercised as written and every tunable arrives with the value the host runs (#3186). The
three hand substitutions this file used to make are what the render replaces.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill_byte_floor.py
"""

import re
import subprocess

from _helpers import load_yaml
from _shell_render import rendered_shell_text
from lib.proc_testing import run
from lib.repo_paths import K3S_DEFAULTS

DRILL = ("setup", "k3s", "longhorn-restore-drill.sh.j2")

# Starts at EMPTY_OK rather than at the first `[[`, so the waiver's own derivation runs here
# instead of being restated by this file.
_FLOOR_GUARD = re.compile(
    r'^EMPTY_OK=.*?\n.*?^\[\[ -n "\$FILES".*?\n.*?MIN_BYTES=.*?\n.*?\n\s*\|\| fail .*?$',
    re.M | re.S,
)


def _run_floor_guard(
    files: int,
    byte_count: int,
    pvc: str = "some-config",
    actual_size: int = 51712000,
) -> subprocess.CompletedProcess:
    """Execute the drill's content assertions with the deployed floor and a stub `fail`.

    `actual_size` defaults to n8n-files' own recorded reading — the observation the
    waiver's ceiling was derived from, so the accept half is measured rather than invented. It
    must be substituted, not left to bash: an unset `ACTUAL_SIZE` is 0 inside `(( ))`, which
    waives everything and would let a rejecting test pass while checking nothing.
    """
    match = _FLOOR_GUARD.search(rendered_shell_text(*DRILL))
    assert match, "the drill's files/bytes guard moved — update _FLOOR_GUARD"
    guard = match.group(0)
    # Non-vacuity for the lift, and what the three dropped `.replace()` calls used to assert: a
    # regex that matched a slice missing one of the three tunables would run a guard the host
    # does not have, and `"{{" not in guard` cannot see that on a rendered script.
    defaults = load_yaml(K3S_DEFAULTS)
    for key in (
        "k3s_longhorn_restore_drill_min_bytes",
        "k3s_longhorn_restore_drill_empty_ok_max_actual_bytes",
    ):
        assert str(defaults[key]) in guard, f"the lifted slice lost {key}:\n{guard}"
    for declared in defaults["k3s_longhorn_restore_drill_empty_ok_pvcs"]:
        assert declared in guard, (
            f"the lifted slice lost the {declared} waiver:\n{guard}"
        )
    script = (
        'fail() { echo "FAIL: $*" >&2; exit 1; }\n'
        f'PVC={pvc}\nPROBE="files={files} bytes={byte_count}"\n'
        f"FILES={files}\nBYTES={byte_count}\nACTUAL_SIZE={actual_size}\n" + guard
    )
    return run(["bash", "-c", script])


def test_a_tiny_but_real_volume_passes_the_floor() -> None:
    """CLEAN half: real data under 1 KiB is still real data."""
    result = _run_floor_guard(files=5, byte_count=1007)
    assert result.returncode == 0, result.stderr


def test_files_holding_no_bytes_fail_the_floor() -> None:
    """FLAGGED half: inodes with no content is the case the floor exists for."""
    result = _run_floor_guard(files=5, byte_count=0)
    assert result.returncode == 1
    assert "under the" in result.stderr and "floor" in result.stderr


def test_a_declared_empty_volume_passes_with_no_files() -> None:
    """CLEAN half of the waiver: n8n-files restores to files=0 and that is its proven state."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    declared = load_yaml(K3S_DEFAULTS)["k3s_longhorn_restore_drill_empty_ok_pvcs"]
    assert "n8n-files" in declared
    result = _run_floor_guard(files=0, byte_count=0, pvc="n8n-files")
    assert result.returncode == 0, result.stderr


def test_a_declared_volume_over_the_size_gate_fails_with_no_files() -> None:
    """FLAGGED half of the size gate: the waiver is withdrawn once the source holds real data."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    defaults = load_yaml(K3S_DEFAULTS)
    ceiling = defaults["k3s_longhorn_restore_drill_empty_ok_max_actual_bytes"]
    result = _run_floor_guard(
        files=0, byte_count=0, pvc="n8n-files", actual_size=ceiling + 1
    )
    assert result.returncode == 1
    assert "has no files" in result.stderr
    # The message has to name the SIZE gate, not just the empty restore: withholding the waiver
    # and never having declared the volume produce the same `files=0` failure otherwise, and they
    # need different fixes.
    assert "the waiver does not apply" in result.stderr, result.stderr
    assert str(ceiling) in result.stderr, result.stderr


def test_an_undeclared_volume_still_fails_with_no_files() -> None:
    """FLAGGED half of the waiver: the exception must not widen past the names that declare it."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    result = _run_floor_guard(files=0, byte_count=0, pvc="authelia-config")
    assert result.returncode == 1
    assert "has no files" in result.stderr
    assert "authelia-config" in result.stderr, result.stderr
