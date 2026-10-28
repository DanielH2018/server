"""leakguard's `Popen` wrapper: a child env built from scratch still gets the temp textfile dir.

`ansible/tests/leakguard.py` points `TEXTFILE_DIR` at a temp directory, but a test that hands
its child an explicit `env` used to drop it, and a check script the child ran wrote a fake
verdict into node_exporter's real directory (#6191, #6238). These run in this process, under
the plugin `pyproject.toml` loads, because the wrapper is what they check.

Run: uv run pytest ansible/tests/repo/test_leakguard_child_env.py
"""

import os
import subprocess
from pathlib import Path

from _helpers import REPO

_CHECK_LIB = REPO / "ansible/roles/setup/initial_setup/files/homelab-check-lib.sh"


def test_a_scratch_env_child_writes_its_textfile_under_the_temp_directory() -> None:
    textfile_dir = Path(os.environ["TEXTFILE_DIR"])
    scratch_env = {"PATH": "/usr/bin:/bin"}
    seen = subprocess.run(
        ["sh", "-c", 'echo "$TEXTFILE_DIR $LEAKGUARD_CALLS"'],
        env=scratch_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Asserted before the writer runs: a child that lacked it would write the real directory.
    assert seen.stdout.split() == [str(textfile_dir), os.environ["LEAKGUARD_CALLS"]]
    writer = f"source {_CHECK_LIB} && write_check_textfile leakguard-scratch 1 0 60"
    subprocess.run(["bash", "-c", writer], env=scratch_env, check=True, timeout=60)
    assert (textfile_dir / "leakguard-scratch.prom").is_file()


def test_a_scratch_env_child_keeps_its_own_textfile_dir(tmp_path: Path) -> None:
    """The wrapper fills only what the env lacks: the child's own TEXTFILE_DIR wins."""
    own = str(tmp_path / "own")
    seen = subprocess.run(
        ["sh", "-c", 'echo "$TEXTFILE_DIR $LEAKGUARD_CALLS"'],
        env={"PATH": "/usr/bin:/bin", "TEXTFILE_DIR": own},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert seen.stdout.split() == [own, os.environ["LEAKGUARD_CALLS"]]
