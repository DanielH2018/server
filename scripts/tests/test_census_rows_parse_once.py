"""The test-module census rows parse each census module once per process, not once per row.

Every detector row in `test_census_rows_test_modules.py` and `test_census_rows_test_renders.py`
reads the same census, `pytest_only_modules()`. Parsing dominates the rows' cost: `ast.parse`
was 5.15s of the 9.23s one row took over 1620 modules (#6437). A row that reparses what another
row already parsed pays that again, so the tree is shared across the rows of both files.

The rows run in a fresh interpreter. In this one, an earlier test on the same xdist worker may
already have parsed the sample, and a count of zero would then hide whether the instrument saw
anything. The count is taken at `compile` with `PyCF_ONLY_AST`, which every `ast.parse` reaches
however the caller imported it.

Run: uv run pytest scripts/tests/test_census_rows_parse_once.py
"""

import json
import os
import sys

from _test_module_rules import pytest_only_modules
from lib.proc_testing import run
from lib.repo_paths import REPO

# Enough modules that every row's selector keeps some, few enough to stay a unit test.
SAMPLE = 60

# Runs every row of both census files over the sample, each row through its own selector, and
# prints how many times each sampled module's text was compiled to an AST.
_COUNT_PARSES = """
import ast, builtins, json, sys
from collections import Counter

import test_census_rows_test_modules, test_census_rows_test_renders
from _row_table import Subject, check
from lib.repo_paths import REPO

sample = set(json.loads(sys.argv[1]))
texts = {(REPO / rel).read_text(errors="replace"): rel for rel in sample}
parses = Counter()
real_compile = builtins.compile

def counting_compile(source, *args, **kwargs):
    flags = args[2] if len(args) > 2 else kwargs.get("flags", 0)
    if flags & ast.PyCF_ONLY_AST:
        text = source.decode(errors="replace") if isinstance(source, bytes) else source
        if isinstance(text, str) and text in texts:
            parses[texts[text]] += 1
    return real_compile(source, *args, **kwargs)

builtins.compile = counting_compile
for row in test_census_rows_test_modules.ROWS + test_census_rows_test_renders.ROWS:
    check(row, [
        item for item in row.files()
        if (item.rel if isinstance(item, Subject) else item) in sample
    ])
print(json.dumps(parses))
"""


def test_every_census_row_of_both_files_shares_one_parse_per_module():
    sample = pytest_only_modules()[:SAMPLE]
    tests_dir = REPO / "scripts" / "tests"
    result = run(
        [sys.executable, "-c", _COUNT_PARSES, json.dumps(sample)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(tests_dir), *sys.path])},
        check=True,
    )
    parses = json.loads(result.stdout.splitlines()[-1])

    assert parses == dict.fromkeys(sample, 1), (
        f"census modules parsed other than once across the rows of both files: "
        f"{dict(sorted((rel, parses.get(rel, 0)) for rel in sample if parses.get(rel) != 1)[:5])}"
    )
