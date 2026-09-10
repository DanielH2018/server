"""What the launch CLI refuses, what a partial launch records, and stop — spec §3-5.

Run: uv run pytest scripts/dev/tests/test_fanout_cli.py
"""

import json
import subprocess

import pytest

from fanout_lib import brief as brief_mod
from fanout_lib import launch as launch_mod
from fanout_lib.brief import Issue
from fanout_lib.manifest import Batch, Manifest, save
from fanout_lib.transport import ISSUE_FIELDS, issue_from_view
from fanout_place import main
from _fanout_fakes import HOST_KEY, fake_tools, ok

# Fleet current, fleet cap, login-plane current, login-plane cap, live agents, signing key.
# The plane side is as roomy as the fleet side on purpose: these tests measure call counts,
# placement order and the ssh budget, so the plane must not become the binding cap and turn a
# refusal meant to come from the budget into a NoHeadroom. The plane's own arithmetic is
# test_fanout_placement.py's, and the key is one the fake registered set holds, so a test
# that says nothing about signing passes the gate rather than being dropped by it.
HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"
CLAIMED = [
    Issue(1, "one", "body one", ("claude",)),
    Issue(2, "two", "body two", ("claude",)),
]


def _launch(tools, tmp_path, *args):
    return main(
        [
            "launch",
            *args,
            "--orchestrator-branch",
            "o",
            "--manifest-root",
            str(tmp_path),
        ],
        tools,
    )


def test_a_failed_second_worktree_add_still_records_the_batch_already_launched(
    tmp_path,
):
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    run.answers_by_call = [
        ok(HEADROOM),  # headroom read
        ok(""),  # health read
        ok(""),  # batch 1: launch (worktree add+lock, brief write, systemd-run)
        subprocess.CompletedProcess(
            [], 1, stdout="", stderr="fatal: branch exists\nfanout-step: worktree add\n"
        ),  # batch 2: launch fails at worktree add
        ok(""),  # batch 2: cleanup
    ]
    assert (
        _launch(tools, tmp_path, "--batch", "1", "--batch", "2", "--host", "daniel-box")
        == 1
    )
    assert len(run.calls) == 5
    written = json.loads(next(iter(tmp_path.glob("*.json"))).read_text())
    assert [b["batch"] for b in written["batches"]] == ["1"]


def test_a_batch_whose_worktree_exists_is_refused_and_the_run_id_is_named(
    tmp_path, capsys
):
    """The refusal is per batch, so batch 1 is already running — say so, and say where."""
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    run.answers_by_call = [
        ok(HEADROOM),  # headroom read
        ok(""),  # health read
        ok(""),  # batch 1: launch
        subprocess.CompletedProcess(
            [], 1, stdout="", stderr="fanout-step: exists\n"
        ),  # batch 2: its worktree or branch is still there from an earlier run
    ]
    assert (
        _launch(tools, tmp_path, "--batch", "1", "--batch", "2", "--host", "daniel-box")
        == 1
    )
    # Four calls, not five: an `exists` refusal runs no cleanup.
    assert len(run.calls) == 4
    err = capsys.readouterr().err
    run_id = next(iter(tmp_path.glob("*.json"))).stem
    assert "clean <run-id>" in err
    assert f"launched before this failure: 1 (run {run_id})" in err


def test_pinning_daniel_server_sends_every_call_there_and_none_to_daniel_box(tmp_path):
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM), "daniel-server": ok(HEADROOM)},
        issues=CLAIMED,
    )
    assert _launch(tools, tmp_path, "--batch", "1", "--host", "daniel-server") == 0
    assert {c[0] for c in run.calls} == {"daniel-server"}
    # headroom read, health read, one launch call for the one batch.
    assert len(run.calls) == 3


def test_two_batches_pinned_to_one_host_cost_exactly_four_calls_there(tmp_path):
    """Ruling 15's budget: read + health per host, plus one launch call per batch."""
    tools, run = fake_tools(
        answers={"daniel-server": ok(HEADROOM)},
        issues=CLAIMED,
    )
    code = _launch(
        tools, tmp_path, "--batch", "1", "--batch", "2", "--host", "daniel-server"
    )
    assert code == 0
    assert [c[0] for c in run.calls] == ["daniel-server"] * 4


