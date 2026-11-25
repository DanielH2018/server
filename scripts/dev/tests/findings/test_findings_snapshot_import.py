"""`findings.py` and `probe.py` run from a tree holding only the fan-out snapshot's paths.

Another repo's batch runs this repo's tools from `.fanout/server`, which holds only
`launch.SNAPSHOT_PATHS`. A module-level import that reaches `ansible/` crashed `findings.py` on
import (#4271), and one that reaches `hosts/lib` crashed `probe.py` (#8878).
"""

import shutil
import sys

from dev.fanout_lib.launch import SNAPSHOT_PATHS
from lib.proc_testing import run as run_proc
from lib.repo_paths import REPO


def _snapshot_only_tree(root):
    for rel in SNAPSHOT_PATHS:
        src, dst = REPO / rel, root / rel
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        elif src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return root


def test_findings_open_help_runs_from_a_snapshot_only_tree(tmp_path):
    root = _snapshot_only_tree(tmp_path)
    assert not (root / "ansible").exists()

    run = run_proc(
        [sys.executable, str(root / "scripts/dev/findings.py"), "open", "--help"],
        cwd=root,
    )

    assert run.returncode == 0, run.stderr
    assert "usage:" in run.stdout


def test_probe_list_runs_from_a_snapshot_only_tree(tmp_path):
    root = _snapshot_only_tree(tmp_path)

    run = run_proc(
        [sys.executable, str(root / "scripts/diagnostics/probe.py"), "--list"],
        cwd=root,
    )

    assert run.returncode == 0, run.stderr
    assert "landing" in run.stdout
