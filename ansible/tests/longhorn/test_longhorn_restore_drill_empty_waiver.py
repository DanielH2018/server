"""The empty-content waiver, run end to end against the rendered drill.

`test_longhorn_restore_drill_byte_floor.py` executes the two content assertions in isolation and
checks their message. This file runs the whole drill, because the claim the waiver makes is about
what gets STAMPED: check 7 reads `last-success` and check 8 reads `success/<pvc>`, and a waiver
that fires wrongly writes both for a restore that brought nothing back.

Until 2026-09-24 the waiver applied by name alone (#2393), so the day n8n-files starts holding
data an empty restore would still have stamped success. The ceiling is what makes the list a
claim about the volume rather than a permanent exemption.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill_empty_waiver.py
"""

from pathlib import Path

from _helpers import ROLES
from _helpers import load_yaml
from _restore_drill import harness

DEFAULTS = ROLES / "setup" / "k3s" / "defaults" / "main.yml"
# n8n-files' own actualSize on 2026-09-22 — the reading the ceiling was derived from.
RECORDED_BARE_EXT4_BYTES = 51712000


def _ceiling() -> int:
    return load_yaml(DEFAULTS)["k3s_longhorn_restore_drill_empty_ok_max_actual_bytes"]


def _declared() -> str:
    declared = load_yaml(DEFAULTS)["k3s_longhorn_restore_drill_empty_ok_pvcs"]
    assert declared, (
        "no volume is declared may-be-empty — this suite has nothing to exercise"
    )
    return declared[0]


def test_a_still_empty_declared_volume_passes_and_stamps(tmp_path: Path) -> None:
    """CLEAN half: the waiver's whole point, at the size the declaring volume actually reads."""
    pvc = _declared()
    run = harness(tmp_path, [pvc], actual_sizes={pvc: RECORDED_BARE_EXT4_BYTES})
    proc = run(restore="pass", env={"K3S_STUB_PROBE": "files=0 bytes=0"})
    assert proc.returncode == 0, proc.stderr
    assert (run.stamp_dir / "last-success").exists()
    assert (run.stamp_dir / "success" / pvc).exists()


def test_a_filled_declared_volume_fails_and_stamps_nothing(tmp_path: Path) -> None:
    """FLAGGED half: over the ceiling, an empty restore is a failure and proves nothing."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    pvc = _declared()
    run = harness(tmp_path, [pvc], actual_sizes={pvc: _ceiling() + 1})
    proc = run(restore="pass", env={"K3S_STUB_PROBE": "files=0 bytes=0"})
    assert proc.returncode == 1
    assert "the waiver does not apply" in proc.stderr, proc.stderr
    # Neither stamp: check 7 would read the first as "the nightly drill is alive" and check 8 the
    # second as "this volume restores", both off a run that brought back nothing.
    assert not (run.stamp_dir / "last-success").exists()
    assert not (run.stamp_dir / "success" / pvc).exists()
