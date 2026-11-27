"""`affected_lib/table.py:select` never leaves out a module a diff can affect, over generated trees.

The oracle reads the base tree B and the head tree H directly. It lists each directory's
immediate children in both trees and shares no code with `select`'s ancestor walk. A module
the oracle names and `select` leaves out is a false green: the pull request passes without
the test its change breaks. Selecting more than the oracle is allowed. The property test
runs under the profile `tests/hypothesis_profiles.py` loads.

Run: uv run pytest scripts/dev/tests/ci/test_affected_table_properties.py
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from dev.affected_lib import table
from hypothesis import given
from hypothesis import strategies as st

# A few names make generated paths share directories, which is where the ancestor walk stops.
_NAMES = ("a", "b", "c")
_ELSEWHERE = "z/test_elsewhere.py"
_paths = st.lists(st.sampled_from(_NAMES), min_size=1, max_size=4).map("/".join)


def _blob(tag: str, path: str) -> str:
    return hashlib.sha1(f"{tag}:{path}".encode()).hexdigest()


def _as_tree(paths: set[str]) -> set[str]:
    """`paths` less each one another path sits under: one path cannot be a file and a directory."""
    return {path for path in paths if not any(o.startswith(f"{path}/") for o in paths)}


def _dirs(tree: Iterable[str]) -> set[str]:
    return {""} | {
        path.rsplit("/", depth)[0]
        for path in tree
        for depth in range(1, path.count("/") + 1)
    }


def _listing(tree: Iterable[str], directory: str) -> set[str]:
    """The names directly inside `directory`, files and directories alike."""
    prefix = f"{directory}/" if directory else ""
    return {
        path[len(prefix) :].split("/")[0] for path in tree if path.startswith(prefix)
    }


@dataclass(frozen=True)
class _Diff:
    base: frozenset[str]
    head: frozenset[str]
    blobs: dict[str, str]
    changes: list[tuple[str, str]]
    census: list[str]
    rows: table.Table


@st.composite
def _diffs(draw) -> _Diff:
    base = _as_tree(draw(st.sets(_paths, min_size=1, max_size=8)))
    removed = draw(st.sets(st.sampled_from(sorted(base))))
    head = _as_tree((base - removed) | draw(st.sets(_paths, max_size=5)))
    kept = sorted(base & head)
    modified = draw(st.sets(st.sampled_from(kept))) if kept else set()
    blobs = {
        path: _blob("head" if path in modified or path not in base else "base", path)
        for path in head
    }
    changes = sorted(
        [("A", path) for path in head - base]
        + [("D", path) for path in base - head]
        + [("M", path) for path in modified]
    )

    paths, dirs = sorted(base | head), sorted(_dirs(base) | _dirs(head))
    named = st.sets(st.sampled_from(paths), max_size=3) if paths else st.just(set())
    census = (
        sorted(draw(st.sets(st.sampled_from(sorted(head)), min_size=1, max_size=4)))
        if head
        else []
    )
    recorded = set(census) | draw(named)
    rows = table.Table()
    files: dict[str, set[str]] = {module: draw(named) for module in recorded}
    # A modified or deleted path no row holds sends `select` to the whole census, which says
    # nothing about the walk, so a module outside the census reads the rest.
    files[_ELSEWHERE] = base - set().union(*files.values())
    for module in sorted(files):
        row = table.Row(
            files=frozenset(files[module]),
            dirs=frozenset(draw(st.sets(st.sampled_from(dirs), max_size=1))),
            missing=frozenset(
                draw(st.sets(st.sampled_from(paths + dirs[1:]), max_size=2))
            ),
            tree=draw(st.integers(0, 5)) == 0,
        )
        current = draw(st.integers(0, 4)) > 0
        rows.put(module, row, blobs.get(module, "") if current else "0" * 40)
    return _Diff(frozenset(base), frozenset(head), blobs, changes, census, rows)


def _must_select(diff: _Diff) -> set[str]:
    changed = {path for _, path in diff.changes}
    listed = {
        directory
        for directory in _dirs(diff.base) | _dirs(diff.head)
        if _listing(diff.base, directory) != _listing(diff.head, directory)
    }
    appeared = (diff.head | _dirs(diff.head)) - (diff.base | _dirs(diff.base))
    stale = {
        module
        for module in diff.census
        if module not in diff.rows.rows
        or diff.rows.stamps[module] != diff.blobs[module][:12]
    }
    return stale | {
        module
        for module in set(diff.census) - stale
        if (row := diff.rows.rows[module]).files & changed
        or row.dirs & listed
        or (row.tree and diff.base != diff.head)
        or row.missing & appeared
    }


@given(_diffs())
def test_select_runs_every_module_the_diff_can_affect(diff):
    selection = table.select(diff.rows, diff.changes, diff.census, diff.blobs)

    assert selection.full or _must_select(diff) <= set(selection.modules)
