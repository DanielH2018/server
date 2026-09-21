"""`k3s_etcd_restore_gates.py` against stamps in `tmp_path`: each gate's clean/flagged pair, the
stop order, and the exit code naming the gate. No gate here reads the cluster.

Run: uv run pytest scripts/deploy_tools/tests/test_k3s_etcd_restore_gates.py
"""

import io
import subprocess
from pathlib import Path

import pytest
from deploy_tools import k3s_etcd_restore_gates as gates

_REPO = Path(__file__).resolve().parents[3]
_RUNBOOK = _REPO / "docs" / "k3s-etcd-restore.md"

NOW = 1_800_000_000.0
DAY = 86400.0


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


def _run(drill_dir, homelab_dir, now=NOW):
    out = io.StringIO()
    code = gates.run_gates(str(drill_dir), str(homelab_dir), now=now, out=out)
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


# ── the runner: order, stop, exit code ──────────────────────────────────────────────────────


def test_all_gates_passing_exits_zero(drill_dir, homelab_dir):
    code, out = _run(drill_dir, homelab_dir)
    assert code == 0, out
    assert out.count(" ok — ") == 2


def test_a_stale_listing_stops_at_gate_one(drill_dir, homelab_dir):
    code, out = _run(drill_dir, homelab_dir, now=NOW + 30 * DAY)
    assert code == 1
    assert "gate 2" not in out


def test_a_missing_baseline_is_gate_two(drill_dir, homelab_dir):
    (homelab_dir / gates.TOKEN_STAMP).unlink()
    code, out = _run(drill_dir, homelab_dir)
    assert code == 2
    assert "gate 1 ok" in out


def test_the_exit_codes_are_the_gate_positions():
    assert [g.number for g in gates.GATES] == [1, 2]
    assert gates.GATES[1].check is gates._gate_token


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
