#!/usr/bin/env python3
"""The subprocess harness the reap-orphan entry-point suites share.

Not a test module — a helper the entry-point suites import. It puts a stub `k3s` on PATH in place of
the real binary and runs an entry point from scripts/backup/ against fixture JSON while
recording every `kubectl delete` argv the stub receives.

The entry point runs as a bare subprocess, not an import, because that is how an operator runs
it (`uv run python scripts/backup/longhorn_reap_orphan_backups.py`). A subprocess does not
inherit pytest's pythonpath, so the entry point's own sys.path bootstrap is the only thing that
makes `host_lib` (ansible/roles/setup/common/files/) and `longhorn_reap_logic` importable.

Consumers: `test_longhorn_reap_entrypoints.py`, `test_longhorn_reap_backups_cli.py`,
`test_longhorn_reap_backups_modes_cli.py`, `test_longhorn_reap_snapshots_cli.py`. The `_volume`,
`_backup` and `_snapshot` builders are also the only copies the in-process suites use:
`test_longhorn_reap_logic.py`, `test_longhorn_reap_selectors.py` and
`ansible/tests/longhorn/test_longhorn_reap_guard.py`, which reaches this module through
`pyproject.toml`'s `pythonpath`.
"""

import json
import os
import pathlib
import subprocess
import sys

from lib.proc_testing import fake_bin, path_with

BACKUP_DIR = pathlib.Path(__file__).resolve().parents[1]
BACKUPS_ENTRY = BACKUP_DIR / "longhorn_reap_orphan_backups.py"
SNAPSHOTS_ENTRY = BACKUP_DIR / "longhorn_reap_orphan_snapshots.py"

# The epoch a dated fixture is measured from when a test passes `now=` to `_run`. Same value as
# the reader suites' `_longhorn_reader_stubs.NOW`.
NOW = 1_800_000_000.0

# Runs the entry point with main(argv, now=NOW) instead of `python <entry> <args>`,
# still as a subprocess: the sys.path bootstrap, the stub `k3s` on
# PATH and the env parsing are the real ones. `-P` keeps the cwd off sys.path, so the bootstrap
# is what makes `host_lib` importable, as it is for an operator; `run_name` is not `__main__`, so
# the file's own entry line does not fire a second, clock-reading run.
_MAIN_WITH_NOW = (
    "import runpy, sys; "
    "g = runpy.run_path(sys.argv[1], run_name='longhorn_reap_entry'); "
    "sys.exit(g['main'](sys.argv[3:], now=float(sys.argv[2])))"
)


_STUB_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

CALLS_LOG = os.environ["STUB_CALLS_LOG"]
FIXTURES = json.loads(os.environ["STUB_FIXTURES"])
FAIL_DELETE_NAMES = set(json.loads(os.environ.get("STUB_FAIL_DELETE_NAMES", "[]")))
# Kinds that must answer `null` instead of a well-formed `{"items": [...]}` body -- what
# `kubectl` emits for some server versions on an empty CRD list. Exercises the
# parse_kubectl_json_items isinstance(dict) guard rather than the ValueError branch.
NULL_KINDS = set(json.loads(os.environ.get("STUB_NULL_KINDS", "[]")))

argv = sys.argv[1:]
with open(CALLS_LOG, "a") as fh:
    fh.write(json.dumps(argv) + "\\n")

if argv[:1] == ["kubectl"] and "get" in argv:
    # e.g. "volumes.longhorn.io" -> "volumes"; a bare resource like "pods" is unchanged.
    kind = argv[argv.index("get") + 1].split(".", 1)[0]
    if kind in NULL_KINDS:
        print("null")
    else:
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


def _volume(name, group=None, state="attached"):
    labels = {}
    if group is not None:
        labels["recurring-job-group.longhorn.io/%s" % group] = "enabled"
    return {"metadata": {"name": name, "labels": labels}, "status": {"state": state}}


def _backup(name, vol, created, job, state="Completed", pvc=None):
    labels = {"RecurringJob": job} if job else {}
    if pvc:
        # Longhorn writes the PVC into this label as compact JSON, not as a struct.
        labels["KubernetesStatus"] = json.dumps(
            {"pvcName": pvc, "namespace": "homelab"}, separators=(",", ":")
        )
    return {
        "metadata": {"name": name},
        "status": {
            "volumeName": vol,
            "snapshotCreatedAt": created,
            "labels": labels,
            "state": state,
        },
    }


def _pvc(name, volume):
    """A PVC as the migrated-chain mode reads it: `get persistentvolumeclaims -o json`."""
    return {
        "metadata": {"name": name, "namespace": "homelab"},
        "spec": {"volumeName": volume},
    }


def _snapshot(name, vol, created, job=None, removed=None):
    status = {"creationTime": created}
    if job is not None:
        status["labels"] = {"RecurringJob": job}
    if removed is not None:
        status["markRemoved"] = removed
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
    null_kinds=(),
    extra_env=None,
    now=None,
):
    stub_dir = fake_bin(tmp_path / "bin", k3s=_STUB_KUBECTL)

    calls_log = tmp_path / "calls.jsonl"
    calls_log.write_text("")

    env = dict(os.environ)
    env["PATH"] = path_with(stub_dir, env=env)
    env["STUB_CALLS_LOG"] = str(calls_log)
    env["STUB_FIXTURES"] = json.dumps(fixtures)
    env["STUB_FAIL_DELETE_NAMES"] = json.dumps(list(fail_delete_names))
    env["STUB_NULL_KINDS"] = json.dumps(list(null_kinds))
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
    if extra_env:
        env.update(extra_env)

    if now is None:
        argv = [sys.executable, str(entry), *args]
    else:
        argv = [
            sys.executable,
            "-P",
            "-c",
            _MAIN_WITH_NOW,
            str(entry),
            repr(now),
            *args,
        ]
    proc = subprocess.run(
        argv,
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
