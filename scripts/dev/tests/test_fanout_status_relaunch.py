"""status reads a no-report batch as landed only on a PR merged after its launch (#7407).

Split from test_fanout_status.py, which sits at the module-length cap.

Run: uv run pytest scripts/dev/tests/test_fanout_status_relaunch.py
"""

import dataclasses
import json

from fanout_lib.branch_pr import parse_merged_before
from fanout_lib.manifest import Batch, Manifest, save
from fanout_lib.place import main
from _fanout_fakes import fake_tools, ok

B = Batch(
    "b",
    "daniel-box",
    "/home/ubuntu/server/.claude/worktrees/fanout-b",
    "worktree-fanout-b",
    "fanout-b",
    [1],
    "2026-11-06T12:00:00+00:00",
)
PR = "https://github.com/o/r/pull/1699"
FINISHED_AND_TIDIED = (
    "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n"
    "--- stderr\n\n--- report\n"
)


def _status_of_batch_whose_pr_merged_at(merged_at, tmp_path, capsys):
    run = Manifest("20260101T000012Z", "o", [B])
    save(run, root=tmp_path)
    rows = json.dumps([{"url": PR, "mergedAt": merged_at}])
    tools, _ = fake_tools(
        answers={"daniel-box": ok(FINISHED_AND_TIDIED)},
        merged_prs={B.branch: PR},
    )
    tools = dataclasses.replace(
        tools,
        earlier_prs=lambda _branch, _repo, launched_at: parse_merged_before(
            rows, launched_at
        ),
    )
    code = main(
        ["status", run.run_id, "--all", "--manifest-root", str(tmp_path)], tools
    )
    return code, capsys.readouterr().out


def test_a_no_report_batch_does_not_land_on_a_pr_merged_before_its_launch(
    tmp_path, capsys
):
    code, out = _status_of_batch_whose_pr_merged_at(
        "2026-11-05T12:00:00Z", tmp_path, capsys
    )
    assert code == 1
    assert "b on daniel-box: no-report" in out


def test_a_no_report_batch_lands_on_a_pr_merged_after_its_launch(tmp_path, capsys):
    code, out = _status_of_batch_whose_pr_merged_at(
        "2026-11-06T13:00:00Z", tmp_path, capsys
    )
    assert code == 0
    assert f"b on daniel-box: landed {PR}" in out
