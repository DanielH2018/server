#!/usr/bin/env python3
"""The issue-fanout tooling: place batches, wait on them, run a review unit, sum the reviews.

Usage::

    fanout.py place read
    fanout.py place launch --batch 1345,1386 [--batch 1288] [--host daniel-box] [--review]
    fanout.py place status <run-id>
    fanout.py probe [--describe] <run-id>...
    fanout.py review --batch 1345-1386 --repo DanielH2018/server [--red-green] < .fanout/brief.md
    fanout.py stats [--dir DIR ...] [--since YYYY-MM-DD] [--json]

`fanout.py <command> --help` prints that command's own options. Each command's logic is one
module under `fanout_lib/`, named in `COMMANDS`. This file imports only the module the command
names: `review` and `place clean-one` run under `uv run --no-project`, which installs nothing,
so no other command's imports may reach them (#4167).

The pre-consolidation paths `fanout_place.py`, `fanout_probe.py`, `fanout_review.py` and
`fanout_review_stats.py` stay beside this file as forwarding shims (#4346). Running fan-out
batches, older checkouts and `fanout_lib.clean`'s remote leg still name them.
"""

import argparse
import importlib
import sys

COMMANDS = {
    "place": "fanout_lib.place",
    "probe": "fanout_lib.wait_probe",
    "review": "fanout_lib.review_unit",
    "stats": "fanout_lib.review_stats",
}

_HELP = {
    "place": "place, claim, watch, stop and clean fan-out batches",
    "probe": "the `fanout` source for cc-wait",
    "review": "run one `launch --review` batch: implement, review, fix, land",
    "stats": "sum the fan-out review records",
}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fanout.py",
        description=__doc__.splitlines()[0],
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(f"  {name:<8}{text}" for name, text in _HELP.items()),
    )
    p.add_argument("command", choices=list(COMMANDS))
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] not in COMMANDS:
        # `--help` exits 0 with the command list; anything else is a usage error, exit 2.
        _parser().parse_args(argv[:1])
        return 2
    return importlib.import_module(COMMANDS[argv[0]]).main(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
