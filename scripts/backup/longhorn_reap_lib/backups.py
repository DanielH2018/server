"""Delete Longhorn Backup objects that no RecurringJob will ever prune.

DRY RUN BY DEFAULT, in every mode. Pass --apply to delete, and --apply-deleted-volumes to
delete backups whose volume no longer exists (the strays mode only). There is deliberately no
cron for this: it is an operator-invoked tool, because the safe-to-delete set depends on live
state this script can check but cannot guarantee will still hold a week from now. See
logic.py beside it for FLOOR 1 (never delete a volume's last recovery point) and why it
shipped inoperative the first time.

THREE MODES, ONE SELECTION LAYER. Every Backup CR deletion in this repo selects in
longhorn_reap_lib/logic.py (the strays mode) or longhorn_reap_lib/selection.py (the other two), where
each selection is a plain function over parsed JSON and each floor is provable against a
fixture.

  --mode strays          (the default) backups some job made that no job will prune now, because
                         the volume changed tier. FLOOR 1 keeps a volume's last recovery point,
                         FLOOR 2 the newest stray per volume.
  --mode migrated-chain  the chain a volume left behind when the block-size migration rebuilt
                         it, for one --claim. Needs the claim's replacement volume to hold a
                         Completed backup of its own, and the live-volume list to contain that
                         volume.
  --mode seeds           the seeds `scripts/backup/seed_volume_backup.py` makes, once the rotation
                         covers their volume: at least --seed-floor Completed backups carrying a
                         RecurringJob label. Narrow with --claim to pace the spend.

WHY THE OPERATOR MODES ARE NOT THE STRAYS MODE. The strays mode is all-or-nothing: it reaps
every stray it finds, and at the per-block cost below the full backlog from a 20-volume
migration is roughly 13,900 Class C against a 2,100/day budget -- the transaction-cap event the
migration exists to prevent. The migrated-chain mode deletes one claim's chain, so the spend can
be paced. For a weekly-tier (B2) backlog too large to pace, use
`scripts/backup/prune_backups.py` instead: it deletes the objects through the B2 API, and Longhorn's
backup-target sync then drops the dangling Backup CRs. the drain cannot reach R2, so a daily-tier
volume's chain still goes through migrated-chain, one claim at a time.

WHY A SEED NEEDS RETIRING AT ALL. A seed carries no RecurringJob label, so no job's `retain`
ever counts it and no job ever prunes it. Every seed is stranded by construction, and the strays
mode leaves it alone too: a backup with no job label reads as the current tier's own. Eleven
seeds from the 16 MiB migration held 5.4 G nominal against a 10 G B2 storage cap,
valheim-config's alone 1.76 G. Retiring those eleven came to ~880 blocks, ~1,130 Class C.

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

HOW MANY DELETIONS ONE RUN MAKES. Every mode is capped by --max-deletions, defaulting to
MAX_DELETIONS_DEFAULT, and --apply plus --apply-deleted-volumes count against it together. Over
the cap the run refuses before deleting anything rather than stopping partway, so the operator
re-decides against `probe.py b2-budget` instead of discovering the cap as a wall of 403s
mid-loop. A large backlog is meant to be reaped across several days.

ONE CAP FOR THREE MODES, CALIBRATED ON THE MOST EXPENSIVE. MAX_DELETIONS_DEFAULT comes from the
strays mode's ~520 Class C per deletion. A seed is cheaper -- about
~103 each -- so a seeds sweep that would fit inside a day's cap still refuses at four and asks
for `--max-deletions 11` deliberately. A per-mode cap table would be the wrong trade: the cap
exists so a human reads `probe.py b2-budget` before a bulk deletion, and the refusal prints the
Class C estimate it is refusing.

HOW A DELETE IS BOUNDED. Reads and deletes both go through host_lib.kubectl_runner, which owns
the /usr/local/bin PATH prepend and the two "never reached the cluster" return codes; the delete
differs only in the timeout bound to its runner. `--timeout` is the SERVER-side wait kubectl
honours, and the subprocess cap sits DELETE_TIMEOUT_MARGIN_S above it so the client can never
fire first and turn a delete that is still legitimately running into a false FAILED. Neither
bound cancels anything: kubectl has already issued the DELETE, and exceeding --timeout only
gives up WAITING for the finalizer while the server carries on. An unbounded run can hang
with the process holding no socket to the API server -- a run that neither finishes nor
reports.

WHY A FAILED `kubectl get volumes` ABORTS HERE rather than falling through with an EMPTY
`existing_volumes`: every completed, labelled backup would read as orphaned and become a
candidate under --apply-deleted-volumes. Aborting explicitly on a failed read refuses instead
of silently reclassifying every backup as belonging to a deleted volume.

Run from the repo root on a k3s host. The dry run reads through the read-only kubeconfig:
    LONGHORN_REAP_READONLY_KUBECONFIG=~/.kube/config \
        uv run python scripts/backup/longhorn_reap.py backups
        [--mode migrated-chain --claim sonarr-config]
        [--mode seeds [--claim valheim-config] [--seed-floor 2]]
Deleting needs the root-only admin kubeconfig, so run the same interpreter under sudo:
    sudo .venv/bin/python -B scripts/backup/longhorn_reap.py backups --apply
        [--mode seeds] [--apply-deleted-volumes] [--max-deletions N]

Read the dry run, then re-run the same command with --apply.
"""

