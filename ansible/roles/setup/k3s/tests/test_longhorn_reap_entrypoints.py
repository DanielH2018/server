#!/usr/bin/env python3
"""Transport tests for the two reap-orphan entry points, run as real subprocesses.

Everything decision-shaped is covered against fixtures in test_longhorn_reap_logic.py; this
file proves the I/O shell around it — that a dry run never calls `kubectl delete`, that --apply
emits exactly the delete argv the classifier chose, and that the sys.path bootstrap
(`sys.path.insert(0, dirname(__file__)); import host_lib`) resolves when the script is invoked
directly rather than imported, which is how it actually runs in production (uv run
<path>/longhorn_reap_orphan_backups.py).

A stub `k3s` script on PATH stands in for the real binary: `host_lib.kubectl_runner` runs
`k3s kubectl -n <namespace> <args>`, so the stub only needs to answer `kubectl get ... -o json`
with fixture JSON and record every `kubectl delete` argv it receives, keyed off argv[0] (`k3s`).

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_reap_entrypoints.py
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

FILES = pathlib.Path(__file__).resolve().parents[1] / "files"
REPO = pathlib.Path(__file__).resolve().parents[5]
# host_lib.py is a cross-role shared module (ansible/roles/setup/common/files), staged into
# /opt/longhorn-reap/ as a sibling by the Ansible copy task — it does not live in this role's
# own files/ in the repo. Deploying is what makes the two entry points' sibling directories the
# same one; `_deployed_entry` below reproduces that shape in a tmp dir so the sys.path bootstrap
# (`sys.path.insert(0, dirname(__file__)); import host_lib`) is exercised for real rather than
# relying on pytest's own pythonpath, which a bare subprocess does not inherit.
HOST_LIB = REPO / "ansible" / "roles" / "setup" / "common" / "files" / "host_lib.py"
BACKUPS_ENTRY = FILES / "longhorn_reap_orphan_backups.py"
SNAPSHOTS_ENTRY = FILES / "longhorn_reap_orphan_snapshots.py"


def _deployed_entry(entry: pathlib.Path, deploy_dir: pathlib.Path) -> pathlib.Path:
    deploy_dir.mkdir(exist_ok=True)
    for src in (
        FILES / "longhorn_reap_logic.py",
        FILES / "longhorn_reap_orphan_backups.py",
        FILES / "longhorn_reap_orphan_snapshots.py",
        HOST_LIB,
    ):
        shutil.copy(src, deploy_dir / src.name)
    return deploy_dir / entry.name


_STUB_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

CALLS_LOG = os.environ["STUB_CALLS_LOG"]
FIXTURES = json.loads(os.environ["STUB_FIXTURES"])

argv = sys.argv[1:]
with open(CALLS_LOG, "a") as fh:
    fh.write(json.dumps(argv) + "\\n")

if argv[:1] == ["kubectl"] and "get" in argv:
    # e.g. "volumes.longhorn.io" -> "volumes"; a bare resource like "pods" is unchanged.
    kind = argv[argv.index("get") + 1].split(".", 1)[0]
    print(json.dumps({"items": FIXTURES.get(kind, [])}))
    sys.exit(0)
if argv[:1] == ["kubectl"] and "delete" in argv:
    sys.exit(0)
sys.exit(0)
"""


def _volume(name, group, state="attached"):
    return {
        "metadata": {
            "name": name,
            "labels": {"recurring-job-group.longhorn.io/%s" % group: "enabled"},
        },
        "status": {"state": state},
    }


def _backup(name, vol, created, job, state="Completed"):
    return {
        "metadata": {"name": name},
        "status": {
            "volumeName": vol,
            "snapshotCreatedAt": created,
            "labels": {"RecurringJob": job} if job else {},
            "state": state,
        },
    }


def _snapshot(name, vol, created, job=None):
    status = {"creationTime": created}
    if job is not None:
        status["labels"] = {"RecurringJob": job}
    return {"metadata": {"name": name}, "spec": {"volume": vol}, "status": status}


