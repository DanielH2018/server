#!/usr/bin/env python3
"""Reap Longhorn Snapshots left stranded by a tier move.

DRY RUN BY DEFAULT. Pass --apply to delete. No cron, for the same reason as its sibling
longhorn_reap_orphan_backups.py: the safe-to-delete set depends on live state this script can
check now but cannot guarantee a week from now.

THE PROBLEM, and why it differs from stranded backups. A RecurringJob's `retain: N` prunes only
the snapshots of volumes currently in its `groups:`. Move a volume off a job's group and the
snapshots it took are never pruned again. A stranded BACKUP costs B2 storage; a stranded
SNAPSHOT costs local Longhorn replica space AND blocks reclamation, because Longhorn's
filesystem-trim only frees blocks below the OLDEST retained snapshot in the chain -- one
stranded snapshot anywhere pins every block beneath it. Reaping is what lets a subsequent trim
reclaim anything, and only after a volume REMOUNT: ext4 skips block groups it has already
trimmed since mount. Scale the workload to 0 until `detached`, scale back, then trim -- a
rollout restart does not detach. See longhorn-trim-volumes.sh.j2's header.

THE ALTERNATIVE, REJECTED. Setting Longhorn's remove-snapshots-during-filesystem-trim=true
would reclaim more, automatically, cluster-wide -- and silently destroy recovery points on
every trim of every volume forever. Operator decision 2026-08-16: leave it false and delete
specific, identified, stale snapshots instead. A snapshot delete is LOCAL ONLY; B2 backups are
untouched.

HOW A DELETE IS BOUNDED. Reads and deletes both go through host_lib.kubectl_runner; the delete
differs only in the timeout bound to its runner. `--timeout=%s % DELETE_TIMEOUT` (120s by
default) is the SERVER-side wait kubectl itself honours, so the CLIENT-side subprocess cap has
to outlive it by DELETE_TIMEOUT_MARGIN_S -- the runner's default 30s cap would kill the process
long before the templated knob could ever return, making the knob unreachable. Neither bound
cancels anything: kubectl has already issued the DELETE, and exceeding --timeout only gives up
WAITING for the finalizer while the server carries on.

A purge POST that gets back a non-2xx counts as a failure, and main() exits 1 when ANY volume
was left unpurged (`urlopen` raises `HTTPError` on 4xx/5xx). bash's `curl` without `-f` printed
nothing and returned 0 either way, so a rejected purge used to look identical to a successful
one. The exit code is what an operator reads first, and a run that reclaimed nothing must not
read as a success: every snapshot it deleted stays marked-removed-but-not-coalesced, so no
space comes back until someone purges from the Longhorn UI or re-runs.

See longhorn_reap_logic.py for the floors (newest-per-volume, detached, the truncated-job-name
prefix match, the age floor) and longhorn-reap-orphan-snapshots.sh.j2 for the wrapper.

Run directly: uv run --no-project --python <pin> longhorn_reap_orphan_snapshots.py [--apply]
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host_lib
import longhorn_reap_logic as logic

NAMESPACE = "longhorn-system"
KUBECTL_BIN = os.environ.get("LONGHORN_REAP_KUBECTL", "k3s kubectl")
TIMEOUT = int(os.environ.get("LONGHORN_REAP_KUBECTL_TIMEOUT_S", "30"))
DELETE_TIMEOUT = os.environ.get("LONGHORN_REAP_DELETE_TIMEOUT", "120s")
# k3s_longhorn_snapshot_reap_min_age_days is an int in defaults/main.yml; bash's arithmetic
# context only ever held one too, and printed "younger than 3d", not "3.0d".
MIN_AGE_DAYS = int(os.environ.get("LONGHORN_REAP_MIN_AGE_DAYS", "3"))
# Margin the CLIENT-side subprocess timeout carries over kubectl's own --timeout, so the
# subprocess can never fire first and turn a delete that was still legitimately running on the
# server into a false "FAILED". See the module docstring.
DELETE_TIMEOUT_MARGIN_S = 30

# Overridable via env purely so a test can point this at a fixture instead of the real
# root-only file; production never sets this, so it stays the fixed admin path.
ADMIN_KUBECONFIG = os.environ.get(
    "LONGHORN_REAP_ADMIN_KUBECONFIG", "/etc/rancher/k3s/k3s.yaml"
)
# No fallback default: the read-only kubeconfig path is templated per sys_user and is not
# derivable here. Left unset, a dry run must refuse rather than let KUBECONFIG stay whatever
# the caller's shell happens to have -- which, run as root, is the admin one. See main().
READONLY_KUBECONFIG = os.environ.get("LONGHORN_REAP_READONLY_KUBECONFIG", "")
SUDO_HINT = "sudo /usr/local/bin/longhorn-reap-orphan-snapshots.sh --apply"


def _delete_timeout_seconds(text: str) -> float:
    """Parse a Kubernetes duration string ("120s", "2m") into seconds.

    Matches kubectl's own --timeout flag format; the repo's default (k3s_longhorn_snapshot_
    reap_delete_timeout = "120s") only ever uses the `s` suffix, but `m`/`h` are cheap to cover.
    """
    text = text.strip()
    if text.endswith("s"):
        return float(text[:-1])
    if text.endswith("m"):
        return float(text[:-1]) * 60
    if text.endswith("h"):
        return float(text[:-1]) * 3600
    return float(text)


def _print_bucket(header: str, rows: list, none_msg: str = "(none)") -> None:
    print("== %s ==" % header)
    if not rows:
        print("  %s" % none_msg)
    else:
        for row in rows:
            if len(row) == 3:  # kept: (name, vol, reason)
                name, vol, reason = row
                print("  %s  %s  (%s)" % (name, vol, reason))
            else:  # candidate: (name, vol, created, job)
                name, vol, created, job = row
                print("%s %s %s %s" % (name, vol, created, job))
    print()


def _delete_snapshot(name: str, subprocess_timeout: float) -> tuple[int, str]:
    """Delete one Snapshot CR. `subprocess_timeout` must outlive kubectl's own --timeout."""
    kubectl = host_lib.kubectl_runner(KUBECTL_BIN, NAMESPACE, int(subprocess_timeout))
    return kubectl(
        "delete",
        "snapshots.longhorn.io",
        name,
        "--ignore-not-found",
        "--timeout=%s" % DELETE_TIMEOUT,
    )


