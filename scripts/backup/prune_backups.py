#!/usr/bin/env python3
"""Drain stranded Longhorn volume prefixes straight out of B2, with credentials from SOPS.

DRY RUN BY DEFAULT: read the dry run, then re-run the same command with --apply. This command
adds three things to `b2_drain.py`, which holds the safety logic: the SOPS credentials, the live
Longhorn volume list that script refuses to act without, and the backup target sync after an
apply, which makes Longhorn drop the Backup CRs whose objects are gone.

WHICH PATH TO USE. This deletes B2 objects through the B2 API: one store listing plus Class A
deletes, which are unmetered. It reaches B2 only, never R2, so a daily-tier volume's chain goes
through `longhorn_reap.py backups --mode migrated-chain`, one claim at a time. That reaper also
deletes through Longhorn, at about 1.28 Class C per stored block, and owns the Backup CRs.
docs/archive/b2-api-drain-scoping.md has the measurements.

Run from the repo root on daniel-box, as yourself and never under sudo: SOPS reads your age key.
The sync asks sudo for its one kubectl write.
    uv run python scripts/backup/prune_backups.py --volumes pvc-aaa,pvc-bbb [--apply]
    uv run python scripts/backup/prune_backups.py --volumes-file <path> [--fresh-listing] [--apply]

Exits with `b2_drain.py`'s own code, or 2 when the sync after a finished drain failed.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(
    0, str(_Path(__file__).resolve().parents[2] / "hosts" / "lib")
)  # host_lib
from diagnostics.probe_lib import core
from lib.cli_help import ArgumentParser, error
from lib.exit_codes import B2_DRAIN_ERRORED, B2_DRAIN_OK, B2_DRAIN_REFUSED
from lib.kubectl import K3S_KUBECONFIG, MissingKubectl, kubectl_argv
from lib.operator_kubectl import Kubectl, KubectlFailed, for_mode
from longhorn_lib.longhorn_backups import B2_TARGET

LONGHORN_NAMESPACE = "longhorn-system"
DRAIN = str(_Path(__file__).resolve().parent / "b2_drain.py")
# In /tmp and not the checkout, where an untracked file parks the deployer. The dry run saves
# its listing here and an --apply within the hour reuses it, so the two steps pay for one.
LISTING_FILE = "/tmp/longhorn-b2-listing.json"


def live_volumes(kubectl: Kubectl) -> list[str]:
    """The Longhorn volumes that exist right now."""
    out = kubectl(
        "-n",
        LONGHORN_NAMESPACE,
        "get",
        "volumes.longhorn.io",
        "-o",
        "custom-columns=V:.metadata.name",
        "--no-headers",
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def drain_argv(args: argparse.Namespace, live_file: str) -> list[str]:
    """The `b2_drain.py` command for these flags."""
    source = (
        ["--volumes-file", args.volumes_file]
        if args.volumes_file
        else ["--volumes", args.volumes]
    )
    return [
        sys.executable,
        DRAIN,
        "--live-volumes-file",
        live_file,
        *source,
        "--listing-file",
        LISTING_FILE,
        *(["--fresh-listing"] if args.fresh_listing else []),
        *(["--apply"] if args.apply else []),
    ]


def sync_argv(now: datetime) -> list[str]:
    """The sudo command asking Longhorn to re-read the B2 target."""
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return kubectl_argv(
        "-n",
        LONGHORN_NAMESPACE,
        "patch",
        "backuptarget",
        B2_TARGET,
        "--type",
        "merge",
        "-p",
        '{"spec":{"syncRequestedAt":"%s"}}' % stamp,
        kubeconfig=K3S_KUBECONFIG,
        privileged=True,
    )


def main(
    argv: list[str] | None = None,
    kubectl: Kubectl | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    secret: Callable[[str], str] = core.sops_extract,
) -> int:
    """Read the live volumes and the credentials, run the drain, and sync after an apply.

    Args:
        argv: the command line; None reads `sys.argv`.
        kubectl: replaces the real read-only runner, for a test.
        run: the process boundary for the drain and the sync.
        secret: reads one SOPS key.

    Returns:
        `b2_drain.py`'s exit code, or B2_DRAIN_ERRORED when this command's own read or the
        sync failed.
    """
    parser = ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--volumes", help="comma-separated volume names to drain")
    # Twenty volume names is 820 characters, which no shell pastes reliably on one line.
    source.add_argument("--volumes-file", help="file of volume names to drain")
    parser.add_argument(
        "--fresh-listing",
        action="store_true",
        help="list the store afresh even when the dry run's listing is reusable",
    )
    parser.add_argument(
        "--apply", action="store_true", help="delete; without it nothing changes"
    )
    args = parser.parse_args(argv)

    try:
        live = live_volumes(kubectl or for_mode(False, ""))
    except (KubectlFailed, MissingKubectl) as err:
        error(f"could not read the live Longhorn volumes: {err}")
        return B2_DRAIN_ERRORED
    if not live:
        error(
            "the cluster returned no Longhorn volumes. Nothing can be proven stranded "
            "against an empty list, so no prefix is safe to delete right now."
        )
        return B2_DRAIN_ERRORED

    # Credentials travel in the environment, so `ps` never shows them.
    env = {
        **os.environ,
        "B2_KEY_ID": secret("longhorn_b2_key_id"),
        "B2_APP_KEY": secret("longhorn_b2_application_key"),
    }
    with tempfile.NamedTemporaryFile("w", prefix="longhorn-live-") as live_file:
        live_file.write("\n".join(live) + "\n")
        live_file.flush()
        code = run(drain_argv(args, live_file.name), env=env, check=False).returncode

    if not args.apply or code not in (B2_DRAIN_OK, B2_DRAIN_REFUSED):
        return code
    sync = sync_argv(datetime.now(timezone.utc))
    if run(sync, check=False).returncode != 0:
        error(
            "the drain finished, but the target sync failed, so Longhorn still lists the "
            f"drained Backup CRs. Retry: {' '.join(sync)}"
        )
        return B2_DRAIN_ERRORED
    return code


if __name__ == "__main__":
    sys.exit(main())
