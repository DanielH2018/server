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

No gate here reads the cluster: both stop conditions are about what survives daniel-box, not
about what runs on it. Both stamps live on daniel-box, so the gates fail on any other host
rather than reading an absent directory as a pass. `ETCD_DRILL_STATE_DIR` and
`HOMELAB_STATE_DIR` point them elsewhere.

Exit codes:
  0      every gate passed
  1..2   the first gate that failed, by its number above

Usage:
    uv run python scripts/deploy_tools/k3s_etcd_restore_gates.py
"""

import os
import sys
import time
from pathlib import Path as _Path

# Reach `lib`: a directly-invoked script gets only its own directory on sys.path, and
# pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from deploy_tools import runbook_gates
from deploy_tools.runbook_gates import Gate, stamp_dir_missing

RUNBOOK = "docs/k3s-etcd-restore.md"

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


# ── the runner ──────────────────────────────────────────────────────────────────────────────


def _gate_listing(drill_dir: str, homelab_dir: str, now: float) -> list[str]:
    return unproven_listing(drill_dir, now)


def _gate_token(drill_dir: str, homelab_dir: str, now: float) -> list[str]:
    return missing_token_baseline(homelab_dir)


# Order is the runbook's, and the exit code is the position. Append; never reorder.
GATES = (
    Gate(1, "the off-box listing leg is proven", _gate_listing),
    Gate(2, "the cluster token has an off-box baseline", _gate_token),
)


def run_gates(
    drill_dir: str | None = None,
    homelab_dir: str | None = None,
    now: float | None = None,
    out=sys.stdout,
) -> int:
    """Run every gate in order, print one line per gate, and return the exit code."""
    drill_dir = drill_dir or os.environ.get("ETCD_DRILL_STATE_DIR") or DRILL_STATE_DIR
    homelab_dir = (
        homelab_dir or os.environ.get("HOMELAB_STATE_DIR") or HOMELAB_STATE_DIR
    )
    now = time.time() if now is None else now
    return runbook_gates.run_gates(GATES, RUNBOOK, drill_dir, homelab_dir, now, out=out)


def main(argv: list[str] | None = None) -> int:
    return runbook_gates.cli(__doc__, argv, run_gates)


if __name__ == "__main__":
    sys.exit(main())
