"""Keep pytest from building a node for every sibling of each test file a run names.

Registered as a plugin from `addopts` in pyproject.toml, beside `leakguard` and `xdist_width`.

## Why

Given a file argument, pytest collects the file's directory and then keeps the child that
matches. A directory named once is cached, but the last step is not: for backward
compatibility a file given on the command line is never deduplicated, so pytest re-collects
its directory once per file argument it holds. Each pass calls `pytest_ignore_collect` and
`pytest_collect_file` on every entry, and builds a `Module` node for every test file it finds.

A CI pytest leg names about 237 files, and `tests/repo` alone holds hundreds. Shard 3 of 6 made
20,542 `pytest_collect_file` calls to keep 237 modules, about 8.5s of its 25.8s collection
under cProfile, and every xdist worker paid it (#8681).

## The rule

A path is skipped when it is not a named path, does not lead to one, and is not inside a
directory the run named. A run with no arguments collects its `testpaths` directories, and a
directory argument collects everything below it, so both see every file as before. Only the
siblings of a named file, which pytest would build and then discard, are skipped. This hook
runs first, so a skipped sibling costs a set lookup and a cached ancestor check rather than a
conftest lookup and a node.

Run: uv run pytest tests/repo/test_named_files.py
"""

import functools
from pathlib import Path

import pytest

_session: pytest.Session | None = None


def pytest_sessionstart(session: pytest.Session) -> None:
    global _session
    _session = session
    _inside_a_named_directory.cache_clear()


@functools.cache
def _inside_a_named_directory(directory: Path) -> bool:
    assert _session is not None
    return _session.isinitpath(directory) or (
        directory.parent != directory and _inside_a_named_directory(directory.parent)
    )


@pytest.hookimpl(tryfirst=True)
def pytest_ignore_collect(collection_path: Path) -> bool | None:
    """Skip a path no named path leads to and no named directory holds, before any lookup.

    A directory a named file sits under passes `isinitpath(with_parents=True)`, so only a
    sibling that cannot hold anything the run named is skipped.
    """
    if _session is None:
        return None
    if _session.isinitpath(collection_path, with_parents=True):
        return None
    if _inside_a_named_directory(collection_path.parent):
        return None
    return True
