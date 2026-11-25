"""The difference between two rendered trees: dyff for YAML, a text diff for the rest.

`dyff between` reads only the top level of a directory and fails on a nested one, so each
file of the two trees is paired by relative path. YAML pairs go to dyff, which compares by
structure and prints the path of a changed field. Every other file gets a unified diff, and a
file in one tree alone is named.
"""

import difflib
import subprocess
from pathlib import Path

YAML_SUFFIXES = {".yaml", ".yml"}


class DyffFailed(Exception):
    """dyff exited nonzero for a pair, which means it could not read or compare the files."""


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
        raise DyffFailed(f"{base}: {run.stderr.strip() or run.stdout.strip()}")
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


def diff_trees(base: Path, head: Path, dyff: str = "dyff") -> list[str]:
    """The printed difference of two rendered trees, empty when they match in structure.

    Args:
        base: The merge base's render.
        head: The PR head's render.
        dyff: The dyff binary.

    Raises:
        DyffFailed: dyff could not compare a pair of YAML files.
    """
    old, new = _files(base), _files(head)
    lines: list[str] = []
    for name in sorted(old.keys() | new.keys()):
        if name not in new:
            lines.append(f"Only in base: {name}")
        elif name not in old:
            lines.append(f"Only in head: {name}")
        elif old[name].read_bytes() == new[name].read_bytes():
            continue
        elif Path(name).suffix in YAML_SUFFIXES:
            changed = _dyff(old[name], new[name], dyff)
            if any(line.strip() for line in changed):
                lines += [f"=== {name}", *changed]
        else:
            lines += [f"=== {name}", *_text(old[name], new[name], name)]
    return lines
