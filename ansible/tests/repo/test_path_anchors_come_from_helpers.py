"""Guard: only `_helpers.py` derives a repo root from `__file__`.

Every guard in this directory reads the repo's own sources, so 50 of them each re-derived the
same roots with a hardcoded `Path(__file__).resolve().parents[N]`. The duplication is the small
half. The index is position-dependent, and it fails silently: from one directory deeper,
`parents[2]` resolves to `ansible/` — a real directory, not an error. A guard that globs and
asserts inside the loop then passes on an empty glob, reporting green while checking nothing.
That is the same silent-coverage-loss shape as monitor-bridge's monkeypatch rule, so it gets a
check rather than a paragraph.

`_helpers.py` is the one place the anchor is allowed, and it publishes REPO, ANSIBLE, ROLES,
K8S_ROLES, SETUP_ROLES and CONTAINER_ROLES for everyone else to import.

Clean/flagged pairs below, per the repo rule that a new check ships with a proof it can go RED.

Run: uv run pytest ansible/tests/test_path_anchors_come_from_helpers.py
"""

from __future__ import annotations

import re

from _helpers import ANSIBLE

TESTS = ANSIBLE / "tests"

# The owner of the anchor. Nothing else in this directory may re-derive it — except this
# module, whose red-proof cases below hold the offending spelling as fixture text.
ANCHOR_OWNER = "_helpers.py"
EXEMPT = {ANCHOR_OWNER, "test_path_anchors_come_from_helpers.py"}

# `Path(__file__).resolve().parents[N]` in any of the spellings this directory has used:
# `Path`, `_Path`, `pathlib.Path`. `parents[0]` is the file's own directory, which is a local
# fact rather than a repo root, so the pattern deliberately starts at 1. The chained form
# `.parent.parent` is the same anchor spelled differently: four modules carried it past the
# `parents[N]` rule, and every one of them resolved to `ansible/` instead of the repo when the
# 2026-09-01 move into subdirectories put them one level deeper.
_ANCHOR = re.compile(
    r"(?:_?Path|pathlib\.Path)\(__file__\)\.resolve\(\)(?:\.parents\[[1-9]\d*\]|(?:\.parent){2,})"
)


def _offenders(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if _ANCHOR.search(line)]


def test_no_module_but_helpers_re_derives_a_root():
    flagged = {
        path.name: _offenders(path.read_text())
        for path in sorted(TESTS.rglob("*.py"))
        if path.name not in EXEMPT
    }
    flagged = {name: lines for name, lines in flagged.items() if lines}
    assert not flagged, (
        "these modules re-derive a repo root from __file__ instead of importing it from "
        f"_helpers, which breaks silently if the file ever moves: {flagged}"
    )


def test_helpers_still_owns_an_anchor():
    """The exemption is only sound while `_helpers` actually defines the roots."""
    assert _offenders((TESTS / ANCHOR_OWNER).read_text())


def test_a_module_importing_from_helpers_is_clean():
    assert _offenders("from _helpers import REPO\nROLE = REPO / 'ansible'\n") == []


def test_a_module_re_deriving_the_root_is_flagged():
    assert _offenders("ROLE = Path(__file__).resolve().parents[2] / 'ansible'\n")


def test_the_pathlib_qualified_spelling_is_flagged():
    """Three spellings were in use; a pattern that misses one lets the class back in."""
    assert _offenders("_REPO = pathlib.Path(__file__).resolve().parents[2]\n")
    assert _offenders("_REPO = _Path(__file__).resolve().parents[2]\n")


def test_the_chained_parent_spelling_is_flagged():
    """`.parent.parent.parent` is the spelling the `parents[N]` rule let through."""
    assert _offenders("REPO = Path(__file__).resolve().parent.parent.parent\n")
    assert _offenders("REPO = Path(__file__).resolve().parent.parent\n")


def test_the_files_own_directory_is_not_flagged():
    """`parents[0]` is a local fact, not a repo root — flagging it would be noise."""
    assert _offenders("HERE = Path(__file__).resolve().parents[0]\n") == []
    assert _offenders("HERE = Path(__file__).resolve().parent\n") == []


def test_the_scan_reaches_the_subdirectories():
    """The guards live one level down since 2026-09-01; a flat glob would scan nothing."""
    scanned = [p for p in TESTS.rglob("test_*.py") if p.parent != TESTS]
    assert len(scanned) > 100, len(scanned)
