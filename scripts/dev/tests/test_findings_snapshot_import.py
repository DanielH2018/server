"""`findings.py` runs from a tree holding only the fan-out snapshot's paths (#4271).

Another repo's batch runs `findings.py open` from `.fanout/server`, which holds only
`launch.SNAPSHOT_PATHS`. A module-level import that reaches `ansible/` crashed it on import.
"""

import shutil
import sys

from fanout_lib.launch import SNAPSHOT_PATHS
from lib.proc_testing import run as run_proc
from lib.repo_paths import REPO


def test_findings_open_help_runs_from_a_snapshot_only_tree(tmp_path):
    for rel in SNAPSHOT_PATHS:
        src, dst = REPO / rel, tmp_path / rel
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        elif src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    assert not (tmp_path / "ansible").exists()

    run = run_proc(
        [sys.executable, str(tmp_path / "scripts/dev/findings.py"), "open", "--help"],
        cwd=tmp_path,
    )

    assert run.returncode == 0, run.stderr
    assert "usage:" in run.stdout
