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
from _fanout_fakes import fake_tools, ok

# Fleet current, fleet cap, login-plane current, login-plane cap, live agents. The plane
# side is as roomy as the fleet side on purpose: these tests measure call counts, placement
# order and the ssh budget, so the plane must not become the binding cap and turn a refusal
# meant to come from the budget into a NoHeadroom. The plane's own arithmetic is
# test_fanout_placement.py's.
HEADROOM = "1\n12884901888\n1\n12884901888\n0\n"
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
            "daniel-box": ok("5368709120\n12884901888\n5368709120\n12884901888\n1\n"),
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
