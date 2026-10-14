#!/usr/bin/env python3
"""Print what `land.sh` derives from a PR's file list -- tags, plane note, or self-applied flag.

WHY THIS IS NOT `land_tags.main`. The landing (`classify.classify`) expands shared-role callers
and renders the plane note over `plane_paths`: the PR's paths minus every shared-role path that
`shared_role_reach.paths_a_hand_must_apply` finds comment-only or deploy-time only. A CLI that
expanded over the raw file list printed nine caller tags for a one-comment edit to
`image-builder/tasks/main.yml`, for which the landing deploys nothing (#4945). The CLI must
therefore read `shared_role_reach`, and `shared_role_reach` imports `land_tags`, so the entry
point sits here, above both. `scripts/tests/test_scripts_import_direction.py` refuses the
cycle that the other placement makes.

Each argument goes to the reader `classify` gives it: `derive`, `own_narrow_tags`,
`self_applied` and the remaining-setup-hosts note read the raw paths; the caller expansion,
the guard-map narrowing and the plane note read `plane_paths`.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tags_cli.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))  # scripts/deploy_tools

import json
import sys
from pathlib import Path

import shared_role_reach
from deploy_tools.land_lib import land_tags
from deploy_tools.land_lib.land_reach import remaining_setup_hosts_note
from lib.cli_help import ArgumentParser
from lib.git import git
from lib.repo_paths import REPO


def hand_paths(paths: list[str], range_: str, repo: Path) -> list[str]:
    """`paths` minus the shared-role paths no hand applies, or `paths` on any failure.

    `classify` reads the declared tags at the merge commit; this CLI has none, so `None`
    makes `shared_role_reach` read the checkout's. A raise keeps every path, the way
    `classify._classified` does, so the CLI still exits 0 with the wider answer.
    """
    try:
        return shared_role_reach.paths_a_hand_must_apply(paths, range_, repo, None)
    except Exception:
        return paths


def main(argv: list[str] | None = None, repo: Path = REPO) -> int:
    """Print one fact about a PR's file list -- tags, plane note, self-applied flag, or remaining-setup-hosts note.

    Which one prints depends on `--plane`/`--self-applied`/`--remaining-setup-hosts`; with
    none of them, prints the derived `--tags` value. Always exits 0. `repo` is where the
    PR's own range is read over `--range`, a seam for the tests. The CLI has no merge commit
    to find a receipt by, so it narrows the way a fast-path landing does.
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
            "`<old>..<new>` — read the diff to drop broad paths whose change is comments "
            "only, and shared-role paths no rendered manifest reads"
        ),
    )
    ns = parser.parse_args(argv)
    payload = json.loads(ns.json)
    paths = [f["path"] for f in payload.get("files", [])]
    quiet = land_tags.quiet_paths(paths, ns.range_)
    if ns.plane:
        narrow = land_tags.own_narrow_tags(paths, ns.range_, repo)
        plane_paths = hand_paths(paths, ns.range_, repo)
        print(land_tags.plane_note(plane_paths, quiet=quiet, narrow_tags=narrow))
        return 0
    if ns.self_applied:
        print("yes" if land_tags.self_applied(paths, quiet=quiet) else "")
        return 0
    if ns.remaining_setup_hosts is not None:
        print(
            remaining_setup_hosts_note(
                paths, ns.remaining_setup_hosts, quiet=quiet, pr_range=ns.range_
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
    # The callers of what `--plane` stopped naming, as `classify` adds them.
    if source == land_tags.DeriveSource.PR:
        plane_paths = hand_paths(paths, ns.range_, repo)
        reached = land_tags.shared_caller_tags(plane_paths)
        rows = land_tags.guard_map_rows(
            plane_paths, ns.range_, lambda *a: git(*a, cwd=repo, check=False)
        )
        if rows is not None and "manifests" in reached:
            reached["manifests"] &= rows
        tags = sorted(set(tags).union(*reached.values()))
    print(f"{source} {','.join(tags)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
