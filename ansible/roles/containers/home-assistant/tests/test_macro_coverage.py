"""Guard: every macro defined in custom_templates/*.jinja must be exercised by a render_macro()
call in this tests/ directory. Deterministic (covered: yes/no) — the replacement for a fuzzy
'is this logic too complex' judgment. Matches the macro name as the 2nd positional arg to
render_macro(FILE, "<name>", ...), NOT a bare substring (a comment/docstring can't satisfy it)."""

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
MACRO_DIR = TESTS_DIR.parent / "files" / "custom_templates"

_MACRO_DEF = re.compile(r"{%-?\s*macro\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")
_RENDER_CALL = re.compile(
    r"""render_macro\(\s*[^,]+,\s*["']([a-zA-Z_][a-zA-Z0-9_]*)["']"""
)


def _defined_macros() -> set[str]:
    names: set[str] = set()
    for jinja in MACRO_DIR.glob("*.jinja"):
        names |= set(_MACRO_DEF.findall(jinja.read_text()))
    return names


def _tested_macros() -> set[str]:
    invoked: set[str] = set()
    for test in TESTS_DIR.glob("test_*.py"):
        invoked |= set(_RENDER_CALL.findall(test.read_text()))
    return invoked


def test_every_macro_has_a_test():
    untested = sorted(_defined_macros() - _tested_macros())
    assert not untested, (
        "macros defined in custom_templates/*.jinja but never invoked via render_macro() in a "
        f"test: {untested} — add a truth-table test (see test_lighting_macros.py)"
    )


def test_guard_detects_defined_and_tested_macros():
    # sanity: the guard actually sees the real corpus (not silently matching nothing)
    defined = _defined_macros()
    assert {"light_decision", "natural_exception", "fan_target_level"} <= defined
    assert {"light_decision", "natural_exception"} <= _tested_macros()