import argparse
import os
import sys
from dataclasses import dataclass

from lib import host_modules  # noqa: F401
import host_lib
from lib.cli_help import ArgumentParser, answer_help
from lib.exit_codes import USAGE_ERROR
from backup.longhorn_reap_lib import logic, selection
from lib.k8s_objects import LonghornVolume

NAMESPACE = "longhorn-system"
KUBECTL_BIN = os.environ.get("LONGHORN_REAP_KUBECTL", "k3s kubectl")
# Each deletion costs ~520 Class C against a 2,500/day free tier, so four is the most that fits in a
# day.
MAX_DELETIONS_DEFAULT = 4
CLASS_C_PER_DELETION = 520

STRAYS_MODE = "strays"
MIGRATED_CHAIN_MODE = "migrated-chain"
SEEDS_MODE = "seeds"
MODES = (STRAYS_MODE, MIGRATED_CHAIN_MODE, SEEDS_MODE)
CLAIM_NAMESPACE = os.environ.get("LONGHORN_REAP_CLAIM_NAMESPACE", "homelab")

# Env override so a test can point at a fixture.
ADMIN_KUBECONFIG = os.environ.get(
    "LONGHORN_REAP_ADMIN_KUBECONFIG", "/etc/rancher/k3s/k3s.yaml"
)
# No default: unset, a dry run must refuse rather than inherit the caller's KUBECONFIG, which as
# root is the admin one.
READONLY_KUBECONFIG = os.environ.get("LONGHORN_REAP_READONLY_KUBECONFIG", "")
SUDO_HINT = logic.sudo_hint(logic.ENTRY_POINT, "backups")


def _delete_backup(name: str, config: logic.ReapConfig) -> tuple[int, str]:
    """Delete one Backup CR, bounded server-side and client-side. See the module docstring."""
    kubectl = host_lib.kubectl_runner(
        KUBECTL_BIN, NAMESPACE, config.delete_subprocess_timeout_s
    )
    return kubectl(
        "delete",
        logic.longhorn_backups.RESOURCE,
        name,
        "--ignore-not-found",
        "--timeout=%ds" % config.delete_timeout_s,
    )


def _delete_bucket(label: str, rows: list, config: logic.ReapConfig) -> int:
    """Delete every row in `rows`, stopping at the FIRST failure.

    Bash's loop did the same (`if ! $KUBECTL delete ...; then ... exit 1; fi`, unconditionally
    exiting the whole script). A caller that kept going into the next bucket after a failure
    here would delete under a kubeconfig or cluster state that had just proven unreliable.
    """
    print("deleting %s..." % label)
    deleted = 0
    for name, _vol, _created, _job in rows:
        rc, out = _delete_backup(name, config)
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


@dataclass
class Args:
    """One parsed command line.

    Attributes:
      mode: which set of Backup CRs to select, one of MODES.
      apply: delete the mode's own bucket.
      apply_deleted: delete the strays mode's orphaned bucket as well.
      max_deletions: the cap across both buckets.
      claim: the PVC a mode selects within. Required by migrated-chain, optional for seeds.
      seed_floor: how many job-labelled Completed backups a volume must hold before its seed
        is surplus. None until the seeds mode resolves it to SEED_FLOOR_DEFAULT, so an
        explicitly-passed floor can be refused in a mode that has no floor.
    """

    mode: str = STRAYS_MODE
    apply: bool = False
    apply_deleted: bool = False
    max_deletions: int = MAX_DELETIONS_DEFAULT
    claim: str = ""
    seed_floor: int | None = None


def _positive_int(raw: str) -> int:
    try:
        count = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("expects an integer, got: %s" % raw) from None
    if count < 1:
        raise argparse.ArgumentTypeError("must be at least 1, got: %d" % count)
    return count