def test_an_unpinned_launch_reads_both_hosts_and_places_on_the_emptier_one(tmp_path):
    tools, run = fake_tools(
        answers={
            # ~4.5 GiB headroom on daniel-box, ~9.5 GiB on daniel-server — both are
            # candidates, daniel-server wins on room alone.
            "daniel-box": ok(
                f"5368709120\n12884901888\n5368709120\n12884901888\n1\n{HOST_KEY}\n"
            ),
            "daniel-server": ok(HEADROOM),
        },
        issues=[CLAIMED[0]],
    )
    assert _launch(tools, tmp_path, "--batch", "1") == 0
    assert {c[0] for c in run.calls} == {"daniel-box", "daniel-server"}
    launched = [c for c in run.calls if "worktree add" in c[1]]
    assert len(launched) == 1 and launched[0][0] == "daniel-server"


def test_three_batches_pinned_to_one_host_cost_exactly_five_calls_there(tmp_path):
    issues = CLAIMED + [Issue(3, "three", "body three", ("claude",))]
    tools, run = fake_tools(answers={"daniel-server": ok(HEADROOM)}, issues=issues)
    code = _launch(
        tools,
        tmp_path,
        "--batch",
        "1",
        "--batch",
        "2",
        "--batch",
        "3",
        "--host",
        "daniel-server",
    )
    assert code == 0
    assert [c[0] for c in run.calls] == ["daniel-server"] * 5


def test_four_batches_pinned_to_one_host_is_refused_before_any_launch(tmp_path, capsys):
    issues = CLAIMED + [
        Issue(3, "three", "body three", ("claude",)),
        Issue(4, "four", "body four", ("claude",)),
    ]
    tools, run = fake_tools(answers={"daniel-server": ok(HEADROOM)}, issues=issues)
    code = _launch(
        tools,
        tmp_path,
        "--batch",
        "1",
        "--batch",
        "2",
        "--batch",
        "3",
        "--batch",
        "4",
        "--host",
        "daniel-server",
    )
    assert code == 3
    assert not any("worktree add" in c[1] for c in run.calls)
    assert "split the fan-out" in capsys.readouterr().err


def test_stop_stops_the_unit_and_names_clean_as_the_next_step(tmp_path, capsys):
    batch = Batch(
        "1",
        "daniel-box",
        launch_mod.worktree_path("1"),
        "worktree-fanout-1",
        "fanout-1",
        [1],
        "t",
    )
    manifest = Manifest("20260101T000010Z", "o", [batch])
    save(manifest, root=tmp_path)
    tools, run = fake_tools(answers={"daniel-box": ok("")})
    code = main(["stop", manifest.run_id, "--manifest-root", str(tmp_path)], tools)
    assert code == 0
    assert [c[1] for c in run.calls] == ["systemctl --user stop fanout-1"]
    out = capsys.readouterr().out
    assert "1 on daniel-box: stopped" in out
    assert f"clean {manifest.run_id}" in out
    assert "once its PR merges" in out


def test_stop_makes_no_call_for_a_batch_clean_already_took(tmp_path, capsys):
    cleaned = Batch(
        "1",
        "daniel-server",
        "/w1",
        "worktree-fanout-1",
        "fanout-1",
        [1],
        "t",
        removed_at="2026-09-10T12:00:00+00:00",
    )
    live = Batch("2", "daniel-box", "/w2", "worktree-fanout-2", "fanout-2", [2], "t")
    manifest = Manifest("20260101T000010Z", "o", [cleaned, live])
    save(manifest, root=tmp_path)
    tools, run = fake_tools(answers={"daniel-box": ok("")})
    assert main(["stop", manifest.run_id, "--manifest-root", str(tmp_path)], tools) == 0
    assert [c[0] for c in run.calls] == ["daniel-box"]
    out = capsys.readouterr().out
    assert "1 on daniel-server: cleaned (2026-09-10T12:00:00+00:00)" in out
    assert "2 on daniel-box: stopped" in out


def test_a_failed_issue_fetch_launches_nothing_and_writes_no_manifest(tmp_path, capsys):
    for error in (
        subprocess.CalledProcessError(1, ["gh"], stderr="gh: issue not found"),
        subprocess.TimeoutExpired(cmd=["gh"], timeout=30.0),
    ):
        tools, run = fake_tools(
            answers={"daniel-box": ok(HEADROOM)},
            issues=[CLAIMED[0]],
            issue_errors={2: error},
        )
        assert _launch(tools, tmp_path, "--batch", "1", "--batch", "2") == 1
        assert not run.calls  # the fetch fails before any host is touched
        assert not list(tmp_path.glob("*.json"))
        assert "could not fetch issue 2" in capsys.readouterr().err


