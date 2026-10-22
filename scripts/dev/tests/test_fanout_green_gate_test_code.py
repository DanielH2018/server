"""The green gate when a red test stands on test code: a conftest, a fixture, a guard.

Real git and real pytest on a scratch repo, as in `test_fanout_red_gate.py`, whose length
cap these cases would cross. The pure lock rules are in `test_fanout_red_lock.py`.

Run: uv run pytest scripts/dev/tests/test_fanout_green_gate_test_code.py
"""

import pytest

from _scratch_pytest import run
from fanout_lib.review.hardened_runs import fixture_closures
from fanout_lib.review.red_gate import green_gate, red_gate
from lib.git_testing import commit, init_repo

CODE = "def double(x):\n    return x\n"
FIXED = "def double(x):\n    return 2 * x\n"
CONFIG = {"pyproject.toml": "[tool.pytest.ini_options]\n"}
# Each fixture of the scratch conftest, by name. `repo` requests `workdir`, which pytest
# resolves from the requesting test's module (#4543).
FIXTURES = {
    "workdir": "@pytest.fixture\ndef workdir(tmp_path):\n    return tmp_path\n",
    "repo": (
        "@pytest.fixture\ndef repo(workdir):\n"
        "    (workdir / 'marker').write_text('x')\n    return workdir\n"
    ),
    "unused": "@pytest.fixture\ndef unused():\n    return None\n",
}


def _conftest(*names):
    return "\n\n".join(["import pytest\n", *(FIXTURES[n] for n in names)])


CONFTEST = _conftest("workdir", "repo", "unused")
RED_TEST = (
    "def test_doubles_in_a_repo(repo):\n    from mod import double\n\n"
    "    assert (repo / 'marker').exists() and double(2) == 4\n"
)
STANDS_ON = "the fix changed what the red tests stand on: "


def _gated(tmp_path, base_files, red_files):
    """A repo at the red commit, and the red gate's verdict on it, which must pass."""
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, "base", **{"mod.py": CODE, **CONFIG, **base_files})
    red = commit(repo, "red", **red_files)
    gate = red_gate(run, repo, base, red)
    assert gate.passed, gate.reason
    return repo, red, gate


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("other", ""),
        ("workdir", "tests/test_new.py adds `def workdir`, which rebinds `workdir`"),
    ],
    ids=["unrequested", "requested-by-a-conftest-fixture"],
)
def test_an_appended_fixture_passes_unless_a_conftest_fixture_requests_it(
    tmp_path, fixture, reason
):
    repo, red, gate = _gated(
        tmp_path, {"tests/conftest.py": CONFTEST}, {"tests/test_new.py": RED_TEST}
    )
    appended = FIXTURES["workdir"].replace("def workdir", f"def {fixture}")
    head = f"import pytest\n\n\n{RED_TEST}\n\n{appended}"
    commit(repo, "fix", **{"mod.py": FIXED, "tests/test_new.py": head})
    assert green_gate(run, repo, red, gate) == (STANDS_ON + reason if reason else "")


# #4548: pytest's builtin `tmp_path` requests `tmp_path_factory` from the red test's module,
# so an appended pass-through `tmp_path_factory` would serve the red node and pass.
TMP_TEST = (
    "def test_doubles_in_tmp(tmp_path):\n    from mod import double\n\n"
    "    assert tmp_path.is_dir() and double(2) == 4\n"
)
PASS_THROUGH = (
    "@pytest.fixture\ndef {name}(tmp_path_factory):\n    return tmp_path_factory\n"
)


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("other", ""),
        (
            "tmp_path_factory",
            "tests/test_new.py adds `def tmp_path_factory`, which rebinds "
            "`tmp_path_factory`",
        ),
    ],
    ids=["unrequested", "requested-by-a-builtin-fixture"],
)
def test_an_appended_fixture_passes_unless_a_builtin_fixture_requests_it(
    tmp_path, fixture, reason
):
    repo, red, gate = _gated(tmp_path, {}, {"tests/test_new.py": TMP_TEST})
    head = f"import pytest\n\n\n{TMP_TEST}\n\n{PASS_THROUGH.format(name=fixture)}"
    commit(repo, "fix", **{"mod.py": FIXED, "tests/test_new.py": head})
    assert green_gate(run, repo, red, gate) == (STANDS_ON + reason if reason else "")


