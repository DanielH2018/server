"""Every detector `docs/failure-classes.md` cites must exist, and every row must cite one.

That page maps the eight recurring failure classes to an executable detector, or says why one
class-general detector cannot exist. A citation naming a test that has moved or was never
written is worse than no citation — it reads as coverage that isn't there, which is class 8
("prose asserts facts nothing re-derives") happening inside the very page that catalogues class
8. This guard is that page's own red-proof pair.

Row-level non-vacuity matters as much as citation resolution. A page where every row's Detector
cell reads "human judgment, see prose" would pass a naive `len(rows) >= 8` check while citing
nothing — which is class 3 ("empty is read as clean") happening in the same page. So this
asserts on the citation COUNT, not the row count: at least 8 distinct cited node ids must
resolve, one per class at minimum.

Run: uv run pytest ansible/tests/repo/test_failure_class_detectors.py
"""

import re
import subprocess

from _helpers import REPO

PAGE = REPO / "docs" / "failure-classes.md"

# One markdown table row: starts with `| <digit>` and is not the header/separator line.
ROW = re.compile(r"^\|\s*\d+\s*\|.*\|\s*$", re.M)

# A citation inside a row's Detector cell, e.g. `path/to/test_x.py::test_the_thing`.
CITATION = re.compile(r"([\w.][\w./-]*\.py)::(test_\w+)")


def _tracked_files() -> set[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {rel for rel in listed.split("\0") if rel}


TRACKED = _tracked_files()


def rows(text: str) -> list[str]:
    return ROW.findall(text)


def citations(row: str) -> list[tuple[str, str]]:
    return CITATION.findall(row)


def citation_resolves(path: str, name: str) -> bool:
    if path not in TRACKED:
        return False
    text = (REPO / path).read_text(errors="replace")
    return (
        re.search(rf"^\s*(?:async\s+)?def {re.escape(name)}\(", text, re.M) is not None
    )


def test_the_page_exists():
    """Without this, every assertion below passes vacuously on a missing file."""
    assert PAGE.is_file()


def test_the_scan_finds_class_rows():
    """Without this, the parametrized checks below pass vacuously on an empty table."""
    assert len(rows(PAGE.read_text())) >= 8


def test_every_row_cites_at_least_one_detector():
    missing = [
        r.split("|")[2].strip() for r in rows(PAGE.read_text()) if not citations(r)
    ]
    assert not missing, (
        f"these classes have no `path.py::test_name` citation in their Detector cell: "
        f"{missing}. A row with no citation reads as coverage while checking nothing — say "
        f"NONE and name the reason in the human column instead of leaving the cell empty."
    )


def test_at_least_eight_distinct_citations_resolve():
    """The count that matters: distinct (path, test) pairs across the whole table."""
    all_citations = {c for r in rows(PAGE.read_text()) for c in citations(r)}
    resolved = {c for c in all_citations if citation_resolves(*c)}
    assert len(resolved) >= 8, (
        f"only {len(resolved)} of {len(all_citations)} cited detectors resolve to a real test "
        f"in the tree; expected at least 8 (one per class)."
    )


def test_every_cited_detector_resolves():
    text = PAGE.read_text()
    broken = [c for r in rows(text) for c in citations(r) if not citation_resolves(*c)]
    assert not broken, (
        f"docs/failure-classes.md cites a detector that does not exist: {broken}. Either the "
        f"test was renamed/removed, or the citation was never real — fix the page or the test."
    )


def test_the_predicate_rejects_a_missing_test_and_accepts_a_real_one():
    """Red-proof pair for `citation_resolves` itself."""
    assert not citation_resolves(
        "ansible/tests/repo/test_documented_paths_exist.py", "test_does_not_exist"
    )
    assert citation_resolves(
        "ansible/tests/repo/test_documented_paths_exist.py",
        "test_every_line_numbered_path_cited_in_the_docs_exists",
    )


def test_the_row_scan_rejects_a_row_with_no_citation():
    """Red-proof pair for the extraction regexes themselves."""
    fixture = (
        "| 1 | Some class | some incident | human judgment only, no detector | NONE | all human |\n"
        "| 2 | Another class | incident | `ansible/tests/repo/test_x.py::test_y` | FULL | none |\n"
    )
    found_rows = rows(fixture)
    assert len(found_rows) == 2
    assert citations(found_rows[0]) == []
    assert citations(found_rows[1]) == [("ansible/tests/repo/test_x.py", "test_y")]
