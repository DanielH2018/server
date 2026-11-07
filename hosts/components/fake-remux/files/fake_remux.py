#!/usr/bin/env python3
"""fake-remux — the detector and the reconciler behind one entrypoint.

Usage:
  fake_remux.py scan      # ffprobe the library for mislabeled remuxes, seed the ledger
  fake_remux.py replace   # reconcile the ledger: grab, verify and swap in genuine files

Each subcommand takes no further arguments. The fake-remux-scan and fake-remux-reconcile crons
run it under fake_remux_lock, and both always exit 0: the state file carries the verdict. The
logic lives in fake_remux_lib/ beside this file; each module's docstring describes its half, and
`fake_remux.py <subcommand> --help` prints it.
"""

from __future__ import annotations

import argparse
import sys

# Run directly, this file's own directory is on sys.path. That directory holds fake_remux_lib/
# and, on the host, the sibling host_lib.py the libraries import.
from fake_remux_lib import fake_remux_replace, fake_remux_scan

SUBCOMMANDS = {"scan": fake_remux_scan, "replace": fake_remux_replace}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True, metavar="{scan,replace}")
    for name, module in SUBCOMMANDS.items():
        doc = (module.__doc__ or "").strip()
        sub.add_parser(
            name,
            help=doc.splitlines()[0] if doc else None,
            description=doc,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return SUBCOMMANDS[parse_args(argv).command].main()


if __name__ == "__main__":
    sys.exit(main())
