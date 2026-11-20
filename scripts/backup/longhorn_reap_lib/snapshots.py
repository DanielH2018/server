"""Reap Longhorn Snapshots left stranded by a tier move.

DRY RUN BY DEFAULT. Pass --apply to delete. No cron, for the same reason as its sibling
`longhorn_reap.py backups`: the safe-to-delete set depends on live state this script can
check now but cannot guarantee a week from now.

THE PROBLEM, and why it differs from stranded backups. A RecurringJob's `retain: N` prunes only
the snapshots of volumes currently in its `groups:`. Move a volume off a job's group and the
snapshots it took are never pruned again. A stranded BACKUP costs B2 storage; a stranded
SNAPSHOT costs local Longhorn replica space AND blocks reclamation, because Longhorn's
filesystem-trim only frees blocks below the OLDEST retained snapshot in the chain -- one
stranded snapshot anywhere pins every block beneath it. Reaping is what lets a subsequent trim
reclaim anything, and only after a volume REMOUNT: ext4 skips block groups it has already
trimmed since mount. Scale the workload to 0 until `detached`, scale back, then trim -- a
rollout restart does not detach. docs/longhorn-backup-tiering.md has the measurement.

THE ALTERNATIVE, REJECTED. Setting Longhorn's remove-snapshots-during-filesystem-trim=true
would reclaim more, automatically, cluster-wide -- and silently destroy recovery points on
every trim of every volume forever. Operator decision 2026-08-16: leave it false and delete
specific, identified, stale snapshots instead. A snapshot delete is LOCAL ONLY; B2 backups are
untouched.

HOW A DELETE IS BOUNDED. Reads and deletes both go through host_lib.kubectl_runner; the delete
differs only in the timeout bound to its runner. `--timeout`, from
`LONGHORN_REAP_DELETE_TIMEOUT_S` (120 by default), is the SERVER-side wait kubectl itself honours, so the CLIENT-side subprocess cap has
to outlive it by DELETE_TIMEOUT_MARGIN_S -- the runner's default 30s cap would kill the process
long before the server-side bound could ever return, making that bound unreachable. Neither bound
cancels anything: kubectl has already issued the DELETE, and exceeding --timeout only gives up
WAITING for the finalizer while the server carries on.

A purge POST that gets back a non-2xx counts as a failure, and main() exits 1 when ANY volume
was left unpurged (`urlopen` raises `HTTPError` on 4xx/5xx), so a rejected purge never looks identical to a
successful one. The exit code is what an operator reads first, and a run that reclaimed nothing must not
read as a success: every snapshot it deleted stays marked-removed-but-not-coalesced, so no
space comes back until someone purges from the Longhorn UI or re-runs.

See logic.py beside it for the floors (newest-per-volume, detached, the truncated-job-name
prefix match, the age floor).

Run from the repo root on a k3s host. The dry run reads through the read-only kubeconfig:
    LONGHORN_REAP_READONLY_KUBECONFIG=~/.kube/config \
        uv run python scripts/backup/longhorn_reap.py snapshots
Deleting needs the root-only admin kubeconfig, so run the same interpreter under sudo:
    sudo .venv/bin/python -B scripts/backup/longhorn_reap.py snapshots --apply
"""

import os
import sys
import time
import urllib.request

from lib import host_modules  # noqa: F401
import host_lib
from renderer.template_filters.longhorn_manager import ready_manager_ip
from lib.cli_help import answer_help
from lib.exit_codes import USAGE_ERROR
from backup.longhorn_reap_lib import logic
from lib.k8s_objects import LonghornVolume

NAMESPACE = "longhorn-system"
KUBECTL_BIN = os.environ.get("LONGHORN_REAP_KUBECTL", "k3s kubectl")

# Overridable via env purely so a test can point this at a fixture instead of the real
# root-only file; production never sets this, so it stays the fixed admin path.
ADMIN_KUBECONFIG = os.environ.get(
    "LONGHORN_REAP_ADMIN_KUBECONFIG", "/etc/rancher/k3s/k3s.yaml"
)
# No fallback default. Left unset, a dry run must refuse rather than let KUBECONFIG stay whatever
# the caller's shell happens to have -- which, run as root, is the admin one. See main().
READONLY_KUBECONFIG = os.environ.get("LONGHORN_REAP_READONLY_KUBECONFIG", "")
SUDO_HINT = logic.sudo_hint(logic.ENTRY_POINT, "snapshots")


