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

from lib import yaml_fast

FILES = pathlib.Path(__file__).resolve().parents[1] / "files"
REPO = pathlib.Path(__file__).resolve().parents[5]
ROLE = pathlib.Path(__file__).resolve().parents[1]
COMMON_FILES = REPO / "ansible" / "roles" / "setup" / "common" / "files"
COPY_TASK_NAME = "Install the Longhorn reap-orphan classifier scripts"
BACKUPS_ENTRY = FILES / "longhorn_reap_orphan_backups.py"
SNAPSHOTS_ENTRY = FILES / "longhorn_reap_orphan_snapshots.py"


def _copy_loop_srcs() -> list[pathlib.Path]:
    """The exact file set health-crons.yml's own copy task installs into /opt/longhorn-reap/,
    resolved to repo paths -- reading it from the task rather than hardcoding it here means
    dropping a file from that loop (host_lib.py, say) breaks these transport tests with a
    ModuleNotFoundError instead of silently shrinking what actually gets deployed. See
    test_longhorn_reap_ship_list.py for the loop/stamp-pair/import-census guard this pairs with.
    """
    tasks = yaml_fast.safe_load((ROLE / "tasks" / "health-crons.yml").read_text())
    matches = [t for t in tasks if t.get("name") == COPY_TASK_NAME]
    assert len(matches) == 1, (
        f"expected exactly one task named {COPY_TASK_NAME!r}, found {len(matches)}"
    )
    resolved = []
    for item in matches[0]["loop"]:
        if item.startswith("{{ role_path }}/../common/files/"):
            resolved.append(COMMON_FILES / item.rsplit("/", 1)[-1])
        else:
            resolved.append(FILES / item)
    return resolved


def _deployed_entry(entry: pathlib.Path, deploy_dir: pathlib.Path) -> pathlib.Path:
    # host_lib.py is a cross-role shared module (ansible/roles/setup/common/files), staged into
    # /opt/longhorn-reap/ as a sibling by the Ansible copy task -- it does not live in this
    # role's own files/ in the repo. Deploying is what makes the two entry points' sibling
    # directories the same one; this reproduces that shape in a tmp dir so the sys.path
    # bootstrap (`sys.path.insert(0, dirname(__file__)); import host_lib`) is exercised for real
    # rather than relying on pytest's own pythonpath, which a bare subprocess does not inherit.
    deploy_dir.mkdir(exist_ok=True)
    for src in _copy_loop_srcs():
        shutil.copy(src, deploy_dir / src.name)
    return deploy_dir / entry.name


_STUB_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

CALLS_LOG = os.environ["STUB_CALLS_LOG"]
FIXTURES = json.loads(os.environ["STUB_FIXTURES"])
FAIL_DELETE_NAMES = set(json.loads(os.environ.get("STUB_FAIL_DELETE_NAMES", "[]")))

argv = sys.argv[1:]
with open(CALLS_LOG, "a") as fh:
    fh.write(json.dumps(argv) + "\\n")

if argv[:1] == ["kubectl"] and "get" in argv:
    # e.g. "volumes.longhorn.io" -> "volumes"; a bare resource like "pods" is unchanged.
    kind = argv[argv.index("get") + 1].split(".", 1)[0]
    print(json.dumps({"items": FIXTURES.get(kind, [])}))
    sys.exit(0)
if argv[:1] == ["kubectl"] and "delete" in argv:
    name = argv[argv.index("delete") + 2]
    if name in FAIL_DELETE_NAMES:
        sys.stderr.write("stub: delete forced to fail for %s\\n" % name)
        sys.exit(1)
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


def _run(
    entry,
    args,
    fixtures,
    tmp_path,
    *,
    admin_readable=False,
    fail_delete_names=(),
    readonly_kubeconfig_set=True,
):
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
    env["STUB_FAIL_DELETE_NAMES"] = json.dumps(list(fail_delete_names))
    env["LONGHORN_REAP_KUBECTL"] = "k3s kubectl"
    env["LONGHORN_REAP_READONLY_KUBECONFIG"] = (
        str(tmp_path / "readonly.yaml") if readonly_kubeconfig_set else ""
    )
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


def _delete_names(calls) -> list[str]:
    """The CR name out of each `kubectl delete <resource> <name> ...` argv the stub recorded."""
    return [c[c.index("delete") + 2] for c in calls if "delete" in c]


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


