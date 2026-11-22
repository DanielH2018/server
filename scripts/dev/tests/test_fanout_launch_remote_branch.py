"""The batch's branch left on origin by an earlier launch: deleted once merged, else refused.

Run: uv run pytest scripts/dev/tests/test_fanout_launch_remote_branch.py
"""

import subprocess

import pytest

from dev.fanout_lib.branch_pr import BranchPrs
from dev.fanout_lib.launch import LaunchError, launch, remote_branch_command
from dev.fanout_lib.target import Target, branch_name
from _fanout_fakes import as_operator, fake_tools, ok

from lib.git_testing import git, init_repo, scrub_process_git_env
from lib.proc_testing import run as proc_run


def _origin_with_branch(tmp_path, branch: str) -> Target:
    """A clone whose origin holds `branch`, as a target the remote step can run against."""
    origin = init_repo(tmp_path / "origin.git", bare=True)
    seed = init_repo(tmp_path / "seed", initial_commit="init")
    git(seed, "push", "-q", str(origin), "master", f"master:{branch}")
    git(tmp_path, "clone", "-q", str(origin), "checkout")
    return Target("DanielH2018/server", str(tmp_path / "checkout"), "origin/master")


def _origin_heads(target: Target) -> str:
    return git(target.checkout, "ls-remote", "--heads", "origin").stdout


def test_a_merged_branch_left_on_origin_is_deleted_and_an_unmerged_one_refused(
    tmp_path, monkeypatch
):
    """Real git: the leftover branch is what sent a relaunched batch's push elsewhere."""
    scrub_process_git_env(monkeypatch)
    branch = branch_name("b")
    target = _origin_with_branch(tmp_path, branch)

    refused = proc_run(["bash", "-c", remote_branch_command("b", target)])
    assert refused.returncode == 1 and "fanout-step: remote branch" in refused.stderr
    assert f"refs/heads/{branch}" in _origin_heads(target)

    proc_run(
        ["bash", "-c", remote_branch_command("b", target, merged=True)], check=True
    )
    assert f"refs/heads/{branch}" not in _origin_heads(target)
    assert "refs/heads/master" in _origin_heads(target)
    proc_run(["bash", "-c", remote_branch_command("b", target)], check=True)


def test_a_branch_with_an_open_pr_refuses_the_launch_before_any_host_call():
    tools, run = fake_tools(
        {"daniel-server": ok("")},
        branch_prs={"worktree-fanout-b": ["https://github.com/o/r/pull/9"]},
    )
    with pytest.raises(LaunchError, match=r"open PR https://github\.com/o/r/pull/9"):
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    assert run.calls == []


def test_only_a_branch_whose_pr_merged_is_deleted_from_origin_at_launch():
    merged = BranchPrs(merged=("https://github.com/o/r/pull/8",))
    tools, run = fake_tools(
        {"daniel-server": ok("")}, branch_prs={"worktree-fanout-b": merged}
    )
    launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    cmd = as_operator(run.calls[0][1])
    delete = "push --quiet origin --delete worktree-fanout-b"
    assert cmd.index("fetch origin") < cmd.index(delete) < cmd.index("worktree add")

    tools, run = fake_tools({"daniel-server": ok("")})
    launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    assert "--delete" not in run.calls[0][1]
    assert "fanout-step: remote branch" in run.calls[0][1]


def test_a_refused_remote_branch_names_the_delete_and_removes_nothing():
    tools, run = fake_tools(
        {
            "daniel-server": subprocess.CompletedProcess(
                [], 1, stdout="", stderr="fanout-step: remote branch\n"
            )
        }
    )
    with pytest.raises(LaunchError, match="git push origin --delete worktree-fanout-b"):
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    assert len(run.calls) == 1