def test_an_unlabelled_issue_is_refused_and_a_labelled_one_launches(tmp_path, capsys):
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)}, issues=[Issue(3, "t", "b", ("bug",))]
    )
    assert _launch(tools, tmp_path, "--batch", "3") == 1
    assert not run.calls  # refused before the first ssh, so nothing was created
    assert "issue 3 does not carry the `claude` label" in capsys.readouterr().err

    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)},
        issues=[Issue(3, "t", "b", ("claude", "bug"))],
    )
    assert _launch(tools, tmp_path, "--batch", "3", "--host", "daniel-box") == 0
    # headroom read, health read, one launch call for the one batch.
    assert len(run.calls) == 3


def test_a_host_whose_signing_key_github_verifies_is_placed_on(tmp_path):
    """The accept half of the #1615 gate: HOST_KEY is in the fake's registered set."""
    tools, run = fake_tools(answers={"daniel-server": ok(HEADROOM)}, issues=CLAIMED)
    assert _launch(tools, tmp_path, "--batch", "1", "--host", "daniel-server") == 0
    assert [c for c in run.calls if "worktree add" in c[1]]


def test_a_host_whose_signing_key_github_does_not_verify_is_not_placed_on(
    tmp_path, capsys
):
    """The reject half: its commits would read verified=false, so the PR could not merge."""
    tools, run = fake_tools(
        answers={"daniel-server": ok(HEADROOM)},
        issues=CLAIMED,
        signing_keys=frozenset({"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIZZZZ"}),
    )
    assert _launch(tools, tmp_path, "--batch", "1", "--host", "daniel-server") == 6
    assert not [c for c in run.calls if "worktree add" in c[1]]
    err = capsys.readouterr().err
    assert "daniel-server: its commit-signing key" in err and "SHA256:" in err
    assert not list(tmp_path.glob("*.json"))


def test_a_launch_is_refused_when_the_registered_keys_cannot_be_read(tmp_path, capsys):
    """Fail closed, and before the first ssh: a gh outage spends no ssh connection."""
    tools, run = fake_tools(
        answers={"daniel-server": ok(HEADROOM)},
        issues=CLAIMED,
        signing_error=subprocess.CalledProcessError(
            1, ["gh"], stderr="gh: not logged in"
        ),
    )
    assert _launch(tools, tmp_path, "--batch", "1", "--host", "daniel-server") == 6
    assert not run.calls
    assert "registered signing keys" in capsys.readouterr().err


def test_an_unpinned_launch_places_on_the_only_host_github_verifies(tmp_path):
    """daniel-box has more headroom here, but its key is unregistered, so the batch moves."""
    box_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    tools, run = fake_tools(
        answers={
            # Roomy on BOTH planes, so the only thing that can move this batch is the key.
            # A five-line reading here would be dropped as unparseable instead, and the
            # test would pass without the signing gate ever running.
            "daniel-box": ok(f"1\n99999999999\n1\n99999999999\n0\n{box_key}\n"),
            "daniel-server": ok(HEADROOM),
        },
        issues=[CLAIMED[0]],
    )
    assert _launch(tools, tmp_path, "--batch", "1") == 0
    launched = [c for c in run.calls if "worktree add" in c[1]]
    assert len(launched) == 1 and launched[0][0] == "daniel-server"


def test_read_prints_both_caps_and_the_signing_verdict(capsys):
    """One line carrying what placement scores on AND what the launch gate would rule."""
    tools, _ = fake_tools(
        answers={"daniel-box": ok(HEADROOM), "daniel-server": ok(HEADROOM)}
    )
    assert main(["read"], tools) == 0
    out = capsys.readouterr().out
    assert "fleet cap=12884901888 current=1" in out
    assert "plane cap=12884901888 current=1" in out
    assert "signing=ok" in out


def test_read_calls_a_verdict_it_could_not_reach_unknown_rather_than_ok(capsys):
    """`unknown` is the state `launch` refuses on, so `read` must not print it as `ok`."""
    tools, _ = fake_tools(
        answers={"daniel-box": ok(HEADROOM), "daniel-server": ok(HEADROOM)},
        signing_error=subprocess.CalledProcessError(
            1, ["gh"], stderr="gh: not logged in"
        ),
    )
    assert main(["read"], tools) == 0
    out = capsys.readouterr().out
    assert "signing=unknown" in out and "signing=ok" not in out


def test_the_issue_fetch_asks_for_labels_and_carries_them_onto_the_issue():
    assert "labels" in ISSUE_FIELDS.split(",")
    issue = issue_from_view(
        {
            "number": 7,
            "title": "t",
            "body": "b",
            "labels": [{"name": "claude"}, {"name": "bug"}],
        }
    )
    assert issue.labels == ("claude", "bug")
    unlabelled = issue_from_view({"number": 7, "title": "t", "body": "b", "labels": []})
    assert unlabelled.labels == ()
    # A fetch that stopped asking for labels must fail loudly rather than read as
    # unlabelled, which would refuse every issue.
    with pytest.raises(KeyError):
        issue_from_view({"number": 7, "title": "t", "body": "b"})


