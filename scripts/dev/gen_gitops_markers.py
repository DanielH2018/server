#!/usr/bin/env python3
"""Copy the deployer's `gitops_markers.py` into every tree that reads its markers.

Run: uv run python scripts/dev/gen_gitops_markers.py [--check]

WHY A COPY. `ansible/roles/setup/gitops_deploy/files/gitops_markers.py` holds the state
directory, the marker basenames and the parsers for the `behind_since`, `manual_plane` and
`contention_since` line formats. Four other trees read those files and none of them can
import the deployer's `files/`: monitor-bridge ships its own `files/` into a pod, deploy-ui
and renovate-agent run from their own `/opt` directories under `uv run --no-project`, and
`scripts/lib/deployer_park.py` is imported by the SessionStart hook with only `scripts/` on
`sys.path`. Each restated the directory and its basenames, and three parsed the same lines
independently (issue #2063). A shared import is structurally impossible, so a committed copy
with a freshness test is the next best single source — the arrangement
`scripts/docs/gen_doc_fragments.py` already uses for the docs fragments.

Every copy is the source verbatim under a `generated_from:` header. That literal is what
`.claude/hooks/block-protected-edits.py` reads to tell a generated file from a hand-written
one, and it names this script, so a reader who opens a copy knows where to edit.
`ansible/tests/deploy/test_gitops_markers_copies.py` fails when a committed copy differs from
what this script writes now, and when a consumer role's ship list does not carry its copy.

`COPIES` is the list. Add a consumer here and to that test's named census, then run this.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import REPO

SELF = "scripts/dev/gen_gitops_markers.py"
SOURCE = "ansible/roles/setup/gitops_deploy/files/gitops_markers.py"

# Every tree that reads the deployer's markers, by the path its copy lands at. The consumer
# reaches it as a sibling module (`import gitops_markers`) or, under `scripts/`, as
# `lib.gitops_markers`.
COPIES = (
    "scripts/lib/gitops_markers.py",
    "ansible/roles/k8s/monitor-bridge/files/gitops_markers.py",
    "ansible/roles/setup/deploy_ui/files/gitops_markers.py",
    "ansible/roles/setup/renovate_agent/files/gitops_markers.py",
)


def header(target: str) -> str:
    """The first lines of a copy: provenance the hook can read, then the edit instruction."""
    return (
        f"# {target}\n"
        f"# generated_from: {SOURCE} -- do not edit.\n"
        f"# A verbatim copy written by {SELF}; edit the source, run it, and\n"
        "# commit every copy in the same PR.\n"
    )


def render(target: str, source_text: str) -> str:
    """What the copy at `target` must contain: the header, then the source minus its own path line."""
    body = source_text
    if body.startswith(f"# {SOURCE}\n"):
        body = body[len(f"# {SOURCE}\n") :]
    return header(target) + body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 naming any copy that differs from the source, writing nothing",
    )
    args = parser.parse_args(argv)
    source_text = (REPO / SOURCE).read_text()
    stale = []
    for target in COPIES:
        path = REPO / target
        want = render(target, source_text)
        have = path.read_text() if path.is_file() else None
        if have == want:
            continue
        stale.append(target)
        if not args.check:
            path.write_text(want)
    if args.check:
        for target in stale:
            print(f"stale: {target}", file=sys.stderr)
        return 1 if stale else 0
    print(f"gen_gitops_markers: {len(COPIES)} copies, {len(stale)} written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
