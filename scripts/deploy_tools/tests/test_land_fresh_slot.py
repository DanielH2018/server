"""A `--fresh` child landing's slot in the live-landing registry (#8280).

Run: uv run pytest scripts/deploy_tools/tests/test_land_fresh_slot.py
"""

import subprocess
import sys

from test_land_detach import _GH_STUB, _LAND_SH, _env

_PARENT_BEFORE_SLOT_HELD = """import os, subprocess, sys
from pathlib import Path
slot = Path(sys.argv[1]) / f"{os.getpid()}.slot"
slot.parent.mkdir(parents=True, exist_ok=True)
slot.write_text("")
env = {k: v for k, v in os.environ.items() if k != "LAND_SLOT_HELD"}
child = subprocess.run(["bash", sys.argv[2], "--pr", "939", "--since", "a" * 40], env=env)
Path(sys.argv[3]).write_text(str(int(slot.exists())))
slot.unlink(missing_ok=True)
sys.exit(child.returncode)
"""


def test_a_fresh_child_of_a_parent_without_slot_held_takes_no_second_slot(tmp_path):
    """A parent `land.py` cut before #8260 holds a slot and never sets LAND_SLOT_HELD.

    Its `--fresh` child runs master's `land.sh`, and the landing pair counts once in the registry.
    """
    live = tmp_path / "live" / "default"
    counts = tmp_path / "slot-counts"
    gh = _GH_STUB.replace(
        'case "$*" in',
        f'ls "{live}" 2>/dev/null | grep -c "\\.slot$" >> "{counts}"\ncase "$*" in',
        1,
    )
    parent = tmp_path / "old" / "scripts" / "deploy_tools" / "land.py"
    parent.parent.mkdir(parents=True)
    parent.write_text(_PARENT_BEFORE_SLOT_HELD)
    kept = tmp_path / "parent-slot-kept"
    result = subprocess.run(
        [
            sys.executable,
            str(parent),
            str(live),
            str(_LAND_SH),
            str(kept),
            # Unread by the stand-in; a fix may find the parent landing by its cmdline.
            "--pr",
            "939",
            "--fresh",
            "--detach",
        ],
        env=_env(tmp_path, gh=gh),
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr
    seen = [int(n) for n in counts.read_text().split()]
    assert seen and min(seen) >= 1, seen
    assert max(seen) == 1, f"slots counted at each gh call: {seen}"
    assert kept.read_text() == "1"
