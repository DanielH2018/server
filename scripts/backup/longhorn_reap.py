#!/usr/bin/env python3
"""Delete Longhorn Backup and Snapshot objects that no RecurringJob will ever prune.

Two operator-invoked reapers behind one entry point, each DRY RUN BY DEFAULT:

  backups    Backup CRs no job will prune: tier-move strays, a migrated volume's old chain,
             and retired seeds (`--mode strays|migrated-chain|seeds`).
  snapshots  Snapshot CRs a tier move stranded on their volume.

Nothing may schedule either one; ansible/tests/longhorn/test_longhorn_reap_orphan_never_scheduled.py
holds every setup role to that. `longhorn_reap.py <subcommand> --help` prints that reaper's
flags, floors and run commands. The code is in longhorn_reap_lib/ beside this file.

Run from the repo root on a k3s host. A dry run reads through the read-only kubeconfig, and
an --apply re-runs the same interpreter under sudo for the admin one:
    LONGHORN_REAP_READONLY_KUBECONFIG=~/.kube/config uv run python scripts/backup/longhorn_reap.py backups
    sudo .venv/bin/python -B scripts/backup/longhorn_reap.py backups --apply
"""

import sys
from pathlib import Path as _Path

# A directly-invoked script gets only its own directory on sys.path, which is the one that
# holds longhorn_reap_lib/; scripts/ is for `lib`.
sys.path.insert(0, str(_Path(__file__).resolve().parent))
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from lib.cli_help import HELP_FLAGS

SUBCOMMANDS = ("backups", "snapshots")


def main(argv: list[str], now: float | None = None) -> int:
    """Run the reaper `argv[0]` names with the rest of `argv`.

    Args:
        argv: the subcommand, then that reaper's own flags.
        now: the epoch the snapshots reaper measures its age floor from; None reads the clock.
            The backups reaper reads no clock.

    Returns:
        The reaper's exit code, or 2 when no known subcommand comes first.
    """
    if argv and argv[0] in HELP_FLAGS:
        print(__doc__.strip())
        return 0
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    subcommand, rest = argv[0], argv[1:]
    # Imported only once chosen: each reaper reads its env at import, and the snapshots
    # reaper's env abort must not fire on a `backups` run.
    if subcommand == "backups":
        from longhorn_reap_lib import backups

        return backups.main(rest)
    if subcommand == "snapshots":
        from longhorn_reap_lib import snapshots

        return snapshots.main(rest, now=now)
    print(
        "unknown subcommand: %s (expected one of: %s)"
        % (subcommand, ", ".join(SUBCOMMANDS)),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