def _run(entry, args, fixtures, tmp_path, *, admin_readable=False):
    deployed = _deployed_entry(entry, tmp_path / "opt")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "k3s"
    stub.write_text(_STUB_KUBECTL)
    stub.chmod(0o755)

    calls_log = tmp_path / "calls.jsonl"
    calls_log.write_text("")

    env = dict(os.environ)
    env["PATH"] = "%s:%s" % (stub_dir, env.get("PATH", ""))
    env["STUB_CALLS_LOG"] = str(calls_log)
    env["STUB_FIXTURES"] = json.dumps(fixtures)
    env["LONGHORN_REAP_KUBECTL"] = "k3s kubectl"
    env["LONGHORN_REAP_READONLY_KUBECONFIG"] = str(tmp_path / "readonly.yaml")
    # The real admin kubeconfig is root-only at a fixed path; both entry points read this
    # override (falling back to the real path when unset) purely so a test can supply a
    # fixture instead of needing root.
    if admin_readable:
        admin_path = tmp_path / "admin.yaml"
        admin_path.write_text("stub-admin-kubeconfig\n")
        env["LONGHORN_REAP_ADMIN_KUBECONFIG"] = str(admin_path)
    else:
        env["LONGHORN_REAP_ADMIN_KUBECONFIG"] = str(tmp_path / "no-such-admin.yaml")

    proc = subprocess.run(
        [sys.executable, str(deployed), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    calls = [
        json.loads(line) for line in calls_log.read_text().splitlines() if line.strip()
    ]
    return proc, calls


# ── sys.path bootstrap ───────────────────────────────────────────────────────────────────


def test_backups_entrypoint_resolves_its_sibling_imports_when_run_directly(tmp_path):
    proc, _calls = _run(BACKUPS_ENTRY, [], {"volumes": []}, tmp_path)
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr


def test_snapshots_entrypoint_resolves_its_sibling_imports_when_run_directly(tmp_path):
    proc, _calls = _run(SNAPSHOTS_ENTRY, [], {"volumes": []}, tmp_path)
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr


# ── backups: dry run emits no delete ────────────────────────────────────────────────────


def test_backups_dry_run_emits_no_delete_call(tmp_path):
    fixtures = {
        "volumes": [_volume("vol-a", "daily-backup")],
        "backups": [
            _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "daily-backup"),
            _backup("stray-1", "vol-a", "2026-08-14T00:00:00Z", "weekly-backup"),
        ],
    }
    proc, calls = _run(BACKUPS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "dry run" in proc.stdout
    assert not any("delete" in c for c in calls)


def test_backups_apply_without_admin_kubeconfig_refuses_and_deletes_nothing(tmp_path):
    # The entry point checks the real /etc/rancher/k3s/k3s.yaml, unreadable in this sandbox —
    # so --apply must refuse before making any kubectl call at all, the same floor
    # resolve_kubeconfig's unit test proves in isolation.
    fixtures = {"volumes": [], "backups": []}
    proc, calls = _run(BACKUPS_ENTRY, ["--apply"], fixtures, tmp_path)
    assert proc.returncode == 1
    assert "admin kubeconfig" in proc.stderr
    assert calls == []


def test_backups_unknown_flag_is_rejected(tmp_path):
    proc, calls = _run(BACKUPS_ENTRY, ["--bogus"], {"volumes": []}, tmp_path)
    assert proc.returncode == 2
    assert calls == []


def test_backups_abort_on_unresolvable_ownership_makes_no_delete_call(tmp_path):
    fixtures = {
        "volumes": [{"metadata": {"name": "vol-a", "labels": {}}, "status": {}}],
        "backups": [],
    }
    proc, calls = _run(BACKUPS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 1
    assert "ABORT" in proc.stderr
    assert not any("delete" in c for c in calls)
    assert not any(
        "backups.longhorn.io" in c for c in calls
    )  # abort before reading backups


def test_backups_apply_emits_exactly_the_delete_argv_the_classifier_chose(tmp_path):
    fixtures = {
        "volumes": [_volume("vol-a", "weekly-backup-d3")],
        "backups": [
            _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "weekly-backup-d3"),
            _backup("stray-2", "vol-a", "2026-08-17T00:00:00Z", "daily-backup"),
            _backup("stray-1", "vol-a", "2026-08-16T00:00:00Z", "daily-backup"),
        ],
    }
    proc, calls = _run(
        BACKUPS_ENTRY, ["--apply"], fixtures, tmp_path, admin_readable=True
    )
    assert proc.returncode == 0, proc.stderr
    deletes = [c for c in calls if "delete" in c]
    # stray-2 is the newer stray and is kept as the FLOOR 2 floor; only stray-1 is reapable.
    assert deletes == [
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "delete",
            "backups.longhorn.io",
            "stray-1",
            "--ignore-not-found",
        ]
    ]


def test_backups_apply_deleted_volumes_only_deletes_the_orphaned_bucket(tmp_path):
    # A backup whose volume no longer exists must be reaped only under
    # --apply-deleted-volumes, never under a bare --apply.
    fixtures = {
        "volumes": [],
        "backups": [
            _backup("stray", "gone-vol", "2026-08-14T00:00:00Z", "daily-backup")
        ],
    }
    plain_apply, plain_calls = _run(
        BACKUPS_ENTRY, ["--apply"], fixtures, tmp_path, admin_readable=True
    )
    assert plain_apply.returncode == 0, plain_apply.stderr
    assert not any("delete" in c for c in plain_calls)

    with_flag, calls = _run(
        BACKUPS_ENTRY,
        ["--apply-deleted-volumes"],
        fixtures,
        tmp_path,
        admin_readable=True,
    )
    assert with_flag.returncode == 0, with_flag.stderr
    deletes = [c for c in calls if "delete" in c]
    assert deletes == [
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "delete",
            "backups.longhorn.io",
            "stray",
            "--ignore-not-found",
        ]
    ]


