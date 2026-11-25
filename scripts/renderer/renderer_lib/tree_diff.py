"""The difference between two rendered trees: dyff for YAML, a text diff for the rest.

`dyff between` reads only the top level of a directory and fails on a nested one, so each
file of the two trees is paired by relative path. YAML pairs go to dyff, which compares by
structure and prints the path of a changed field. Every other file gets a unified diff, and a
file in one tree alone is named.
"""

import dataclasses
import difflib
import subprocess
from pathlib import Path

YAML_SUFFIXES = {".yaml", ".yml"}
# Its digests hash raw file bytes, so a reorder-only change moves them; `shadow_diff.py digest` reports that.
SKIPPED = {"digests.json"}


class DyffFailed(Exception):
    """dyff exited nonzero for a pair, which means it could not read or compare the files."""


@dataclasses.dataclass
class TreeDiff:
    """What `diff_trees` found.

    Attributes:
        lines: The printed difference, empty when the trees match in structure.
        errors: One line per YAML pair dyff could not compare.
    """

    lines: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)


def _files(root: Path) -> dict[str, Path]:
    return {str(p.relative_to(root)): p for p in sorted(root.rglob("*")) if p.is_file()}


def _dyff(base: Path, head: Path, dyff: str) -> list[str]:
    run = subprocess.run(
        [dyff, "between", "--omit-header", str(base), str(head)],
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
        return [f"Binary files {name} differ"]
    return list(
        difflib.unified_diff(old, new, f"base/{name}", f"head/{name}", lineterm="")
    )


def diff_trees(base: Path, head: Path, dyff: str = "dyff") -> TreeDiff:
    """Compare two rendered trees, carrying on past a YAML pair dyff cannot read.

    Args:
        base: The merge base's render.
        head: The PR head's render.
        dyff: The dyff binary.
    """
    old, new = _files(base), _files(head)
    found = TreeDiff()
    for name in sorted((old.keys() | new.keys()) - SKIPPED):
        if name not in new:
            found.lines.append(f"Only in base: {name}")
        elif name not in old:
            found.lines.append(f"Only in head: {name}")
        elif old[name].read_bytes() == new[name].read_bytes():
            continue
        elif Path(name).suffix in YAML_SUFFIXES:
            try:
                changed = _dyff(old[name], new[name], dyff)
            except DyffFailed as exc:
                found.errors.append(f"{name}: {exc}")
                continue
            if any(line.strip() for line in changed):
                found.lines += [f"=== {name}", *changed]
        else:
            found.lines += [f"=== {name}", *_text(old[name], new[name], name)]
    return found