def _delete_snapshot(name: str, config: logic.ReapConfig) -> tuple[int, str]:
    """Delete one Snapshot CR, bounded server-side and client-side. See the module docstring."""
    kubectl = host_lib.kubectl_runner(
        KUBECTL_BIN, NAMESPACE, config.delete_subprocess_timeout_s
    )
    return kubectl(
        "delete",
        "snapshots.longhorn.io",
        name,
        "--ignore-not-found",
        "--timeout=%ds" % config.delete_timeout_s,
    )


def _purge(kubectl, node: str, volumes: set[str]) -> int:
    """POST snapshotPurge to the manager pod on THIS node for each touched volume.

    Deleting a Snapshot CR only marks it removed; the blocks come back when the engine
    coalesces it into its parent, which is what snapshotPurge asks for. Must be the local
    node's manager pod, not the longhorn-backend ClusterIP: a process in the host netns can
    only reach its own node's pod CIDR, and kube-proxy's per-connection pick over the Service
    fails whenever it lands on the other node's manager.

    The ready-pod rule is `longhorn_manager.ready_manager_ip`, shared with `volume_revert.py`
    and the trim cron.

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
    pods, err = logic.parse_kubectl_json_items(out, "pod list")
    if err:
        print("WARNING: %s; nothing purged" % err, file=sys.stderr)
        return nothing_purged

    backend = ready_manager_ip(pods, node)

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


def main(argv: list[str], now: float | None = None) -> int:
    """Classify, print, and under --apply delete.

    `now` is the epoch the age floor is measured from; a real run leaves it None and reads the
    clock. A test hands one in so a fixture dated "one day ago" stays one day old however long
    the suite has run — an argument, not an env var or a flag, so nothing the caller's
    environment carries can move it.
    """
    answer_help(__doc__, argv)
    apply = "--apply" in argv
    unknown = [a for a in argv if a != "--apply"]
    if unknown:
        print("unknown argument: %s (expected --apply)" % unknown[0], file=sys.stderr)
        return USAGE_ERROR

    refusal = logic.readonly_kubeconfig_refusal(apply, READONLY_KUBECONFIG, "--apply")
    if refusal:
        print(refusal, file=sys.stderr)
        return 1

    try:
        config = logic.parse_reap_config()
    except ValueError as e:
        print("ABORT: %s" % e, file=sys.stderr)
        return USAGE_ERROR

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

    kubectl = host_lib.kubectl_runner(KUBECTL_BIN, NAMESPACE, config.kubectl_timeout_s)

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
    recurringjobs, err = logic.parse_kubectl_json_items(rj_out, "RecurringJob list")
    if err:
        print("ABORT: %s" % err, file=sys.stderr)
        return 1
    volume_items, err = logic.parse_kubectl_json_items(vol_out, "volume list")
    if err:
        print("ABORT: %s" % err, file=sys.stderr)
        return 1

    volumes = [LonghornVolume.from_item(item) for item in volume_items]
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
    snapshots, err = logic.parse_kubectl_json_items(snap_out, "snapshot list")
    if err:
        print("ABORT: %s" % err, file=sys.stderr)
        return 1

    try:
        result = logic.classify_snapshots(
            snapshots,
            owner,
            attached,
            config.min_age_days,
            time.time() if now is None else now,
        )
    except logic.ReapAbort as e:
        # A volume whose group label named no RecurringJob resolves to owner "", which the
        # current-tier test reads as "no snapshot belongs to the current tier" -- every snapshot
        # past the age floor then becomes a candidate. classify_snapshots refuses; without this
        # the refusal reaches the operator as a traceback. `abort_reason` already writes the
        # `ABORT: ` prefix into the message, so this prints it as it stands rather than doubling
        # it.
        print(str(e), file=sys.stderr)
        return 1

    logic.print_bucket("kept by a floor", result.kept)
    logic.print_bucket("reapable", result.candidates)

    count = len(result.candidates)

    if not apply:
        print("dry run — %d stranded snapshot(s) would be deleted." % count)
        print("  --apply  deletes them, then asks each affected volume to purge")
        return 0

    deleted = 0
    partial = False
    touched: set[str] = set()
    for name, vol, _created, _job in result.candidates:
        rc, out = _delete_snapshot(name, config)
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
