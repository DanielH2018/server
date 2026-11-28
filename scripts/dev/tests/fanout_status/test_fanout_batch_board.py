"""`fanout.py board`: each standing batch read from its host alone, for the deploy UI.

Run: uv run pytest scripts/dev/tests/fanout_status/test_fanout_batch_board.py
"""

import json
import subprocess

from dev.fanout_lib.batch_board import main
from dev.fanout_lib.manifest import Batch, Manifest, save
from _fanout_fakes import fake_tools, ok


def test_board_reads_each_batch_from_its_host_and_blanks_a_host_that_times_out(
    tmp_path, capsys
):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Changes
    box = Batch("1", "daniel-box", "/w1", "worktree-fanout-1", "u1", [1], "t")
    server = Batch("2", "daniel-server", "/w2", "worktree-fanout-2", "u2", [2], "t")
    save(Manifest("20260101T000010Z", "worktree-a", [box, server]), root=tmp_path)
    tools, run = fake_tools()
    run.answers_by_call = [
        ok(
            "--- board 1\nphase review\nphase_since 1700000000\n"
            "result LAND-RESULT: verdict=pr-ci-red cause=- exit=1 pr=9 next=fix-and-rerun\n"
            "=== 1\nActiveState=active\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
        ),
        subprocess.TimeoutExpired("ssh", 30),
    ]
    assert main(["--manifest-root", str(tmp_path)], tools) == 0
    rows = {r["batch"]: r for r in json.loads(capsys.readouterr().out)}
    read = ("state", "running", "phase", "phase_since", "next")
    assert {k: rows["1"][k] for k in read} == {
        "state": "running",
        "running": True,
        "phase": "review",
        "phase_since": 1700000000.0,
        "next": "fix-and-rerun",
    }
    assert (rows["2"]["state"], rows["2"]["running"]) == ("unread", None)
