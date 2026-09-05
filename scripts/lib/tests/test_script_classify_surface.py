"""Guard: `lib.script_classify.__all__` names only what the rest of `scripts/` uses.

A split leaves stale exports behind. PR #1142 moved the `is_candidate` call down into the
census helpers; the name stayed in `__all__` and in the module docstring until issue #1204
noticed. An exported name nobody imports reads as public API and outlives the reason it was
public, so the census is checked rather than remembered.

Run: uv run pytest scripts/lib/tests/test_script_classify_surface.py
"""

import re
from pathlib import Path

from lib import script_classify as sc
from lib.repo_paths import SCRIPTS

MODULE = Path(sc.__file__).resolve()
# This file names the exports it checks, so counting itself would make every name look used.
SKIP = {MODULE, Path(__file__).resolve()}


def _scanned() -> list[Path]:
    """Every .py under scripts/ except the module itself — where a consumer would be."""
    return [
        p
        for p in SCRIPTS.rglob("*.py")
        if "__pycache__" not in p.parts and p.resolve() not in SKIP
    ]


def _unreferenced(names) -> list[str]:
    """Which of `names` no other file under scripts/ mentions."""
    files = _scanned()
    assert len(files) > 20, (
        f"the scan found only {len(files)} files — it is checking nothing"
    )
    text = "\n".join(p.read_text() for p in files)
    return sorted(n for n in names if not re.search(rf"\b{re.escape(n)}\b", text))


def test_every_exported_name_has_a_consumer_under_scripts():
    assert _unreferenced(sc.__all__) == []


def test_the_guard_reports_a_name_nothing_outside_the_module_uses():
    # `is_candidate` is the predicate behind `candidates`; only script_classify.py calls it.
    assert _unreferenced(["is_candidate"]) == ["is_candidate"]


def test_the_exported_surface_is_not_empty():
    assert {"classify", "candidates", "importers"} <= set(sc.__all__)
