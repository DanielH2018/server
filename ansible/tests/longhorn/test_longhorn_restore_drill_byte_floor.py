"""The restore drill's content assertions, run for real against the values the role deploys.

Split from `test_longhorn_restore_drill.py`, which holds the floor's presence but never ran it.
The floor exists to reject a mounted-but-empty restore; `files > 0` on the line above it already
does that, so the floor's own job is files that hold nothing. At 1024 it also rejected
tdarr-configs — five JSON files, 1007 bytes in total, the whole of Tdarr's /app/configs — on
2026-09-07, and the volume read as unproven for the rest of the 26-night rotation.

Both assertions are waived for a PVC named in `k3s_longhorn_restore_drill_empty_ok_pvcs`, whose
content is legitimately empty (n8n-files). The waiver is the reason the second pair below exists:
a volume NOT on that list must still fail on an empty restore, or the exception has widened to
everything and the drill proves nothing about content anywhere.

The guards are lifted out of the template by pattern rather than restated, so a reworded check is
exercised as written; the Jinja placeholders are the only substitutions.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill_byte_floor.py
"""

import re
import subprocess

from _helpers import ROLES
from _helpers import load_yaml

K3S = ROLES / "setup" / "k3s"
DRILL = K3S / "templates" / "longhorn-restore-drill.sh.j2"

# Starts at EMPTY_OK rather than at the first `[[`, so the waiver's own derivation runs here
# instead of being restated by this file.
_FLOOR_GUARD = re.compile(
    r'^EMPTY_OK=.*?\n.*?^\[\[ -n "\$FILES".*?\n.*?MIN_BYTES=.*?\n.*?\n\s*\|\| fail .*?$',
    re.M | re.S,
)


def _run_floor_guard(
    files: int, byte_count: int, pvc: str = "some-config"
) -> subprocess.CompletedProcess:
    """Execute the drill's content assertions with the deployed floor and a stub `fail`."""
    match = _FLOOR_GUARD.search(DRILL.read_text())
    assert match, "the drill's files/bytes guard moved — update _FLOOR_GUARD"
    defaults = load_yaml(K3S / "defaults" / "main.yml")
    guard = (
        match.group(0)
        .replace(
            "{{ k3s_longhorn_restore_drill_min_bytes }}",
            str(defaults["k3s_longhorn_restore_drill_min_bytes"]),
        )
        .replace(
            "{{ k3s_longhorn_restore_drill_empty_ok_pvcs | join(' ') }}",
            " ".join(defaults["k3s_longhorn_restore_drill_empty_ok_pvcs"]),
        )
    )
    script = (
        'fail() { echo "FAIL: $*" >&2; exit 1; }\n'
        f'PVC={pvc}\nPROBE="files={files} bytes={byte_count}"\n'
        f"FILES={files}\nBYTES={byte_count}\n" + guard
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


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
    declared = load_yaml(K3S / "defaults" / "main.yml")[
        "k3s_longhorn_restore_drill_empty_ok_pvcs"
    ]
    assert "n8n-files" in declared
    result = _run_floor_guard(files=0, byte_count=0, pvc="n8n-files")
    assert result.returncode == 0, result.stderr


def test_an_undeclared_volume_still_fails_with_no_files() -> None:
    """FLAGGED half of the waiver: the exception must not widen past the names that declare it."""
    result = _run_floor_guard(files=0, byte_count=0, pvc="authelia-config")
    assert result.returncode == 1
    assert "has no files" in result.stderr
    assert "authelia-config" in result.stderr, result.stderr
