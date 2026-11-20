"""The tracked files an issue's cited paths resolve against.

WHY. An issue body cites a file from the repo root, from a subdirectory or by its name alone.
`issue_model.cited_paths` turns the last two into the tracked file they name, and it is a pure
function, so the `git ls-files` read it resolves against lives here.
"""

from functools import cache

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
from pathlib import Path


from lib.git import git

REPO = Path(__file__).resolve().parents[3]


@cache
def tracked_files(repo: Path = REPO) -> frozenset[str]:
    """The files `git ls-files` lists in `repo`, or none when git cannot read it."""
    proc = git("ls-files", "-z", cwd=repo, check=False)
    if proc.returncode:
        return frozenset()
    return frozenset(p for p in proc.stdout.split("\0") if p)
