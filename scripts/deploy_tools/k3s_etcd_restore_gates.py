#!/usr/bin/env python3
"""Run the stop conditions of `docs/k3s-etcd-restore.md` in order, exit code naming the first failure.

A `--cluster-reset` restore rolls the whole cluster back to the snapshot's moment, and two
facts decide whether the snapshot it rolls back to is a backup at all. The runbook carried
both as prose and a hand-run `sha256sum`; here each is a verdict over the stamps the crons
write, the runner stops at the first failure, and the exit code is the gate number (#2216,
the shape `k3s_upgrade_gates.py` set in #2162). Run it BEFORE `systemctl stop k3s`.

The gates, in the order the runbook gives them:

  1. The off-box listing leg is proven. The weekly `--list-only` drill on daniel-box writes
     `/var/lib/etcd-restore-drill/last-success-list-only` when the R2 credentials, bucket and
     folder work and `k3s etcd-snapshot list --s3` returns real snapshots. The stamp must say
     `mode=list-only` and be younger than `MAX_DRILL_AGE_DAYS` — the number monitor-bridge's
     `check_etcd_restore_drill` defaults to for the same weekly cadence.
  2. The cluster token has an off-box baseline. Since 2026-08-20 the snapshot's bootstrap
     blob — CA certs, service key, the Secrets encryption config — is encrypted with the
     cluster token, so a restore onto a rebuilt host restores nothing without the copy. The
     operator stamps `/var/lib/homelab/etcd-token.sha256` at the moment the copy is taken, and
     the daily off-box cron reports DOWN when the live token stops matching it. The stamp is
     root-only by design, so this gate checks that it EXISTS; the match is the cron's verdict
     and the Kuma tile is where to read it.
  3. The snapshot you named is one the cluster knows and calls restorable. k3s records every
     local and S3 snapshot as an `ETCDSnapshotFile`, whose `spec.snapshotName` is exactly what
     `--cluster-reset-restore-path` takes and whose `status.readyToUse` says whether it can be
     restored at all. A name carrying `/` is refused before the cluster is asked: k3s reads
     that flag as a NAME, so a path there resolves to nothing and the restore fails after k3s
     is already stopped (#2243).

Gates 1 and 2 read only daniel-box: both stop conditions are about what survives the host, not
about what runs on it, so they fail on any other host rather than reading an absent directory
as a pass. `ETCD_DRILL_STATE_DIR` and `HOMELAB_STATE_DIR` point them elsewhere. Gate 3 is the
one cluster read, through `lib.kubectl` naming `prod`, so a staging kubectl is refused here too
(#1663) — run it before `systemctl stop k3s`, while the API server still answers.

A SINGLE GATE CAN BE RUN ON ITS OWN, and that is how gate 3 stops being exercised only on the
day of a real restore. Gate 3's live dependencies — the readonly SA's access to
`etcdsnapshotfiles`, and k3s still recording that CR at all — would otherwise first be read
during an outage, so the weekly `--list-only` etcd restore drill runs `--gate 3` against the
snapshot name it just listed and treats any refusal as a drill failure (#2420). That drill
cannot run the whole runner: gate 1 reads the stamp the same drill writes, which is circular,
and gate 2's stamp is the operator's to take.

Exit codes:
  0      every gate run passed
  1..3   the first gate that failed, by its number above
  64     usage
  69     the cluster could not be asked (no kubectl, no readable kubeconfig, wrong cluster,
         or a list that returned nothing parseable)

Usage:
    uv run python scripts/deploy_tools/k3s_etcd_restore_gates.py <snapshot-name>
    uv run python scripts/deploy_tools/k3s_etcd_restore_gates.py --gate 3 <snapshot-name>
"""

import os
import sys
import time
from pathlib import Path as _Path

# Reach `lib`: a directly-invoked script gets only its own directory on sys.path, and
# pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from deploy_tools import runbook_gates
from deploy_tools.runbook_gates import Gate, cluster_doc, items, stamp_dir_missing
from lib.kubectl import DEFAULT_TOOLS, Tools

CLUSTER = "prod"
RUNBOOK = "docs/k3s-etcd-restore.md"

