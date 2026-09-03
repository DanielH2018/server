"""A guard that finds its own subject by glob/rglob must not be vacuous.

CLAUDE.md's own rule: "A check that finds its subject by pattern ships with a named member it
must find." A census that globs for the files it checks returns an EMPTY set the moment those
files are renamed or move a directory down, and an `all(...)` over nothing still passes — a
second way to be green while checking nothing, on top of the missing-rejecting-half failure
mode `test_every_validator_has_a_red_proof.py` covers. Nine guards broke this way across six
consecutive PRs (#838, #846, #852, two in #858, four in the monitor-bridge package move); every
one was caught solely by a non-vacuity assertion, and the guards that lacked one had to be
found by running the entry point instead.

This is the mechanical half of recurring-failure class 4, "a guard's selector drifts from the
hazard's extent" (docs/failure-classes.md). It cannot see whether a census covers the right
SET, only whether it is defended against covering an empty one — the rest of that class stays
a human judgment call.

SCOPE IS DELIBERATELY NARROW: `ansible/tests/repo/`, `scripts/tests/`, and `scripts/*/tests/`.
Measured before writing, per the same class's own cautionary tale: a proposed repo-wide
"every validator needs a negative test" check was measured first and would have dropped the
two best-paired validators in the repo, because a fixed name vocabulary missed how they
actually wrote their rejecting half. The same trap applies here — a crude "assert nearby"
regex over `ansible/tests/{k8s,deploy,longhorn,setup,services,staging}` flags roughly half of
the ~68 glob-using files there, and most of those are `tmp_path` fixtures or lists already
asserted elsewhere, not real gaps; sorting the two apart needs a human reading each file. The
scope above was checked file-by-file and holds a real invariant: every glob-using file in it
already carries a truthy/count/named-member assertion.

Run: uv run pytest ansible/tests/repo/test_glob_census_non_vacuity.py
"""

import re
from pathlib import Path

import pytest

from _helpers import REPO

TARGET_DIRS = [
    REPO / "ansible" / "tests" / "repo",
    REPO / "scripts" / "tests",
    *sorted((REPO / "scripts").glob("*/tests")),
]

GLOB_CALL = re.compile(r"\.r?glob\(")

# What counts as "defended": a size floor, a frozenset/named-member set, or a truthy assert
# (with or without a trailing message) on the census result or something derived from it.
NON_VACUITY = re.compile(
    r">=\s*\d"
    r"|assert\s+len\([^)]*\)\s*(==|>|>=)\s*\d"
    r"|frozenset\("
    r"|KNOWN_[A-Z_]+|EXPECTED_[A-Z_]+"
    r"|assert\s+[a-zA-Z_][a-zA-Z0-9_.\[\]]*\s*(,|$)"
    r"|assert\s+not\s+"
)


def _test_files() -> list[Path]:
    files: list[Path] = []
    for d in TARGET_DIRS:
        if d.is_dir():
            files.extend(sorted(d.glob("test_*.py")))
    return files


def _globbers() -> list[Path]:
    return [f for f in _test_files() if GLOB_CALL.search(f.read_text(errors="replace"))]


def test_the_scan_finds_glob_using_test_files():
    """Without this, the parametrized test below passes vacuously on an empty list."""
    assert len(_globbers()) >= 10


@pytest.mark.parametrize("path", _globbers(), ids=lambda p: str(p.relative_to(REPO)))
def test_a_glob_based_census_carries_a_non_vacuity_assertion(path: Path):
    text = path.read_text(errors="replace")
    assert NON_VACUITY.search(text), (
        f"{path.relative_to(REPO)} calls .glob()/.rglob() to find its own check's subject, "
        f"with no nearby count, frozenset, KNOWN_*/EXPECTED_* member set, or truthy assertion. "
        f"A census that returns empty the moment its target renames or moves would still pass "
        f"here. Add `assert len(found) >= N` or a named-member assertion — "
        f"scripts/diagnostics/tests/test_probe_boundaries.py's KNOWN_CONSUMERS is the worked "
        f"example."
    )


def test_the_predicate_rejects_a_glob_with_no_assertion_and_accepts_one_with_it():
    """Red-proof pair for NON_VACUITY itself.

    Without this, a NON_VACUITY pattern that had silently stopped matching would leave every
    parametrized test above passing on the empty search it now performs.
    """
    vacuous = "found = sorted(ROLES.glob('*.py'))\nfor f in found:\n    check(f)\n"
    assert not NON_VACUITY.search(vacuous)

    guarded = vacuous + "assert len(found) >= 5\n"
    assert NON_VACUITY.search(guarded)
