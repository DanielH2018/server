"""The append-only rule the green gate applies to a red test file (#4214).

Pure source-to-source checks, so no case here starts git or pytest; the two gate-level cases
are in `test_fanout_red_gate.py`.

Run: uv run pytest scripts/dev/tests/test_fanout_red_lock.py
"""

import pytest

from fanout_lib.review.red_tests import append_only

RED = '''"""Tests for mod."""

import pytest

from mod import double


def _twice(x):
    return double(double(x))


def test_zero():
    assert double(0) == 0


def test_two(tmp_path):
    assert _twice(1) == 4
'''

CLEAN = {
    "a new test function and the import it needs": (
        "\n\nfrom mod import halve\n\n\ndef test_halve():\n    assert halve(4) == 2\n"
    ),
    "a parametrized test with a new helper and a fixture": (
        "\n\ndef _many():\n    return [1, 2]\n\n\n@pytest.fixture\ndef numbers():\n"
        "    return _many()\n\n\n@pytest.mark.parametrize('n', [1, 2])\n"
        "def test_many(n, numbers):\n    assert n in numbers\n"
    ),
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


@pytest.mark.parametrize("appended", CLEAN.values(), ids=CLEAN.keys())
def test_an_appended_test_is_clean(appended):
    assert append_only(RED, RED + appended) == ""


def test_a_changed_module_docstring_is_clean():
    assert (
        append_only(RED, RED.replace("Tests for mod.", "Tests for mod's double.")) == ""
    )


@pytest.mark.parametrize(("appended", "reason"), FLAGGED.values(), ids=FLAGGED.keys())
def test_an_appended_statement_that_reaches_a_red_test_is_flagged(appended, reason):
    assert reason in append_only(RED, RED + appended)


def test_an_edited_helper_a_red_test_calls_is_flagged():
    """#4183: the fix edited a pre-existing function the red node calls."""
    edited = RED.replace("double(double(x))", "4")
    assert append_only(RED, edited) == "changes or removes `def _twice`"


def test_a_removed_red_test_is_flagged():
    removed = RED.replace("def test_zero():\n    assert double(0) == 0\n\n\n", "")
    assert append_only(RED, removed) == "changes or removes `def test_zero`"


def test_a_file_that_no_longer_parses_is_flagged():
    assert append_only(RED, RED + "\ndef broken(:\n").startswith("does not parse")
