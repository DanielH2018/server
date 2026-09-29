#!/usr/bin/env python3
"""Tests for the fan-out Stop hook (issue #2816).

The hook blocks a headless fan-out session from stopping on a progress report, at most three
times per batch, and stays silent outside a fan-out worktree. Each rule is a block/allow pair:
a hook that blocks everything and one that blocks nothing look the same from one side.

Run: uv run pytest .claude/hooks/tests/test_fanout_stop.py
"""

import importlib.util
import io
import json
import os

from fanout_lib import status
from fanout_lib.brief import Issue, render_brief

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fanout-stop.py"
)
_spec = importlib.util.spec_from_file_location("fanout_stop", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

PROGRESS = "Tests pass. Next I will open the PR."
FINISHED = "Opened https://github.com/DanielH2018/server/pull/2900 and landed it."


# The line only the daniel-box brief carries; a daniel-server brief says to stop at
# `gh pr create`, which is what makes the marker the right thing to key on.
LANDING_BRIEF = "# Fan-out batch\n./scripts/deploy_tools/land.sh --pr 1 --arm-merge\n"
LANDED = FINISHED + "\nVERDICT: settled"


def _fanout_tree(tmp_path, brief="# Fan-out batch\n"):
    (tmp_path / ".fanout").mkdir(parents=True)
    (tmp_path / ".fanout" / "brief.md").write_text(brief)
    return tmp_path


def _stop(cwd, message):
    return _mod.decide({"cwd": str(cwd), "last_assistant_message": message})


def test_a_progress_report_in_a_fanout_tree_is_blocked_and_the_reason_names_the_open_item(
    tmp_path,
):
    reason = _stop(_fanout_tree(tmp_path), PROGRESS)
    assert reason and "neither a PR URL nor a line starting `needs input:`" in reason
    assert "1 of 3" in reason


def test_the_same_progress_report_outside_a_fanout_tree_is_allowed(tmp_path):
    assert _stop(tmp_path, PROGRESS) is None
    assert not (tmp_path / ".fanout").exists()


def test_a_final_message_carrying_a_pr_url_is_allowed(tmp_path):
    root = _fanout_tree(tmp_path)
    assert _stop(root, FINISHED) is None
    assert not (root / ".fanout" / "stop-blocks").exists()


def test_a_blocker_line_is_allowed_but_the_same_words_mid_sentence_are_not(tmp_path):
    root = _fanout_tree(tmp_path)
    assert (
        _stop(root, "Summary so far.\nneeds input: which host owns the apply?") is None
    )
    assert _stop(root, "Summary.\nfailed: the render test needs a live cluster") is None
    assert _stop(root, "The render step failed: retrying it next.") is not None


def test_it_blocks_three_times_then_lets_the_session_stop(tmp_path):
    root = _fanout_tree(tmp_path)
    reasons = [_stop(root, PROGRESS) for _ in range(5)]
    assert [r is not None for r in reasons] == [True, True, True, False, False]
    assert (root / ".fanout" / "stop-blocks").read_text().strip() == "3"


def test_a_cwd_below_the_worktree_root_still_finds_the_marker(tmp_path):
    root = _fanout_tree(tmp_path)
    sub = root / "scripts" / "dev"
    sub.mkdir(parents=True)
    assert _stop(sub, PROGRESS) is not None
    assert (root / ".fanout" / "stop-blocks").exists()


def test_main_emits_a_block_decision_on_stdout(tmp_path):
    root = _fanout_tree(tmp_path)
    out = io.StringIO()
    payload = {"cwd": str(root), "last_assistant_message": PROGRESS}
    assert _mod.main(io.StringIO(json.dumps(payload)), out) == 0
    decision = json.loads(out.getvalue())
    assert decision["decision"] == "block" and decision["reason"]

    quiet = io.StringIO()
    payload["last_assistant_message"] = FINISHED
    assert _mod.main(io.StringIO(json.dumps(payload)), quiet) == 0
    assert quiet.getvalue() == ""


def test_the_hook_and_status_read_the_same_patterns():
    """A stop the hook allows must read as `done` or `needs-input` in `status`, never `no-pr`.

    The hook copies the patterns because the hooks stay stdlib-only and cannot import
    `fanout_lib`. This holds the copies equal.
    """
    assert _mod.PR_URL.pattern == status.PR_URL.pattern
    assert _mod.PR_URL.flags == status.PR_URL.flags
    assert _mod.BLOCKER.pattern == status.BLOCKER.pattern
    assert _mod.BLOCKER.flags == status.BLOCKER.flags
    assert _mod.VERDICT.pattern == status.VERDICT.pattern
    assert _mod.VERDICT.flags == status.VERDICT.flags


def test_a_landing_batch_that_names_a_pr_but_no_verdict_is_blocked(tmp_path):
    """#2890: on daniel-box `gh pr create` returning is not the finish the brief asks for."""
    root = _fanout_tree(tmp_path, LANDING_BRIEF)
    reason = _stop(root, FINISHED)
    assert reason and "no `VERDICT:` line" in reason
    # The clean half: the same message on a batch not told to land is a finish.
    assert _stop(_fanout_tree(tmp_path / "other"), FINISHED) is None


def test_a_landing_batch_is_allowed_once_a_verdict_exists(tmp_path):
    root = _fanout_tree(tmp_path, LANDING_BRIEF)
    assert _stop(root, LANDED) is None
    assert not (root / ".fanout" / "stop-blocks").exists()


def test_a_verdict_in_the_land_log_finishes_a_batch_that_reported_tersely(tmp_path):
    root = _fanout_tree(tmp_path, LANDING_BRIEF)
    (root / ".fanout" / "land1.log").write_text("deploying...\nVERDICT: settled\n")
    assert _stop(root, FINISHED) is None
    # The rejecting half: a log with no verdict in it leaves the batch owing one.
    other = _fanout_tree(tmp_path / "other", LANDING_BRIEF)
    (other / ".fanout" / "land1.log").write_text("deploying...\n")
    assert _stop(other, FINISHED) is not None


def test_a_landing_batch_may_still_stop_on_a_blocker_line(tmp_path):
    """A blocker is a finish on either host; the verdict rule must not trap one."""
    root = _fanout_tree(tmp_path, LANDING_BRIEF)
    assert _stop(root, "needs input: master CI is red") is None


def test_the_landing_marker_is_a_line_the_real_daniel_box_brief_carries():
    """Non-vacuity: a reworded brief would silently stop the hook asking for a verdict."""
    issues = [Issue(1, "one", "body")]
    box = render_brief(issues, "daniel-box", "1", "worktree-orch", [])
    server = render_brief(issues, "daniel-server", "1", "worktree-orch", [])
    assert _mod.LANDING_MARKER in box
    assert _mod.LANDING_MARKER not in server
