#!/usr/bin/env python3
"""Reap Longhorn Backup objects left stranded by a tier move.

DRY RUN BY DEFAULT. Pass --apply to delete the reapable strays, --apply-deleted-volumes to
delete backups whose volume no longer exists. There is deliberately no cron for this: it is an
operator-invoked tool, because the safe-to-delete set depends on live state this script can
check but cannot guarantee will still hold a week from now. See longhorn-reap-orphan-backups.sh.j2
for the invocation wrapper and longhorn_reap_logic.py for FLOOR 1 (never delete a volume's last
recovery point) and why it shipped inoperative the first time.

THE PROBLEM. Longhorn enforces a RecurringJob's `retain: N` only as a side effect of that job
executing against a volume currently in its `groups:`. When a volume moves tier -- the daily
group to a weekday shard, or to no-backup -- the job that made its existing backups stops
selecting it, and so can never prune them again. They are stranded by construction. There is no
global backup GC and no backup-target-level cleanup.

THE OTHER FLOOR IS COST, AND THE DRY RUN DOES NOT SHOW IT. Every deletion here goes through
Longhorn, and each Longhorn backup deletion walks the volume's whole block tree -- about 1.28
LISTs per stored block (backupstore deltablock.go:1496-1510). LIST is a Backblaze Class C
transaction against a free-tier 2,500/day. Measured 2026-08-17: seven reapable strays across
three volumes came to ~3,640 Class C -- 1.5x the entire daily cap. Before --apply, check
`probe.py b2-budget` against the day's remaining headroom.

Run directly: uv run --no-project --python <pin> longhorn_reap_orphan_backups.py [--apply]
[--apply-deleted-volumes]
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host_lib
import longhorn_reap_logic as logic

NAMESPACE = "longhorn-system"
KUBECTL_BIN = os.environ.get("LONGHORN_REAP_KUBECTL", "k3s kubectl")
TIMEOUT = int(os.environ.get("LONGHORN_REAP_KUBECTL_TIMEOUT_S", "30"))

# The admin kubeconfig is the same absolute path on every k3s host, unlike the read-only one
# (which is templated per sys_user), so the wrapper never overrides it. Overridable via env
# purely so a test can point this at a fixture instead of the real root-only file.
ADMIN_KUBECONFIG = os.environ.get(
    "LONGHORN_REAP_ADMIN_KUBECONFIG", "/etc/rancher/k3s/k3s.yaml"
)
READONLY_KUBECONFIG = os.environ.get("LONGHORN_REAP_READONLY_KUBECONFIG", "")
SUDO_HINT = "sudo /usr/local/bin/longhorn-reap-orphan-backups.sh --apply"

_FLAGS = ("--apply", "--apply-deleted-volumes")


def _print_bucket(header: str, rows: list, none_msg: str = "(none)") -> None:
    print("== %s ==" % header)
    if not rows:
        print("  %s" % none_msg)
    else:
        for row in rows:
            if len(row) == 3:  # kept: (name, vol, reason)
                name, vol, reason = row
                print("  %s  %s  (%s)" % (name, vol, reason))
            else:  # candidate/orphaned: (name, vol, created, job)
                name, vol, _created, _job = row
                print("%s %s" % (name, vol))
    print()


def _delete_bucket(kubectl, label: str, rows: list) -> int:
    print("deleting %s..." % label)
    deleted = 0
    for name, _vol, _created, _job in rows:
        rc, out = kubectl("delete", "backups.longhorn.io", name, "--ignore-not-found")
        if rc != 0:
            print(
                "delete FAILED for %s after %d deletion(s) — stopping: %s"
                % (name, deleted, out.strip()[:200]),
                file=sys.stderr,
            )
            return 1
        deleted += 1
    print("deleted %d %s." % (deleted, label))
    return 0


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    apply_deleted = "--apply-deleted-volumes" in argv
    unknown = [a for a in argv if a not in _FLAGS]
    if unknown:
        print(
            "unknown argument: %s (expected --apply, --apply-deleted-volumes)"
            % unknown[0],
            file=sys.stderr,
        )
        return 2

    needs_admin = apply or apply_deleted
    kubeconfig, err = logic.resolve_kubeconfig(
        needs_admin=needs_admin,
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

    vol_rc, vol_out = kubectl("get", "volumes.longhorn.io", "-o", "json")
    if vol_rc != 0:
        print(
            "ABORT: could not read volumes: %s" % vol_out.strip()[:200], file=sys.stderr
        )
        return 1
    try:
        volumes = json.loads(vol_out).get("items", [])
    except ValueError as e:
        print("ABORT: unparseable volume list: %s" % e, file=sys.stderr)
        return 1

    owner = logic.backup_owner_map(volumes)
    existing = logic.existing_volume_set(volumes)

    abort = logic.abort_reason(len(volumes), len(owner))
    if abort:
        print(abort, file=sys.stderr)
        return 1

    bkp_rc, bkp_out = kubectl("get", "backups.longhorn.io", "-o", "json")
    if bkp_rc != 0:
        print(
            "ABORT: could not read backups: %s" % bkp_out.strip()[:200], file=sys.stderr
        )
        return 1
    try:
        backups = json.loads(bkp_out).get("items", [])
    except ValueError as e:
        print("ABORT: unparseable backup list: %s" % e, file=sys.stderr)
        return 1

    result = logic.classify_backups(backups, owner, existing)

    _print_bucket("kept by a floor", result.kept)
    _print_bucket("reapable", result.candidates)
    _print_bucket("orphaned (volume deleted)", result.orphaned)

    count = len(result.candidates)
    orphan_count = len(result.orphaned)

    if not apply and not apply_deleted:
        print(
            "dry run — %d stray(s) and %d orphan(s) would be deleted."
            % (count, orphan_count)
        )
        print()
        print(
            "  COST: each deletion walks that volume's whole block tree — roughly 1.28 Class C"
        )
        print(
            "  transactions per stored block, against a 2,500/day free-tier cap. Seven strays"
        )
        print(
            "  measured ~3,640 Class C on 2026-08-17, 1.5x the daily cap. A short list is not a"
        )
        print(
            "  cheap one. Check 'probe.py b2-budget' for the per-volume figure before --apply."
        )
        print()
        print("  --apply                  deletes the reapable strays")
        print(
            "  --apply-deleted-volumes  deletes backups whose volume no longer exists"
        )
        return 0

    rc = 0
    if apply:
        rc = _delete_bucket(kubectl, "stray backup(s)", result.candidates) or rc
    if apply_deleted:
        rc = _delete_bucket(kubectl, "orphaned backup(s)", result.orphaned) or rc
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