def test_backups_dry_run_prints_all_four_columns_for_a_reapable_row(tmp_path):
    # bash printed "NAME VOL CREATED JOB" per candidate/orphaned row; an earlier draft here
    # printed only "NAME VOL", silently dropping the two columns an operator reads to decide
    # whether a stray is safe to delete.
    fixtures = {
        "volumes": [_volume("vol-a", "daily-backup")],
        "backups": [
            _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "daily-backup"),
            _backup(
                "stray-2", "vol-a", "2026-08-15T00:00:00Z", "weekly-backup"
            ),  # kept, FLOOR 2
            _backup(
                "stray-1", "vol-a", "2026-08-14T00:00:00Z", "weekly-backup"
            ),  # reapable
        ],
    }
    proc, _calls = _run(BACKUPS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "stray-1 vol-a 2026-08-14T00:00:00Z weekly-backup" in proc.stdout


def test_backups_dry_run_refuses_when_readonly_kubeconfig_is_unset(tmp_path):
    # An unset LONGHORN_REAP_READONLY_KUBECONFIG must not silently fall through to whatever
    # KUBECONFIG the caller's shell already has -- run as root (the shim's --apply path), that
    # would be the admin kubeconfig, letting a plain dry run read through write credentials.
    proc, calls = _run(
        BACKUPS_ENTRY, [], {"volumes": []}, tmp_path, readonly_kubeconfig_set=False
    )
    assert proc.returncode == 1
    assert "LONGHORN_REAP_READONLY_KUBECONFIG is not set" in proc.stderr
    assert calls == []


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
            "--timeout=120s",
        ]
    ]


def test_backups_apply_stops_at_the_first_failed_delete(tmp_path):
    # Bash's loop `exit 1`-ed the whole script on the first failed delete; a Python version
    # that printed "stopping" but kept going into the rest of the bucket (or the next one)
    # would delete under a kubeconfig or cluster state that had just proven unreliable.
    fixtures = {
        "volumes": [_volume("vol-a", "weekly-backup-d3")],
        "backups": [
            _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "weekly-backup-d3"),
            _backup(
                "stray-4", "vol-a", "2026-08-19T00:00:00Z", "daily-backup"
            ),  # kept, FLOOR 2
            _backup("stray-3", "vol-a", "2026-08-18T00:00:00Z", "daily-backup"),
            _backup("stray-2", "vol-a", "2026-08-17T00:00:00Z", "daily-backup"),
            _backup("stray-1", "vol-a", "2026-08-16T00:00:00Z", "daily-backup"),
        ],
    }
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--apply"],
        fixtures,
        tmp_path,
        admin_readable=True,
        fail_delete_names=["stray-2"],
    )
    assert proc.returncode == 1
    deletes = _delete_names(calls)
    # stray-3 attempted and succeeded, stray-2 attempted and failed, stray-1 NEVER attempted.
    assert deletes == ["stray-3", "stray-2"]


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
            "--timeout=120s",
        ]
    ]


def test_backups_apply_deleted_volumes_never_runs_after_a_failed_apply(tmp_path):
    # Both flags given at once: if the stray bucket fails, the orphaned bucket must never be
    # touched. Bash's single `exit 1` on the first failed delete made this true by construction;
    # a Python `rc = _delete_bucket(...) or rc` for EACH bucket independently would not.
    fixtures = {
        "volumes": [_volume("vol-a", "daily-backup")],
        "backups": [
            # A current backup so CURRENT_TIER_COUNT > 0 -- otherwise FLOOR 1 keeps every stray.
            _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "daily-backup"),
            _backup(
                "stray-newest", "vol-a", "2026-08-15T00:00:00Z", "weekly-backup"
            ),  # kept, FLOOR 2
            _backup(
                "stray", "vol-a", "2026-08-14T00:00:00Z", "weekly-backup"
            ),  # the sole candidate
            _backup("orphan", "gone-vol", "2026-08-14T00:00:00Z", "daily-backup"),
        ],
    }
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--apply", "--apply-deleted-volumes"],
        fixtures,
        tmp_path,
        admin_readable=True,
        fail_delete_names=["stray"],
    )
    assert proc.returncode == 1
    deletes = _delete_names(calls)
    assert deletes == [
        "stray"
    ]  # the "stray" delete failed; "orphan" was never attempted


# ── backups: the deletion cap ───────────────────────────────────────────────────────────


def _three_candidates_and_one_orphan():
    """Fixtures whose classification is exactly 3 reapable strays and 1 orphan.

    stray-4 is the newest stray and is kept by FLOOR 2, so the reapable bucket is stray-3,
    stray-2, stray-1 -- a count the --max-deletions pair below sits either side of.
    """
    return {
        "volumes": [_volume("vol-a", "weekly-backup-d3")],
        "backups": [
            _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "weekly-backup-d3"),
            _backup("stray-4", "vol-a", "2026-08-19T00:00:00Z", "daily-backup"),
            _backup("stray-3", "vol-a", "2026-08-18T00:00:00Z", "daily-backup"),
            _backup("stray-2", "vol-a", "2026-08-17T00:00:00Z", "daily-backup"),
            _backup("stray-1", "vol-a", "2026-08-16T00:00:00Z", "daily-backup"),
            _backup("orphan", "gone-vol", "2026-08-14T00:00:00Z", "daily-backup"),
        ],
    }


def test_backups_apply_deletes_when_the_candidate_count_equals_the_cap(tmp_path):
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--apply", "--max-deletions", "3"],
        _three_candidates_and_one_orphan(),
        tmp_path,
        admin_readable=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert _delete_names(calls) == ["stray-3", "stray-2", "stray-1"]


