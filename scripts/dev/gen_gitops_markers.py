#!/usr/bin/env python3
"""Copy the deployer's `gitops_markers.py` into monitor-bridge, the one tree that cannot ship it.

Run: uv run python scripts/dev/gen_gitops_markers.py [--check]

WHY A COPY. `ansible/roles/setup/gitops_deploy/files/gitops_markers.py` holds the state
directory, the marker basenames and the parsers for the `behind_since`, `manual_plane` and
`contention_since` line formats. Each reader once restated the directory and its basenames,
and three parsed the same lines independently (issue #2063).

Every other reader uses the source itself. Code that runs from the checkout imports it
through a named `sys.path` insert of `GITOPS_DEPLOY_FILES` (`scripts/lib/deployer_park.py`,
`gitops_state.py`). deploy-ui and renovate-agent install it into `/opt` with a `src:` naming
the deployer's `files/`, and `deploy_changes.SETUP_FILES_SHIPPED_BY_OTHER_ROLES` routes a
change to it to both roles (#3275, #3306). monitor-bridge is the exception: it ships its own
`files/` into a pod through a ConfigMap built from `monitor_bridge_modules`, so the module has
to sit in that directory. A committed copy with a freshness test is the single source there,
the arrangement `scripts/docs/gen_doc_fragments.py` already uses for the docs fragments.

Every copy is the source verbatim under a `generated_from:` header. That literal is what
`.claude/hooks/block-protected-edits.py` reads to tell a generated file from a hand-written
one, and it names this script, so a reader who opens a copy knows where to edit.
`ansible/tests/deploy/test_gitops_markers_copies.py` fails when a committed copy differs from
what this script writes now, and when monitor-bridge's module list does not carry it.

`COPIES` is the list. Add a consumer here and to that test's named census, then run this.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import REPO

SELF = "scripts/dev/gen_gitops_markers.py"
SOURCE = "ansible/roles/setup/gitops_deploy/files/gitops_markers.py"

# Every role that ships the deployer's markers from its own `files/`, by the path its copy
# lands at. Each consumer reaches it as a sibling module (`import gitops_markers`).
COPIES = ("ansible/roles/k8s/monitor-bridge/files/gitops_markers.py",)


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
