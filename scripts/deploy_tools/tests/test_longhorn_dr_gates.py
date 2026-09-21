"""`longhorn_dr_gates.py` against a fake kubectl: each gate's clean/flagged pair, the stop
order, and the exit code naming the gate.

Run: uv run pytest scripts/deploy_tools/tests/test_longhorn_dr_gates.py
"""

import io
import json
import subprocess
from pathlib import Path

import pytest
from _gates_fakes import fake_tools
from deploy_tools import longhorn_dr_gates as gates
from lib import kubectl

_REPO = Path(__file__).resolve().parents[3]
_RUNBOOK = _REPO / "docs" / "longhorn-disaster-recovery.md"


def _target(name: str, url: str, available) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"backupTargetURL": url},
        "status": {"available": available},
    }


def _backup_volume(name: str, target: str, pvc: tuple[str, str] | None) -> dict:
    labels = {}
    if pvc is not None:
        # Longhorn writes the PVC binding as a JSON document inside one label value.
        labels["KubernetesStatus"] = json.dumps(
            {"namespace": pvc[0], "pvcName": pvc[1], "pvStatus": "Bound"}
        )
    return {
        "metadata": {"name": name},
        "spec": {"backupTargetName": target},
        "status": {"labels": labels, "lastBackupName": "backup-1"},
    }


def _volume(name: str, pvc: tuple[str, str], from_backup: str = "") -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"fromBackup": from_backup},
        "status": {"kubernetesStatus": {"namespace": pvc[0], "pvcName": pvc[1]}},
    }


BOTH_ARMED = {
    "items": [
        _target("default", "s3://bucket@us-east-005/longhorn", True),
        _target("r2", "s3://bucket@auto/longhorn", True),
    ]
}
SYNCED = {
    "items": [
        _backup_volume("pvc-old-a-1234", "default", ("homelab", "bazarr-config")),
        _backup_volume("pvc-old-b-5678", "r2", ("homelab", "authelia-config")),
        _backup_volume("pvc-old-c-9abc", "default", None),
    ]
}
NO_VOLUMES = {"items": []}


@pytest.fixture(autouse=True)
def _fresh_identity():
    kubectl.forget_served_cluster()
    yield
    kubectl.forget_served_cluster()


def _run(targets=BOTH_ARMED, backup_volumes=SYNCED, volumes=NO_VOLUMES):
    tools = fake_tools(
        {
            gates.TARGETS_ARGS: targets,
            gates.BACKUP_VOLUMES_ARGS: backup_volumes,
            gates.VOLUMES_ARGS: volumes,
        }
    )
    out = io.StringIO()
    code = gates.run_gates(tools=tools, out=out)
    return code, out.getvalue()


# ── gate 1: both targets ────────────────────────────────────────────────────────────────────


def test_both_targets_armed_and_available_is_clean():
    assert gates.unreachable_targets(BOTH_ARMED, gates.REQUIRED_TARGETS) == []


def test_a_b2_only_bring_up_is_flagged():
    doc = {
        "items": [
            _target("default", "s3://bucket@us-east-005/longhorn", True),
            _target("r2", "", False),
        ]
    }
    assert gates.unreachable_targets(doc, gates.REQUIRED_TARGETS) == [
        "r2 (disarmed: backupTargetURL is empty)"
    ]
    assert gates.unreachable_targets({"items": []}, gates.REQUIRED_TARGETS) == [
        "default (no such backup target)",
        "r2 (no such backup target)",
    ]


# ── gate 2: the sync ────────────────────────────────────────────────────────────────────────


def test_a_backup_volume_on_every_target_is_clean():
    assert gates.unsynced_targets(SYNCED) == []


def test_a_target_with_no_backup_volume_is_flagged():
    doc = {"items": [_backup_volume("x", "default", None)]}
    found = gates.unsynced_targets(doc)
    assert len(found) == 1 and found[0].startswith("r2 (no BackupVolume yet")
    assert len(gates.unsynced_targets({"items": []})) == 2


# ── gate 3: nothing provisioned empty ───────────────────────────────────────────────────────


def test_no_live_volume_or_a_restored_one_is_clean():
    assert gates.provisioned_empty(SYNCED, NO_VOLUMES) == []
    restored = {
        "items": [
            _volume(
                "pvc-new-1", ("homelab", "bazarr-config"), "s3://...?backup=b&volume=v"
            )
        ]
    }
    assert gates.provisioned_empty(SYNCED, restored) == []


def test_an_empty_volume_under_a_backed_up_name_is_flagged():
    live = {
        "items": [
            _volume("pvc-new-1", ("homelab", "bazarr-config")),
            _volume("pvc-new-2", ("homelab", "no-backup-here")),
        ]
    }
    assert gates.provisioned_empty(SYNCED, live) == [
        "homelab/bazarr-config is bound to pvc-new-1, provisioned empty — "
        "a deploy ran before the restore (backup volume pvc-old-a-1234)"
    ]


def test_a_backup_volume_without_a_pvc_label_has_no_name_to_collide_with():
    assert gates.pvc_of_backup(_backup_volume("x", "default", None)) is None
    assert (
        gates.pvc_of_backup({"status": {"labels": {"KubernetesStatus": "not json"}}})
        is None
    )
    assert gates.pvc_of_backup(
        _backup_volume("x", "default", ("homelab", "bazarr-config"))
    ) == ("homelab", "bazarr-config")


def test_a_failed_read_never_passes_a_gate():
    assert gates.unreachable_targets(None, gates.REQUIRED_TARGETS)
    assert gates.unsynced_targets(None)
    assert gates.provisioned_empty(None, NO_VOLUMES)
    assert gates.provisioned_empty(SYNCED, None)


# ── the runner: order, stop, exit code ──────────────────────────────────────────────────────


def test_all_gates_passing_exits_zero():
    code, out = _run()
    assert code == 0, out
    assert out.count(" ok — ") == 3


@pytest.mark.parametrize(
    ("number", "docs"),
    [
        (1, {"targets": {"items": [_target("default", "", False)]}}),
        (2, {"backup_volumes": {"items": []}}),
        (
            3,
            {
                "volumes": {
                    "items": [_volume("pvc-new-1", ("homelab", "bazarr-config"))]
                }
            },
        ),
    ],
)
def test_the_exit_code_is_the_first_failing_gate(number, docs):
    code, out = _run(**docs)
    assert code == number
    assert f"gate {number} FAILED" in out
    assert f"gate {number + 1} " not in out


def test_the_exit_codes_are_the_gate_positions():
    assert [g.number for g in gates.GATES] == [1, 2, 3]
    assert gates.GATES[2].check is gates._gate_not_provisioned


# ── the runbook names the script ────────────────────────────────────────────────────────────


def test_the_runbook_calls_the_script_and_it_runs():
    script = "scripts/deploy_tools/longhorn_dr_gates.py"
    assert script in _RUNBOOK.read_text()
    proc = subprocess.run(
        ["uv", "run", "python", str(_REPO / script), "--bogus"],
        capture_output=True,
        text=True,
        cwd=_REPO,
        check=False,
    )
    assert proc.returncode == 64, proc.stderr
