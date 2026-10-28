"""The test-module census rows parse each census module once per process, not once per row.

Every detector row in `test_census_rows_test_modules.py` and `test_census_rows_test_renders.py`
reads the same census, `pytest_only_modules()`. Parsing dominates the rows' cost: `ast.parse`
was 5.15s of the 9.23s one row took over 1620 modules (#6437). A row that reparses what another
row already parsed pays that again, so the tree is shared across the rows of both files.

The count is taken at `compile` with `PyCF_ONLY_AST`, which every `ast.parse` reaches however
the caller imported it. A tree parsed by an earlier test in the same process counts zero here,
which is the cache working, so the bound is "at most once" rather than "exactly once".

Run: uv run pytest scripts/tests/test_census_rows_parse_once.py
"""

import ast
import builtins
from collections import Counter

import test_census_rows_test_modules
import test_census_rows_test_renders
from _row_table import check
from _test_module_rules import pytest_only_modules
from lib.repo_paths import REPO

# Enough modules that every row's selector keeps some, few enough to stay a unit test.
SAMPLE = 60


def test_every_census_row_of_both_files_shares_one_parse_per_module(monkeypatch):
    sample = pytest_only_modules()[:SAMPLE]
    texts = {(REPO / rel).read_text(errors="replace"): rel for rel in sample}
    parses: Counter[str] = Counter()
    real_compile = builtins.compile

    def counting_compile(source, *args, **kwargs):
        flags = args[2] if len(args) > 2 else kwargs.get("flags", 0)
        if flags & ast.PyCF_ONLY_AST:
            text = source.decode() if isinstance(source, bytes) else source
            if isinstance(text, str) and text in texts:
                parses[texts[text]] += 1
        return real_compile(source, *args, **kwargs)

    monkeypatch.setattr(builtins, "compile", counting_compile)
    rows = test_census_rows_test_modules.ROWS + test_census_rows_test_renders.ROWS
    for row in rows:
        check(row, sample)

    reparsed = {rel: n for rel, n in parses.items() if n > 1}
    assert not reparsed, (
        f"{len(reparsed)} of {len(sample)} census modules were parsed more than once "
        f"across {len(rows)} rows: {dict(sorted(reparsed.items())[:5])}"
    )
