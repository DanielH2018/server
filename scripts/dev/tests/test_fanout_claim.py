"""The claims `launch` and `claim` take under the orchestrator's own branch (#3695).

Run: uv run pytest scripts/dev/tests/test_fanout_claim.py
"""

import dataclasses
import json
import subprocess

import pytest

from fanout_lib.brief import Issue
from fanout_place import main
from _fanout_fakes import HOST_KEY, fake_tools, ok

HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"
ISSUES = [
    Issue(1, "one", "body one", ("claude",)),
    Issue(2, "two", "body two", ("claude",)),
]


def _launch(tools, tmp_path, *batches):
    argv = ["launch", "--host", "daniel-box", "--manifest-root", str(tmp_path)]
    for batch in batches:
        argv += ["--batch", batch]
    return main(argv, tools)


def _findings(run):
    return [cmd for host, cmd, _ in run.calls if host == "findings"]


def test_launch_claims_each_issue_under_head_before_its_batch_starts(tmp_path):
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=ISSUES)
    assert _launch(tools, tmp_path, "1,2") == 0
    hosts = [host for host, _, _ in run.calls]
    # Headroom and health reads first, then the reap and both claims, then the one launch.
    assert hosts == [
        "daniel-box", "daniel-box", "findings", "findings", "findings", "daniel-box",
    ]  # fmt: skip
    assert _findings(run) == [
        "reap",
        "claim 1 --worktree worktree-orch",
        "claim 2 --worktree worktree-orch",
    ]
    manifest = json.loads(next(iter(tmp_path.glob("*.json"))).read_text())
    assert manifest["orchestrator_branch"] == "worktree-orch"


@pytest.mark.parametrize("head", ["HEAD", "master"])
def test_a_detached_head_or_the_base_branch_is_refused_before_any_call(
    tmp_path, capsys, head
):
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)}, issues=ISSUES, head=head
    )
    assert _launch(tools, tmp_path, "1") == 1
    assert run.calls == []
    assert f"HEAD is {head!r}" in capsys.readouterr().err


def test_a_refused_issue_is_dropped_and_the_batch_renamed_from_what_it_kept(
    tmp_path, capsys
):
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)}, issues=ISSUES, refused_claims={2}
    )
    assert _launch(tools, tmp_path, "1,2") == 3
    launch_call = run.host_calls[-1][1]
    assert "fanout-1 " in launch_call and "fanout-1-2" not in launch_call
    assert "body two" not in (run.host_calls[-1][2] or "")
    manifest = json.loads(next(iter(tmp_path.glob("*.json"))).read_text())
    assert [(b["batch"], b["issues"]) for b in manifest["batches"]] == [("1", [1])]
    assert "1-2: dropped #2: claim refused (3)" in capsys.readouterr().err


def test_a_batch_with_no_claimed_issue_launches_nothing_and_writes_no_manifest(
    tmp_path,
):
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)}, issues=ISSUES, refused_claims={1}
    )
    assert _launch(tools, tmp_path, "1") == 3
    assert [host for host, _, _ in run.host_calls] == ["daniel-box", "daniel-box"]
    assert not any(tmp_path.iterdir())


def test_a_failed_launch_releases_the_claims_it_took(tmp_path, capsys):
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=ISSUES)
    run.answers_by_call = [
        ok(HEADROOM),
        ok(""),
        subprocess.CompletedProcess([], 1, stdout="", stderr="fanout-step: exists\n"),
    ]
    assert _launch(tools, tmp_path, "1,2") == 1
    assert _findings(run)[-1] == (
        "release 1 2 --worktree worktree-orch --reason fan-out launch refused"
    )
    assert "released #1, #2" in capsys.readouterr().err


def test_claim_takes_every_batch_and_exits_3_naming_a_refusal(capsys):
    tools, run = fake_tools(refused_claims={3})
    assert main(["claim", "--batch", "1,2", "--batch", "3"], tools) == 3
    assert _findings(run) == [f"claim {n} --worktree worktree-orch" for n in (1, 2, 3)]
    out = capsys.readouterr()
    assert "#1 claimed by `worktree-orch`" in out.out
    assert "#3: claim refused (3)" in out.err
    assert [host for host, _, _ in run.calls] == ["findings"] * 3


def test_a_failed_reap_warns_and_the_launch_still_claims(tmp_path, capsys):
    """`claim` reaps a stale claim on the issue it takes, so a failed `reap` blocks nothing."""
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=ISSUES)
    claim = tools.findings

    def findings(argv):
        if argv[0] == "reap":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="git failed")
        return claim(argv)

    assert _launch(dataclasses.replace(tools, findings=findings), tmp_path, "1") == 0
    assert "reap failed (1): git failed; claiming without it" in capsys.readouterr().err
    assert _findings(run) == ["claim 1 --worktree worktree-orch"]


def test_a_placement_refusal_claims_nothing(tmp_path):
    """Four batches on one host is over the ssh budget: exit 3 before any launch."""
    issues = [Issue(n, str(n), "b", ("claude",)) for n in (1, 2, 3, 4)]
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=issues)
    assert _launch(tools, tmp_path, "1", "2", "3", "4") == 3
    assert _findings(run) == []
