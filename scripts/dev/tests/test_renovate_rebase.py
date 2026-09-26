"""`renovate_rebase.py` against a fake `gh`: the box is ticked once, never twice, never
invented, and never while the PR is soaking.

Run: uv run pytest scripts/dev/tests/test_renovate_rebase.py
"""

import io
import json
import subprocess
from pathlib import Path

import pytest
from renovate_rebase import (
    SOAK_CONTEXT,
    TICKED,
    UNTICKED,
    main,
    soak_cleared,
    soaking,
    tick_rebase_box,
)

BODY = f"This PR contains the following updates:\n\n---\n\n{UNTICKED}\n\nSome text\n"

# The shapes `gh pr view --json statusCheckRollup` really returns, copied off PRs #2335
# (soaking) and #2620 (soak over) on 2026-09-26. A CI check is a `CheckRun` with
# `name`/`conclusion`; the soak is a `StatusContext` with `context`/`state`.
CI_RUN = {
    "__typename": "CheckRun",
    "name": "pytest (shard 1 of 6)",
    "status": "COMPLETED",
    "conclusion": "SUCCESS",
}
SOAK_PENDING = {
    "__typename": "StatusContext",
    "context": SOAK_CONTEXT,
    "state": "PENDING",
    "startedAt": "2026-09-24T01:31:18Z",
    "targetUrl": "https://docs.renovatebot.com/key-concepts/minimum-release-age/",
}
SOAK_DONE = {**SOAK_PENDING, "state": "SUCCESS"}


def _gh(body: str, edit_fails: bool = False, rollup: list[dict] | None = None):
    """A `gh` whose `pr view` answers `body` and whose `pr edit` records what it was handed."""
    calls: list[tuple[str, str]] = []

    def gh(*args, **kwargs):
        if args[:2] == ("pr", "view"):
            payload = json.dumps(
                {"body": body, "statusCheckRollup": [CI_RUN, *(rollup or [])]}
            )
            return subprocess.CompletedProcess(args, 0, payload, "")
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


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["abc"],
        ["1", "2"],
        ["--retick"],
        ["--force", "1"],
        ["--retick", "--retick", "1"],
    ],
)
def test_a_bad_argument_is_a_usage_error(argv):
    def gh(*args, **kwargs):
        raise AssertionError(f"gh must not run on a usage error: {args}")

    assert main(argv, gh=gh, out=io.StringIO()) == 64


def test_a_pending_soak_status_is_detected():
    assert soaking([CI_RUN, SOAK_PENDING]) is True


def test_a_finished_soak_is_not_pending():
    assert soaking([CI_RUN, SOAK_DONE]) is False


def test_a_pr_with_no_soak_status_is_not_soaking():
    """Renovate posts the status only where a `minimumReleaseAge` rule matched; absent is
    not pending, or every PR without one would be refused."""
    assert soaking([CI_RUN]) is False
    assert soaking([]) is False


def test_a_lowercase_state_still_counts_as_pending():
    """The commit-status API answers `pending`; the rollup answers `PENDING`."""
    assert soaking([{"context": SOAK_CONTEXT, "state": "pending"}]) is True


def test_another_pending_status_is_not_the_soak():
    assert soaking([{"context": "renovate/artifacts", "state": "PENDING"}]) is False


def test_main_refuses_to_tick_a_soaking_pr():
    gh, calls = _gh(BODY, rollup=[SOAK_PENDING])
    out = io.StringIO()
    assert main(["123"], gh=gh, out=out) == 3
    assert calls == []
    assert SOAK_CONTEXT in out.getvalue()
    assert "minimumReleaseAge" in out.getvalue()


def test_main_names_the_soak_on_a_pr_whose_box_is_already_ticked():
    """#2335's state: the box was ticked, Renovate answered 'Rebase not applied', and exit 0
    read as 'the rebase is coming' for 38 hours (#2368)."""
    gh, calls = _gh(BODY.replace(UNTICKED, TICKED), rollup=[SOAK_PENDING])
    out = io.StringIO()
    assert main(["123"], gh=gh, out=out) == 3
    assert calls == []
    assert "already ticked" in out.getvalue()
    assert SOAK_CONTEXT in out.getvalue()


def test_a_finished_soak_ticks_normally():
    gh, calls = _gh(BODY, rollup=[SOAK_DONE])
    assert main(["123"], gh=gh, out=io.StringIO()) == 0
    assert calls == [("123", BODY.replace(UNTICKED, TICKED))]


def test_a_soaking_pr_with_no_box_is_still_a_no_box_error():
    """The box check comes first: a non-Renovate PR is exit 1 whatever its statuses say."""
    gh, calls = _gh("hand-written PR body", rollup=[SOAK_PENDING])
    assert main(["123"], gh=gh, out=io.StringIO()) == 1
    assert calls == []


def test_a_success_soak_status_is_cleared():
    assert soak_cleared([CI_RUN, SOAK_DONE]) is True


def test_a_pending_or_absent_soak_status_is_not_cleared():
    """Absent is not cleared: a renamed context reads as absent, and must not open a retick."""
    assert soak_cleared([CI_RUN, SOAK_PENDING]) is False
    assert soak_cleared([CI_RUN]) is False


def test_retick_unticks_then_reticks_a_spent_box_after_the_soak():
    """#2655: two separate edits, because one write of an unchanged body changes nothing."""
    ticked = BODY.replace(UNTICKED, TICKED)
    gh, calls = _gh(ticked, rollup=[SOAK_DONE])
    out = io.StringIO()
    assert main(["--retick", "123"], gh=gh, out=out) == 0
    assert calls == [("123", BODY), ("123", ticked)]
    assert "unticked and ticked again" in out.getvalue()


def test_retick_refuses_a_pr_still_soaking():
    gh, calls = _gh(BODY.replace(UNTICKED, TICKED), rollup=[SOAK_PENDING])
    assert main(["123", "--retick"], gh=gh, out=io.StringIO()) == 3
    assert calls == []


def test_retick_refuses_a_pr_with_no_soak_status():
    gh, calls = _gh(BODY.replace(UNTICKED, TICKED))
    out = io.StringIO()
    assert main(["--retick", "123"], gh=gh, out=out) == 3
    assert calls == []
    assert "does not read SUCCESS" in out.getvalue()


def test_retick_on_an_unticked_box_ticks_it_once():
    gh, calls = _gh(BODY, rollup=[SOAK_DONE])
    assert main(["--retick", "123"], gh=gh, out=io.StringIO()) == 0
    assert calls == [("123", BODY.replace(UNTICKED, TICKED))]


def test_retick_names_the_unticked_box_when_the_second_edit_fails():
    ticked = BODY.replace(UNTICKED, TICKED)
    edits: list[str] = []

    def gh(*args, **kwargs):
        if args[:2] == ("pr", "view"):
            payload = json.dumps({"body": ticked, "statusCheckRollup": [SOAK_DONE]})
            return subprocess.CompletedProcess(args, 0, payload, "")
        edits.append(Path(args[4]).read_text())
        if len(edits) == 2:
            raise subprocess.CalledProcessError(1, args, "", "HTTP 502")
        return subprocess.CompletedProcess(args, 0, "", "")

    out = io.StringIO()
    assert main(["--retick", "123"], gh=gh, out=out) == 2
    assert edits == [BODY, ticked]
    assert "UNTICKED" in out.getvalue()
    assert "HTTP 502" in out.getvalue()
