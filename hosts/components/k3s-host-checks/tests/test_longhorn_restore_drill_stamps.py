#!/usr/bin/env python3
"""The restore drill's stamp ConfigMap, as checks 7 and 8, the upgrade gate and the state page read it.

All four read through `from_configmap`. An absent ConfigMap, or one with no `last-success`, is a
drill that has never passed (H2.3f dropped the host-directory fallback).

The tests after the divider run the backup-health reader as a subprocess against the shared stub
kubectl, whose ConfigMap branch answers `STUB_DRILL_CM`.

Run: uv run pytest hosts/components/k3s-host-checks/tests/test_longhorn_restore_drill_stamps.py
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


def test_the_configmap_arm_reads_every_key_checks_7_and_8_use():
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


def test_an_absent_or_stampless_configmap_is_a_drill_that_never_passed():
    """Neither is a pass: check 7 and the upgrade gate read `last_success` None as never run."""
    for doc in (None, _cm({}), _cm({"candidates": "pvc-z\n"})):
        got = stamps.from_configmap(doc, "homelab")
        assert got.last_success is None
        assert (
            got.where
            == "configmap homelab/longhorn-restore-drill-stamps key last-success"
        )


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
    """Control for the pairs below: the ConfigMap arm with every candidate seen."""
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


def test_reader_pages_on_a_stale_configmap_stamp(tmp_path):
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


def test_reader_pages_on_an_unseen_candidate_past_the_configmaps_creation_time(
    tmp_path,
):
    """The reader hands check 8 the ConfigMap's creation time for a candidate with no seen entry.

    `pvc-b` has no `seen.pvc-b`, and the ConfigMap was created 30 days ago, past the 7-day window.
    A reader that dropped `seen_default` would skip `pvc-b` and print `up`.
    """
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(READER_NOW - 60))
    cm = _drill_cm(
        {
            "last-success": str(int(READER_NOW - 3600)),
            "candidates": "pvc-a\npvc-b\n",
            "seen.pvc-a": str(int(READER_NOW - 3600)),
        }
    )
    proc = _run_reader_against(stub, tmp_path, STUB_DRILL_CM=cm)

    assert proc.stdout.startswith("down\t"), proc.stdout
    assert "not restore-proven in 7d: pvc-b" in proc.stdout, proc.stdout


def test_reader_pages_on_a_configmap_with_no_last_success_beside_a_fresh_directory(
    tmp_path,
):
    """The directory the drill wrote before H2.3c is no evidence: no `last-success` is never run."""
    drill_dir = tmp_path / "drill"
    drill_dir.mkdir()
    (drill_dir / "last-success").write_text(str(int(READER_NOW - 3600)))
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(READER_NOW - 60))
    proc = _run_reader_against(
        stub,
        tmp_path,
        STUB_DRILL_CM=_drill_cm({"candidates": ""}),
        LONGHORN_RESTORE_DRILL_STAMP_DIR=str(drill_dir),
    )

    assert proc.stdout.startswith("down\t"), proc.stdout
    assert "no restore drill has ever succeeded" in proc.stdout, proc.stdout