# `pytest --setup-plan -q` with pytest 9.1.1, measured on 2026-10-12: a node with fixtures, one
# with none, a parametrize id holding a space, and a fixture's SETUP line with the same suffix.
PLAN = """\
SETUP    S tmp_path_factory
        SETUP    F tmp_path (fixtures used: tmp_path_factory)
        tests/test_x.py::test_a (fixtures used: request, tmp_path, tmp_path_factory)
        TEARDOWN F tmp_path
        tests/test_x.py::test_b
        SETUP    F n['a b']
        tests/test_x.py::test_c[a b] (fixtures used: n, request)
        TEARDOWN F n['a b']
TEARDOWN S tmp_path_factory
no tests ran in 0.00s
"""


def test_the_setup_plan_gives_each_listed_node_its_fixtures():
    a, b, c, unlisted = (
        f"tests/test_x.py::{n}" for n in ("test_a", "test_b", "test_c[a b]", "test_d")
    )
    assert fixture_closures(PLAN, [a, b, c, unlisted]) == {
        a: ["request", "tmp_path", "tmp_path_factory"],
        b: [],
        c: ["n", "request"],
    }


def test_the_red_gate_refuses_a_new_test_the_setup_plan_does_not_list(tmp_path):
    hidden = (
        "import pytest\n\n\ndef pytest_runtest_setup(item):\n"
        "    if item.config.option.setupplan:\n        pytest.skip('hidden')\n"
    )
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, "base", **{"mod.py": CODE, **CONFIG, "conftest.py": hidden})
    red = commit(repo, "red", **{"tests/test_new.py": TMP_TEST})
    assert red_gate(run, repo, base, red).reason == (
        "pytest --setup-plan exited 0 without listing the fixtures of: "
        "tests/test_new.py::test_doubles_in_tmp"
    )


# #4359: the red test demanded that the conftest drop a fixture nothing requests.
CENSUS = (
    "from pathlib import Path\n\n\ndef test_no_unused_fixture(repo):\n"
    "    conftest = (Path(__file__).parent / 'conftest.py').read_text()\n"
    "    assert (repo / 'marker').exists() and 'def unused' not in conftest\n"
)


@pytest.mark.parametrize(
    ("kept", "reason"),
    [
        (("workdir", "repo"), ""),
        (
            ("workdir",),
            "tests/conftest.py deletes `def repo`, which binds `repo`, which a red test "
            "or the conftest still uses",
        ),
    ],
    ids=["unrequested", "requested"],
)
def test_a_conftest_may_lose_only_a_fixture_no_red_test_requests(
    tmp_path, kept, reason
):
    repo, red, gate = _gated(
        tmp_path, {"tests/conftest.py": CONFTEST}, {"tests/test_census.py": CENSUS}
    )
    commit(repo, "fix", **{"tests/conftest.py": _conftest(*kept)})
    assert green_gate(run, repo, red, gate) == (STANDS_ON + reason if reason else "")


@pytest.mark.parametrize(
    ("deleted", "reason"),
    [
        ("unused", ""),
        (
            "tmp_path_factory",
            "tests/conftest.py deletes `def tmp_path_factory`, which binds "
            "`tmp_path_factory`, which a red test or the conftest still uses",
        ),
    ],
    ids=["unrequested", "requested-by-a-builtin-fixture"],
)
def test_a_conftest_may_not_lose_a_fixture_a_builtin_fixture_requests(
    tmp_path, deleted, reason
):
    """The red test requests only `tmp_path`, whose `tmp_path_factory` the conftest serves."""
    fixtures = {
        "tmp_path_factory": PASS_THROUGH.format(name="tmp_path_factory"),
        "unused": FIXTURES["unused"],
    }
    conftest = "\n\n".join(["import pytest\n", *fixtures.values()])
    repo, red, gate = _gated(
        tmp_path, {"tests/conftest.py": conftest}, {"tests/test_new.py": TMP_TEST}
    )
    kept = [f for name, f in fixtures.items() if name != deleted]
    head = "\n\n".join(["import pytest\n", *kept])
    commit(repo, "fix", **{"mod.py": FIXED, "tests/conftest.py": head})
    assert green_gate(run, repo, red, gate) == (STANDS_ON + reason if reason else "")


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ({"conftest.py": "x = 1\n"}, "conftest.py added"),
        ({"tests/conftest.py": None}, "tests/conftest.py deleted"),
    ],
    ids=["added", "deleted"],
)
def test_a_new_or_deleted_conftest_is_refused(tmp_path, files, reason):
    repo, red, gate = _gated(
        tmp_path, {"tests/conftest.py": CONFTEST}, {"tests/test_census.py": CENSUS}
    )
    commit(repo, "fix", **files)
    assert green_gate(run, repo, red, gate) == STANDS_ON + reason


