"""Two shapes the green gate's lock refused although the fix was correct (#5400).

A constant built from another constant (#5229), and a helper in the red file that the issue
names as the code under test (#5197). Both were landed by hand. Source-to-source like
`test_fanout_red_lock.py`, so no case here starts git or pytest.

Run: uv run pytest scripts/dev/tests/test_fanout_red_lock_shapes.py
"""

import importlib

from fanout_lib.review.red_lock import forbidden_change

# #5229: the red commits added `RENDERER_ONLY` and a test that reads it, and the fix appended
# a second constant built from it for its own test.
RENDERER_BASE = '''"""The renderer guard."""


def judge(paths):
    return [p for p in paths if not p.endswith(".py")]
'''
RENDERER_RED = RENDERER_BASE + (
    '\n\nRENDERER_ONLY = ["cluster.py"]\n\n\n'
    "def test_renderer_only():\n    assert judge(RENDERER_ONLY) == []\n"
)
RENDERER_NODES = {"test_renderer_only"}


def _with_derived(value):
    return RENDERER_RED + (
        f"\n\nRENDERER_WITH_DOCS = {value}\n\n\n"
        "def test_with_docs():\n    assert judge(RENDERER_WITH_DOCS) == ['docs.md']\n"
    )


def _renderer_change(head):
    return forbidden_change(RENDERER_BASE, RENDERER_RED, head, RENDERER_NODES)


def test_an_appended_constant_built_from_another_constant_is_clean():
    """Reading a constant runs no code, so a value built from one is still data."""
    assert _renderer_change(_with_derived('RENDERER_ONLY + ["docs.md"]')) == ""
    # A value that calls a function still runs code at import, around the red test.
    assert _renderer_change(_with_derived('RENDERER_ONLY + sorted(["docs.md"])')) != ""
    # Rebinding the constant the red test reads changes what it checks.
    rebound = RENDERER_RED + '\n\nRENDERER_ONLY = RENDERER_ONLY + ["docs.md"]\n'
    assert _renderer_change(rebound) != ""


# #5197's shape: `_twice` was in the file before the red commits, and the red test calls it.
BASE = '''"""Tests for mod."""

from mod import double


def _once(x):
    return double(x)


def _twice(x):
    return _once(_once(x))


def test_zero():
    assert double(0) == 0
'''
RED = BASE + "\n\ndef test_two():\n    assert _twice(1) == 4\n"


def test_a_helper_the_issue_names_as_its_subject_is_the_fixs_to_change():
    """The fix for #5197 restricted `_restarts_privately`, the helper the issue was about.

    The lock refused it as a change to what the red test stands on. The issue names the
    helper, so `forbidden_change(..., subjects=...)` frees it. The function is reached through
    `getattr` because the `subjects` parameter does not exist yet.
    """
    name = "forbidden_change"
    check = getattr(importlib.import_module("fanout_lib.review.red_lock"), name)
    restricted = RED.replace("return _once(_once(x))", "return _once(_once(x)) * 1")
    assert check(BASE, RED, restricted, {"test_two"}, subjects={"_twice"}) == ""
    # Unnamed, the same helper stays locked: #4183's weakened helper was not the subject.
    assert check(BASE, RED, restricted, {"test_two"}, subjects=set()) != ""
    # Naming the helper frees it, never the red test that calls it.
    weakened = RED.replace("assert _twice(1) == 4", "assert True")
    assert check(BASE, RED, weakened, {"test_two"}, subjects={"_twice"}) != ""


def test_a_named_helper_the_red_commits_wrote_stays_locked():
    """The issue names the code under test, never what the red author wrote to check it."""
    red = RED + "\n\ndef _thrice(x):\n    return _once(_twice(x))\n"
    red += "\n\ndef test_three():\n    assert _thrice(1) == 8\n"
    edited = red.replace("return _once(_twice(x))", "return 8")
    assert forbidden_change(
        BASE, red, edited, {"test_three"}, subjects={"_thrice"}
    ) == ("changes or removes `def _thrice`")


import pytest  # noqa: E402


@pytest.mark.parametrize(
    ("appended", "name"),
    [
        ("from os import sep\n\nNEW = sep + []", "sep"),
        ("NEW = [UNBOUND]", "UNBOUND"),
        ("A = [1]\nfrom os import sep as A\nNEW = A + []", "A"),
    ],
    ids=["an-import", "unbound", "a-constant-an-import-rebinds"],
)
def test_an_appended_constant_reading_a_name_no_constant_binds_is_flagged(
    appended, name
):
    """An imported object's `__add__` would run the fix's code at import (#5229)."""
    problem = _renderer_change(f"{RENDERER_RED}\n\n{appended}\n")
    assert problem.endswith(f"reads `{name}`, which no constant above it binds")


from fanout_lib.review.red_lock import forbidden_conftest_change  # noqa: E402

# An older helper the red test calls reads `SEPS`, whose value reads an import.
SEP_BASE = (
    BASE + "\n\nfrom os import sep\n\nSEPS = [sep]\n\n\ndef _seps():\n    return SEPS\n"
)
SEP_RED = SEP_BASE + "\n\ndef test_seps():\n    assert _seps()\n"


def test_an_older_constant_reading_an_import_stays_locked():
    """Its shape is a constant's, but reading `sep` is not data, so a fix may not rewrite it."""
    edited = SEP_RED.replace("SEPS = [sep]", "SEPS = []")
    problem = forbidden_change(SEP_BASE, SEP_RED, edited, {"test_seps"})
    assert problem.startswith("changes or removes `assign at line")


def test_deleting_a_conftest_constant_that_reads_an_import_is_flagged():
    conftest = "from os import sep\n\nSEPS = [sep]\n"
    problem = forbidden_conftest_change(conftest, "from os import sep\n", [RED], [])
    assert problem == (
        "deletes `assign at line 3`, which reads `sep`, which no constant above it binds"
    )


# #5520: the fix appended a table of lambdas that only its own appended test reads.
LAMBDA_TABLE = '_TABLE = {"a": (1, lambda x: x), "b": ([2], lambda x: judge(x))}'


def _with_table(statement):
    return (
        f"{RENDERER_RED}\n\n{statement}\n\n\n"
        "def test_table():\n    assert all(f(v) for v, f in _TABLE.values())\n"
    )


def test_an_appended_lambda_table_no_red_test_reads_is_clean():
    assert _renderer_change(_with_table(LAMBDA_TABLE)) == ""


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ('RENDERER_ONLY = {"a": lambda x: x}', "rebinds `RENDERER_ONLY`"),
        ('judge = {"a": lambda x: x}', "rebinds `judge`"),
        ('_TABLE = {"a": lambda x=judge([]): x}', "is not allowed"),
        ('_TABLE = {"a": judge([])}', "is not allowed"),
        ('_TABLE = {**RENDERER_ONLY, "a": lambda x: x}', "is not allowed"),
        ('_TABLE = {"a": lambda x: x}["a"]', "is not allowed"),
    ],
    ids=[
        "a-name-a-red-test-reads",
        "a-locked-helper",
        "default",
        "call",
        "star",
        "subscript",
    ],
)
def test_an_appended_table_that_rebinds_or_runs_code_is_flagged(statement, reason):
    assert reason in _renderer_change(_with_table(statement))
