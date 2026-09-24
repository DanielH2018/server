"""The etcd restore drill's gate-3 call and the order it runs in, issue #2420.

`scripts/deploy_tools/k3s_etcd_restore_gates.py` gate 3 asks the cluster whether a named
snapshot has an `ETCDSnapshotFile` reporting `readyToUse`. Until 2026-09-24 the runbook was its
only caller, so the gate ran on the day of a real restore and never before it — a k3s change to
that CR, or to the readonly ServiceAccount's access to it, would first surface during the
outage. The weekly `--list-only` drill now runs it against the snapshot it just listed.

Two behaviours, and the second is the one that makes the first worth anything:

  * a refusal fails the drill, whatever the exit code — 69 ("could not ask the cluster") is the
    RBAC half of what the call exercises, so passing on it would leave the blind spot open;
  * a refusal leaves NO `last-success-list-only` stamp. Restore gate 1 reads that stamp as
    proof this leg works, so a drill that stamped first and refused afterwards would leave the
    gate's own evidence in place and the call would be cosmetic.

`finish_list_only()` exists in the script to make that order drivable here, the same way
`verify_restored_objects()` does for the thresholds in `test_etcd_restore_drill_verify.py`. The
script's `BASH_SOURCE` guard returns before argument parsing, the root check and the live S3
credentials, so sourcing it runs none of the drill.

Run: uv run pytest ansible/tests/setup/test_etcd_restore_drill_gate.py
"""

import os
import subprocess
from pathlib import Path

import pytest

from _helpers import REPO

_SCRIPT = REPO / "scripts" / "backup" / "etcd_restore_drill.sh"
_SNAPSHOT = "offbox-daniel-box-1789958702.zip"


def _finish_list_only(gate_exit: int, stamp_dir) -> subprocess.CompletedProcess:
    """Source the drill and run its `--list-only` tail against a gate stub exiting `gate_exit`.

    The stub stands in for the whole `k3s_etcd_restore_gates.py --gate 3` invocation, so nothing
    here needs a cluster, a kubeconfig or an interpreter of its own — the same way the verify
    test's stub `k3s` stands in for the real kubectl pipeline.
    """
    script = f"""
    STAMP_DIR="{stamp_dir}"
    source "{_SCRIPT}"
    STAMP_DIR="{stamp_dir}"
    SNAPSHOT={_SNAPSHOT}
    GATE_CMD=(bash -c 'exit {gate_exit}')
    finish_list_only
    echo "LIST_ONLY_OK"
    """
    return subprocess.run(["bash", "-c", script, "_"], capture_output=True, text=True)


def test_a_passing_gate_lets_the_drill_stamp(tmp_path):
    """The accepting half: gate 3 clean, so the list-only leg is recorded as proven."""
    proc = _finish_list_only(0, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "LIST_ONLY_OK" in proc.stdout
    stamp = tmp_path / "last-success-list-only"
    assert stamp.is_file(), proc.stdout
    assert "mode=list-only" in stamp.read_text()


@pytest.mark.parametrize(
    "gate_exit",
    [
        # Gate 3's own refusal: the cluster records no restorable ETCDSnapshotFile for the name.
        3,
        # `EX_UNAVAILABLE` — no kubectl, an unreadable kubeconfig, the wrong cluster, or a
        # Forbidden read of `etcdsnapshotfiles`. Fail closed: that read is what this call
        # exercises, so treating "could not look" as a pass reopens the gap.
        69,
        # Usage, which is what a rename of the script or of `--gate` would produce. A drill that
        # passed on it would report a gate that never ran as a gate that passed.
        64,
    ],
)
def test_a_refusing_gate_fails_the_drill_and_leaves_no_stamp(tmp_path, gate_exit):
    proc = _finish_list_only(gate_exit, tmp_path)
    assert proc.returncode != 0, proc.stdout
    assert f"restore gate 3 refused {_SNAPSHOT} (exit {gate_exit})" in proc.stderr
    assert "LIST_ONLY_OK" not in proc.stdout
    assert not (tmp_path / "last-success-list-only").exists(), (
        "a refused gate stamped the leg as proven — restore gate 1 reads that stamp"
    )


def test_the_gate_command_names_the_gate_the_drill_can_actually_run():
    """Non-vacuity: `GATE_CMD` must name the real script and gate 3, not just any command.

    The two tests above pass against whatever `GATE_CMD` holds, so without this they would stay
    green against an array pointing at nothing. This reads the SHIPPED array — whichever of its
    two branches this checkout takes, the `.venv` interpreter or the `uv run` fallback — and
    checks that its interpreter is executable and the script it names exists. The branch not
    taken is not exercised; `.venv` is gitignored, so which one runs depends on the checkout.
    """
    out = subprocess.run(
        ["bash", "-c", f'source "{_SCRIPT}"; printf "%s\\n" "${{GATE_CMD[@]}}"', "_"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    assert "--gate" in out
    assert out[out.index("--gate") + 1] == "3"
    named = [a for a in out if a.endswith("k3s_etcd_restore_gates.py")]
    assert len(named) == 1, out
    assert Path(named[0]).is_file(), named
    assert os.access(out[0], os.X_OK), (
        f"GATE_CMD's interpreter {out[0]} is not executable, so the gate would never run"
    )
    from deploy_tools import k3s_etcd_restore_gates as gates

    assert gates.GATES[2].check is gates._gate_snapshot, (
        "gate 3 is no longer the snapshot gate, so --gate 3 drills the wrong stop condition"
    )
