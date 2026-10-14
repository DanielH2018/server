"""status tests a blocker line before a PR URL, so a held batch never reads `done` (#4910).

Split from test_fanout_status.py, which sits at the module-length cap.

Run: uv run pytest scripts/dev/tests/test_fanout_status_blocker_first.py
"""

import dataclasses
import json

from fanout_lib.manifest import Batch
from fanout_lib.status import parse_status, status_line

BOX = Batch(
    "b",
    "daniel-box",
    "/home/ubuntu/server/.claude/worktrees/fanout-b",
    "worktree-fanout-b",
    "fanout-b",
    [1],
    "t",
)
# On daniel-server a PR URL alone is `done`, which is where a held batch read as finished.
SERVER = dataclasses.replace(BOX, host="daniel-server")
PR = "https://github.com/DanielH2018/server/pull/1500"


def _final(result: str) -> str:
    return (
        "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n"
        "--- stderr\n--- report\n"
        + json.dumps({"type": "result", "result": result})
        + "\n"
    )


# The ending `held_for_findings` and `held_for_green` give a batch: a blocker line, then the PR.
HELD_WITH_A_PR_URL = _final(f"needs input: the review held this PR.\n{PR}")
FAILED_WITH_A_PR_URL = _final(f"failed: the green gate refused it.\n{PR}")


def test_a_blocker_line_ending_in_a_pr_url_is_flagged():
    for batch in (BOX, SERVER):
        for text in (HELD_WITH_A_PR_URL, FAILED_WITH_A_PR_URL):
            st = parse_status([batch], text)[0]
            assert (st.state, st.pr_url) == ("needs-input", PR), batch.host
    line, tier = status_line(
        parse_status([SERVER], HELD_WITH_A_PR_URL)[0],
        "daniel-server",
        SERVER.branch,
        lambda *_: "",
        str,
    )
    assert line.startswith(f"b on daniel-server: needs-input {PR} ") and tier == 1


def test_a_finished_text_with_a_pr_url_and_no_blocker_line_is_clean():
    st = parse_status([SERVER], _final(f"Opened {PR} for #1."))[0]
    assert (st.state, st.pr_url) == ("done", PR)