def test_backups_apply_refuses_when_the_candidate_count_exceeds_the_cap(tmp_path):
    # The rejecting half. Each deletion measured ~520 Class C against a 2,500/day free tier, so
    # an unbounded --apply exhausts the cap mid-run and the 403s that follow read as missing
    # backups. Refusing BEFORE the first delete is what keeps the run from ending part-done.
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--apply", "--max-deletions=2"],
        _three_candidates_and_one_orphan(),
        tmp_path,
        admin_readable=True,
    )
    assert proc.returncode == 1
    assert _delete_names(calls) == []
    assert "--max-deletions cap of 2" in proc.stderr


def test_backups_cap_counts_both_buckets_together(tmp_path):
    # 3 strays + 1 orphan is 4 deletions, over a cap of 3 that --apply alone would satisfy.
    # The Class C cost is per deletion, so which bucket a deletion came from does not matter.
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--apply", "--apply-deleted-volumes", "--max-deletions", "3"],
        _three_candidates_and_one_orphan(),
        tmp_path,
        admin_readable=True,
    )
    assert proc.returncode == 1
    assert _delete_names(calls) == []
    assert "4 deletion(s) requested" in proc.stderr


def test_backups_max_deletions_without_a_count_is_rejected(tmp_path):
    proc, calls = _run(
        BACKUPS_ENTRY, ["--apply", "--max-deletions"], {"volumes": []}, tmp_path
    )
    assert proc.returncode == 2
    assert calls == []


def test_backups_max_deletions_with_a_non_integer_count_is_rejected(tmp_path):
    proc, calls = _run(
        BACKUPS_ENTRY, ["--apply", "--max-deletions", "lots"], {"volumes": []}, tmp_path
    )
    assert proc.returncode == 2
    assert calls == []


def test_backups_dry_run_documents_the_cap(tmp_path):
    proc, _calls = _run(BACKUPS_ENTRY, [], {"volumes": []}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "--max-deletions" in proc.stdout


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


def test_snapshots_aborts_when_recurringjobs_is_empty_but_a_volume_carries_the_label(
    tmp_path,
):
    # Verified without this check: 3 current-tier snapshots deleted on a dry run. An empty
    # recurringjobs.longhorn.io list makes every group resolve to "", which the snapshot's own
    # truncated job label ("daily-backup") never equals -- so the "current, not stranded" skip
    # never fires and every current-tier snapshot past the newest reads as stranded.
    fixtures = {
        "recurringjobs": [],  # the RecurringJob CRs failed to list, or none exist
        "volumes": [_volume("vol-a", "daily-backup")],
        "snapshots": [
            _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
            _snapshot("current-2", "vol-a", "2026-08-18T00:00:00Z", job="daily-backup"),
            _snapshot("current-3", "vol-a", "2026-08-17T00:00:00Z", job="daily-backup"),
        ],
    }
    proc, calls = _run(SNAPSHOTS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 1
    assert "ABORT" in proc.stderr
    assert "reapable" not in proc.stdout  # never gets far enough to print a verdict
    # ABORT fires right after reading recurringjobs + volumes; snapshots.longhorn.io is never
    # read and no delete is ever attempted.
    assert not any("snapshots.longhorn.io" in c for c in calls)
    assert not any("delete" in c for c in calls)


def test_snapshots_dry_run_prints_all_four_columns_for_a_reapable_row(tmp_path):
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
    proc, _calls = _run(SNAPSHOTS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "stale vol-a 2026-08-01T00:00:00Z weekly-backup" in proc.stdout


def test_snapshots_dry_run_prints_the_age_floor_as_a_bare_integer_day_count(tmp_path):
    # k3s_longhorn_snapshot_reap_min_age_days is an int in defaults/main.yml; bash's arithmetic
    # context only ever held one, and printed "younger than 3d" -- not "3.0d", which a
    # float-typed MIN_AGE_DAYS would print instead.
    import datetime

    one_day_ago = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    fixtures = {
        # A resolvable RecurringJob so `abort_reason`'s recurringjob check does not fire; the
        # "recent" snapshot's own job ("daily-backup") deliberately does not match what the
        # volume's group resolves to ("some-job"), so it reaches the age floor instead of being
        # skipped as current.
        "recurringjobs": [
            {"metadata": {"name": "some-job"}, "spec": {"groups": ["some-group"]}}
        ],
        "volumes": [_volume("vol-a", "some-group")],
        "snapshots": [
            _snapshot("newest", "vol-a", one_day_ago),
            _snapshot("recent", "vol-a", one_day_ago, job="daily-backup"),
        ],
    }
    proc, _calls = _run(SNAPSHOTS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "younger than 3d" in proc.stdout
    assert "3.0d" not in proc.stdout


def test_snapshots_dry_run_refuses_when_readonly_kubeconfig_is_unset(tmp_path):
    proc, calls = _run(
        SNAPSHOTS_ENTRY,
        [],
        {"recurringjobs": [], "volumes": []},
        tmp_path,
        readonly_kubeconfig_set=False,
    )
    assert proc.returncode == 1
    assert "LONGHORN_REAP_READONLY_KUBECONFIG is not set" in proc.stderr
    assert calls == []


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
