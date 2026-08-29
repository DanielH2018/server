#!/usr/bin/env python3
"""Derive deploy tags from a merged PR's own file list.

WHY NOT A SHA RANGE. `deploy.sh --changed <ref>` diffs a commit range, and after a merge
that range covers every other session's merged work too. Deploying somebody else's
half-finished landing is not this session's to do. A PR's file list is exactly this
session's scope.

WHY THE COUNT ASSERTION. `gh pr view --json files` paginates at 100. A 137-file PR returns
100 entries with no error and no marker, so the derived tag list is a silent subset of what
merged -- and every downstream check reads green over it. When the returned count disagrees
with the PR's own `changedFiles`, this reports `fallback` and the caller widens to
`deploy.sh --changed <since>`: wider than the truth is recoverable, narrower is not.

Run: uv run pytest scripts/deploy_tools/test_land_tags.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys

_K8S = re.compile(r"^ansible/roles/k8s/([^/]+)/")
_DOCKER = re.compile(r"^ansible/roles/containers/([^/]+)/")

# Directories under the role trees that are not services. `common` is the shared Docker
# deploy path and `archive` holds roles retired by the k3s migration; `--tags` for either
# matches no containers_list entry, and Ansible exits 0 on a tag that selects nothing --
# so a green run would prove only that nothing happened.
_NOT_SERVICES = frozenset({"common", "archive"})


def tag_for(path: str) -> str | None:
    """The deploy tag a changed path maps to, or None."""
    for pattern in (_K8S, _DOCKER):
        m = pattern.match(path)
        if m and m.group(1) not in _NOT_SERVICES:
            return m.group(1)
    return None


def derive(files, changed_files: int) -> tuple[list[str], str]:
    """(sorted tags, 'pr'|'fallback').

    'fallback' means the file list could not be trusted and the caller must widen to a SHA
    range. The tag list returned alongside it is empty on purpose: a partial list is worse
    than none, because it looks like an answer.
    """
    files = list(files)
    if len(files) != changed_files:
        return [], "fallback"
    tags = {t for p in files if (t := tag_for(p))}
    return sorted(tags), "pr"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json", required=True, help="`gh pr view --json files,changedFiles` output"
    )
    ns = parser.parse_args(argv)
    payload = json.loads(ns.json)
    tags, source = derive(
        [f["path"] for f in payload.get("files", [])],
        # -1 rather than 0: `gh` omitting the field must not be read as agreement with an
        # empty file list, which would silently license a zero-tag deploy.
        payload.get("changedFiles", -1),
    )
    print(f"{source} {','.join(tags)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