SNAPSHOT_FILES_ARGS = ("get", "etcdsnapshotfiles.k3s.cattle.io", "-o", "json")

DRILL_STATE_DIR = "/var/lib/etcd-restore-drill"
DRILL_STAMP = "last-success-list-only"
DRILL_MODE = "list-only"
# The weekly cron's window: one missed Monday tolerated, the same default as monitor-bridge's
# `ETCD_DRILL_MAX_AGE_DAYS`.
MAX_DRILL_AGE_DAYS = 8

HOMELAB_STATE_DIR = "/var/lib/homelab"
TOKEN_STAMP = "etcd-token.sha256"


# ── the gates, as pure verdicts over what the stamps answered ───────────────────────────────


def parse_drill_stamp(body: str) -> dict[str, str]:
    """The `key=value` lines of a drill stamp, as the drill script writes them."""
    fields = {}
    for line in body.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def unproven_listing(
    state_dir: str | os.PathLike,
    now: float | None = None,
    max_age_s: float = MAX_DRILL_AGE_DAYS * 86400,
) -> list[str]:
    """Why the list-only stamp does not prove the off-box listing, or nothing when it does.

    Absent, unreadable, the wrong mode, no epoch, or too old are each a failure with its own
    message, because each needs a different fix.
    """
    missing = stamp_dir_missing(
        state_dir, "daniel-box, the host that runs the weekly drill"
    )
    if missing:
        return missing
    stamp = _Path(state_dir) / DRILL_STAMP
    try:
        fields = parse_drill_stamp(stamp.read_text())
    except FileNotFoundError:
        return [
            f"<{stamp} does not exist — the weekly list-only drill on daniel-box has never passed here>"
        ]
    except OSError as exc:
        return [f"<cannot read {stamp}: {exc}>"]
    mode = fields.get("mode", "")
    if mode != DRILL_MODE:
        return [f"{stamp} records mode={mode or 'nothing'}, not {DRILL_MODE}"]
    try:
        epoch = float(fields["epoch"])
    except KeyError, ValueError:
        return [f"{stamp} carries no readable epoch"]
    age_s = (time.time() if now is None else now) - epoch
    if age_s > max_age_s:
        return [
            f"the list-only drill last passed {age_s / 86400:.1f} days ago "
            f"(window {max_age_s / 86400:.0f} days; snapshot {fields.get('snapshot', '?')})"
        ]
    return []


def missing_token_baseline(state_dir: str | os.PathLike) -> list[str]:
    """The reason no off-box token baseline is on record, or nothing when the stamp exists."""
    missing = stamp_dir_missing(state_dir, "daniel-box, where the stamp is taken")
    if missing:
        return missing
    stamp = _Path(state_dir) / TOKEN_STAMP
    if stamp.is_file():
        return []
    return [
        f"<{stamp} does not exist — copy /var/lib/rancher/k3s/server/token out of band, "
        "then take the stamp (the runbook has the command); every snapshot since 2026-08-20 "
        "is undecryptable without that copy>"
    ]


def unusable_snapshot_name(name: str) -> list[str]:
    """Why the name cannot go to `--cluster-reset-restore-path` at all. Asks nothing.

    k3s reads that flag as a NAME and resolves it against the snapshot directory and the S3
    folder itself, so a path there names no snapshot — and the restore only says so after k3s
    has already been stopped. Refused here, before the cluster is asked, because neither
    failure depends on what the cluster holds.
    """
    if not name.strip():
        return ["<no snapshot name given — pass the one you intend to restore>"]
    if "/" in name:
        return [
            f"{name} is a path, and --cluster-reset-restore-path takes a NAME — pass the "
            "bare name `k3s etcd-snapshot list --s3` printed, not a file:// or s3:// location"
        ]
    return []


