"""`k3s_etcd_restore_gates.py` against stamps in `tmp_path` and a fake kubectl: each gate's
clean/flagged pair, the stop order, and the exit code naming the gate.

Run: uv run pytest scripts/deploy_tools/tests/test_k3s_etcd_restore_gates.py
"""

import io
import subprocess
from pathlib import Path

import pytest
from _gates_fakes import failing_read, fake_tools
from deploy_tools import k3s_etcd_restore_gates as gates

_REPO = Path(__file__).resolve().parents[3]
_RUNBOOK = _REPO / "docs" / "k3s-etcd-restore.md"

NOW = 1_800_000_000.0
DAY = 86400.0

# The off-box cron's naming: `offbox-<node>-<unix-timestamp>.zip`, compressed, and the
# `.zip` is part of the name `--cluster-reset-restore-path` takes.
SNAPSHOT = "offbox-daniel-box-1789958702.zip"


def _stamp(epoch: float, mode: str = "list-only") -> str:
    # The drill script's own format, four `key=value` lines.
    return (
        f"mode={mode}\nsnapshot=offbox-daniel-box-1789958702.zip\n"
        f"utc=2026-09-21T10:20:03Z\nepoch={int(epoch)}\n"
    )


@pytest.fixture
def drill_dir(tmp_path):
    d = tmp_path / "etcd-restore-drill"
    d.mkdir()
    (d / gates.DRILL_STAMP).write_text(_stamp(NOW - DAY))
    return d


@pytest.fixture
def homelab_dir(tmp_path):
    d = tmp_path / "homelab"
    d.mkdir()
    (d / gates.TOKEN_STAMP).write_text("0" * 64 + "\n")
    return d


def _snapshot_file(name: str, ready=True, location: str = "", error: str = "") -> dict:
    status: dict = {"readyToUse": ready}
    if error:
        status["error"] = {"message": error}
    return {
        # k3s names the CR after the node and a hash, never after the snapshot; the name the
        # restore flag takes is `spec.snapshotName`, which is why the gate matches on it.
        "metadata": {"name": f"s3-{name}-9f2c1a"},
        "spec": {
            "snapshotName": name,
            "location": location or f"s3://bucket/etcd-snapshots/{name}",
        },
        "status": status,
    }


def _tools(*snapshot_files):
    return fake_tools({gates.SNAPSHOT_FILES_ARGS: {"items": list(snapshot_files)}})


@pytest.fixture
def tools():
    return _tools(_snapshot_file(SNAPSHOT))


def _run(drill_dir, homelab_dir, tools, snapshot=SNAPSHOT, now=NOW):
    out = io.StringIO()
    code = gates.run_gates(
        snapshot, str(drill_dir), str(homelab_dir), now=now, tools=tools, out=out
    )
    return code, out.getvalue()


# ── gate 1: the listing leg ─────────────────────────────────────────────────────────────────


def test_a_fresh_list_only_stamp_is_clean(drill_dir):
    assert gates.unproven_listing(drill_dir, now=NOW) == []


def test_a_stale_stamp_is_flagged_with_its_age_and_snapshot(drill_dir):
    (drill_dir / gates.DRILL_STAMP).write_text(_stamp(NOW - 9 * DAY))
    found = gates.unproven_listing(drill_dir, now=NOW)
    assert found == [
        "the list-only drill last passed 9.0 days ago "
        "(window 8 days; snapshot offbox-daniel-box-1789958702.zip)"
    ]


def test_a_full_mode_stamp_does_not_prove_the_listing(drill_dir):
    # The list-only leg and the full drill write different files; a stamp claiming the
    # wrong mode is a future format, not a pass.
    (drill_dir / gates.DRILL_STAMP).write_text(_stamp(NOW, mode="full"))
    assert gates.unproven_listing(drill_dir, now=NOW) == [
        f"{drill_dir / gates.DRILL_STAMP} records mode=full, not list-only"
    ]


def test_a_stamp_without_an_epoch_is_flagged(drill_dir):
    (drill_dir / gates.DRILL_STAMP).write_text("mode=list-only\n")
    assert "no readable epoch" in gates.unproven_listing(drill_dir, now=NOW)[0]


def test_an_absent_stamp_or_directory_is_flagged(drill_dir, tmp_path):
    (drill_dir / gates.DRILL_STAMP).unlink()
    assert "has never passed here" in gates.unproven_listing(drill_dir, now=NOW)[0]
    found = gates.unproven_listing(tmp_path / "nowhere", now=NOW)
    assert len(found) == 1 and "not a directory" in found[0]


# ── gate 2: the token baseline ──────────────────────────────────────────────────────────────


def test_an_existing_token_stamp_is_clean(homelab_dir):
    assert gates.missing_token_baseline(homelab_dir) == []


def test_a_missing_token_stamp_or_directory_is_flagged(homelab_dir, tmp_path):
    (homelab_dir / gates.TOKEN_STAMP).unlink()
    found = gates.missing_token_baseline(homelab_dir)
    assert len(found) == 1 and "copy /var/lib/rancher/k3s/server/token" in found[0]
    found = gates.missing_token_baseline(tmp_path / "nowhere")
    assert len(found) == 1 and "not a directory" in found[0]


def test_the_stamp_is_checked_for_existence_not_read(homelab_dir):
    # The real stamp is root-only 0600; an unreadable one must still pass.
    (homelab_dir / gates.TOKEN_STAMP).chmod(0o000)
    try:
        assert gates.missing_token_baseline(homelab_dir) == []
    finally:
        (homelab_dir / gates.TOKEN_STAMP).chmod(0o600)