# #4372: the guard's red test reaches its constant only through an older helper.
GUARD = """NAMES = frozenset({"old.py"})


def flagged(text):
    return [n for n in sorted(NAMES) if n in text]


def test_old_is_flagged():
    assert flagged("run old.py") == ["old.py"]
"""
GUARD_RED = (
    "\n\ndef test_new_is_flagged():\n    assert flagged('run new.py') == ['new.py']\n"
)


def test_the_green_gate_passes_a_guard_constant_the_red_test_needs_changed(tmp_path):
    repo, red, gate = _gated(
        tmp_path,
        {"tests/test_guard.py": GUARD},
        {"tests/test_guard.py": GUARD + GUARD_RED},
    )
    fixed = GUARD.replace('{"old.py"}', '{"old.py", "new.py"}') + GUARD_RED
    commit(repo, "fix", **{"tests/test_guard.py": fixed})
    assert green_gate(run, repo, red, gate) == ""


# #5397: the red commits registered their patching module, and the fix raised its count.
REGISTRY = "ansible/tests/repo/monkeypatch_allowlist.txt"
LISTED = "tests/test_listed.py 2\n"
RED_LINE = "tests/test_new.py 8\n"
PLAIN_RED_TEST = (
    "def test_doubles():\n    from mod import double\n\n    assert double(2) == 4\n"
)


@pytest.mark.parametrize(
    ("head", "reason"),
    [
        (LISTED + "tests/test_new.py 9\n", ""),
        (LISTED, ""),
        ("tests/test_listed.py 3\n" + RED_LINE, "drops `tests/test_listed.py 2`"),
        (RED_LINE, "drops `tests/test_listed.py 2`"),
        (LISTED + "tests/test_old.py 1\n" + RED_LINE, "adds `tests/test_old.py 1`"),
    ],
    ids=[
        "raises-the-red-modules-line",
        "drops-the-red-modules-line",
        "raises-an-older-modules-line",
        "deletes-an-older-modules-line",
        "adds-a-line-for-a-module-at-base",
    ],
)
def test_a_patch_registry_may_change_only_the_red_modules_lines(tmp_path, head, reason):
    repo, red, gate = _gated(
        tmp_path,
        {"tests/test_old.py": "def test_old():\n    pass\n", REGISTRY: LISTED},
        {"tests/test_new.py": PLAIN_RED_TEST, REGISTRY: LISTED + RED_LINE},
    )
    commit(repo, "fix", **{"mod.py": FIXED, REGISTRY: head})
    refusal = f"{REGISTRY} {reason}, a line that names no file the red commits added"
    assert green_gate(run, repo, red, gate) == (STANDS_ON + refusal if reason else "")


# #5197: the code under test was `_restarts_privately`, a helper already in the red file, and
# the fix restricted it so that a comment saying "rollout restart" no longer counts.
SUBJECT_BASE = (
    "def _flagged(command):\n    return 'rollout restart' in command\n\n\n"
    "def test_a_restart_command_is_flagged():\n"
    "    assert _flagged('kubectl rollout restart deploy/x')\n"
)
SUBJECT_RED = SUBJECT_BASE + (
    "\n\ndef test_a_comment_is_not_flagged():\n"
    "    assert not _flagged('# no rollout restart here')\n"
)


def test_a_fix_to_the_helper_the_issue_names_as_its_subject_passes_the_green_gate(
    tmp_path,
):
    """The gate carries the names the issue gives as its subject, as `Gate.subjects`.

    Set through `setattr` because the field does not exist yet.
    """
    path = "tests/test_subject.py"
    repo, red, gate = _gated(tmp_path, {path: SUBJECT_BASE}, {path: SUBJECT_RED})
    restricted = SUBJECT_RED.replace(
        "return 'rollout restart' in command",
        "return command.startswith('kubectl') and 'rollout restart' in command",
    )
    commit(repo, "fix", **{path: restricted})
    field = "subjects"
    setattr(gate, field, {"_flagged"})
    assert green_gate(run, repo, red, gate) == ""
    setattr(gate, field, set())
    assert green_gate(run, repo, red, gate).startswith(STANDS_ON)
