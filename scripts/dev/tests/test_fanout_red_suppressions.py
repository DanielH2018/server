"""The suppression comments a red test may not add (#4439).

Pure source-to-source checks, so no case here starts git or pytest; the gate-level case is in
`test_fanout_red_gate.py`.

Run: uv run pytest scripts/dev/tests/test_fanout_red_suppressions.py
"""

from fanout_lib.review.red_tests import stale_suppressions

BASE = "def test_old():\n    assert True\n"
# The shape of #4403's red file: an in-body import of the module the fix creates.
ADDED = (
    "\n\ndef test_new():\n"
    "    from function_imports import scan  # ty: ignore[unresolved-import]\n\n"
    "    assert scan() == []\n"
)


def test_an_added_ignore_on_an_import_of_a_missing_module_is_named():
    assert stale_suppressions(BASE, BASE + ADDED) == [
        "line 6: `# ty: ignore[unresolved-import]`"
    ]


def test_a_blanket_ignore_in_either_spelling_is_named():
    red = BASE + "x = 1  # type: ignore\ny = 2  # ty:ignore\n"
    assert stale_suppressions(BASE, red) == [
        "line 3: `# type: ignore`",
        "line 4: `# ty:ignore`",
    ]


def test_an_ignore_the_base_already_had_is_not_named():
    base = BASE + ADDED
    assert stale_suppressions(base, base + ADDED.replace("test_new", "test_newer")) == [
        "line 12: `# ty: ignore[unresolved-import]`"
    ]
    assert stale_suppressions(base, "# extended\n" + base) == []


def test_an_ignore_of_a_rule_the_fix_does_not_resolve_or_inside_a_string_is_not_named():
    red = (
        BASE
        + "def test_raises():\n    f()  # ty: ignore[missing-argument]\n"
        + "TEXT = '# ty: ignore[unresolved-import]'\n"
    )
    assert stale_suppressions(BASE, red) == []
