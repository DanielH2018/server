#!/usr/bin/env python3
"""The subprocess harness the reap-orphan entry-point suites share.

Not a test module — a helper the three suites import. It stages the entry points into a tmp
directory the way health-crons.yml's copy task stages them into /opt/longhorn-reap/, puts a stub
`k3s` on PATH in place of the real binary, and runs the entry point against fixture JSON while
recording every `kubectl delete` argv the stub receives.

Staging is what makes the sys.path bootstrap real: the two entry points do
`sys.path.insert(0, dirname(__file__)); import host_lib`, and host_lib.py lives in another role
in the repo. Only deploying makes them siblings, so a bare subprocess — which does not inherit
pytest's pythonpath — is the shape production actually runs.

The file set comes from the copy task rather than a list here, so dropping an entry from that
loop breaks these suites with a ModuleNotFoundError instead of silently shrinking what gets
deployed. `test_longhorn_reap_ship_list.py` is the loop/stamp-pair/import-census guard this
pairs with.

Consumers: `test_longhorn_reap_entrypoints.py`, `test_longhorn_reap_backups_cli.py`,
`test_longhorn_reap_snapshots_cli.py`.
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
    resolved = [FILES / item for item in matches[0]["loop"]]
    # host_lib.py arrives through roles/setup/common/tasks/install_host_lib.yml, not the copy
    # loop; include it only when that include is present and aimed at the same directory, so
    # dropping the include breaks these tests the way dropping a loop entry does.
    for t in tasks:
        if (
            t.get("ansible.builtin.import_tasks", "").endswith(
                "common/tasks/install_host_lib.yml"
            )
            and t.get("vars", {}).get("host_lib_dir") == "/opt/longhorn-reap"
        ):
            resolved.append(COMMON_FILES / "host_lib.py")
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
    null_kinds=(),
    extra_env=None,
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