# ── gate 3: the named snapshot ──────────────────────────────────────────────────────────────


def test_a_plain_snapshot_name_is_clean():
    assert gates.unusable_snapshot_name(SNAPSHOT) == []


def test_a_path_is_refused_without_asking_the_cluster():
    # `--cluster-reset-restore-path` takes a NAME; a path there resolves to no snapshot, and
    # k3s only says so once it has been stopped. No document is passed: this half asks nothing.
    found = gates.unusable_snapshot_name(
        f"/var/lib/rancher/k3s/server/db/snapshots/{SNAPSHOT}"
    )
    assert len(found) == 1 and "takes a NAME" in found[0]
    assert "<no snapshot name given" in gates.unusable_snapshot_name("  ")[0]


def test_a_ready_snapshot_the_cluster_records_is_clean():
    doc = {"items": [_snapshot_file(SNAPSHOT)]}
    assert gates.snapshot_not_restorable(SNAPSHOT, doc) == []


def test_an_unknown_name_is_flagged_and_lists_what_the_cluster_records():
    # The two ways to get here need different fixes — a typo, or a bucket k3s has not
    # reconciled into CRs — so the offender names the snapshots that DO have a record.
    doc = {"items": [_snapshot_file("etcd-daniel-box-1789958700.zip")]}
    found = gates.snapshot_not_restorable(SNAPSHOT, doc)
    assert len(found) == 1
    assert f"no ETCDSnapshotFile records {SNAPSHOT}" in found[0]
    assert "records etcd-daniel-box-1789958700.zip" in found[0]
    assert "no snapshots at all" in gates.snapshot_not_restorable(SNAPSHOT, {})[0]


def test_a_snapshot_that_is_not_ready_to_use_is_flagged_with_its_error():
    doc = {
        "items": [
            _snapshot_file(SNAPSHOT, ready=False, error="failed to upload snapshot")
        ]
    }
    found = gates.snapshot_not_restorable(SNAPSHOT, doc)
    assert len(found) == 1
    assert "readyToUse=False" in found[0] and "failed to upload snapshot" in found[0]
    # A CR with no `readyToUse` at all is not a pass either: absent is not true.
    doc = {"items": [{"spec": {"snapshotName": SNAPSHOT}}]}
    assert "readyToUse=None" in gates.snapshot_not_restorable(SNAPSHOT, doc)[0]


# ── the runner: order, stop, exit code ──────────────────────────────────────────────────────


def test_all_gates_passing_exits_zero(drill_dir, homelab_dir, tools):
    code, out = _run(drill_dir, homelab_dir, tools)
    assert code == 0, out
    assert out.count(" ok — ") == 3


def test_a_stale_listing_stops_at_gate_one(drill_dir, homelab_dir, tools):
    code, out = _run(drill_dir, homelab_dir, tools, now=NOW + 30 * DAY)
    assert code == 1
    assert "gate 2" not in out


def test_a_missing_baseline_is_gate_two(drill_dir, homelab_dir, tools):
    (homelab_dir / gates.TOKEN_STAMP).unlink()
    code, out = _run(drill_dir, homelab_dir, tools)
    assert code == 2
    assert "gate 1 ok" in out and "gate 3" not in out


def test_a_snapshot_the_cluster_cannot_restore_is_gate_three(drill_dir, homelab_dir):
    code, out = _run(
        drill_dir, homelab_dir, _tools(_snapshot_file(SNAPSHOT, ready=False))
    )
    assert code == 3
    assert "gate 2 ok" in out and "readyToUse=False" in out


def test_a_forbidden_snapshot_read_is_not_a_verdict(drill_dir, homelab_dir, tools):
    # Until `k3s.cattle.io` is in k3s_readonly_crd_api_groups the read is Forbidden. That is
    # "could not look", so it must exit 69 rather than pass gate 3 or fail it as gate 3.
    code, out = _run(
        drill_dir, homelab_dir, failing_read(tools, "etcdsnapshotfiles.k3s.cattle.io")
    )
    assert code == gates.runbook_gates.EX_UNAVAILABLE
    assert "cannot ask the cluster" in out


def test_the_exit_codes_are_the_gate_positions():
    assert [g.number for g in gates.GATES] == [1, 2, 3]
    assert gates.GATES[1].check is gates._gate_token
    assert gates.GATES[2].check is gates._gate_snapshot


def test_the_snapshot_name_is_required():
    # `cli(takes=1)`: no name, a flag, and a second positional are each usage — none of them
    # runs a gate, so neither the stamps nor the cluster are read.
    assert gates.main([]) == gates.runbook_gates.EX_USAGE
    assert gates.main(["--bogus"]) == gates.runbook_gates.EX_USAGE
    assert gates.main([SNAPSHOT, "extra"]) == gates.runbook_gates.EX_USAGE


# ── the runbook names the script ────────────────────────────────────────────────────────────


def test_the_runbook_calls_the_script_and_it_runs():
    script = "scripts/deploy_tools/k3s_etcd_restore_gates.py"
    assert script in _RUNBOOK.read_text()
    proc = subprocess.run(
        ["uv", "run", "python", str(_REPO / script), "--bogus"],
        capture_output=True,
        text=True,
        cwd=_REPO,
        check=False,
    )
    assert proc.returncode == 64, proc.stderr