def test_one_issue_in_two_batches_is_refused_and_a_repeated_batch_is_placed_once(
    tmp_path, capsys
):
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    assert _launch(tools, tmp_path, "--batch", "1,2", "--batch", "2") == 1
    assert not run.calls
    assert "issue 2 appears in more than one --batch" in capsys.readouterr().err

    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    assert (
        _launch(
            tools, tmp_path, "--batch", "1,2", "--batch", "1,2", "--host", "daniel-box"
        )
        == 0
    )
    # headroom read, health read, one launch call for the one placed batch.
    assert len(run.calls) == 3
    assert "--batch 1,2 given twice; placing it once" in capsys.readouterr().err


def test_a_within_spec_duplicate_issue_gets_its_own_message(tmp_path, capsys):
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    assert _launch(tools, tmp_path, "--batch", "1,1") == 1
    assert not run.calls  # refused before the first ssh
    assert "issue 1 is listed twice in --batch 1,1" in capsys.readouterr().err


def test_the_briefs_worktree_path_matches_the_one_launch_creates():
    # brief duplicates the path rather than importing launch, which would cycle; this is
    # the check that keeps the duplicate honest.
    for batch in ("1345-1386", "b"):
        assert brief_mod._worktree_path(batch) == launch_mod.worktree_path(batch)


def test_a_batch_still_live_in_a_manifest_is_refused_before_any_host_is_read(
    tmp_path, capsys
):
    """F4: the per-host existence check cannot see a relaunch placed on the OTHER host."""
    live = Batch("1-2", "daniel-server", "/w", "b", "u", [1, 2], "t")
    save(Manifest("20260101T000010Z", "o", [live]), root=tmp_path)
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    assert _launch(tools, tmp_path, "--batch", "1,2") == 1
    assert not run.calls  # refused before the headroom read, so it costs no ssh
    err = capsys.readouterr().err
    assert "batch 1-2 shares #1, #2 with batch 1-2, live in run" in err
    assert "20260101T000010Z on daniel-server" in err
    assert "run clean 20260101T000010Z first" in err


def test_a_reordered_or_narrowed_spec_cannot_slip_past_the_live_guard(tmp_path, capsys):
    """The batch id is derived from the spec as typed, so it is not what must be unique."""
    live = Batch("1345-1386", "daniel-server", "/w", "b", "u", [1345, 1386], "t")
    save(Manifest("20260101T000010Z", "o", [live]), root=tmp_path)
    for spec, shared in (("1386,1345", "#1345, #1386"), ("1345", "#1345")):
        tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
        assert _launch(tools, tmp_path, "--batch", spec) == 1
        assert not run.calls
        err = capsys.readouterr().err
        assert f"shares {shared} with batch 1345-1386" in err
        assert "on daniel-server" in err


def test_a_first_batch_refused_writes_no_manifest_at_all(tmp_path, capsys):
    """F15: an empty manifest records no work, and `launch` now reads every manifest."""
    tools, _run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    _run.answers_by_call = [
        ok(HEADROOM),
        ok(""),  # health read
        subprocess.CompletedProcess([], 1, stdout="", stderr="fanout-step: exists\n"),
    ]
    assert _launch(tools, tmp_path, "--batch", "1", "--host", "daniel-box") == 1
    assert not list(tmp_path.glob("*.json"))
    assert "launched before this failure: none" in capsys.readouterr().err


def test_an_unknown_run_id_is_one_line_rather_than_a_traceback(tmp_path, capsys):
    tools, calls = fake_tools()
    for command in ("status", "stop", "clean"):
        assert (
            main([command, "nosuchrun", "--manifest-root", str(tmp_path)], tools) == 1
        )
        assert not calls.calls  # refused before any host is touched
        assert (
            f"no manifest for run nosuchrun under {tmp_path}" in capsys.readouterr().err
        )


def test_a_batch_already_cleaned_in_an_old_run_launches_again(tmp_path):
    cleaned = Batch(
        "1-2", "daniel-server", "/w", "b", "u", [1, 2], "t", "2026-09-10T12:00:00+00:00"
    )
    save(Manifest("20260101T000010Z", "o", [cleaned]), root=tmp_path)
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    assert _launch(tools, tmp_path, "--batch", "1,2", "--host", "daniel-box") == 0
    assert len(run.calls) == 3
