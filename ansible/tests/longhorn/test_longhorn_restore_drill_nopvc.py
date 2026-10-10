#!/usr/bin/env python3
"""The restore drill publishes the backed-up volumes it skips for having no bound PVC.

The drill restores into a PVC in the source namespace, so its eligible set drops any volume
whose `kubernetesStatus.pvcName` is empty. Check 8 iterates only the candidates file, so such a
volume leaves the rotation and the coverage check together, unreported. The drill writes
`excluded_nopvc` beside `excluded_oversize`, and the backup-health reader names every
volume in it.

These tests RUN the rendered script through the shared harness in `_restore_drill.py`.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill_nopvc.py
"""

from pathlib import Path

from lib import yaml_fast

from lib.repo_paths import K3S_DEFAULTS
from _restore_drill import harness

CAP = int(
    yaml_fast.safe_load(K3S_DEFAULTS.read_text())[
        "k3s_longhorn_restore_drill_max_actual_bytes"
    ]
)
BOUND = "a-config"
LOOSE = "b-config"


def _nopvc(run) -> list[str]:
    return (run.stamp_dir / "excluded_nopvc").read_text().splitlines()


def test_backed_volume_with_no_pvc_is_published_as_excluded(tmp_path: Path) -> None:
    run = harness(tmp_path, [BOUND, LOOSE], no_pvc={LOOSE})
    run()
    assert _nopvc(run) == [f"pvc-{LOOSE}"]
    assert (run.stamp_dir / "candidates").read_text().split() == [BOUND]


def test_backed_volume_with_a_pvc_is_a_candidate_not_excluded(tmp_path: Path) -> None:
    run = harness(tmp_path, [BOUND, LOOSE])
    run()
    assert _nopvc(run) == []
    assert (run.stamp_dir / "candidates").read_text().split() == [BOUND, LOOSE]


def test_pvcless_volume_with_no_completed_backup_is_not_excluded(
    tmp_path: Path,
) -> None:
    """Check 4 owns a backed-up-in-name-only volume; this file lists only what the drill skips."""
    run = harness(tmp_path, [BOUND, LOOSE], backed=[BOUND], no_pvc={LOOSE})
    run()
    assert _nopvc(run) == []


def test_pvcless_volume_over_the_cap_is_named_only_as_oversize(tmp_path: Path) -> None:
    run = harness(
        tmp_path, [BOUND, LOOSE], actual_sizes={LOOSE: CAP + 1}, no_pvc={LOOSE}
    )
    run()
    assert _nopvc(run) == []
    oversize = (run.stamp_dir / "excluded_oversize").read_text().splitlines()
    assert oversize == [f"pvc-{LOOSE}\t{CAP + 1}"]


def test_pinned_run_still_publishes_the_full_nopvc_set(tmp_path: Path) -> None:
    run = harness(tmp_path, [BOUND, LOOSE], no_pvc={LOOSE})
    run([BOUND])
    assert _nopvc(run) == [f"pvc-{LOOSE}"]


def test_nopvc_set_is_published_when_nothing_is_eligible(tmp_path: Path) -> None:
    run = harness(tmp_path, [LOOSE], no_pvc={LOOSE})
    proc = run()
    assert proc.returncode == 1
    assert "no eligible volume" in proc.stderr, proc.stderr
    assert _nopvc(run) == [f"pvc-{LOOSE}"]
