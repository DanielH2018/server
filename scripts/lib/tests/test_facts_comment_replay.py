"""`replay.comment_replay`: the comment class of the removed-name rule, replayed over history.

Each fixture commits a history whose commits are dated by the live clock, so every replay here
runs over a window that ends past today.
"""

from datetime import datetime, timezone

from lib.facts.lint import vanished_advice
from lib.facts.replay import comment_replay
from lib.git_testing import commit, init_repo

_SINCE = datetime(2000, 1, 1, tzinfo=timezone.utc)
_LATER = datetime(2099, 1, 1, tzinfo=timezone.utc)
_CALLER = "# Calls old_helper_name before the deploy.\nrun()\n"


def _rows(repo):
    replay = comment_replay(repo, _SINCE, _LATER)
    assert replay["crashed"] == []
    return [(r["commit"], r["token"], r["place"]) for r in replay["rows"]]


def test_a_comment_naming_a_name_its_commit_took_out_of_code_is_a_row(tmp_path):
    repo = init_repo(tmp_path)
    commit(
        repo, "base", **{"a.py": "def old_helper_name():\n    pass\n", "b.py": _CALLER}
    )
    removal = commit(repo, "remove the helper", **{"a.py": "def other():\n    pass\n"})
    commit(repo, "unrelated", **{"c.py": "x = 1\n"})

    assert _rows(repo) == [(removal, "old_helper_name", "b.py:1")]


def test_a_name_taken_out_of_a_comment_alone_is_no_row(tmp_path):
    repo = init_repo(tmp_path)
    commit(
        repo,
        "base",
        **{"a.py": "# Mentions old_helper_name.\nrun()\n", "b.py": _CALLER},
    )
    commit(repo, "drop the comment", **{"a.py": "run()\n"})

    assert _rows(repo) == []


def test_a_name_code_still_holds_is_no_row(tmp_path):
    repo = init_repo(tmp_path)
    kept = "old_helper_name()\n"
    commit(
        repo,
        "base",
        **{"a.py": kept, "b.py": _CALLER, "d.py": kept},
    )
    commit(repo, "drop one caller", **{"a.py": "run()\n"})

    assert _rows(repo) == []


def test_the_replay_lists_what_the_advice_lists_for_the_same_commit(tmp_path):
    repo = init_repo(tmp_path)
    base = commit(
        repo, "base", **{"a.py": "def old_helper_name():\n    pass\n", "b.py": _CALLER}
    )
    removal = commit(repo, "remove the helper", **{"a.py": "def other():\n    pass\n"})

    advice = (vanished_advice(repo, base) or "").splitlines()[1:]

    assert advice == ["  old_helper_name: b.py:1"]
    assert _rows(repo) == [(removal, "old_helper_name", "b.py:1")]