def _pvc_name(raw: str) -> str:
    if not raw:
        raise argparse.ArgumentTypeError("needs a PVC name")
    return raw


def _parse_args(argv: list[str]) -> Args:
    """Parse argv into an `Args`; a bad command line exits 64 through `parser.error`.

    A flag that belongs to another mode is an error rather than a silent no-op: `--claim`
    ignored in the strays mode would read as "this run was scoped to one claim" while reaping
    every stray in the cluster.
    """
    parser = ArgumentParser(
        prog="longhorn_reap.py backups",
        allow_abbrev=False,
        example="longhorn_reap.py backups --mode migrated-chain --claim sonarr-config",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--apply-deleted-volumes", dest="apply_deleted", action="store_true"
    )
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--claim", type=_pvc_name, metavar="PVC")
    parser.add_argument("--max-deletions", type=_positive_int, metavar="N")
    parser.add_argument("--seed-floor", type=_positive_int, metavar="N")
    # The namespace is an `Args`, so its field defaults stand for every flag left off.
    args = parser.parse_args(argv, namespace=Args())

    if args.apply_deleted and args.mode != STRAYS_MODE:
        parser.error(
            "--apply-deleted-volumes is the %s mode's flag: it deletes backups whose volume no "
            "longer exists, which in the %s mode is the selection itself. Pass --apply."
            % (STRAYS_MODE, args.mode)
        )
    if args.claim and args.mode == STRAYS_MODE:
        parser.error(
            "--claim selects within the %s and %s modes; the %s mode reaps every stray it finds."
            % (MIGRATED_CHAIN_MODE, SEEDS_MODE, STRAYS_MODE)
        )
    if args.mode == MIGRATED_CHAIN_MODE and not args.claim:
        parser.error("--mode %s needs --claim <pvc>" % MIGRATED_CHAIN_MODE)
    if args.seed_floor is not None and args.mode != SEEDS_MODE:
        parser.error(
            "--seed-floor is the %s mode's floor; this run is --mode %s"
            % (SEEDS_MODE, args.mode)
        )
    return args


def _print_dry_run(args: Args, buckets: list[tuple[str, list, bool]]) -> None:
    """Say what an --apply would delete, what it would cost, and the next step, in three lines.

    The cost paragraph is the reason this prints at all: the reapable list reads as cheap
    because it is short, while the spend is per stored block, so the deletion cost has to
    survive someone reading only this output. The flag table is in `--help`; a 20-line
    banner on every dry run buries the count. ENFORCED:
    scripts/backup/tests/test_longhorn_reap_backups_cli.py
    ::test_every_modes_dry_run_prints_the_per_block_cost
    """
    if args.mode == STRAYS_MODE:
        print(
            "dry run — %d stray(s) and %d orphan(s) would be deleted."
            % (len(buckets[0][1]), len(buckets[1][1]))
        )
    else:
        print(
            "dry run — %d %s would be deleted in the %s mode."
            % (len(buckets[0][1]), buckets[0][0], args.mode)
        )
    print(
        "COST: ~1.28 Class C per stored block against a 2,500/day free tier; seven strays cost "
        "~3,640 on 2026-08-17. Check 'probe.py b2-budget' before --apply."
    )
    print(
        "next: --apply deletes this mode's bucket, capped at --max-deletions (default %d, "
        "~%s Class C); --help lists the other flags."
        % (MAX_DELETIONS_DEFAULT, f"{MAX_DELETIONS_DEFAULT * CLASS_C_PER_DELETION:,}")
    )


