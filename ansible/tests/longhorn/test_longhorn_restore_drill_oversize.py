#!/usr/bin/env python3
"""The restore drill publishes the backed-up volumes its actualSize cap keeps out.

Check 8 iterates only the drill's candidates file, and the drill writes that file after the cap.
A volume that grows past `k3s_longhorn_restore_drill_max_actual_bytes` therefore leaves the
rotation and the coverage check together, unreported. The drill writes `excluded_oversize` beside the candidates, and
the backup-health reader names every volume in it.

These tests RUN the rendered script through the shared harness in `_restore_drill.py`.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill_oversize.py
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
SMALL = "a-config"
BIG = "b-config"


def _excluded(run) -> list[str]:
    return (run.stamp_dir / "excluded_oversize").read_text().splitlines()


def test_backed_volume_over_the_cap_is_published_as_excluded(tmp_path: Path) -> None:
    run = harness(tmp_path, [SMALL, BIG], actual_sizes={BIG: CAP + 1})
    run()
    assert _excluded(run) == [f"{BIG}\t{CAP + 1}"]
    assert (run.stamp_dir / "candidates").read_text().split() == [SMALL]


def test_volume_at_the_cap_is_a_candidate_not_excluded(tmp_path: Path) -> None:
    run = harness(tmp_path, [SMALL, BIG], actual_sizes={BIG: CAP})
    run()
    assert _excluded(run) == []
    assert (run.stamp_dir / "candidates").read_text().split() == [SMALL, BIG]


def test_oversize_volume_with_no_completed_backup_is_not_excluded(
    tmp_path: Path,
) -> None:
    """Check 4 owns a backed-up-in-name-only volume; this file lists only what the cap drops."""
    run = harness(tmp_path, [SMALL, BIG], backed=[SMALL], actual_sizes={BIG: CAP + 1})
    run()
    assert _excluded(run) == []


def test_pinned_run_still_publishes_the_full_excluded_set(tmp_path: Path) -> None:
    """Written before the pin, like the candidates file, per the DECIDED marker beside it."""
    run = harness(tmp_path, [SMALL, BIG], actual_sizes={BIG: CAP + 1})
    run([SMALL])
    assert _excluded(run) == [f"{BIG}\t{CAP + 1}"]


def test_excluded_set_is_published_when_the_cap_leaves_nothing_eligible(
    tmp_path: Path,
) -> None:
    run = harness(tmp_path, [BIG], actual_sizes={BIG: CAP + 1})
    proc = run()
    assert proc.returncode == 1
    assert "no eligible volume" in proc.stderr, proc.stderr
    assert _excluded(run) == [f"{BIG}\t{CAP + 1}"]
