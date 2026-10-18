#!/usr/bin/env python3
"""Which setup-role files read a `secrets.yml` key a range moved (#5436).

THE PROBLEM. PR #5405 added `claude_code_triage_claude_oauth_token`, read only by
`setup/claude_code`. The tick derives setup tags from changed paths, and `secrets.yml` sits
under no setup role, so the range applied no setup role and `claude_code` ran by hand.

WHAT THIS PRINTS. One JSON object on stdout. `paths` lists every file under
`ansible/roles/setup/` at NEW naming a key whose ciphertext differs between OLD and NEW. The
deployer maps each through its own `setup_roles_for`, so a file another role ships reaches that
role as well. `unread` maps a shipped `defaults/` or `vars/` file to the shippers that read
none of its keys built from a moved secret, which the deployer leaves out. `narrow_lib.secret_keys`
holds the derivation, beside the deploy-plane half that maps the same keys to deploy tags.

WHO CALLS IT. `deployer_lib.deploy_setup_roles.secret_setup_readers` runs it as a SUBPROCESS,
because it parses YAML and the deployer's unit runs under `uv run --no-project`. Exit 1 is a
refusal, with the reason on stderr. The tick then applies no setup role for the range, which
is the behaviour before #5436.

Usage: secret_setup_readers.py [--repo PATH] OLD NEW
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import json
import subprocess
import sys

from deploy_tools.narrow_lib import broad, secret_keys
from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK
from lib.narrow_git import CannotNarrow


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("old", help="the commit the checkout is on")
    parser.add_argument("new", help="the commit carrying the change")
    parser.add_argument(
        "--repo", default=".", help="the checkout to read (default: the cwd)"
    )
    args = parser.parse_args(argv)

    def explain(message: str) -> None:
        print(message, file=sys.stderr)

    ctx = broad.Context(_Path(args.repo), args.new, set(), {}, explain)
    rules = secret_keys.Rules(
        broad._grep, broad._sort_hits, broad.template_importers, broad._role_tags
    )
    try:
        found = secret_keys.setup_reader_paths(args.old, ctx, rules)
    except (CannotNarrow, subprocess.CalledProcessError) as exc:
        print(f"secret-readers: cannot say ({exc})", file=sys.stderr)
        return FAILED
    unread = {path: sorted(roles) for path, roles in sorted(found.unread.items())}
    print(json.dumps({"paths": sorted(found.paths), "unread": unread}))
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
