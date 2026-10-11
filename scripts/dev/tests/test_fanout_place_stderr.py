"""Every stderr line `fanout.py place` prints carries argparse's `<prog>:` prefix (#4500).

One `place launch` run mixed `fanout.py: error:` lines with lines that carried no prefix at
all, so a grep for the prefix missed part of what a run reported. Each case drives one path
through `main` and checks every stderr line the run printed, not only the one it expects.
Several cases feed multi-line remote stderr, because a message that embeds it unflattened
leaves its second line unprefixed.

Run: uv run pytest scripts/dev/tests/test_fanout_place_stderr.py
"""

import subprocess

import pytest

from fanout_lib.brief import Issue
from fanout_lib.manifest import Batch, Manifest, save
from fanout_lib.place import main
from _fanout_fakes import HOST_KEY, fake_tools, ok

PREFIXES = ("fanout.py: error: ", "fanout.py: warning: ")
HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"
FULL = f"12884901888\n12884901888\n12884901888\n12884901888\n0\n{HOST_KEY}\n"
ISSUES = [Issue(n, str(n), "body", ("claude",)) for n in (1, 2)]
SSH_REFUSED = subprocess.CompletedProcess(
    [], 255, stdout="", stderr="ssh: connect to host\nConnection refused\n"
)
RUN_ID = "20260101T000010Z"
UNREGISTERED = "nobody/nowhere"


def _launch_argv(tmp_path, *args):
    return ["launch", *args, "--manifest-root", str(tmp_path)]


def _read_with_a_bad_host_and_no_keys(tmp_path):
    tools, _ = fake_tools(
        answers={"daniel-box": ok(HEADROOM), "daniel-server": SSH_REFUSED},
        signing_error=subprocess.CalledProcessError(
            1, ["gh"], stderr="HTTP 502\ntry again\n"
        ),
    )
    return (
        tools,
        ["read"],
        [
            "fanout.py: warning: could not read the GitHub account's registered signing "
            "keys (HTTP 502 try again)",
            "fanout.py: warning: daniel-server: headroom read failed (255): ssh: connect "
            "to host Connection refused",
        ],
    )


def _launch_past_an_unreadable_host(tmp_path):
    tools, _ = fake_tools(
        answers={"daniel-box": ok(HEADROOM), "daniel-server": SSH_REFUSED},
        issues=ISSUES,
    )
    return (
        tools,
        _launch_argv(tmp_path, "--batch", "1"),
        [
            "fanout.py: warning: placing without daniel-server: headroom read failed "
            "(255): ssh: connect to host Connection refused"
        ],
    )


def _launch_with_no_headroom(tmp_path):
    tools, _ = fake_tools(answers={"daniel-box": ok(FULL)}, issues=ISSUES)
    return (
        tools,
        _launch_argv(tmp_path, "--batch", "1", "--host", "daniel-box"),
        ["fanout.py: error: no headroom for batch 1"],
    )


def _launch_whose_second_batch_fails(tmp_path):
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=ISSUES)
    run.answers_by_call = [
        ok(HEADROOM),  # headroom read
        ok(""),  # health read
        ok(""),  # batch 1: launch
        subprocess.CompletedProcess(
            [], 1, stdout="", stderr="fatal: branch exists\nfanout-step: worktree add\n"
        ),  # batch 2: launch fails at worktree add
        ok(""),  # batch 2: cleanup
    ]
    return (
        tools,
        _launch_argv(tmp_path, "--batch", "1", "--batch", "2", "--host", "daniel-box"),
        [
            "fanout.py: error: 2 on daniel-box: worktree add failed (1): fatal: branch "
            "exists fanout-step: worktree add",
            "fanout.py: error: launched before this failure: 1 (run ",
        ],
    )


def _launch_whose_every_claim_is_refused(tmp_path):
    tools, _ = fake_tools(
        answers={"daniel-box": ok(HEADROOM)}, issues=ISSUES, refused_claims={1}
    )
    return (
        tools,
        _launch_argv(tmp_path, "--batch", "1", "--host", "daniel-box"),
        [
            "fanout.py: warning: 1: dropped #1: claim refused (3)",
            "fanout.py: warning: 1: no issue claimed; not launching it",
            "fanout.py: error: launched nothing: every issue's claim was refused",
        ],
    )


