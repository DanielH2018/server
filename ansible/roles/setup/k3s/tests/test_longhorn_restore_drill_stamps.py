#!/usr/bin/env python3
"""The restore drill's two stores: the ConfigMap arm, the host-directory arm, and which one a run reads.

Checks 7 and 8, the Longhorn upgrade gate and the docs state page all pick their store through
`select_stamps`. The ConfigMap is read while it holds `last-success`; otherwise the host directory
is, which is the live state until the drill writes the ConfigMap (H2.3c).

The last three tests run the backup-health reader as a subprocess against the shared stub kubectl,
whose ConfigMap branch answers `STUB_DRILL_CM`.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_restore_drill_stamps.py
"""

import json

import pytest

from longhorn_lib import longhorn_restore_drill_stamps as stamps
from _longhorn_reader_stubs import (
    NOW as READER_NOW,
    _green_path_stub_kubectl,
    _rfc3339,
    _run_reader_against,
)

NOW = 1_800_000_000.0
CREATED = "2027-01-01T00:00:00Z"
CREATED_EPOCH = 1_798_761_600.0


def _cm(data: dict[str, str]) -> dict:
    return {
        "kind": "ConfigMap",
        "metadata": {"name": stamps.CONFIGMAP, "creationTimestamp": CREATED},
        "data": data,
    }


@pytest.fixture
def stamp_dir(tmp_path):
    """A host directory as the drill leaves it after a pass over two candidates."""
    (tmp_path / "seen").mkdir()
    (tmp_path / "success").mkdir()
    (tmp_path / "last-success").write_text("1700000000\n")
    (tmp_path / "candidates").write_text("pvc-a\npvc-b\n")
    (tmp_path / "seen" / "pvc-a").touch()
    (tmp_path / "success" / "pvc-a").write_text("1700000000\n")
    (tmp_path / "excluded_oversize").write_text("big\t5368709120\n")
    (tmp_path / "excluded_nopvc").write_text("orphan\n")
    return tmp_path


def test_the_configmap_arm_returns_the_directory_arms_shapes():
    doc = _cm(
        {
            "last-success": f"{int(NOW)}\n",
            "candidates": "pvc-a\npvc-b\n",
            "seen.pvc-a": f"{int(NOW - 3600)}",
            "seen.pvc-b": "not-an-epoch",
            "success.pvc-a": f"{int(NOW)}\n",
            "excluded_oversize": "big\t5368709120\n",
            "excluded_nopvc": "orphan\n",
            "attempts.pvc-a": f"{int(NOW)}",
        }
    )
    got = stamps.from_configmap(doc, "homelab")
    assert got == stamps.DrillStamps(
        where="configmap homelab/longhorn-restore-drill-stamps key last-success",
        last_success=f"{int(NOW)}",
        candidates=["pvc-a", "pvc-b"],
        seen={"pvc-a": NOW - 3600},
        seen_default=CREATED_EPOCH,
        success={"pvc-a": f"{int(NOW)}"},
        excluded_oversize=[("big", 5368709120)],
        excluded_nopvc=["orphan"],
    )


def test_the_directory_arm_has_no_seen_default(stamp_dir):
    got = stamps.from_directory(str(stamp_dir))
    assert got.last_success == "1700000000"
    assert got.candidates == ["pvc-a", "pvc-b"]
    assert list(got.seen) == ["pvc-a"]
    assert got.seen_default is None
    assert got.success == {"pvc-a": "1700000000"}
    assert got.excluded_oversize == [("big", 5368709120)]
    assert got.excluded_nopvc == ["orphan"]


def test_a_configmap_holding_last_success_is_the_store(stamp_dir):
    doc = _cm({"last-success": f"{int(NOW)}"})
    got = stamps.select_stamps(doc, "homelab", str(stamp_dir))
    assert got.last_success == f"{int(NOW)}" and got.candidates == []


def test_an_absent_or_stampless_configmap_reads_the_directory(stamp_dir):
    """An absent ConfigMap is the live state after H2.3a; an empty one, after H2.3b."""
    for doc in (None, _cm({}), _cm({"candidates": "pvc-z\n"})):
        got = stamps.select_stamps(doc, "homelab", str(stamp_dir))
        assert got.where == str(stamp_dir / "last-success")
        assert got.candidates == ["pvc-a", "pvc-b"]


def test_both_stores_absent_is_a_missing_stamp_not_a_pass(tmp_path):
    got = stamps.select_stamps(None, "homelab", str(tmp_path / "nowhere"))
    assert got.last_success is None and not got.last_success_unreadable


def test_an_empty_answer_is_an_absent_configmap_and_garbage_is_a_failed_read():
    assert stamps.parse_configmap("") is None
    assert stamps.parse_configmap('{"kind": "ConfigMap"}') == {"kind": "ConfigMap"}
    for body in ("null", '{"kind": "Secret"}', "not json"):
        with pytest.raises(ValueError, match="not a ConfigMap"):
            stamps.parse_configmap(body)


# ── through the reader: checks 7 and 8 read the ConfigMap arm ──────────────────────────────


def _drill_cm(data: dict[str, str]) -> str:
    """`STUB_DRILL_CM`: the stamp ConfigMap as `kubectl get -o json` prints it."""
    return json.dumps(
        {
            "kind": "ConfigMap",
            "metadata": {"creationTimestamp": _rfc3339(READER_NOW - 30 * 86400)},
            "data": data,
        }
    )


def test_reader_is_up_on_a_fresh_configmap_with_every_candidate_seen(tmp_path):
    """Control for the pair below: the ConfigMap arm, read in place of the directory."""
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(READER_NOW - 60))
    cm = _drill_cm(
        {
            "last-success": str(int(READER_NOW - 3600)),
            "candidates": "pvc-a\n",
            "seen.pvc-a": str(int(READER_NOW - 3600)),
        }
    )
    proc = _run_reader_against(stub, tmp_path, STUB_DRILL_CM=cm)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("up\t"), proc.stdout


def test_reader_pages_on_a_stale_configmap_stamp_over_a_fresh_directory(tmp_path):
    """The ConfigMap decides once it holds `last-success`; the directory stamp is fresh."""
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(READER_NOW - 60))
    cm = _drill_cm({"last-success": str(int(READER_NOW - 4 * 86400))})
    proc = _run_reader_against(stub, tmp_path, STUB_DRILL_CM=cm)

    assert proc.stdout.startswith("down\t"), proc.stdout
    assert "last successful restore drill was 4d ago" in proc.stdout, proc.stdout


def test_reader_pages_at_once_on_a_configmap_with_no_seen_entry(tmp_path):
    """The empty-seen guard, through the reader: the creation-time rule must not mask it."""
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(READER_NOW - 60))
    cm = _drill_cm(
        {"last-success": str(int(READER_NOW - 3600)), "candidates": "pvc-a\n"}
    )
    proc = _run_reader_against(stub, tmp_path, STUB_DRILL_CM=cm)

    assert proc.stdout.startswith("down\t"), proc.stdout
    assert "restore-drill state did not persist" in proc.stdout, proc.stdout
