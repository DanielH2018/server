#!/usr/bin/env python3
"""Delete Longhorn Backup objects that no RecurringJob will ever prune.

DRY RUN BY DEFAULT, in every mode. Pass --apply to delete, and --apply-deleted-volumes to
delete backups whose volume no longer exists (the strays mode only). There is deliberately no
cron for this: it is an operator-invoked tool, because the safe-to-delete set depends on live
state this script can check but cannot guarantee will still hold a week from now. See
longhorn_reap_logic.py for FLOOR 1 (never delete a volume's last recovery point) and why it
shipped inoperative the first time.

THREE MODES, ONE SELECTION LAYER. Every Backup CR deletion in this repo selects in
longhorn_reap_logic.py (the strays mode) or longhorn_reap_selectors.py (the other two), where
each selection is a plain function over parsed JSON and each floor is provable against a
fixture. The two operator-driven modes selected in kubectl and Jinja under
`ansible/prune_backups.yml` until #3279; that playbook now carries only its b2-drain mode, which
deletes B2 objects through the B2 API rather than Backup CRs through Longhorn.

  --mode strays          (the default) backups some job made that no job will prune now, because
                         the volume changed tier. FLOOR 1 keeps a volume's last recovery point,
                         FLOOR 2 the newest stray per volume.
  --mode migrated-chain  the chain a volume left behind when the block-size migration rebuilt
                         it, for one --claim. Needs the claim's replacement volume to hold a
                         Completed backup of its own, and the live-volume list to contain that
                         volume.
  --mode seeds           the seeds `ansible/seed_volume_backup.yml` made, once the rotation
                         covers their volume: at least --seed-floor Completed backups carrying a
                         RecurringJob label. Narrow with --claim to pace the spend.

WHY THE OPERATOR MODES ARE NOT THE STRAYS MODE. The strays mode is all-or-nothing: it reaps
every stray it finds, and at the per-block cost below the full backlog from a 20-volume
migration is roughly 13,900 Class C against a 2,100/day budget -- the transaction-cap event the
migration exists to prevent. The migrated-chain mode deletes one claim's chain, so the spend can
be paced. For a weekly-tier (B2) backlog too large to pace, use `ansible/prune_backups.yml -e
prune_mode=b2-drain` instead: it deletes the objects through the B2 API, and Longhorn's
backup-target sync then drops the dangling Backup CRs. b2-drain cannot reach R2, so a daily-tier
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
strays mode's ~520 Class C per deletion. A seed is cheaper -- the eleven-seed campaign measured
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

Run from the repo root on a k3s host. The dry run reads through the read-only kubeconfig:
    LONGHORN_REAP_READONLY_KUBECONFIG=~/.kube/config \
        uv run python scripts/backup/longhorn_reap_orphan_backups.py
        [--mode migrated-chain --claim sonarr-config]
        [--mode seeds [--claim valheim-config] [--seed-floor 2]]
Deleting needs the root-only admin kubeconfig, so run the same interpreter under sudo:
    sudo .venv/bin/python -B scripts/backup/longhorn_reap_orphan_backups.py --apply
        [--mode seeds] [--apply-deleted-volumes] [--max-deletions N]

Read the dry run, then re-run the same command with --apply.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path as _Path

# host_lib.py is the setup roles' shared host module and stays in ansible/roles/setup/common/files/.
# A directly-invoked script gets only its own directory on sys.path, so both inserts are needed.
sys.path.insert(
    0,
    str(
        _Path(__file__).resolve().parents[2]
        / "ansible"
        / "roles"
        / "setup"
        / "common"
        / "files"
    ),
)
sys.path.insert(0, str(_Path(__file__).resolve().parent))
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
import host_lib
from lib.cli_help import answer_help
import longhorn_reap_logic as logic
import longhorn_reap_selectors as selectors

NAMESPACE = "longhorn-system"
KUBECTL_BIN = os.environ.get("LONGHORN_REAP_KUBECTL", "k3s kubectl")
TIMEOUT = int(os.environ.get("LONGHORN_REAP_KUBECTL_TIMEOUT_S", "30"))
# Server-side wait on each `kubectl delete`, in whole seconds. Integer rather than the
# snapshot reaper's "120s" duration string, and an int
# needs no duration parser to reach the subprocess cap below.
DELETE_TIMEOUT_S = int(os.environ.get("LONGHORN_REAP_DELETE_TIMEOUT_S", "120"))
# Margin the CLIENT-side subprocess cap carries over kubectl's own --timeout, so the subprocess
# can never fire first and report a still-running delete as FAILED. See the module docstring.
DELETE_TIMEOUT_MARGIN_S = 30
# Measured 2026-08-17: seven strays came to ~3,640 Class C, about 520 per deletion, against a
# 2,500/day free tier. Four deletions is ~2,080 -- the most that fits in one day's cap.
MAX_DELETIONS_DEFAULT = 4
CLASS_C_PER_DELETION = 520

STRAYS_MODE = "strays"
MIGRATED_CHAIN_MODE = "migrated-chain"
SEEDS_MODE = "seeds"
MODES = (STRAYS_MODE, MIGRATED_CHAIN_MODE, SEEDS_MODE)
# The namespace whose PVCs the migrated-chain mode resolves a --claim against. Every workload
# claim in this homelab lives here; overridable by env so a test needs no second flag.
CLAIM_NAMESPACE = os.environ.get("LONGHORN_REAP_CLAIM_NAMESPACE", "homelab")

# The admin kubeconfig is the same absolute path on every k3s host. Overridable via env purely
# so a test can point this at a fixture instead of the real root-only file.
ADMIN_KUBECONFIG = os.environ.get(
    "LONGHORN_REAP_ADMIN_KUBECONFIG", "/etc/rancher/k3s/k3s.yaml"
)
# No fallback default. Left unset, a dry run must refuse rather than let KUBECONFIG stay whatever
# the caller's shell happens to have -- which, run as root, is the admin one. See main().
READONLY_KUBECONFIG = os.environ.get("LONGHORN_REAP_READONLY_KUBECONFIG", "")
SUDO_HINT = logic.sudo_hint(__file__)

_MAX_DELETIONS_FLAG = "--max-deletions"
_MODE_FLAG = "--mode"
_CLAIM_FLAG = "--claim"
_SEED_FLOOR_FLAG = "--seed-floor"
_USAGE = "expected --apply, --apply-deleted-volumes, %s N, %s <%s>, %s <pvc>, %s N" % (
    _MAX_DELETIONS_FLAG,
    _MODE_FLAG,
    "|".join(MODES),
    _CLAIM_FLAG,
    _SEED_FLOOR_FLAG,
)


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


def _flag_value(arg: str, flag: str, rest: list[str]) -> tuple[str, str]:
    """The value of `flag`, taken from `arg` after an `=` or popped off `rest`.

    Both spellings are accepted for every value flag, because an operator typing this by hand
    under sudo will write either. Returns (value, error); `error` is empty on success.
    """
    if "=" in arg:
        return arg.split("=", 1)[1], ""
    if rest:
        return rest.pop(0), ""
    return "", "%s needs a value (%s)" % (flag, _USAGE)


def _parse_args(argv: list[str]) -> tuple[Args, str]:
    """Split argv into an `Args`, or return the error to print before exiting 2.

    `error` is empty on a good parse. A flag that belongs to another mode is an error rather
    than a silent no-op: `--claim` ignored in the strays mode would read as "this run was
    scoped to one claim" while reaping every stray in the cluster.
    """
    args = Args()
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--apply":
            args.apply = True
        elif arg == "--apply-deleted-volumes":
            args.apply_deleted = True
        elif arg == _MODE_FLAG or arg.startswith(_MODE_FLAG + "="):
            value, err = _flag_value(arg, _MODE_FLAG, rest)
            if err:
                return args, err
            if value not in MODES:
                return args, "unknown mode: %s (expected one of: %s)" % (
                    value,
                    ", ".join(MODES),
                )
            args.mode = value
        elif arg == _CLAIM_FLAG or arg.startswith(_CLAIM_FLAG + "="):
            value, err = _flag_value(arg, _CLAIM_FLAG, rest)
            if err:
                return args, err
            if not value:
                return args, "%s needs a PVC name (%s)" % (_CLAIM_FLAG, _USAGE)
            args.claim = value
        elif arg in (_MAX_DELETIONS_FLAG, _SEED_FLOOR_FLAG) or arg.startswith(
            (_MAX_DELETIONS_FLAG + "=", _SEED_FLOOR_FLAG + "=")
        ):
            flag = (
                _MAX_DELETIONS_FLAG
                if arg.startswith(_MAX_DELETIONS_FLAG)
                else _SEED_FLOOR_FLAG
            )
            value, err = _flag_value(arg, flag, rest)
            if err:
                return args, err
            try:
                count = int(value)
            except ValueError:
                return args, "%s expects an integer, got: %s" % (flag, value)
            if count < 1:
                return args, "%s must be at least 1, got: %d" % (flag, count)
            if flag == _MAX_DELETIONS_FLAG:
                args.max_deletions = count
            else:
                args.seed_floor = count
        else:
            return args, "unknown argument: %s (%s)" % (arg, _USAGE)

    if args.apply_deleted and args.mode != STRAYS_MODE:
        return args, (
            "--apply-deleted-volumes is the %s mode's flag: it deletes backups whose volume no "
            "longer exists, which in the %s mode is the selection itself. Pass --apply."
            % (STRAYS_MODE, args.mode)
        )
    if args.claim and args.mode == STRAYS_MODE:
        return args, (
            "%s selects within the %s and %s modes; the %s mode reaps every stray it finds."
            % (_CLAIM_FLAG, MIGRATED_CHAIN_MODE, SEEDS_MODE, STRAYS_MODE)
        )
    if args.mode == MIGRATED_CHAIN_MODE and not args.claim:
        return args, "--mode %s needs %s <pvc>" % (MIGRATED_CHAIN_MODE, _CLAIM_FLAG)
    if args.seed_floor is not None and args.mode != SEEDS_MODE:
        return args, "%s is the %s mode's floor; this run is --mode %s" % (
            _SEED_FLOOR_FLAG,
            SEEDS_MODE,
            args.mode,
        )
    return args, ""


def _print_dry_run(args: Args, buckets: list[tuple[str, list, bool]]) -> None:
    """Say what an --apply would delete, then what it would cost and which flags it takes.

    The cost paragraph is the reason this prints at all: the reapable list reads as cheap
    because it is short, while the spend is per stored block. It is the prose the retired
    `prune_backups.yml` header carried for the two operator modes, which is why the deletion
    cost now has to survive someone reading only this output. ENFORCED:
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
    print("  --apply                  deletes this mode's own bucket")
    print("  --apply-deleted-volumes  deletes backups whose volume no longer exists")
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
    print("  --mode <%s>" % "|".join(MODES))
    print(
        "                           which set to select. --claim <pvc> scopes the two"
    )
    print(
        "                           operator modes; --seed-floor N is the seeds floor."
    )


