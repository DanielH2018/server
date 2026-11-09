"""`comment <n> --body-file f`: a plain comment that changes no register state.

Run: uv run pytest scripts/dev/tests/test_findings_comment.py
"""

import pytest
from _findings_fakes import Fakes, make_issue

from dev import findings
from lib.exit_codes import FINDINGS_BAD_ARGS
from dev.findings_lib.plans import ClaimRefused, plan_comment

_NOTE = "The gate in the body is too early: the census still lists 146 items."


# --- the planner ------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
def test_comment_posts_the_body_and_nothing_else(state):
    assert plan_comment(make_issue(6643, state=state), _NOTE) == [
        ["issue", "comment", "6643", "--body", _NOTE]
    ]


def test_comment_naming_a_marker_mid_text_is_clean():
    body = "Nothing here is a `Re-observed` sighting or a Claim: `x` trailer."
    assert plan_comment(make_issue(6643), body)


def test_comment_outside_the_register_is_refused():
    issue = make_issue(6643)
    issue["labels"] = []
    with pytest.raises(ClaimRefused) as exc:
        plan_comment(issue, _NOTE)
    assert "not in the register" in exc.value.reason


def test_comment_with_a_blank_body_is_refused():
    with pytest.raises(ClaimRefused) as exc:
        plan_comment(make_issue(6643), "  \n")
    assert "empty" in exc.value.reason


@pytest.mark.parametrize(
    ("body", "marker"),
    [
        ("Re-observed by session (sighting 2).", "Re-observed"),
        ("Refuted: the census is empty.", "Refuted:"),
        ("Accepted: the operator keeps the key.", "Accepted:"),
        ("Taking this one.\nClaim: `worktree-foo`", "Claim:"),
        ("Done here.\nReleased: `worktree-foo`", "Released:"),
    ],
)
def test_comment_carrying_a_fold_marker_is_refused(body, marker):
    """Each body would otherwise count a sighting, settle a close, or move a claim."""
    with pytest.raises(ClaimRefused) as exc:
        plan_comment(make_issue(6643), body)
    assert marker in exc.value.reason


# --- the CLI ----------------------------------------------------------------------------------


@pytest.fixture
def note(tmp_path):
    path = tmp_path / "note.md"
    path.write_text(_NOTE)
    return path


def test_comment_cli_posts_through_gh(capsys, make_tools, note):
    tools, calls = make_tools(Fakes(view=make_issue(6643)))
    assert findings.main(["comment", "6643", "--body-file", str(note)], tools) == 0
    assert calls.gh == [["issue", "comment", "6643", "--body", _NOTE]]
    assert "#6643 commented" in capsys.readouterr().out


def test_comment_cli_dry_run_prints_one_command_and_writes_nothing(
    capsys, make_tools, note
):
    tools, calls = make_tools(Fakes(view=make_issue(6643)))
    argv = ["comment", "6643", "--body-file", str(note), "--dry-run"]
    assert findings.main(argv, tools) == 0
    assert calls.gh == []
    printed = [
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("gh ")
    ]
    assert printed == [f"gh issue comment 6643 --body {_NOTE}"]


def test_comment_cli_refusal_exits_3_and_writes_nothing(make_tools, tmp_path):
    marked = tmp_path / "marked.md"
    marked.write_text("Re-observed by session.")
    tools, calls = make_tools(Fakes(view=make_issue(6643)))
    assert findings.main(["comment", "6643", "--body-file", str(marked)], tools) == 3
    assert calls.gh == []


def test_comment_cli_missing_body_file_is_bad_args(make_tools, tmp_path):
    tools, calls = make_tools()
    argv = ["comment", "6643", "--body-file", str(tmp_path / "absent.md")]
    assert findings.main(argv, tools) == FINDINGS_BAD_ARGS
    assert calls.none()
