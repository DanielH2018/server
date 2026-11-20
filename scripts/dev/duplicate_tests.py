#!/usr/bin/env python3
"""List the test functions whose bodies are identical, grouped.

Run: uv run python scripts/dev/duplicate_tests.py [PATH ...] [--json]

Reads every tracked `test_*.py` under each PATH, or under the whole repo with none. Two tests
are in one group when they take the same arguments, carry the same decorators and their bodies
parse to the same tree once the docstring is dropped, so a comment or a reformat hides nothing. A body of one
statement is skipped, because a lone `assert` matches by chance.

A group is a candidate, not a verdict. Copies that each read their own module's constant
guard different things, and the `prune-tests` skill has the rest of the keeps.

Prints a count line, then each group's node ids, largest group first. `--json` prints the
groups as one compact list of lists. Exits 0 with groups or without.
"""

import ast
import json
import sys
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path


from lib.cli_help import ArgumentParser
from lib.git import git
from lib.repo_paths import REPO

type TestDef = ast.FunctionDef | ast.AsyncFunctionDef


def _tests(
    scope: ast.Module | ast.ClassDef, prefix: str
) -> Iterator[tuple[str, TestDef]]:
    for node in scope.body:
        if isinstance(node, ast.ClassDef):
            yield from _tests(node, f"{prefix}::{node.name}")
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name.startswith("test_"):
            yield f"{prefix}::{node.name}", node


def _shape(test: TestDef) -> str | None:
    """What two tests must share to count as copies, or None for a body too short to judge."""
    body = test.body[1:] if ast.get_docstring(test) is not None else test.body
    if len(body) < 2:
        return None
    decorators = [ast.dump(decorator) for decorator in test.decorator_list]
    return "".join(
        [
            *decorators,
            ast.dump(test.args),
            ast.dump(ast.Module(body=body, type_ignores=[])),
        ]
    )


def duplicate_groups(sources: Mapping[str, str]) -> list[list[str]]:
    """The node ids of the tests in `sources` that share a body, one sorted list per group.

    Args:
      sources: each test module's text, keyed by its repo-relative path. A module that does
        not parse is skipped.

    Returns:
      Groups of two or more node ids, the largest group first, then by first node id.
    """
    groups: defaultdict[str, list[str]] = defaultdict(list)
    for path, text in sources.items():
        try:
            module = ast.parse(text)
        except SyntaxError:
            continue
        for node_id, test in _tests(module, path):
            shape = _shape(test)
            if shape is not None:
                groups[shape].append(node_id)
    found = [sorted(ids) for ids in groups.values() if len(ids) > 1]
    return sorted(found, key=lambda ids: (-len(ids), ids[0]))


def tracked_test_sources(paths: Sequence[str], repo: Path = REPO) -> dict[str, str]:
    """The text of each tracked `test_*.py` under `paths`, keyed by repo-relative path."""
    listed = git("ls-files", "--", *(paths or ["."]), cwd=repo).stdout.splitlines()
    return {
        path: (repo / path).read_text()
        for path in listed
        if Path(path).name.startswith("test_") and path.endswith(".py")
    }


def main(argv: Sequence[str] = ()) -> int:
    parser = ArgumentParser(
        description=(__doc__ or "").split("\n\n", 1)[0],
        epilog="A group is a candidate: the prune-tests skill lists what to keep.",
    )
    parser.add_argument("paths", nargs="*", metavar="PATH")
    parser.add_argument("--json", action="store_true", help="print compact JSON")
    args = parser.parse_args(list(argv))
    groups = duplicate_groups(tracked_test_sources(args.paths))
    if args.json:
        print(json.dumps(groups))
        return 0
    tests = sum(len(group) for group in groups)
    print(f"{len(groups)} groups of identical test bodies ({tests} tests)")
    for group in groups:
        print()
        for node_id in group:
            print(node_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
