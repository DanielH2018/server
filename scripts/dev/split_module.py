#!/usr/bin/env python3
"""Split a large Python module along its seams: show the references, then move names by spec.

Usage::

    uv run python scripts/dev/split_module.py graph SRC
    uv run python scripts/dev/split_module.py split SRC SPEC.json

`graph` prints one line per top-level definition -- its line number, its name, and the
other top-level names its body references -- so the seams are visible before anything
moves. `split` then cuts the named definitions out of SRC into new files::

    {"<new path>": {"header": "<docstring + imports>", "names": ["_helper", "test_x", ...]}}

Each named def, class or assignment is cut with its decorators and the comment block
directly above it, and appended to its target in SRC order. SRC keeps everything the spec
does not name. Imports are NOT rewritten: the header carries the target's imports, and
`uv run ruff check --fix` afterwards removes the ones SRC no longer uses and reports the
ones a target still lacks (F401 / F821).

This is the tool that split the 1,000-line guards under `ansible/tests/` during the
2026-09-02 reorganization (PRs #762 and #768). It works on the AST, so a name that only
appears inside a string or a comment is neither moved nor counted as a reference.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import TypedDict


class Target(TypedDict):
    """One entry of a split spec: the header text for the new file, and the names to move."""

    header: str
    names: list[str]


Spec = dict[str, Target]


def _names_of(node: ast.stmt) -> list[str]:
    """The top-level names a statement defines: a def/class name, or its assignment targets."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name]
    if isinstance(node, ast.Assign):
        # `A = B = 1` has two targets; `A, B = 1, 2` has one Tuple target naming both.
        return [
            elt.id
            for t in node.targets
            for elt in (t.elts if isinstance(t, ast.Tuple | ast.List) else [t])
            if isinstance(elt, ast.Name)
        ]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def references(source: str) -> list[tuple[int, str, list[str]]]:
    """(lineno, name, referenced top-level names) for every top-level definition, in order."""
    tree = ast.parse(source)
    top: dict[str, ast.stmt] = {}
    for node in tree.body:
        for name in _names_of(node):
            top[name] = node
    names = set(top)
    return [
        (
            node.lineno,
            name,
            sorted(
                {
                    n.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Name) and n.id in names and n.id != name
                }
            ),
        )
        for name, node in top.items()
    ]


def _span(node: ast.stmt, lines: list[str]) -> tuple[int, int]:
    """1-based inclusive (start, end) covering the decorators and the comment block above."""
    start = node.lineno
    if getattr(node, "decorator_list", None):
        start = min(d.lineno for d in node.decorator_list)
    while start > 1 and lines[start - 2].lstrip().startswith("#"):
        start -= 1
    assert node.end_lineno is not None
    return start, node.end_lineno


def split(source: str, spec: Spec) -> tuple[str, dict[str, str]]:
    """Return (what SRC keeps, {target path: its new content}) without touching any file.

    Raises ValueError when a name is claimed by two targets, when one statement (such as a
    tuple assignment) would have to go to two targets, or when a named definition is not in
    the source at all -- each of those is a spec bug, and a partial move is worse than none.
    """
    owner: dict[str, str] = {}
    for target, cfg in spec.items():
        for name in cfg["names"]:
            if name in owner:
                raise ValueError(f"{name} named twice ({owner[name]} and {target})")
            owner[name] = target

    lines = source.splitlines(keepends=True)
    moved: dict[str, list[str]] = {target: [] for target in spec}
    cut: set[int] = set()
    seen: set[str] = set()
    for node in ast.parse(source).body:
        names = _names_of(node)
        targets = {owner[n] for n in names if n in owner}
        if not targets:
            continue
        if len(targets) > 1:
            raise ValueError(f"one statement maps to two targets: {names}")
        a, b = _span(node, lines)
        moved[targets.pop()].append("".join(lines[a - 1 : b]))
        cut.update(range(a, b + 1))
        seen.update(names)

    missing = set(owner) - seen
    if missing:
        raise ValueError(f"not found in the source: {sorted(missing)}")

    outputs = {}
    for target, cfg in spec.items():
        header = cfg["header"].rstrip("\n")
        body = "\n\n".join(chunk.rstrip("\n") for chunk in moved[target])
        outputs[target] = f"{header}\n\n\n{body}\n"
    kept = "".join(line for i, line in enumerate(lines, 1) if i not in cut)
    return kept, outputs


def main(argv: list[str] | None = None) -> int:
    """Dispatch to `graph` (print references) or `split` (move names per SPEC).

    `split` writes its outputs to disk. Exits 1 when the spec is invalid, 0 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    graph = sub.add_parser(
        "graph", help="print each top-level name and what it references"
    )
    graph.add_argument("src", type=Path)
    mover = sub.add_parser(
        "split", help="move the names in SPEC out of SRC into new files"
    )
    mover.add_argument("src", type=Path)
    mover.add_argument("spec", type=Path)
    args = parser.parse_args(argv)

    source = args.src.read_text()
    if args.command == "graph":
        for lineno, name, used in references(source):
            print(f"{lineno:4d} {name} -> {' '.join(used)}")
        return 0

    try:
        kept, outputs = split(source, json.loads(args.spec.read_text()))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for target, content in outputs.items():
        Path(target).write_text(content)
        print(f"{target}: {len(spec_names(content))} definitions")
    args.src.write_text(kept)
    print(f"{args.src}: {len(kept.splitlines())} lines kept")
    return 0


def spec_names(content: str) -> list[str]:
    """The top-level names a written target defines, for the summary line."""
    return [name for node in ast.parse(content).body for name in _names_of(node)]


if __name__ == "__main__":
    sys.exit(main())
