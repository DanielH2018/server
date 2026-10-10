"""Resolve a path an issue cites relative to some directory to the tracked file it names.

WHY (#4232). An issue body often cites a path from a subdirectory: #4116 cites
`fanout_lib/clean.py`, meaning `scripts/dev/fanout_lib/clean.py`. `issue_model.cited_paths`
returns what the body spells, so two consumers that compare against repo-root paths missed:
`red_green_eligible` refused the fragment, and the fan-out collision check let a fragment and
its full path land in two batches. 337 of the 1,617 `claude` issues filed from 2026-09-01 to
2026-10-10 cite at least one such fragment.

A fragment resolves only when exactly one tracked file ends with `/<fragment>`. An ambiguous or
unmatched one stays as written, so nothing is resolved by guesswork.
"""

from collections.abc import Collection, Iterable
from functools import cache

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib.git import git

REPO = Path(__file__).resolve().parents[3]


@cache
def tracked_files(repo: Path = REPO) -> frozenset[str]:
    """The files `git ls-files` lists in `repo`, or none when git cannot read it."""
    proc = git("ls-files", "-z", cwd=repo, check=False)
    if proc.returncode:
        return frozenset()
    return frozenset(p for p in proc.stdout.split("\0") if p)


def resolve_fragments(paths: Iterable[str], tracked: Collection[str]) -> list[str]:
    """Each of `paths`, replaced by the one tracked file it is a suffix of when there is one."""
    return [_resolve(path, tracked) for path in paths]


def _resolve(path: str, tracked: Collection[str]) -> str:
    if path in tracked:
        return path
    hits = [t for t in tracked if t.endswith(f"/{path}")]
    return hits[0] if len(hits) == 1 else path
