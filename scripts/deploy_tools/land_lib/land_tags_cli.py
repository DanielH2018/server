#!/usr/bin/env python3
"""Print what `land.sh` derives from a PR's file list -- tags, plane note, or self-applied flag.

WHY THIS IS NOT `land_tags.main`. The remaining-setup-hosts note comes from `land_reach`, which
imports `land_tags`, so the entry point sits above both.
`scripts/tests/test_scripts_import_direction.py` refuses the cycle that the other placement makes.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tags.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))  # scripts/deploy_tools

import json
import sys
from pathlib import Path

from deploy_tools.land_lib import land_tags
from deploy_tools.land_lib.key_readers import readable_key_readers
from deploy_tools.land_lib.land_reach import remaining_setup_hosts_note
from lib.cli_help import ArgumentParser
from lib.exit_codes import LAND_GAVE_UP
from lib.repo_paths import REPO


def main(argv: list[str] | None = None, repo: Path = REPO) -> int:
    """Print one fact about a PR's file list -- tags, plane note, self-applied flag, or remaining-setup-hosts note.

    Which one prints depends on `--plane`/`--self-applied`/`--remaining-setup-hosts`; with
    none of them, prints the derived `--tags` value. Exits 0, except `--plane` exits 75 when
    the owed ledger cannot be read and a path nominates a setup role for it to answer, as the
    landing ends `deferred` there. `repo` is where the PR's own range is read over `--range`,
    a seam for the tests. The CLI has no merge commit to find a receipt by, so it narrows the
    way a fast-path landing does.
    """
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json", required=True, help="`gh pr view --json files,changedFiles` output"
    )
    parser.add_argument(
        "--plane",
        action="store_true",
        help="print what still needs a manual apply (empty if nothing), not the tags",
    )
    parser.add_argument(
        "--self-applied",
        action="store_true",
        help="print `yes` if the tick applies part of this PR itself (empty otherwise)",
    )
    parser.add_argument(
        "--remaining-setup-hosts",
        metavar="HOST",
        default=None,
        help=(
            "print what a self-applied setup role in this PR still needs beyond HOST -- "
            "the host the tick just ran on (empty if nothing)"
        ),
    )
    parser.add_argument(
        "--range",
        dest="range_",
        default="",
        help=(
            "`<old>..<new>` — read the diff to drop broad paths whose change is comments only"
        ),
    )
    parser.add_argument(
        "--deployer-state",
        type=Path,
        default=land_tags.DEPLOYER_STATE,
        help="the deployer's state directory, whose owed ledger `--plane` reads",
    )
    ns = parser.parse_args(argv)
    payload = json.loads(ns.json)
    paths = [f["path"] for f in payload.get("files", [])]
    quiet = land_tags.quiet_paths(paths, ns.range_)
    if ns.plane:
        narrow = land_tags.own_narrow_tags(paths, ns.range_, repo)
        try:
            owed = land_tags.owed_for(paths, quiet, ns.deployer_state)
        except land_tags.LedgerUnreadable as exc:
            print(exc, file=sys.stderr)
            return LAND_GAVE_UP
        print(land_tags.plane_note(paths, quiet=quiet, narrow_tags=narrow, owed=owed))
        return 0
    if ns.self_applied:
        print("yes" if land_tags.self_applied(paths, quiet=quiet) else "")
        return 0
    if ns.remaining_setup_hosts is not None:
        print(
            remaining_setup_hosts_note(
                paths,
                ns.remaining_setup_hosts,
                quiet=quiet,
                pr_range=ns.range_,
                key_readers=readable_key_readers(ns.range_, repo),
            )
        )
        return 0
    tags, source = land_tags.derive(
        paths,
        # -1 rather than 0: `gh` omitting the field must not be read as agreement with an
        # empty file list, which would silently license a zero-tag deploy.
        payload.get("changedFiles", -1),
        quiet=quiet,
    )
    print(f"{source} {','.join(tags)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
