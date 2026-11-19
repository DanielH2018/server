"""`status` reads a daniel-server batch's landing from the server lander's log on daniel-box.

Split from test_fanout_status.py, which is at its length cap.

Run: uv run pytest scripts/dev/tests/test_fanout_status_lander.py
"""

from pathlib import Path

import pytest

from fanout_lib import autoland
from fanout_lib.brief import LANDS
from fanout_lib.manifest import Batch, Manifest
from fanout_lib.status import AWAITING_LANDER, LANDER_LOGS, NEEDS_INPUT, PARKED, collect
from fanout_lib.status_view import one_line
from lib.exit_codes import FANOUT_PLACE_FAILED, FANOUT_PLACE_OK
from _fanout_fakes import fake_tools, ok

PR = "https://github.com/DanielH2018/server/pull/1500"
B = Batch("b", "daniel-server", "/w/b", "worktree-fanout-b", "fanout-b", [1], "t")
STOPPED_AT_ITS_PR = (
    "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
    f'{{"type":"result","result":"Opened {PR} for #1."}}\n'
)


def _log(verdict: str, rc: int, cause: str = "-", owner: str = "-") -> str:
    hold = "aaaa" if owner != "-" else "-"
    return (
        f"=== 1500\nLAND-RESULT: verdict={verdict} cause={cause} exit={rc} pr=1500 "
        f"sha=bbbbbbbb tags=1 hold_sha={hold} hold_owner={owner} merge=bbbb\n"
        f"VERDICT: {verdict} (detail)\n"
    )


@pytest.mark.parametrize(
    ("lander", "state", "shown"),
    [
        (_log("settled", 0), "landed", "b on daniel-server: landed"),
        (
            _log("deploy-failed", 1, "promote-held", "other"),
            PARKED,
            "parked: merged, unverified behind DEPLOY_HOLD aaaa",
        ),
        (
            _log("deploy-failed", 1, "promote-held", "own"),
            NEEDS_INPUT,
            "VERDICT: deploy-failed (detail)",
        ),
        ("=== 1500\n", "done", f"done ({AWAITING_LANDER})"),
    ],
    ids=["settled", "held-by-another-pr", "held-by-its-own-service", "no-result-yet"],
)
def test_a_server_lander_batch_shows_the_landers_verdict(lander, state, shown):
    """The batch stopped at its PR, so `status` reads the landing's result in one more call."""
    tools, run = fake_tools(branch_prs={B.branch: [PR]})
    run.answers_by_call = [ok(STOPPED_AT_ITS_PR), ok(lander)]
    rows, tier = collect(Manifest("r", "o", [B]), tools, one_line)
    (row,) = rows
    assert (row["state"], shown in row["line"]) == (state, True), row
    attention = state in (PARKED, NEEDS_INPUT)
    assert tier == (FANOUT_PLACE_FAILED if attention else FANOUT_PLACE_OK)
    assert run.host_calls[1][0] == LANDS


def test_status_reads_the_directory_the_server_lander_writes_its_logs_to():
    lander_dir = (autoland.STATE_DIR / "logs").relative_to(Path.home())
    assert LANDER_LOGS == f"$HOME/{lander_dir}"
