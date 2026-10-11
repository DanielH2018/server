"""The `fanout` source cc-wait reads, driven with the rows `fanout.py place status --json` prints.

Run: uv run pytest scripts/dev/tests/test_wait_probe.py
"""

import pytest

from fanout_lib import place
from fanout_lib import wait_probe
from fanout_lib.manifest import Batch, Manifest, save
from fanout_lib.status import UNREAD
from _fanout_fakes import fake_tools


def status(**runs: tuple[int, list[dict]]):
    """A status reader answering each run id with (exit tier, rows)."""
    return lambda run_id: runs[run_id]


def row(batch: str, state: str, line: str = "") -> dict:
    """One row as `status --json` prints it; only `batch`, `state` and `line` are read."""
    return {
        "batch": batch,
        "host": "daniel-box",
        "branch": f"worktree-fanout-{batch}",
        "state": state,
        "pr_url": "",
        "line": line or f"{batch} on daniel-box: {state}",
    }


def test_a_running_batch_keeps_the_wait_open_and_names_the_finished_ones():
    rows = [row("a", "running"), row("b", "done")]
    reading = wait_probe.read(["r1"], status(r1=(0, rows)))
    assert reading == {"state": "running", "detail": "1 running; finished: b done"}


def test_every_batch_done_is_finished_and_counts_them():
    rows = [row("a", "done"), row("b", "landed")]
    reading = wait_probe.read(["r1"], status(r1=(0, rows)))
    assert reading == {"state": "finished", "detail": "2 batches finished"}


def test_output_with_no_batch_row_is_unreadable_not_finished():
    """An empty read's worst tier is 0, which read `finished` before this was refused."""
    with pytest.raises(wait_probe.Unreadable):
        wait_probe.read(["r1"], status(r1=(0, [])))


def test_a_batch_needing_a_hand_ends_needs_attention_with_its_line():
    line = "b on daniel-box: needs-input needs input: which host?"
    rows = [row("a", "landed"), row("b", "needs-input", line)]
    reading = wait_probe.read(["r1"], status(r1=(1, rows)))
    assert reading == {"state": "needs-attention", "detail": line}


def test_a_failed_batch_ends_failed():
    rows = [row("a", "failed")]
    assert wait_probe.read(["r1"], status(r1=(5, rows)))["state"] == "failed"


def test_a_host_whose_status_read_timed_out_keeps_the_wait_open():
    rows = [row("a", UNREAD), row("b", "landed")]
    assert wait_probe.read(["r1"], status(r1=(1, rows)))["state"] == "running"


def test_several_runs_are_one_wait():
    readers = status(r1=(0, [row("a", "landed")]), r2=(0, [row("b", "running")]))
    assert wait_probe.read(["r1", "r2"], readers)["state"] == "running"


def test_read_status_reads_the_json_rows_status_prints(tmp_path):
    """The real `status --json` path end to end, so the probe and `status` cannot drift."""
    cleaned = Batch("1", "daniel-box", "/w1", "b1", "u1", [1], "t", "2026-10-09T00:00Z")
    save(Manifest("20260101T000010Z", "o", [cleaned]), root=tmp_path)
    tools, _run = fake_tools()

    def main(argv):
        return place.main([*argv, "--manifest-root", str(tmp_path)], tools)

    rc, rows = wait_probe.read_status("20260101T000010Z", main)
    assert rc == 0
    assert [(r["batch"], r["state"]) for r in rows] == [("1", "cleaned")]


def test_the_declared_states_keep_cc_wait_s_contract():
    codes = set(wait_probe.TERMINAL.values())
    assert not {2, 75} & codes
    assert any(code != 0 for code in codes)


def test_an_unknown_run_is_refused():
    assert wait_probe.main(["--describe", "no-such-run-20000101T000000Z"]) == 1
