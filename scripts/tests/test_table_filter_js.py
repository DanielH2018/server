"""Tests for docs/assets/table-filter.js — the filter bar on the long docs tables.

HOW. The script exports its pure half under node: which tables get a bar, which columns get
a dropdown, whether a row matches, and how a filter is written to and read from the query
string. These call that half directly. The DOM half -- building the bar, hiding rows and
sections, clearing the filter for a link into a hidden row -- was driven in a headless
Chromium against a local build when it landed; the repo has no DOM harness, and a stub of
tables, headings and events would test the stub.

Skipped where node is absent, like test_fqdn_links_js.py.

Run: uv run pytest scripts/tests/test_table_filter_js.py
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "docs" / "assets" / "table-filter.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)

_HARNESS = """
const api = require(SCRIPT_PATH);
const result = api[process.argv[1]](...JSON.parse(process.argv[2]));
console.log(JSON.stringify(result === undefined ? null : result));
"""


def _call(function: str, *args):
    """What the script's exported `function` returns for `args`, round-tripped as JSON."""
    harness = _HARNESS.replace("SCRIPT_PATH", json.dumps(str(SCRIPT)))
    result = subprocess.run(
        ["node", "-e", harness, "--", function, json.dumps(args)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"node failed: {result.stderr.strip()[:800]}")
    return json.loads(result.stdout.strip())


HEADERS = ["Script", "Directory", "What it does", "Days left"]
PROSE = (
    "Generate a reference page from the tree, which runs longer than forty characters."
)
ROWS = [[f"s{i}.py", "docs" if i % 3 else "dev", PROSE, str(i % 4)] for i in range(12)]


def _choice_names(headers, rows, section_column=-1):
    return [c["name"] for c in _call("choiceColumns", headers, rows, section_column)]


def test_a_repeating_short_column_is_a_choice():
    choices = _call("choiceColumns", HEADERS, ROWS, -1)
    directory = next(c for c in choices if c["name"] == "Directory")
    assert directory["values"] == [["dev", 4], ["docs", 8]]


def test_identifier_prose_and_number_columns_are_not_choices():
    """Script never repeats. What it does repeats but reads as prose, not a category, and
    Days left repeats but is a quantity, which sorting serves better than a dropdown."""
    assert _choice_names(HEADERS, ROWS) == ["Directory"]


def test_the_section_column_skips_the_thresholds_and_keeps_page_order():
    """The Scripts page's headings are longer than the prose cap and not alphabetical."""
    headers = ["Script", "Directory", "Section"]
    sections = ["Run automatically, on a schedule", "Imported, never run on their own"]
    rows = [[f"s{i}.py", "docs", sections[i // 6]] for i in range(12)]
    choices = _call("choiceColumns", headers, rows, 2)
    assert [c["name"] for c in choices] == ["Section"]
    assert [v[0] for v in choices[0]["values"]] == sections


def _matches(cells, text="", choices=None):
    return _call("rowMatches", HEADERS, cells, {"text": text, "choices": choices or {}})


def test_every_word_of_the_text_must_match_in_any_case():
    cells = ["scripts/docs/build_docs.py", "docs", "Regenerate the reference pages"]
    assert _matches(cells, "REFERENCE build")
    assert not _matches(cells, "reference deploy")


def test_a_choice_must_equal_its_cell_exactly():
    """`dev` must not match `devtools`: a dropdown value is a category, not a search."""
    cells = ["scripts/devtools/x.py", "devtools", "Summary"]
    assert _matches(cells, choices={"Directory": "devtools"})
    assert not _matches(cells, choices={"Directory": "dev"})


def test_the_filter_round_trips_through_the_query_string_keeping_other_params():
    """Material's search adds `?h=`; writing the filter must not drop it."""
    names = ["Section", "Directory"]
    fltr = {"text": "land", "choices": {"Directory": "deploy_tools"}}
    search = _call("writeFilter", "?h=land", names, "", fltr)
    assert "h=land" in search
    assert _call("readFilter", search, names, "") == fltr


def test_a_second_group_reads_only_its_own_prefixed_params():
    search = "?Directory=docs&2.Directory=dev"
    assert _call("readFilter", search, ["Directory"], "2.")["choices"] == {
        "Directory": "dev"
    }


def test_a_small_or_two_column_group_gets_no_bar():
    """hosts.md's `Fact | Value` tables and short fragment tables stay as they are."""
    assert _call("qualifies", 3, 10)
    assert not _call("qualifies", 2, 40)
    assert not _call("qualifies", 5, 9)


def test_sorting_is_numeric_aware():
    """Secrets' Days left column: a plain string sort puts 10 before 9."""
    assert _call("compareText", "9", "10") < 0


def test_a_header_click_cycles_ascending_descending_then_off():
    sort = {"column": -1, "direction": 0}
    seen = []
    for _ in range(3):
        sort = _call("nextSort", sort, 2)
        seen.append((sort["column"], sort["direction"]))
    assert seen == [(2, 1), (2, -1), (-1, 0)]
