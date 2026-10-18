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
because it parses YAML and the deployer's unit runs under `uv run --no-project`. Exit
`DEPLOY_BROAD` (3), the code `deploy_tags.py narrow` refuses with, is a refusal, with the
reason on stderr, and the tick then applies no
setup role for the range, which is the behaviour before #5436. Any other non-zero exit, such
as a traceback's 1, is a failure the tick parks on and retries.

WITH `--deploy-tags` it prints the deploy plane's half instead (#5577): the comma-joined
`deploy.yml` tags whose render reads a moved key, as `deploy_tags.py narrow` does for a range
carrying a deploy-plane path. The tick asks it only for a range with no such path, which
`narrow` never sees. Exit 0 with empty stdout is the answer "no k8s role reads one". Exit
`DEPLOY_BROAD` is a refusal; its stdout names the tags when the refusal is the fleet-coverage
ceiling, and is empty when the derivation could not say at all.

Usage: secret_setup_readers.py [--repo PATH] [--deploy-tags] OLD NEW
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import json
import subprocess
import sys

from deploy_tools.narrow_lib import broad, secret_keys
from lib.cli_help import ArgumentParser
from lib.exit_codes import DEPLOY_BROAD, OK
from lib.narrow_git import CannotNarrow


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("old", help="the commit the checkout is on")
    parser.add_argument("new", help="the commit carrying the change")
    parser.add_argument(
        "--repo", default=".", help="the checkout to read (default: the cwd)"
    )
    parser.add_argument(
        "--deploy-tags",
        action="store_true",
        help="print the deploy tags reading a moved key instead of the setup files",
    )
    args = parser.parse_args(argv)

    def explain(message: str) -> None:
        print(message, file=sys.stderr)

    rules = secret_keys.Rules(
        broad._grep, broad._sort_hits, broad.template_importers, broad._role_tags
    )
    if args.deploy_tags:
        return _deploy_tags(args.old, args.new, _Path(args.repo), rules, explain)
    ctx = broad.Context(_Path(args.repo), args.new, set(), {}, explain)
    try:
        found = secret_keys.setup_reader_paths(args.old, ctx, rules)
    except (CannotNarrow, subprocess.CalledProcessError) as exc:
        print(f"secret-readers: cannot say ({exc})", file=sys.stderr)
        return DEPLOY_BROAD
    unread = {path: sorted(roles) for path, roles in sorted(found.unread.items())}
    print(json.dumps({"paths": sorted(found.paths), "unread": unread}))
    return OK


def _deploy_tags(old: str, new: str, repo: _Path, rules, explain) -> int:
    """Print the deploy tags reading a key `old..new` moved; exit `DEPLOY_BROAD` to refuse."""
    try:
        ctx = broad.context_for(new, repo, explain=explain)
        tags = secret_keys.reached(old, ctx, rules)
    except (CannotNarrow, subprocess.CalledProcessError) as exc:
        print(f"secret-readers: cannot say ({exc})", file=sys.stderr)
        reached = getattr(exc, "reached", ())
        if reached:
            print(",".join(sorted(reached)))
        return DEPLOY_BROAD
    # `broad.narrow`'s own ceiling, for its reason: a list covering most of the fleet saves
    # none of the full run's time and adds a way to miss what the full run would apply.
    if len(tags) * 2 > len(ctx.declared):
        print(
            f"secret-readers: cannot say ({len(tags)} of {len(ctx.declared)} services — "
            "most of the fleet, which is not a narrowing)",
            file=sys.stderr,
        )
        print(",".join(sorted(tags)))
        return DEPLOY_BROAD
    if tags:
        print(",".join(sorted(tags)))
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
