#!/usr/bin/env python3
"""The restore drill's operator pin path: one volume on demand, without steering the rotation.

Until 2026-09-21 `PIN` was rendered from `k3s_longhorn_restore_drill_pvc` and nothing else read
it, so `PIN=<pvc> longhorn-restore-drill.sh` was overwritten on line 26 and the rotation's own
pick was drilled instead. PR #2182 printed exactly that non-working command as a remediation.
The only way to drill one volume was to backdate its attempt stamp so the next nightly run
picked it (#2183).

These tests RUN the rendered script rather than grep it. `k3s` and `logger` are bash functions
exported into the child through the environment, which is how bash resolves a command name
before it searches PATH — a stub on PATH would lose to the real `/usr/local/bin/k3s` the script
prepends. The stub answers the drill's reads from fixtures and either refuses the `apply` (the
selection tests stop there, with the attempt stamp already written) or plays the restore
through to a passing probe (the stamp tests).

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill_operator_pin.py
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment
from lib import yaml_fast
from _helpers import ANSIBLE

K3S = ANSIBLE / "roles" / "setup" / "k3s"
DRILL = K3S / "templates" / "longhorn-restore-drill.sh.j2"

OLDEST = "a-config"  # the rotation's own pick: oldest attempt stamp
PINNED = "b-config"  # attempted more recently, so never the rotation's pick
UNBACKED = "c-config"  # in a backup group but with no Completed backup


def _volume(pvc: str) -> dict:
    return {
        "metadata": {
            "name": f"pvc-{pvc}",
            "labels": {"recurring-job-group.longhorn.io/default": "enabled"},
        },
        "spec": {"size": "16777216", "backupBlockSize": "16777216"},
        "status": {
            "actualSize": 1024,
            "kubernetesStatus": {"pvcName": pvc, "namespace": "homelab"},
        },
    }


def _backup(pvc: str) -> dict:
    return {
        "metadata": {"name": f"backup-{pvc}"},
        "status": {
            "state": "Completed",
            "volumeName": f"pvc-{pvc}",
            "snapshotCreatedAt": "2026-09-20T00:00:00Z",
        },
    }


# The `k3s` stand-in. Dispatches on the argv the drill passes; every read comes from a fixture.
STUB = r"""
fixtures="$K3S_STUB_FIXTURES"
printf '%s\n' "$*" >>"$fixtures/calls"
case "$*" in
  *"get backups.longhorn.io -o json"*) cat "$fixtures/backups.json" ;;
  *"get volume -o json"*) cat "$fixtures/volumes.json" ;;
  *"get backuptarget"*) printf 's3://bucket@region/' ;;
  *"apply -f"*) [[ "$K3S_STUB_RESTORE" == pass ]] || return 1 ;;
  *"get volume restore-drill-"*) printf 'detached false' ;;
  *"get pod "*) printf 'Succeeded' ;;
  *"logs "*) printf 'files=3 bytes=4096\n' ;;
  *) : ;;
esac
"""


def _render(stamp_dir: Path) -> str:
    defaults = yaml_fast.safe_load((K3S / "defaults" / "main.yml").read_text())
    defaults["k3s_longhorn_restore_drill_stamp_dir"] = str(stamp_dir)
    defaults["sys_user"] = "ubuntu"
    return Environment().from_string(DRILL.read_text()).render(**defaults)


@pytest.fixture
def drill(tmp_path: Path):
    """A rendered drill plus a runner: `run(argv, env=..., restore=...)` -> CompletedProcess."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "volumes.json").write_text(
        json.dumps({"items": [_volume(OLDEST), _volume(PINNED), _volume(UNBACKED)]})
    )
    (fixtures / "backups.json").write_text(
        json.dumps({"items": [_backup(OLDEST), _backup(PINNED)]})
    )
    stamp_dir = tmp_path / "stamps"
    attempts = stamp_dir / "attempts"
    attempts.mkdir(parents=True)
    (attempts / OLDEST).touch()
    os.utime(attempts / OLDEST, (1_700_000_000, 1_700_000_000))
    (attempts / PINNED).touch()
    os.utime(attempts / PINNED, (1_750_000_000, 1_750_000_000))
    script = tmp_path / "drill.sh"
    script.write_text(_render(stamp_dir))

    def run(
        argv: list[str] | None = None, env: dict | None = None, restore: str = "fail"
    ):
        child_env = {
            "PATH": os.environ["PATH"],
            "K3S_STUB_FIXTURES": str(fixtures),
            "K3S_STUB_RESTORE": restore,
            "BASH_FUNC_k3s%%": f"() {{ {STUB} }}",
            "BASH_FUNC_logger%%": "() { :; }",
            **(env or {}),
        }
        return subprocess.run(
            ["bash", str(script), *(argv or [])],
            env=child_env,
            capture_output=True,
            text=True,
        )

    run.stamp_dir = stamp_dir
    return run


