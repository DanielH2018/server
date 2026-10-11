"""The lock the green gate puts on a red test file (#4214, #4517).

Pure source-to-source checks, so no case here starts git or pytest; the gate-level cases are
in `test_fanout_red_gate.py`.

Run: uv run pytest scripts/dev/tests/test_fanout_red_lock.py
"""

import pytest

from fanout_lib.review.red_tests import forbidden_change

# The file before the red commits.
BASE = '''"""Tests for mod."""

import pytest

from mod import double
from mod import scale

LIMIT = 10


def _once(x):
    return double(x)


def _twice(x):
    return _once(_once(x))


def test_zero():
    assert double(0) == 0


def test_scale(fake_factor):
    assert scale(2, fake_factor) == 6
'''

# The red commits append one test, which reaches `_twice`, `_once` and `double`.
RED = (
    BASE + "\n\ndef test_two(tmp_path):\n    from mod import halve\n\n"
    "    assert _twice(1) == 4 and halve(4) == 2\n"
)

CLEAN = {
    "a new test function and the import it needs": (
        "\n\nfrom mod import triple\n\n\ndef test_triple():\n    assert triple(1) == 3\n"
    ),
    "a parametrized test with a new helper and a fixture": (
        "\n\ndef _many():\n    return [1, 2]\n\n\n@pytest.fixture\ndef numbers():\n"
        "    return _many()\n\n\n@pytest.mark.parametrize('n', [1, 2])\n"
        "def test_many(n, numbers):\n    assert n in numbers\n"
    ),
    "a red test's own import, repeated at module level": "\n\nfrom mod import halve\n",
}

FLAGGED = {
    "a second test with a red test's name": (
        "\n\ndef test_two():\n    pass\n",
        "rebinds `test_two`",
    ),
    "a new import that shadows the code under test": (
        "\n\nfrom fake import double\n",
        "rebinds `double`",
    ),
    "a new fixture that overrides one a red test requests": (
        "\n\n@pytest.fixture\ndef tmp_path():\n    return None\n",
        "rebinds `tmp_path`",
    ),
    "an autouse fixture": (
        "\n\n@pytest.fixture(autouse=True)\ndef _patch(monkeypatch):\n    pass\n",
        "autouse",
    ),
    "a fixture renamed onto a name a red test reads": (
        "\n\n@pytest.fixture(name='tmp_path')\ndef _other():\n    return None\n",
        "`name=`",
    ),
    "an xunit hook that runs around every test": (
        "\n\ndef setup_function(function):\n    pass\n",
        "runs around the tests",
    ),
    "a collection hook": (
        "\n\ndef pytest_collection_modifyitems(items):\n    items.clear()\n",
        "runs around the tests",
    ),
    "a module-level statement": (
        "\n\npytestmark = pytest.mark.skip\n",
        "only test functions, fixtures, helpers and imports",
    ),
    "a decorator that is not a pytest mark or fixture": (
        "\n\n@some_wrapper\ndef test_wrapped():\n    pass\n",
        "decorator",
    ),
}


def _without_test_scale(src):
    """`src` with `test_scale` deleted, and the import only it used."""
    src = src[: src.index("def test_scale")] + src[src.index("def test_two") :]
    return src.replace("from mod import scale\n", "")


# Edits to the part of the file that was there before the red commits and that no red test
# reaches, each one a fix forced in #3675's judged records.
UNREACHED = {
    "callers that follow a parameter the fix dropped (#4338)": RED.replace(
        "def test_scale(fake_factor):\n    assert scale(2, fake_factor) == 6\n",
        "def test_scale():\n    assert scale(2) == 6\n",
    ),
    "a test the red tests contradict, updated to the new contract (#4470)": RED.replace(
        "assert scale(2, fake_factor) == 6", "assert scale(2, fake_factor) == 7"
    ),
    "a deleted test and the import only it used": _without_test_scale(RED),
}


@pytest.mark.parametrize("appended", CLEAN.values(), ids=CLEAN.keys())
def test_an_appended_test_is_clean(appended):
    assert forbidden_change(BASE, RED, RED + appended) == ""


def test_a_changed_module_docstring_is_clean():
    edited = RED.replace("Tests for mod.", "Tests for mod's double.")
    assert forbidden_change(BASE, RED, edited) == ""


@pytest.mark.parametrize(("appended", "reason"), FLAGGED.values(), ids=FLAGGED.keys())
def test_an_appended_statement_that_reaches_a_red_test_is_flagged(appended, reason):
    assert reason in forbidden_change(BASE, RED, RED + appended)


@pytest.mark.parametrize("edited", UNREACHED.values(), ids=UNREACHED.keys())
def test_an_edit_no_red_test_reaches_is_clean(edited):
    assert edited != RED
    assert forbidden_change(BASE, RED, edited) == ""


def test_a_weakened_red_assertion_is_flagged():
    weakened = RED.replace("assert _twice(1) == 4 and", "assert")
    assert forbidden_change(BASE, RED, weakened) == "changes or removes `def test_two`"


@pytest.mark.parametrize("helper", ["_twice", "_once"])
def test_an_edited_helper_a_red_test_reaches_is_flagged(helper):
    """#4183: the fix edited a pre-existing function the red node calls, here or one call down."""
    body = {"_twice": "_once(_once(x))", "_once": "double(x)"}[helper]
    edited = RED.replace(f"    return {body}\n", "    return 4\n")
    assert forbidden_change(BASE, RED, edited) == f"changes or removes `def {helper}`"


def test_an_assignment_already_in_the_file_stays_locked_though_unreached():
    """A module-level statement runs at import, around every test; #4372 changed one."""
    edited = RED.replace("LIMIT = 10", "LIMIT = 11")
    assert forbidden_change(BASE, RED, edited).startswith("changes or removes `assign")


def test_a_red_node_collected_from_an_unchanged_function_stays_locked():
    """A red data file can add `test_zero[case]` without touching the function."""
    edited = RED.replace("double(0) == 0", "True")
    assert forbidden_change(BASE, RED, edited) == ""
    assert forbidden_change(BASE, RED, edited, {"test_zero"}) == (
        "changes or removes `def test_zero`"
    )


def test_a_repeated_import_over_a_locked_module_level_name_is_flagged():
    red = RED.replace("LIMIT = 10", "halve = None")
    hoisted = red + "\n\nfrom mod import halve\n"
    assert forbidden_change(BASE, red, hoisted).endswith("rebinds `halve`")


def test_with_no_base_every_statement_is_locked():
    edited = RED.replace("double(0) == 0", "True")
    assert forbidden_change("", RED, edited) == "changes or removes `def test_zero`"


def test_a_removed_red_test_is_flagged():
    removed = RED[: RED.index("\n\ndef test_two")] + "\n"
    assert forbidden_change(BASE, RED, removed) == "changes or removes `def test_two`"


def test_a_file_that_no_longer_parses_is_flagged():
    assert forbidden_change(BASE, RED, RED + "\ndef broken(:\n").startswith(
        "does not parse"
    )