def main(argv: list[str]) -> int:
    answer_help(__doc__, argv)
    args, arg_err = _parse_args(argv)
    if arg_err:
        print(arg_err, file=sys.stderr)
        return 2

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

    kubectl = host_lib.kubectl_runner(KUBECTL_BIN, NAMESPACE, TIMEOUT)

    vol_rc, vol_out = kubectl("get", "volumes.longhorn.io", "-o", "json")
    if vol_rc != 0:
        print(
            "ABORT: could not read volumes: %s" % vol_out.strip()[:200], file=sys.stderr
        )
        return 1
    volumes, err = logic.parse_kubectl_json_items(vol_out, "volume list")
    if err:
        print("ABORT: %s" % err, file=sys.stderr)
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
    backups, err = logic.parse_kubectl_json_items(bkp_out, "backup list")
    if err:
        print("ABORT: %s" % err, file=sys.stderr)
        return 1

    if args.mode == STRAYS_MODE:
        try:
            result = logic.classify_backups(backups, owner, existing)
        except logic.ReapAbort as e:
            # classify_backups owns the empty-volume-list refusal because it is the first place
            # the backup count is known. Uncaught it would reach the operator as a traceback,
            # which is not the shape every other refusal in this function prints. `abort_reason`
            # already writes the `ABORT: ` prefix into the message, so this prints it as it
            # stands.
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
        # Read the PVC collection for one namespace, not `get pvc <name>`: the collection comes
        # back as the same `{"items": [...]}` body every other read here parses, so a claim that
        # does not exist is an empty selection rather than an exit code to interpret.
        claims = host_lib.kubectl_runner(KUBECTL_BIN, CLAIM_NAMESPACE, TIMEOUT)
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
        current_volume, err = selectors.claim_volume(pvcs, args.claim, CLAIM_NAMESPACE)
        if err:
            print(err, file=sys.stderr)
            return 1

        chain = selectors.select_migrated_chain(
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
            selectors.SEED_FLOOR_DEFAULT if args.seed_floor is None else args.seed_floor
        )
        seeds = selectors.select_seeds(backups, existing, claim=args.claim, floor=floor)
        logic.print_bucket("kept by the rotation floor", seeds.kept)
        logic.print_bucket(
            "superseded by %d rotation backup(s)" % floor, seeds.superseded
        )
        buckets = [("superseded seed(s)", seeds.superseded, args.apply)]
        selection_refusal = seeds.refusal

    # A floor refuses in a dry run too, after the plan is printed, so the dry run says whether
    # an apply would be allowed instead of making the operator discover it under --apply.
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
        rc = _delete_bucket(label, rows)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
