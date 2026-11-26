"""The difference between two rendered trees: dyff for YAML and JSON, a text diff for the rest.

`dyff between` reads only the top level of a directory and fails on a nested one, so each
file of the two trees is paired by relative path. YAML and JSON pairs go to dyff, which
compares by structure: a reordered key prints nothing and a changed field prints its path.
Every other file gets a unified diff, and a file in one tree alone is named.
"""

import dataclasses
import difflib
import subprocess
from pathlib import Path

STRUCTURED_SUFFIXES = {".yaml", ".yml", ".json"}
# It hashes each directory's raw bytes, so a reorder-only change moves it; `digest` prints those.
SKIPPED = {"digests.json"}


class DyffFailed(Exception):
    """dyff exited nonzero for a pair, which means it could not read or compare the files."""


@dataclasses.dataclass
class TreeDiff:
    """What `diff_trees` found.

    Attributes:
        changed: Each file that differs, by its path relative to the tree.
        lines: The printed difference, empty when the trees match in structure.
        errors: One line per structured pair dyff could not compare.
    """

    changed: list[str] = dataclasses.field(default_factory=list)
    lines: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)


def _files(root: Path) -> dict[str, Path]:
    return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}


def _dyff(base: Path, head: Path, dyff: str) -> list[str]:
    run = subprocess.run(
        [dyff, "between", "--omit-header", "--color=off", str(base), str(head)],
        capture_output=True,
        text=True,
        check=False,
    )
    # Without --set-exit-code dyff exits 0 on a difference, so a nonzero exit is an error.
    if run.returncode:
        raise DyffFailed(run.stderr.strip() or run.stdout.strip())
    return run.stdout.splitlines()


def _text(base: Path, head: Path, name: str) -> list[str]:
    try:
        old = base.read_text(encoding="utf-8").splitlines()
        new = head.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return [f"Binary files base/{name} and head/{name} differ"]
    return list(
        difflib.unified_diff(old, new, f"base/{name}", f"head/{name}", lineterm="")
    )


def diff_trees(base: Path, head: Path, dyff: str = "dyff") -> TreeDiff:
    """Compare two rendered trees, carrying on past a pair dyff cannot read.

    Args:
        base: The merge base's render.
        head: The PR head's render.
        dyff: The dyff binary; a test passes a stand-in.

    Raises:
        OSError: dyff is not on PATH, or a file could not be read.
    """
    old, new = _files(base), _files(head)
    found = TreeDiff()
    for name in sorted((old.keys() | new.keys()) - SKIPPED):
        if name not in new:
            lines = [f"Only in base: {name}"]
        elif name not in old:
            lines = [f"Only in head: {name}"]
        elif old[name].read_bytes() == new[name].read_bytes():
            continue
        elif Path(name).suffix in STRUCTURED_SUFFIXES:
            try:
                fields = _dyff(old[name], new[name], dyff)
            except DyffFailed as exc:
                found.errors.append(f"{name}: {exc}")
                continue
            if not any(line.strip() for line in fields):
                continue
            lines = [f"=== {name}", *fields]
        else:
            lines = [f"=== {name}", *_text(old[name], new[name], name)]
        found.changed.append(name)
        found.lines += lines
    return found
