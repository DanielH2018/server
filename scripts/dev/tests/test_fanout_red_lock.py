"""The lock the green gate puts on a red test file and on a conftest (#4214, #4517, #4542).

Pure source-to-source checks, so no case here starts git or pytest; the gate-level cases are
in `test_fanout_red_gate.py` and `test_fanout_green_gate_test_code.py`.

Run: uv run pytest scripts/dev/tests/test_fanout_red_lock.py
"""

import pytest

from fanout_lib.review.red_tests import forbidden_change, forbidden_conftest_change

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
        "only test functions, fixtures, helpers, imports and constants",
    ),
    "a constant that shadows the code under test": (
        "\n\ndouble = 4\n",
        "rebinds `double`",
    ),
    "a builtin a constant may call, rebound to the fix's code": (
        "\n\ndef frozenset(items):\n    return items\n",
        "rebinds `frozenset`",
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


def test_an_assignment_that_runs_code_stays_locked_though_unreached():
    """A module-level statement that is not literal data runs at import, around every test."""
    base, red = (s.replace("LIMIT = 10", "LIMIT = len('ten')") for s in (BASE, RED))
    edited = red.replace("len('ten')", "len('eleven')")
    assert forbidden_change(base, red, edited).startswith("changes or removes `assign")


def test_an_unreached_constant_is_the_fixs_to_change():
    edited = RED.replace("LIMIT = 10", "LIMIT = 11")
    assert forbidden_change(BASE, RED, edited) == ""


# A guard module whose red test reaches its constant only through an older helper, as #4372's
# reached `REAPERS` through `scheduled_reapers`.
GUARD_BASE = '''"""The guard."""

NAMES = frozenset({"old.py"})
WANT = ["new.py"]


def flagged(text):
    return [n for n in sorted(NAMES) if n in text]


def test_old_is_flagged():
    assert flagged("run old.py") == ["old.py"]
'''
GUARD_RED = GUARD_BASE + (
    "\n\ndef test_new_is_flagged():\n    assert flagged('run new.py') == ['new.py']\n"
)
NEW_NAMES = 'NAMES = frozenset({"old.py", "new.py"})'


def test_a_constant_only_an_older_helper_reads_is_the_fixs_to_change():
    """#4372: the fix had to change the guard's constant, which the red test reaches."""
    edited = GUARD_RED.replace('NAMES = frozenset({"old.py"})', NEW_NAMES)
    assert forbidden_change(GUARD_BASE, GUARD_RED, edited) == ""


def test_a_constant_a_red_test_names_itself_stays_locked():
    red = (
        GUARD_BASE + "\n\ndef test_want():\n    assert flagged('run new.py') == WANT\n"
    )
    edited = red.replace('WANT = ["new.py"]', "WANT = []")
    assert forbidden_change(GUARD_BASE, red, edited) == (
        "changes or removes `assign at line 4`"
    )


def test_a_constant_a_module_level_statement_reads_stays_locked():
    """`sorted(...)` runs at import, around the red test, so what it reads is the red test's."""
    base, red = (s + "\n\nORDERED = sorted(NAMES)\n" for s in (GUARD_BASE, GUARD_RED))
    edited = red.replace('NAMES = frozenset({"old.py"})', NEW_NAMES)
    assert (
        forbidden_change(base, red, edited) == "changes or removes `assign at line 3`"
    )


def test_a_changed_constant_that_runs_code_is_flagged():
    edited = GUARD_RED.replace(
        'NAMES = frozenset({"old.py"})', "NAMES = frozenset(__import__('os').listdir())"
    )
    assert "is not allowed" in forbidden_change(GUARD_BASE, GUARD_RED, edited)


# Statements a fix may append under a name nothing locked reads, and ones it may not, because
# evaluating them runs code or because pytest reads the name they bind.
LITERALS = [
    "NEW = 1",
    "NEW = ('a', -1, 2 * 3)",
    "NEW = frozenset({'a', 'b'})",
    "NEW = {'k': [1, 2], 'j': set()}",
]
NOT_LITERALS = [
    "NEW: int = 1",
    "NEW = LIMIT",
    "NEW = pytest.mark",
    "NEW = ('a',)[0]",
    "NEW = sorted({'a'})",
    "NEW = dict(a=1)",
    "__all__ = ['x']",
    "collect_ignore = ['x']",
]


@pytest.mark.parametrize("statement", LITERALS)
def test_an_appended_literal_constant_is_clean(statement):
    assert forbidden_change(BASE, RED, f"{RED}\n\n{statement}\n") == ""


@pytest.mark.parametrize("statement", NOT_LITERALS)
def test_an_appended_assignment_that_is_not_a_literal_is_flagged(statement):
    assert "is not allowed" in forbidden_change(BASE, RED, f"{RED}\n\n{statement}\n")


# #4543: pytest resolves `repo`'s request for `workdir` from the red test's module, so a
# `workdir` fixture there serves every red node that requests `repo`.
CONFTEST = """import pytest


@pytest.fixture
def workdir(scratch):
    return scratch


@pytest.fixture
def repo(workdir):
    return workdir


@pytest.fixture(autouse=True)
def _env(clock):
    yield
"""
REPO_RED = BASE + "\n\ndef test_repo(repo):\n    assert repo\n"
FIXTURE = "\n\n@pytest.fixture\ndef {name}():\n    return None\n"


@pytest.mark.parametrize("name", ["workdir", "scratch", "clock"])
def test_an_appended_fixture_a_conftest_fixture_requests_is_flagged(name):
    """Directly, one fixture further down, and through an autouse fixture."""
    head = REPO_RED + FIXTURE.format(name=name)
    assert forbidden_change(BASE, REPO_RED, head) == ""
    assert forbidden_change(BASE, REPO_RED, head, conftests=[CONFTEST]) == (
        f"adds `def {name}`, which rebinds `{name}`"
    )


def test_an_appended_fixture_no_conftest_fixture_requests_is_clean():
    head = REPO_RED + FIXTURE.format(name="other")
    assert forbidden_change(BASE, REPO_RED, head, conftests=[CONFTEST]) == ""


def test_an_older_fixture_that_serves_a_conftest_fixture_stays_locked():
    base = BASE + "\n\n@pytest.fixture\ndef workdir():\n    return 'a'\n"
    red = base + "\n\ndef test_repo(repo):\n    assert repo == 'a'\n"
    edited = red.replace("return 'a'\n", "return 'b'\n", 1)
    assert forbidden_change(base, red, edited) == ""
    assert forbidden_change(base, red, edited, conftests=[CONFTEST]) == (
        "changes or removes `def workdir`"
    )


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


# #4542: the shape of `scripts/conftest.py` before #4359 deleted its unused resolver.
SHARED = '''"""Shared fixtures."""

import json

import pytest

import side_effects
from lib import kubectl

IPS = {"prometheus": "10.0.0.1"}


def _fake_resolve(name):
    return IPS[name]


@pytest.fixture
def fake_resolve():
    return _fake_resolve


@pytest.fixture
def ips():
    return IPS


@pytest.fixture
def repo(workdir):
    return json.dumps(str(workdir))


@pytest.fixture
def workdir(tmp_path):
    return tmp_path


@pytest.fixture(autouse=True)
def _fresh():
    kubectl.forget()
    yield


def pytest_configure(config):
    pass
'''
CENSUS = "def test_no_resolver(repo):\n    assert repo\n"


def _without(src, *names):
    """`src` with each top-level statement that binds one of `names` deleted."""
    blocks = src.split("\n\n\n")
    return "\n\n\n".join(b for b in blocks if not any(f" {n}(" in b for n in names))


def _conftest_change(head):
    return forbidden_conftest_change(SHARED, head, [CENSUS], [SHARED])


def test_deleting_an_unrequested_fixture_and_what_only_it_used_is_clean():
    """#4359's whole deletion: the fixture, its helper, then the map and its other fixture."""
    head = _without(SHARED, "_fake_resolve", "fake_resolve", "ips").replace(
        'IPS = {"prometheus": "10.0.0.1"}\n\n\n', ""
    )
    assert "IPS" not in head and "def repo" in head
    assert _conftest_change(head) == ""
    assert _conftest_change(_without(SHARED, "fake_resolve")) == ""


@pytest.mark.parametrize(
    ("deleted", "reason"),
    [
        (("repo",), "binds `repo`, which a red test"),
        (("_fake_resolve",), "binds `_fake_resolve`, which a red test or the conftest"),
        (("_fresh",), "is an autouse fixture"),
        (("pytest_configure",), "is not a fixture, a plain helper"),
    ],
    ids=["requested", "still-named", "autouse", "hook"],
)
def test_deleting_what_a_red_test_or_the_conftest_still_reaches_is_flagged(
    deleted, reason
):
    assert reason in _conftest_change(_without(SHARED, *deleted))


def test_deleting_an_import_only_its_effect_could_need_is_flagged():
    head = SHARED.replace("import side_effects\n", "")
    assert "is an import no deleted statement used" in _conftest_change(head)


@pytest.mark.parametrize(
    "head",
    [
        SHARED.replace('"10.0.0.1"', '"10.0.0.9"'),
        SHARED + "\n\n@pytest.fixture\ndef extra():\n    return 1\n",
    ],
    ids=["changed", "added"],
)
def test_a_conftest_change_that_is_not_a_deletion_is_flagged(head):
    assert _conftest_change(head).startswith("adds or changes ")


def test_deleting_a_fixture_another_conftests_fixture_requests_is_flagged():
    """`repo` in a nearer conftest requests `workdir` from this one for the red test."""
    outer = _without(SHARED, "repo")
    inner = (
        "import pytest\n\n\n@pytest.fixture\ndef repo(workdir):\n    return workdir\n"
    )
    head = _without(outer, "workdir")
    assert forbidden_conftest_change(outer, head, [CENSUS], [outer]) == ""
    assert forbidden_conftest_change(outer, head, [CENSUS], [outer, inner]) == (
        "deletes `def workdir`, which binds `workdir`, which a red test or the conftest "
        "still uses"
    )
