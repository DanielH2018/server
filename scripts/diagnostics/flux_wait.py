#!/usr/bin/env python3
"""The `flux` source for cc-wait: one Flux Kustomization, Ready at an expected commit.

`cc-wait flux <name> [--revision SHA]` runs this through `.claude/wait-sources/flux`. cc-wait
owns the loop, the budget and the output; this answers one question per call: has Flux applied
this commit to the Kustomization yet?

    flux_wait.py --describe <name> [--revision SHA]   # terminal states, as JSON
    flux_wait.py <name> [--revision SHA]              # the state now, one JSON line

The contract is the dotfiles repo's docs/specs/2026-10-04-cc-wait-design.md. The read and the
judgement are `diagnostics/probe_lib/flux.py`'s `read_flux` and `wait_state`.

WHICH COMMIT. `--revision` takes a commit or a prefix of one. Without it, the commit is the
one the Kustomization's OCIRepository serves at each read, so the wait follows a promotion
that lands while it runs.

THE STATES. `ready` (exit 0): Ready=True with `lastAppliedOriginRevision` at that commit.
`failed` (exit 1): Ready=False for a reason other than a pending dependency, after an attempt
at the artifact the source serves, and the source serves that commit. `suspended` (exit 3):
`spec.suspend` is set, so nothing will apply. Every other reading keeps the wait open:
`absent` (no such Kustomization yet), `waiting` (the source serves no commit) and
`progressing`. A `--revision` the source never serves therefore waits until the budget ends.

A failed kubectl read exits 1 with the reason on stderr, which cc-wait retries.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
from diagnostics.probe_lib.flux import FluxUnreadable, read_flux, wait_state
from lib.cli_help import ArgumentParser, refuse_help_under_describe
from lib.kubectl import DEFAULT_CLUSTER, WrongCluster, kubectl_json

TERMINAL = {"ready": 0, "failed": 1, "suspended": 3}
# kustomize-controller's own interval is a minute, so a closer look finds nothing new.
INTERVAL_S = 15


def describe() -> dict:
    return {"terminal": TERMINAL, "watch": [], "interval_s": INTERVAL_S}


def main(argv: list[str] | None = None, read=kubectl_json) -> int:
    parser = ArgumentParser(prog="flux_wait.py", description=__doc__.splitlines()[0])
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("name", help="the Kustomization in flux-system, such as sonarr")
    parser.add_argument(
        "--revision",
        default=None,
        help="the commit, or a prefix of it (default: the one the source serves)",
    )
    parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    refuse_help_under_describe(parser, argv)
    ns = parser.parse_args(argv)
    if ns.describe:
        print(json.dumps(describe()))
        return 0
    try:
        items = read_flux(ns.cluster, read=read)
    except (FluxUnreadable, WrongCluster) as exc:
        print(f"flux_wait: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(wait_state(items, ns.name, ns.revision)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
