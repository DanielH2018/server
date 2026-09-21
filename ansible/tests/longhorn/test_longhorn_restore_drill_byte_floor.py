"""The restore drill's byte floor, run for real against the value the role deploys.

Split from `test_longhorn_restore_drill.py`, which holds the floor's presence but never ran it.
The floor exists to reject a mounted-but-empty restore; `files > 0` on the line above it already
does that, so the floor's own job is files that hold nothing. At 1024 it also rejected
tdarr-configs — five JSON files, 1007 bytes in total, the whole of Tdarr's /app/configs — on
2026-09-07, and the volume read as unproven for the rest of the 26-night rotation.

The guard is lifted out of the template by pattern rather than restated, so a reworded check is
exercised as written; the Jinja placeholder is the only substitution.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill_byte_floor.py
"""

import re
import subprocess

from _helpers import ROLES
from _helpers import load_yaml

K3S = ROLES / "setup" / "k3s"
DRILL = K3S / "templates" / "longhorn-restore-drill.sh.j2"

_FLOOR_GUARD = re.compile(
    r'^\[\[ -n "\$FILES".*?\n.*?MIN_BYTES=.*?\n.*?\n\s*\|\| fail .*?$', re.M | re.S
)


def _run_floor_guard(files: int, byte_count: int) -> subprocess.CompletedProcess:
    """Execute the drill's two data assertions with the deployed floor and a stub `fail`."""
    match = _FLOOR_GUARD.search(DRILL.read_text())
    assert match, "the drill's files/bytes guard moved — update _FLOOR_GUARD"
    floor = load_yaml(K3S / "defaults" / "main.yml")[
        "k3s_longhorn_restore_drill_min_bytes"
    ]
    guard = match.group(0).replace(
        "{{ k3s_longhorn_restore_drill_min_bytes }}", str(floor)
    )
    script = (
        'fail() { echo "FAIL: $*" >&2; exit 1; }\n'
        f'PROBE="files={files} bytes={byte_count}"\nFILES={files}\nBYTES={byte_count}\n'
        + guard
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