def snapshot_not_restorable(name: str, doc) -> list[str]:
    """Why the cluster's `ETCDSnapshotFile` for `name` does not clear a restore.

    Unknown and not-`readyToUse` are separate offenders. An unknown name lists what the
    cluster DOES record, because the two ways to get here need different fixes: a typo, or a
    bucket whose snapshots k3s has not reconciled into CRs.
    """
    known: dict[str, dict] = {}
    for item in items(doc):
        snapshot = str((item.get("spec") or {}).get("snapshotName") or "")
        if snapshot:
            known[snapshot] = item
    record = known.get(name)
    if record is None:
        recorded = ", ".join(sorted(known)) if known else "no snapshots at all"
        return [
            f"no ETCDSnapshotFile records {name} — the cluster records {recorded}. "
            "A snapshot k3s has not reconciled into a CR has no verdict here; "
            "`k3s etcd-snapshot list --s3` as root is the other listing"
        ]
    status = record.get("status") or {}
    if status.get("readyToUse") is not True:
        message = str((status.get("error") or {}).get("message") or "").strip()
        location = str((record.get("spec") or {}).get("location") or "?")
        return [
            f"{name} reports readyToUse={status.get('readyToUse')} (location {location})"
            + (f": {message}" if message else "")
        ]
    return []


# ── the runner ──────────────────────────────────────────────────────────────────────────────


def _gate_listing(
    drill_dir: str, homelab_dir: str, now: float, snapshot: str, tools: Tools
) -> list[str]:
    return unproven_listing(drill_dir, now)


def _gate_token(
    drill_dir: str, homelab_dir: str, now: float, snapshot: str, tools: Tools
) -> list[str]:
    return missing_token_baseline(homelab_dir)


def _gate_snapshot(
    drill_dir: str, homelab_dir: str, now: float, snapshot: str, tools: Tools
) -> list[str]:
    unusable = unusable_snapshot_name(snapshot)
    if unusable:
        return unusable
    return snapshot_not_restorable(
        snapshot, cluster_doc(CLUSTER, tools, SNAPSHOT_FILES_ARGS)
    )


# Order is the runbook's, and the exit code is the position. Append; never reorder.
GATES = (
    Gate(1, "the off-box listing leg is proven", _gate_listing),
    Gate(2, "the cluster token has an off-box baseline", _gate_token),
    Gate(3, "the named snapshot exists and is restorable", _gate_snapshot),
)


def run_gates(
    snapshot: str,
    drill_dir: str | None = None,
    homelab_dir: str | None = None,
    now: float | None = None,
    tools: Tools = DEFAULT_TOOLS,
    out=sys.stdout,
    only: int | None = None,
) -> int:
    """Run the gates in order, print one line per gate, and return the exit code.

    `only` restricts the run to the gate of that NUMBER, and does not change the exit code:
    `runbook_gates.run_gates` returns `gate.number`, not a position in the sequence it was
    handed, so `--gate 3` still exits 3 on a refusal.
    """
    drill_dir = drill_dir or os.environ.get("ETCD_DRILL_STATE_DIR") or DRILL_STATE_DIR
    homelab_dir = (
        homelab_dir or os.environ.get("HOMELAB_STATE_DIR") or HOMELAB_STATE_DIR
    )
    now = time.time() if now is None else now
    selected = GATES if only is None else tuple(g for g in GATES if g.number == only)
    if not selected:
        print(f"no gate numbered {only} — this runbook has {len(GATES)}", file=out)
        return runbook_gates.EX_USAGE
    return runbook_gates.run_gates(
        selected, RUNBOOK, drill_dir, homelab_dir, now, snapshot, tools, out=out
    )


def main(argv: list[str] | None = None) -> int:
    """`--gate N` is peeled off here rather than in the shared `cli`.

    `runbook_gates.cli` reads any argument starting with `-` as usage, and says in its own
    docstring that no gate script has a flag. That stays true of the shared entry point: this
    script takes its one flag off the front and hands `cli` the positionals it expects, so the
    other four gate scripts are untouched and `--bogus` is still usage here.
    """
    argv = sys.argv[1:] if argv is None else argv
    only: int | None = None
    if argv[:1] == ["--gate"]:
        if len(argv) < 2 or not argv[1].isdigit():
            print(__doc__, file=sys.stderr)
            return runbook_gates.EX_USAGE
        only, argv = int(argv[1]), argv[2:]
    return runbook_gates.cli(
        __doc__, argv, lambda snapshot: run_gates(snapshot, only=only), takes=1
    )


if __name__ == "__main__":
    sys.exit(main())
