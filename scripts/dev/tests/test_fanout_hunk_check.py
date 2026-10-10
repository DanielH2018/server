"""Whether the red tests notice each fix hunk taken out on its own, on a scratch repo.

Run: uv run pytest scripts/dev/tests/test_fanout_hunk_check.py
"""

from _scratch_pytest import run
from fanout_lib.review.hunk_check import red_detection
from fanout_lib.review.red_gate import red_gate
from lib.git_testing import commit, init_repo

CODE = (
    "def double(x):\n    return x\n\n\n"
    "def unrelated():\n    return 'a'\n\n\n"
    "def label():\n    return 'x'\n"
)
RED_TEST = (
    "def test_two():\n    from mod import double\n\n    assert double(2) == 4\n\n\n"
    "def test_triple():\n    from mod import triple\n\n    assert triple(1) == 3\n"
)


def _red(tmp_path):
    repo = init_repo(tmp_path / "repo")
    files = {"mod.py": CODE, "pyproject.toml": "[tool.pytest.ini_options]\n"}
    base = commit(repo, "base", **files)
    red = commit(repo, "red", **{"tests/test_new.py": RED_TEST})
    gate = red_gate(run, repo, base, red)
    assert gate.passed, gate.reason
    return repo, red, gate


def _fix(repo, mod):
    return commit(
        repo,
        "fix",
        **{"mod.py": mod, "tests/test_extra.py": "def test_x():\n    pass\n"},
    )


def test_a_hunk_no_red_test_notices_is_flagged(tmp_path):
    repo, red, gate = _red(tmp_path)
    fixed = (
        CODE.replace("return x", "return 2 * x").replace("'a'", "'b'")
        + "\n\ndef triple(x):\n    return 3 * x\n"
    )
    _fix(repo, fixed)
    check = red_detection(run, repo, red, gate)
    assert check.hunks == 3
    assert check.missed == ["mod.py:6"]
    assert check.by_absence == 1


def test_a_hunk_every_red_test_pins_is_clean(tmp_path):
    repo, red, gate = _red(tmp_path)
    _fix(
        repo,
        CODE.replace("return x", "return 2 * x")
        + "\n\ndef triple(x):\n    return 3 * x\n",
    )
    check = red_detection(run, repo, red, gate)
    assert (check.hunks, check.missed) == (2, [])


def test_a_hunk_in_a_file_no_test_runs_is_skipped(tmp_path):
    """#4182's fix also edited `.claude/rules/facts.md`, which no red test could notice."""
    repo, red, gate = _red(tmp_path)
    fixed = (
        CODE.replace("return x", "return 2 * x")
        + "\n\ndef triple(x):\n    return 3 * x\n"
    )
    commit(repo, "fix", **{"mod.py": fixed, "docs/notes.md": "Doubles now.\n"})
    check = red_detection(run, repo, red, gate)
    assert (check.hunks, check.skipped, check.missed) == (2, 1, [])


def test_a_comment_or_docstring_hunk_is_skipped(tmp_path):
    repo, red, gate = _red(tmp_path)
    fixed = (
        CODE.replace("return x", "return 2 * x").replace(
            "def label():\n", 'def label():\n    """The label."""\n'
        )
        + "\n\n# A comment.\ndef triple(x):\n    return 3 * x\n"
    )
    _fix(repo, fixed)
    check = red_detection(run, repo, red, gate)
    assert (check.hunks, check.skipped, check.missed) == (2, 1, [])
