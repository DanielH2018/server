#!/usr/bin/env python3
"""Which hosts beyond the tick's own a setup-role change over a range still reaches (#6510).

THE PROBLEM. The GitOps tick applies a setup role only on its own host. `hypervisor` routes
`on_tick_host: true`, so the tick runs it on daniel-box, where only its teardown half applies,
and records nothing for daniel-server, which runs the install half. `land.sh` printed the one
line naming daniel-server, and a session that missed it left the host stale for four days
(#6411).

WHAT THIS PRINTS. One JSON object on stdout: each setup role the tick applies on HOST, mapped
to the sorted hosts other than HOST that the range's change to it reaches. `{}` when none
does. `land_lib.land_reach.remaining_setup_hosts` holds the derivation that `land.sh`'s note
reads too, so the tick and the landing name the same hosts.

WHO CALLS IT. `deployer_lib.deploy_setup_roles.setup_reach_hosts` runs it as a SUBPROCESS,
because it parses YAML and the deployer's unit runs under `uv run --no-project`. The deployer
writes one `manual_plane` line per role it names. Any non-zero exit records nothing.

Usage: setup_reach_hosts.py OLD NEW --host HOST
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
_sys.path.insert(1, str(_Path(__file__).resolve().parent))  # scripts/deploy_tools

import json

from deploy_tools.land_lib import land_tags
from deploy_tools.land_lib.land_reach import remaining_setup_hosts
from lib.cli_help import ArgumentParser
from lib.exit_codes import OK
from lib.git import git
from lib.repo_paths import REPO


def main(argv: list[str] | None = None, repo: _Path = REPO) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("old", help="the commit the checkout was on")
    parser.add_argument("new", help="the commit carrying the change")
    parser.add_argument("--host", required=True, help="the host the tick runs on")
    args = parser.parse_args(argv)
    range_ = f"{args.old}..{args.new}"
    paths = git("diff", "--name-only", range_, cwd=repo).stdout.splitlines()
    reach = remaining_setup_hosts(
        paths,
        args.host,
        quiet=land_tags.quiet_paths(paths, range_),
        pr_range=range_,
        ref=args.new,
        repo=repo,
    )
    print(json.dumps({role: sorted(hosts) for role, hosts in sorted(reach.items())}))
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