def _purge(kubectl, node: str, volumes: set[str]) -> int:
    """POST snapshotPurge to the manager pod on THIS node for each touched volume.

    Deleting a Snapshot CR only marks it removed; the blocks come back when the engine
    coalesces it into its parent, which is what snapshotPurge asks for. Must be the local
    node's manager pod, not the longhorn-backend ClusterIP: a process in the host netns can
    only reach its own node's pod CIDR, and kube-proxy's per-connection pick over the Service
    fails whenever it lands on the other node's manager (measured 2026-08-16: 27 of 37 calls).

    Returns the number of volumes left unpurged, so 0 means every POST was accepted. A path
    that never reaches the POST loop at all -- no readable pod list, no ready manager pod --
    counts every volume as unpurged, and at least one, because a run that purged nothing is
    never a success.
    """
    nothing_purged = max(len(volumes), 1)
    rc, out = kubectl("get", "pods", "-l", "app=longhorn-manager", "-o", "json")
    if rc != 0:
        print(
            "WARNING: could not list longhorn-manager pods; snapshots are marked removed but "
            "NOT purged: %s" % out.strip()[:200],
            file=sys.stderr,
        )
        return nothing_purged
    try:
        pods = json.loads(out).get("items", [])
    except ValueError as e:
        print("WARNING: unparseable pod list; nothing purged: %s" % e, file=sys.stderr)
        return nothing_purged

    backend = ""
    for pod in pods:
        spec = pod.get("spec") or {}
        status = pod.get("status") or {}
        if spec.get("nodeName") != node or status.get("phase") != "Running":
            continue
        statuses = status.get("containerStatuses") or []
        # A pod reporting NO containerStatuses is NOT ready. Bash agreed, though not the way an
        # earlier comment here claimed: its `select([.status.containerStatuses[].ready] | all)`
        # never reached the vacuously-true `all` on such a pod, because `.[]` over a null field
        # raises "Cannot iterate over null" and jq exits nonzero -- so bash took its "no ready
        # manager pod" branch. Counting the empty list as ready aims the purge POST at a pod
        # whose containers have not started, and the refused connection surfaces only as a
        # purge failure well after the snapshots are already marked removed.
        if statuses and all(cs.get("ready") for cs in statuses):
            backend = status.get("podIP", "")
            break

    if not backend:
        print(
            "WARNING: no ready longhorn-manager pod on %s; snapshots are marked removed but "
            "NOT purged, so nothing is reclaimed yet. Purge from the Longhorn UI, or re-run."
            % node,
            file=sys.stderr,
        )
        return nothing_purged

    unpurged = 0
    for vol in sorted(volumes):
        print("purging %s..." % vol)
        req = urllib.request.Request(
            "http://%s:9500/v1/volumes/%s?action=snapshotPurge" % (backend, vol),
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                pass
        except Exception as e:
            unpurged += 1
            print(
                "  purge request failed for %s — retry from the Longhorn UI: %s"
                % (vol, e),
                file=sys.stderr,
            )

    return unpurged


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    unknown = [a for a in argv if a != "--apply"]
    if unknown:
        print("unknown argument: %s (expected --apply)" % unknown[0], file=sys.stderr)
        return 2

    if not apply and not READONLY_KUBECONFIG:
        print(
            "LONGHORN_REAP_READONLY_KUBECONFIG is not set, and a dry run must not silently "
            "fall through to whatever KUBECONFIG the caller's shell already has -- run as "
            "root, that would be the admin kubeconfig. Set the shim's env var, or pass "
            "--apply to use the admin kubeconfig explicitly.",
            file=sys.stderr,
        )
        return 1

    kubeconfig, err = logic.resolve_kubeconfig(
        needs_admin=apply,
        admin_readable=os.access(ADMIN_KUBECONFIG, os.R_OK),
        admin_path=ADMIN_KUBECONFIG,
        readonly_path=READONLY_KUBECONFIG,
        sudo_hint=SUDO_HINT,
    )
    if err:
        print(err, file=sys.stderr)
        return 1
    if kubeconfig:
        os.environ["KUBECONFIG"] = kubeconfig

    kubectl = host_lib.kubectl_runner(KUBECTL_BIN, NAMESPACE, TIMEOUT)

    rj_rc, rj_out = kubectl("get", "recurringjobs.longhorn.io", "-o", "json")
    if rj_rc != 0:
        print(
            "ABORT: could not read RecurringJobs: %s" % rj_out.strip()[:200],
            file=sys.stderr,
        )
        return 1
    vol_rc, vol_out = kubectl("get", "volumes.longhorn.io", "-o", "json")
    if vol_rc != 0:
        print(
            "ABORT: could not read volumes: %s" % vol_out.strip()[:200], file=sys.stderr
        )
        return 1
    try:
        recurringjobs = json.loads(rj_out).get("items", [])
        volumes = json.loads(vol_out).get("items", [])
    except ValueError as e:
        print("ABORT: unparseable Longhorn object list: %s" % e, file=sys.stderr)
        return 1

    group_job = logic.recurringjob_group_to_job(recurringjobs)
    owner = logic.snapshot_owner_map(volumes, group_job)
    attached = logic.attached_volume_set(volumes)

    abort = logic.abort_reason(
        len(volumes),
        len(owner),
        recurringjob_count=len(recurringjobs),
        volumes_with_group_label=logic.volumes_with_group_label(volumes),
    )
    if abort:
        print(abort, file=sys.stderr)
        return 1

    snap_rc, snap_out = kubectl("get", "snapshots.longhorn.io", "-o", "json")
    if snap_rc != 0:
        print(
            "ABORT: could not read snapshots: %s" % snap_out.strip()[:200],
            file=sys.stderr,
        )
        return 1
    try:
        snapshots = json.loads(snap_out).get("items", [])
    except ValueError as e:
        print("ABORT: unparseable snapshot list: %s" % e, file=sys.stderr)
        return 1

    try:
        result = logic.classify_snapshots(
            snapshots, owner, attached, MIN_AGE_DAYS, time.time()
        )
    except logic.ReapAbort as e:
        # A volume whose group label named no RecurringJob resolves to owner "", which the
        # current-tier test reads as "no snapshot belongs to the current tier" -- every snapshot
        # past the age floor then becomes a candidate. classify_snapshots refuses; without this
        # the refusal reaches the operator as a traceback.
        print("ABORT: %s" % e, file=sys.stderr)
        return 1

    _print_bucket("kept by a floor", result.kept)
    _print_bucket("reapable", result.candidates)

    count = len(result.candidates)

    if not apply:
        print("dry run — %d stranded snapshot(s) would be deleted." % count)
        print("  --apply  deletes them, then asks each affected volume to purge")
        return 0

    delete_subprocess_timeout = (
        _delete_timeout_seconds(DELETE_TIMEOUT) + DELETE_TIMEOUT_MARGIN_S
    )

    deleted = 0
    partial = False
    touched: set[str] = set()
    for name, vol, _created, _job in result.candidates:
        rc, out = _delete_snapshot(name, delete_subprocess_timeout)
        if rc != 0:
            print(
                "delete FAILED or timed out for %s after %d deletion(s) — stopping: %s"
                % (name, deleted, out.strip()[:200]),
                file=sys.stderr,
            )
            print(
                "Re-run the dry run to see what is left; %d deletion(s) are already durable."
                % deleted,
                file=sys.stderr,
            )
            partial = True
            break
        deleted += 1
        touched.add(vol)
    print("deleted %d stranded snapshot(s)." % deleted)

    if deleted > 0:
        # A hard exit here, ahead of the PARTIAL check below, mirrors the shell original: an
        # unreachable manager pod or a rejected POST leaves the deletions marked-removed-but-
        # not-purged, which is worse left unreported than a partial-run notice for a break that
        # already happened.
        unpurged = _purge(kubectl, os.uname().nodename, touched)
        if unpurged:
            print(
                "%d of %d volume(s) were NOT purged — the deleted snapshots are marked removed "
                "but never coalesced, so no space is reclaimed yet."
                % (unpurged, len(touched)),
                file=sys.stderr,
            )
            return 1

    if partial:
        print(
            "run was PARTIAL — re-run the dry run to see what remains.", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
