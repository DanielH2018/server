"""Longhorn's B2 backup objects: what the estate holds, what it costs, and what block size it's on.

Backs the `b2-longhorn`, `b2-budget` and `longhorn-blocks` subcommands. B2 publishes no usage
API, so most of this is reconstruction — from a listing of the backup store and from live
Volume CRs. See b2_ledger for the transaction ledger these commands record into.

Split again at 630 lines into four helper modules this one drives:

  - `b2_api.py`          — the B2 calls, the paged listing and its parser
  - `longhorn_budget.py` — the Class C price of a retention prune, per weekly shard
  - `longhorn_cluster.py`— the live Volume/Backup/PV/BackupTarget reads
  - `longhorn_blocks.py` — the block-size census and its verdict

What stays here is the part that needs several of them at once: the three `run_*` subcommand
entry points, which decrypt the B2 credentials, record the spend into `b2_ledger`, and print.
b2_ledger imports this module back by module object, so no helper module may import it.
"""

import json
import subprocess

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib import b2_ledger as ledger
from diagnostics.probe_lib import core
from diagnostics.probe_lib.b2_api import (
    B2_AUTHORIZE_URL,
    # Re-exported only: cli_parser.py imports it here as the `--prefix` default.
    # Nothing in this module reads it, so ruff --fix deletes the import without the noqa,
    # and probe.py then fails at import.
    LONGHORN_PREFIX,  # noqa: F401
    b2_longhorn_lines,
    format_longhorn_summary,
    parse_longhorn_listing,
)
from diagnostics.probe_lib.longhorn_blocks import (
    format_block_census,
    volume_tier_census,
)
from diagnostics.probe_lib.longhorn_budget import (
    format_backup_budget,
    parse_backup_budget,
)
from diagnostics.probe_lib.longhorn_cluster import (
    # Re-exported only: b2_ledger reaches it as `longhorn.backup_target_url()`
    # (b2_ledger.py:474), and nothing in this module calls it.
    backup_target_url,  # noqa: F401
    pvc_names,
    volume_owned_backup_counts,
    volume_shard_labels,
)


def run_b2_budget(ns):
    """Project each weekly shard's Class C spend against B2's free-tier daily cap.

    One listing (~10 Class C), so it is cheap enough to run weekly and far too expensive to
    put in the 10-minute monitor cron.
    """
    if ns.dry_run:
        print(
            f"GET {B2_AUTHORIZE_URL} then b2_list_file_names prefix={ns.prefix.rstrip('/')}/ "
            "plus kubectl get volumes.longhorn.io,pv"
        )
        return 0

    stats = {}
    lines = b2_longhorn_lines(
        core.sops_extract("kopia_b2_key_id"),
        core.sops_extract("kopia_b2_application_key"),
        ns.bucket or core.sops_extract("kopia_b2_bucket"),
        ns.prefix,
        _stats=stats,
    )
    ledger.record_b2_spend(
        "b2-budget",
        class_c=stats.get("class_c", 0),
        note=f"{stats.get('pages', 0)} pages",
    )
    vols = parse_backup_budget(lines)
    # Persist the per-volume prune price for `b2-deletions`, which prices a deletion that has
    # ALREADY happened and so can no longer measure the tree it walked. This listing is the only
    # thing in the repo that computes those directory counts, and it costs 2-3 Class C to make —
    # so writing them down here is what turns an unrepeatable measurement into one an after-the-
    # fact accounting pass can use.
    ledger.write_prune_snapshot(vols)
    text, code = format_backup_budget(
        vols,
        volume_shard_labels(),
        pvc_names(),
        ns.retain,
        volume_owned_backup_counts(),
    )
    print(text)
    return code


def run_b2_longhorn(ns):
    """Prove Longhorn's backups hold real data blocks in B2, not just metadata.

    The design doc's §6 gate is that a service's data must be visible in its NEW backup
    path before the Docker copy is decommissioned, so slice 2 needs this per service.

    Costs a handful of transactions (one listing, paged at 1000 objects) — negligible
    against the daily free allowance, but not free: don't put it in a loop.
    """
    # Ahead of the credential read: --dry-run exists to describe the call WITHOUT making it,
    # so it must not decrypt anything either.
    if ns.dry_run:
        bucket = ns.bucket or "<kopia_b2_bucket>"
        print(
            f"GET {B2_AUTHORIZE_URL} then b2_list_file_names "
            f"bucket={bucket} prefix={ns.prefix.rstrip('/')}/"
        )
        return 0

    bucket = ns.bucket or core.sops_extract("kopia_b2_bucket")
    stats = {}
    lines = b2_longhorn_lines(
        core.sops_extract("kopia_b2_key_id"),
        core.sops_extract("kopia_b2_application_key"),
        bucket,
        ns.prefix,
        _stats=stats,
    )
    ledger.record_b2_spend(
        "b2-longhorn",
        class_c=stats.get("class_c", 0),
        note=f"{stats.get('pages', 0)} pages",
    )
    text, code = format_longhorn_summary(parse_longhorn_listing(lines))
    print(text)
    return code


def run_longhorn_blocks(ns):
    """Census live Volume CRs by tier and backup block size (read-only, spends no B2)."""
    argv = [
        "kubectl",
        "-n",
        "longhorn-system",
        "get",
        "volumes.longhorn.io",
        "-o",
        "json",
    ]
    if getattr(ns, "dry_run", False):
        print(" ".join(argv))
        return 0
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"cannot list Longhorn volumes: {proc.stderr.strip()}")
        return 2
    text, code = format_block_census(volume_tier_census(json.loads(proc.stdout)))
    print(text)
    return code