def _attempted(run, pvc: str) -> float:
    return (run.stamp_dir / "attempts" / pvc).stat().st_mtime


def _selected(run, proc: subprocess.CompletedProcess) -> str:
    """Which volume the drill tried to restore: the attempt stamp it refreshed before the apply."""
    assert proc.returncode == 1, proc.stderr
    assert "could not create the drill volume" in proc.stderr, proc.stderr
    attempts = run.stamp_dir / "attempts"
    return max(attempts.iterdir(), key=lambda p: p.stat().st_mtime).name


def test_nightly_run_drills_the_rotations_own_pick(drill) -> None:
    """Control: with no pin the least-recently-attempted volume is selected."""
    assert _selected(drill, drill()) == OLDEST


def test_argv_pin_drills_that_volume_not_the_rotations_pick(drill) -> None:
    """`longhorn-restore-drill.sh <pvc>` reaches the selection; the oldest stamp is untouched."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    before = _attempted(drill, OLDEST)
    assert _selected(drill, drill([PINNED])) == PINNED
    assert _attempted(drill, OLDEST) == before


def test_env_pin_drills_that_volume(drill) -> None:
    """`RESTORE_DRILL_PIN=<pvc>` is the same override for a caller that cannot pass argv."""
    assert _selected(drill, drill(env={"RESTORE_DRILL_PIN": PINNED})) == PINNED


def test_argv_beats_env(drill) -> None:
    assert (
        _selected(drill, drill([PINNED], env={"RESTORE_DRILL_PIN": OLDEST})) == PINNED
    )


def test_pin_leaves_the_published_candidate_list_whole(drill) -> None:
    """Check 8 sizes its coverage window from this file; a one-line list would shrink it."""
    drill([PINNED])
    assert (drill.stamp_dir / "candidates").read_text().split() == [OLDEST, PINNED]


def test_pin_naming_an_ineligible_volume_fails_naming_it(drill) -> None:
    """A pin with no Completed backup fails closed, naming the pin, and stamps no attempt."""
    proc = drill([UNBACKED])
    assert proc.returncode == 1
    assert f"pinned volume {UNBACKED} is not eligible" in proc.stderr, proc.stderr
    assert not (drill.stamp_dir / "attempts" / UNBACKED).exists()


def test_operator_pin_writes_the_volume_stamp_but_not_the_liveness_stamp(drill) -> None:
    """The issue's verify-by: `success/<pvc>` is written.

    `last-success` is check 7's "is the nightly drill alive", and a hand run must not refresh
    it over a dead cron.
    """
    proc = drill([PINNED], restore="pass")
    assert proc.returncode == 0, proc.stderr
    assert (drill.stamp_dir / "success" / PINNED).exists()
    assert not (drill.stamp_dir / "last-success").exists()


def test_nightly_run_writes_both_stamps(drill) -> None:
    proc = drill(restore="pass")
    assert proc.returncode == 0, proc.stderr
    assert (drill.stamp_dir / "success" / OLDEST).exists()
    assert (drill.stamp_dir / "last-success").exists()
