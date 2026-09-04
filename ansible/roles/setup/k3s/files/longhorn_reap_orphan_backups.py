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

HOW MANY DELETIONS ONE RUN MAKES. --apply and --apply-deleted-volumes are capped together by
--max-deletions, defaulting to MAX_DELETIONS_DEFAULT. Over the cap the run refuses before
deleting anything rather than stopping partway, so the operator re-decides against
`probe.py b2-budget` instead of discovering the cap as a wall of 403s mid-loop. A large backlog
is meant to be reaped across several days.

HOW A DELETE IS BOUNDED. Reads and deletes both go through host_lib.kubectl_runner, which owns
the /usr/local/bin PATH prepend and the two "never reached the cluster" return codes; the delete
differs only in the timeout bound to its runner. `--timeout` is the SERVER-side wait kubectl
honours, and the subprocess cap sits DELETE_TIMEOUT_MARGIN_S above it so the client can never
fire first and turn a delete that is still legitimately running into a false FAILED. Neither
bound cancels anything: kubectl has already issued the DELETE, and exceeding --timeout only
gives up WAITING for the finalizer while the server carries on. Bash had no bound at all, and
the sibling snapshot reaper hung that way for 23 minutes on 2026-08-16 with the process holding
no socket to the API server -- a run that neither finishes nor reports.

WHY A FAILED `kubectl get volumes` ABORTS HERE rather than falling through, unlike bash. Bash's
volume read and its VOLUME_COUNT read were two separate `kubectl` calls, both swallowing stderr
(`2>/dev/null`); if the first failed, the second usually failed the same way, so VOLUME_COUNT
came back 0 too and the `VOLUME_COUNT>0 && OWNER_COUNT==0` abort check never fired (0>0 is
false) -- bash fell through with an EMPTY `existing_volumes`, so every completed, labelled
backup read as orphaned and became a candidate under --apply-deleted-volumes. Aborting
explicitly on a failed read is a deliberate improvement, not a port: it refuses instead of
silently reclassifying every backup as belonging to a deleted volume.

Run directly: uv run --no-project --python <pin> longhorn_reap_orphan_backups.py [--apply]
[--apply-deleted-volumes] [--max-deletions N]
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
# Server-side wait on each `kubectl delete`, in whole seconds. Integer rather than the
# snapshot reaper's "120s" duration string because nothing templates this one, and an int
# needs no duration parser to reach the subprocess cap below.
DELETE_TIMEOUT_S = int(os.environ.get("LONGHORN_REAP_DELETE_TIMEOUT_S", "120"))
# Margin the CLIENT-side subprocess cap carries over kubectl's own --timeout, so the subprocess
# can never fire first and report a still-running delete as FAILED. See the module docstring.
DELETE_TIMEOUT_MARGIN_S = 30
# Measured 2026-08-17: seven strays came to ~3,640 Class C, about 520 per deletion, against a
# 2,500/day free tier. Four deletions is ~2,080 -- the most that fits in one day's cap.
MAX_DELETIONS_DEFAULT = 4
CLASS_C_PER_DELETION = 520

# The admin kubeconfig is the same absolute path on every k3s host, unlike the read-only one
# (which is templated per sys_user), so the wrapper never overrides it. Overridable via env
# purely so a test can point this at a fixture instead of the real root-only file.
ADMIN_KUBECONFIG = os.environ.get(
    "LONGHORN_REAP_ADMIN_KUBECONFIG", "/etc/rancher/k3s/k3s.yaml"
)
# No fallback default: the read-only kubeconfig path is templated per sys_user and is not
# derivable here. Left unset, a dry run must refuse rather than let KUBECONFIG stay whatever
# the caller's shell happens to have -- which, run as root, is the admin one. See main().
READONLY_KUBECONFIG = os.environ.get("LONGHORN_REAP_READONLY_KUBECONFIG", "")
SUDO_HINT = "sudo /usr/local/bin/longhorn-reap-orphan-backups.sh --apply"

_MAX_DELETIONS_FLAG = "--max-deletions"
_USAGE = "expected --apply, --apply-deleted-volumes, %s N" % _MAX_DELETIONS_FLAG


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
                name, vol, created, job = row
                print("%s %s %s %s" % (name, vol, created, job))
    print()


def _delete_backup(name: str) -> tuple[int, str]:
    """Delete one Backup CR, bounded server-side and client-side. See the module docstring."""
    kubectl = host_lib.kubectl_runner(
        KUBECTL_BIN, NAMESPACE, DELETE_TIMEOUT_S + DELETE_TIMEOUT_MARGIN_S
    )
    return kubectl(
        "delete",
        "backups.longhorn.io",
        name,
        "--ignore-not-found",
        "--timeout=%ds" % DELETE_TIMEOUT_S,
    )


