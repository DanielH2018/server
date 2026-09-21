"""`renovate_rebase.py` against a fake `gh`: the box is ticked once, never twice, never invented.

Run: uv run pytest scripts/dev/tests/test_renovate_rebase.py
"""

import io
import subprocess
from pathlib import Path

import pytest
from renovate_rebase import TICKED, UNTICKED, main, tick_rebase_box

BODY = f"This PR contains the following updates:\n\n---\n\n{UNTICKED}\n\nSome text\n"


def _gh(body: str, edit_fails: bool = False):
    """A `gh` whose `pr view` answers `body` and whose `pr edit` records what it was handed."""
    calls: list[tuple[str, str]] = []

    def gh(*args, **kwargs):
        if args[:2] == ("pr", "view"):
            return subprocess.CompletedProcess(args, 0, body, "")
        if args[:2] == ("pr", "edit"):
            if edit_fails:
                raise subprocess.CalledProcessError(1, args, "", "HTTP 403")
            calls.append((args[2], Path(args[4]).read_text()))
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected gh call {args}")

    return gh, calls


def test_an_unticked_box_is_ticked_and_nothing_else_moves():
    new_body, outcome = tick_rebase_box(BODY)
    assert outcome == "ticked"
    assert new_body == BODY.replace(UNTICKED, TICKED)


def test_a_ticked_box_is_left_alone():
    assert tick_rebase_box(BODY.replace(UNTICKED, TICKED)) == (None, "already-ticked")


def test_a_body_without_the_box_is_flagged():
    assert tick_rebase_box("not a renovate body") == (None, "no-box")


def test_main_writes_the_ticked_body_back_through_body_file():
    gh, calls = _gh(BODY)
    out = io.StringIO()
    assert main(["123"], gh=gh, out=out) == 0
    assert calls == [("123", BODY.replace(UNTICKED, TICKED))]
    assert "ticked" in out.getvalue()


def test_main_is_idempotent_on_a_ticked_box():
    gh, calls = _gh(BODY.replace(UNTICKED, TICKED))
    assert main(["123"], gh=gh, out=io.StringIO()) == 0
    assert calls == []


def test_main_refuses_a_body_with_no_box_without_editing():
    gh, calls = _gh("hand-written PR body")
    out = io.StringIO()
    assert main(["123"], gh=gh, out=out) == 1
    assert calls == []
    assert UNTICKED in out.getvalue()


def test_a_failed_edit_is_reported_with_gh_stderr():
    gh, _ = _gh(BODY, edit_fails=True)
    out = io.StringIO()
    assert main(["123"], gh=gh, out=out) == 2
    assert "HTTP 403" in out.getvalue()


@pytest.mark.parametrize("argv", [[], ["abc"], ["1", "2"]])
def test_a_bad_argument_is_a_usage_error(argv):
    def gh(*args, **kwargs):
        raise AssertionError(f"gh must not run on a usage error: {args}")

    assert main(argv, gh=gh, out=io.StringIO()) == 64
