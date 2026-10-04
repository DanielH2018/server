"""The `fanout` source cc-wait reads, driven with `fanout_place status` output it would print.

Run: uv run pytest scripts/dev/tests/test_fanout_probe.py
"""

import pytest

import fanout_probe
from fanout_lib.status import BatchStatus, status_line


def status(**runs: tuple[int, str]):
    """A status reader answering each run id with (exit tier, printed lines)."""
    return lambda run_id: runs[run_id]


def rendered(batch: str, state: str, pr_url: str = "") -> str:
    """One batch line as `fanout_place status` prints it, from the real renderer."""
    s = BatchStatus(batch, state, pr_url, final_text="", exit_code=0, stderr_tail="")
    line, _tier = status_line(s, "daniel-box", f"worktree-{batch}", lambda *_: "", str)
    return line


def test_a_running_batch_keeps_the_wait_open_and_names_the_finished_ones():
    text = "a on daniel-box: running\nb on daniel-server: done https://x/pull/1 opened it\n"
    reading = fanout_probe.read(["r1"], status(r1=(0, text)))
    assert reading == {"state": "running", "detail": "1 running; finished: b done"}


def test_every_batch_done_is_finished_and_counts_them():
    text = (
        rendered("a", "done", "https://x/pull/1")
        + "\n"
        + rendered("b", "done", "u")
        + "\n"
    )
    reading = fanout_probe.read(["r1"], status(r1=(0, text)))
    assert reading == {"state": "finished", "detail": "2 batches finished"}


def test_output_with_no_batch_line_is_unreadable_not_finished():
    """An empty read's worst tier is 0, which read `finished` before this was refused."""
    with pytest.raises(fanout_probe.Unreadable):
        fanout_probe.read(
            ["r1"], status(r1=(0, "fanout_place: something else entirely\n"))
        )


def test_a_batch_needing_a_hand_ends_needs_attention_with_its_line():
    line = "b on daniel-box: needs-input needs input: which host?"
    text = f"a on daniel-box: landed https://x/pull/1\n{line}\n"
    reading = fanout_probe.read(["r1"], status(r1=(1, text)))
    assert reading == {"state": "needs-attention", "detail": line}


def test_a_failed_batch_ends_failed():
    text = "a on daniel-box: failed (exit 1) boom\n"
    assert fanout_probe.read(["r1"], status(r1=(5, text)))["state"] == "failed"


def test_a_host_whose_status_read_timed_out_keeps_the_wait_open():
    text = "a on daniel-server: status read timed out\nb on daniel-box: landed https://x/pull/1\n"
    assert fanout_probe.read(["r1"], status(r1=(1, text)))["state"] == "running"


def test_several_runs_are_one_wait():
    readers = status(
        r1=(0, "a on daniel-box: landed u\n"), r2=(0, "b on daniel-box: running\n")
    )
    assert fanout_probe.read(["r1", "r2"], readers)["state"] == "running"


def test_the_declared_states_keep_cc_wait_s_contract():
    codes = set(fanout_probe.TERMINAL.values())
    assert not {2, 75} & codes
    assert any(code != 0 for code in codes)


def test_an_unknown_run_is_refused():
    assert fanout_probe.main(["--describe", "no-such-run-20000101T000000Z"]) == 1