def _delete_bucket(label: str, rows: list) -> int:
    """Delete every row in `rows`, stopping at the FIRST failure.

    Bash's loop did the same (`if ! $KUBECTL delete ...; then ... exit 1; fi`, unconditionally
    exiting the whole script). A caller that kept going into the next bucket after a failure
    here would delete under a kubeconfig or cluster state that had just proven unreliable.
    """
    print("deleting %s..." % label)
    deleted = 0
    for name, _vol, _created, _job in rows:
        rc, out = _delete_backup(name)
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


def _parse_args(argv: list[str]) -> tuple[bool, bool, int, str]:
    """Split argv into the two apply flags and the deletion cap.

    Returns (apply, apply_deleted, max_deletions, error). `error` is empty on a good parse;
    a caller that gets a non-empty one prints it and exits 2. Both `--max-deletions N` and
    `--max-deletions=N` are accepted, because an operator typing this by hand under sudo will
    write either.
    """
    apply = False
    apply_deleted = False
    max_deletions = MAX_DELETIONS_DEFAULT
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--apply":
            apply = True
        elif arg == "--apply-deleted-volumes":
            apply_deleted = True
        elif arg == _MAX_DELETIONS_FLAG or arg.startswith(_MAX_DELETIONS_FLAG + "="):
            if "=" in arg:
                value = arg.split("=", 1)[1]
            elif rest:
                value = rest.pop(0)
            else:
                return (
                    apply,
                    apply_deleted,
                    max_deletions,
                    ("%s needs a count (%s)" % (_MAX_DELETIONS_FLAG, _USAGE)),
                )
            try:
                max_deletions = int(value)
            except ValueError:
                return (
                    apply,
                    apply_deleted,
                    max_deletions,
                    ("%s expects an integer, got: %s" % (_MAX_DELETIONS_FLAG, value)),
                )
            if max_deletions < 1:
                return (
                    apply,
                    apply_deleted,
                    max_deletions,
                    (
                        "%s must be at least 1, got: %d"
                        % (_MAX_DELETIONS_FLAG, max_deletions)
                    ),
                )
        else:
            return (
                apply,
                apply_deleted,
                max_deletions,
                ("unknown argument: %s (%s)" % (arg, _USAGE)),
            )
    return apply, apply_deleted, max_deletions, ""


def main(argv: list[str]) -> int:
    apply, apply_deleted, max_deletions, arg_err = _parse_args(argv)
    if arg_err:
        print(arg_err, file=sys.stderr)
        return 2

    needs_admin = apply or apply_deleted
    if not needs_admin and not READONLY_KUBECONFIG:
        print(
            "LONGHORN_REAP_READONLY_KUBECONFIG is not set, and a dry run must not silently "
            "fall through to whatever KUBECONFIG the caller's shell already has -- run as "
            "root, that would be the admin kubeconfig. Set the shim's env var, or pass "
            "--apply / --apply-deleted-volumes to use the admin kubeconfig explicitly.",
            file=sys.stderr,
        )
        return 1

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

    try:
        result = logic.classify_backups(backups, owner, existing)
    except logic.ReapAbort as e:
        # classify_backups owns the empty-volume-list refusal because it is the first place the
        # backup count is known. Uncaught it would reach the operator as a traceback, which is
        # not the shape every other refusal in this function prints.
        print("ABORT: %s" % e, file=sys.stderr)
        return 1

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
        print(
            "  --max-deletions N        caps the deletions one run makes across both buckets."
        )
        print(
            "                           Default %d: ~%d Class C per deletion measured "
            "2026-08-17, so %d"
            % (
                MAX_DELETIONS_DEFAULT,
                CLASS_C_PER_DELETION,
                MAX_DELETIONS_DEFAULT,
            )
        )
        print(
            "                           costs ~%s against the 2,500/day free tier. Over the "
            "cap the" % f"{MAX_DELETIONS_DEFAULT * CLASS_C_PER_DELETION:,}"
        )
        print("                           run refuses before deleting anything.")
        return 0

    planned = (count if apply else 0) + (orphan_count if apply_deleted else 0)
    if planned > max_deletions:
        print(
            "REFUSING: %d deletion(s) requested, over the --max-deletions cap of %d. At the "
            "~%d Class C measured per deletion that is ~%s against a 2,500/day free tier. "
            "Reap the backlog across several days, or check 'probe.py b2-budget' and pass "
            "--max-deletions %d to override deliberately."
            % (
                planned,
                max_deletions,
                CLASS_C_PER_DELETION,
                f"{planned * CLASS_C_PER_DELETION:,}",
                planned,
            ),
            file=sys.stderr,
        )
        return 1

    if apply:
        rc = _delete_bucket("stray backup(s)", result.candidates)
        if rc != 0:
            return rc
    if apply_deleted:
        rc = _delete_bucket("orphaned backup(s)", result.orphaned)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