def main(argv: list[str]) -> int:
    answer_help(__doc__, argv)
    args = _parse_args(argv)
    try:
        config = logic.parse_reap_config()
    except ValueError as e:
        print("ABORT: %s" % e, file=sys.stderr)
        return USAGE_ERROR

    needs_admin = args.apply or args.apply_deleted
    refusal = logic.readonly_kubeconfig_refusal(
        needs_admin, READONLY_KUBECONFIG, "--apply / --apply-deleted-volumes"
    )
    if refusal:
        print(refusal, file=sys.stderr)
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

    kubectl = host_lib.kubectl_runner(KUBECTL_BIN, NAMESPACE, config.kubectl_timeout_s)

    vol_rc, vol_out = kubectl("get", "volumes.longhorn.io", "-o", "json")
    if vol_rc != 0:
        print(
            "ABORT: could not read volumes: %s" % vol_out.strip()[:200], file=sys.stderr
        )
        return 1
    volume_items, err = logic.parse_kubectl_json_items(vol_out, "volume list")
    if err:
        print("ABORT: %s" % err, file=sys.stderr)
        return 1

    volumes = [LonghornVolume.from_item(item) for item in volume_items]
    owner = logic.backup_owner_map(volumes)
    existing = logic.existing_volume_set(volumes)

    abort = logic.abort_reason(len(volumes), len(owner))
    if abort:
        print(abort, file=sys.stderr)
        return 1

    bkp_rc, bkp_out = kubectl(*logic.longhorn_backups.LIST_ARGS)
    if bkp_rc != 0:
        print(
            "ABORT: could not read backups: %s" % bkp_out.strip()[:200], file=sys.stderr
        )
        return 1
    backups, err = logic.parse_kubectl_json_items(bkp_out, "backup list")
    if err:
        print("ABORT: %s" % err, file=sys.stderr)
        return 1

    if args.mode == STRAYS_MODE:
        try:
            result = logic.classify_backups(backups, owner, existing)
        except logic.ReapAbort as e:
            # `abort_reason` already writes the `ABORT: ` prefix, so print the message as it stands.
            print(str(e), file=sys.stderr)
            return 1

        logic.print_bucket("kept by a floor", result.kept)
        logic.print_bucket("reapable", result.candidates)
        logic.print_bucket("orphaned (volume deleted)", result.orphaned)
        buckets = [
            ("stray backup(s)", result.candidates, args.apply),
            ("orphaned backup(s)", result.orphaned, args.apply_deleted),
        ]
        selection_refusal = None

    elif args.mode == MIGRATED_CHAIN_MODE:
        # Read the PVC collection, not `get pvc <name>`: a missing claim is then an empty selection,
        # not an exit code.
        claims = host_lib.kubectl_runner(
            KUBECTL_BIN, CLAIM_NAMESPACE, config.kubectl_timeout_s
        )
        pvc_rc, pvc_out = claims("get", "persistentvolumeclaims", "-o", "json")
        if pvc_rc != 0:
            print(
                "ABORT: could not read PVCs in %s: %s"
                % (CLAIM_NAMESPACE, pvc_out.strip()[:200]),
                file=sys.stderr,
            )
            return 1
        pvcs, err = logic.parse_kubectl_json_items(pvc_out, "PVC list")
        if err:
            print("ABORT: %s" % err, file=sys.stderr)
            return 1
        current_volume, err = selection.claim_volume(pvcs, args.claim, CLAIM_NAMESPACE)
        if err:
            print(err, file=sys.stderr)
            return 1

        chain = selection.select_migrated_chain(
            backups, existing, claim=args.claim, current_volume=current_volume
        )
        logic.print_bucket(
            "%s's replacement volume %s, Completed backups"
            % (args.claim, current_volume),
            chain.current,
        )
        logic.print_bucket("stranded chain of %s" % args.claim, chain.stranded)
        buckets = [("stranded backup(s)", chain.stranded, args.apply)]
        selection_refusal = chain.refusal

    else:
        floor = (
            selection.SEED_FLOOR_DEFAULT if args.seed_floor is None else args.seed_floor
        )
        seeds = selection.select_seeds(backups, existing, claim=args.claim, floor=floor)
        logic.print_bucket("kept by the rotation floor", seeds.kept)
        logic.print_bucket(
            "superseded by %d rotation backup(s)" % floor, seeds.superseded
        )
        buckets = [("superseded seed(s)", seeds.superseded, args.apply)]
        selection_refusal = seeds.refusal

    # A floor refuses in a dry run too, so the dry run says whether an apply would be allowed.
    if selection_refusal:
        print(selection_refusal, file=sys.stderr)
        return 1

    if not any(enabled for _label, _rows, enabled in buckets):
        _print_dry_run(args, buckets)
        return 0

    planned = sum(len(rows) for _label, rows, enabled in buckets if enabled)
    if planned > args.max_deletions:
        print(
            "REFUSING: %d deletion(s) requested, over the --max-deletions cap of %d. At the "
            "~%d Class C measured per deletion that is ~%s against a 2,500/day free tier. "
            "Reap the backlog across several days, or check 'probe.py b2-budget' and pass "
            "--max-deletions %d to override deliberately."
            % (
                planned,
                args.max_deletions,
                CLASS_C_PER_DELETION,
                f"{planned * CLASS_C_PER_DELETION:,}",
                planned,
            ),
            file=sys.stderr,
        )
        return 1

    for label, rows, enabled in buckets:
        if not enabled:
            continue
        rc = _delete_bucket(label, rows, config)
        if rc != 0:
            return rc
    return 0