# ── snapshots: dry run emits no delete ──────────────────────────────────────────────────


def test_snapshots_dry_run_emits_no_delete_call(tmp_path):
    fixtures = {
        "recurringjobs": [
            {"metadata": {"name": "daily-backup"}, "spec": {"groups": ["daily-backup"]}}
        ],
        "volumes": [_volume("vol-a", "daily-backup")],
        "snapshots": [
            _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
            _snapshot("stale", "vol-a", "2026-08-01T00:00:00Z", job="weekly-backup"),
        ],
    }
    proc, calls = _run(SNAPSHOTS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "dry run" in proc.stdout
    assert not any("delete" in c for c in calls)


def test_snapshots_apply_without_admin_kubeconfig_refuses_and_deletes_nothing(tmp_path):
    fixtures = {"recurringjobs": [], "volumes": [], "snapshots": []}
    proc, calls = _run(SNAPSHOTS_ENTRY, ["--apply"], fixtures, tmp_path)
    assert proc.returncode == 1
    assert "admin kubeconfig" in proc.stderr
    assert calls == []


def test_snapshots_apply_emits_exactly_the_delete_argv_the_classifier_chose(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LONGHORN_REAP_MIN_AGE_DAYS", "3")
    fixtures = {
        "recurringjobs": [
            {"metadata": {"name": "daily-backup"}, "spec": {"groups": ["daily-backup"]}}
        ],
        "volumes": [_volume("vol-a", "daily-backup")],
        "snapshots": [
            _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
            _snapshot("stale", "vol-a", "2026-08-01T00:00:00Z", job="weekly-backup"),
        ],
        # A pod on a DIFFERENT node than this process's hostname, so the purge step finds no
        # ready manager pod locally and the run reports that rather than hanging on a real
        # connection — same "no ready backend" branch the module docstring explains.
        "pods": [
            {
                "spec": {"nodeName": "not-this-host"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"ready": True}],
                    "podIP": "127.0.0.1",
                },
            }
        ],
    }
    proc, calls = _run(
        SNAPSHOTS_ENTRY, ["--apply"], fixtures, tmp_path, admin_readable=True
    )
    deletes = [c for c in calls if "delete" in c]
    assert deletes == [
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "delete",
            "snapshots.longhorn.io",
            "stale",
            "--ignore-not-found",
            "--timeout=120s",
        ]
    ]
    # The delete itself succeeded; the run still fails closed because no ready manager pod
    # was found on this node, so the purge that reclaims the freed blocks never ran.
    assert proc.returncode == 1
    assert "NOT purged" in proc.stderr


def test_snapshots_unknown_flag_is_rejected(tmp_path):
    proc, calls = _run(
        SNAPSHOTS_ENTRY, ["--bogus"], {"recurringjobs": [], "volumes": []}, tmp_path
    )
    assert proc.returncode == 2
    assert calls == []