def _status_of_an_unknown_run(tmp_path):
    tools, _ = fake_tools()
    return (
        tools,
        ["status", "nosuchrun", "--manifest-root", str(tmp_path)],
        [f"fanout.py: error: no manifest for run nosuchrun under {tmp_path}"],
    )


def _abandon_argv(tmp_path, batch, repo="DanielH2018/server"):
    batches = [
        Batch("1-2", "daniel-box", "/w1", "worktree-fanout-1-2", "fanout-1-2", [1, 2], "t", repo=repo),
        Batch("3", "daniel-box", "/w3", "worktree-fanout-3", "fanout-3", [3], "t", repo=repo),
    ]  # fmt: skip
    save(Manifest(RUN_ID, "worktree-orch", batches), root=tmp_path)
    return ["abandon", RUN_ID, batch, "--manifest-root", str(tmp_path)]


def _abandon_of_an_unknown_batch(tmp_path):
    tools, _ = fake_tools()
    return (
        tools,
        _abandon_argv(tmp_path, "9"),
        [f"fanout.py: error: run {RUN_ID} has no batch 9 (it has 1-2, 3)"],
    )


def _abandon_of_an_unregistered_repo(tmp_path):
    tools, _ = fake_tools()
    return (
        tools,
        _abandon_argv(tmp_path, "1-2", repo=UNREGISTERED),
        [f"fanout.py: error: 1-2 on daniel-box: abandon failed: {UNREGISTERED} is not"],
    )


def _abandon_that_times_out(tmp_path):
    tools, run = fake_tools()
    run.answers_by_call = [subprocess.TimeoutExpired("ssh", 120.0)]
    return (
        tools,
        _abandon_argv(tmp_path, "1-2"),
        ["fanout.py: error: 1-2 on daniel-box: abandon timed out"],
    )


def _abandon_of_a_kept_tree(tmp_path):
    kept = ok("kept: /w1 — unit fanout-1-2 still active after stop")
    tools, _ = fake_tools(answers={"daniel-box": kept})
    return (
        tools,
        _abandon_argv(tmp_path, "1-2"),
        [
            "fanout.py: error: 1-2 on daniel-box: kept: /w1 — unit fanout-1-2 still active"
        ],
    )


def _abandon_whose_release_fails(tmp_path):
    tools, _ = fake_tools(
        answers={"daniel-box": ok("removed: /w3 (abandoned)")}, findings_exit=1
    )
    return (
        tools,
        _abandon_argv(tmp_path, "3"),
        ["fanout.py: error: 3 on daniel-box: release failed (1)"],
    )


def _stop_of_an_unregistered_repo(tmp_path):
    tools, _ = fake_tools(answers={"daniel-box": ok("")})
    batch = Batch(
        "1", "daniel-box", "/w1", "b", "fanout-1", [1], "t", repo=UNREGISTERED
    )
    save(Manifest(RUN_ID, "worktree-orch", [batch]), root=tmp_path)
    return (
        tools,
        ["stop", RUN_ID, "--manifest-root", str(tmp_path)],
        [
            f"fanout.py: error: 1 on daniel-box: claims not released: {UNREGISTERED} is not"
        ],
    )


CASES = [
    _read_with_a_bad_host_and_no_keys,
    _launch_past_an_unreadable_host,
    _launch_with_no_headroom,
    _launch_whose_second_batch_fails,
    _launch_whose_every_claim_is_refused,
    _status_of_an_unknown_run,
    _abandon_of_an_unknown_batch,
    _abandon_of_an_unregistered_repo,
    _abandon_that_times_out,
    _abandon_of_a_kept_tree,
    _abandon_whose_release_fails,
    _stop_of_an_unregistered_repo,
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.__name__.lstrip("_"))
def test_every_stderr_line_carries_the_prefix(case, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["fanout.py"])
    tools, argv, expected = case(tmp_path)
    main(argv, tools)
    err = capsys.readouterr().err
    # The expected messages keep a case honest: a path that printed nothing at all would
    # otherwise pass the prefix check over an empty stderr.
    for message in expected:
        assert message in err
    assert [ln for ln in err.splitlines() if not ln.startswith(PREFIXES)] == []
