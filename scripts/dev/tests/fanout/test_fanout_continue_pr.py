"""A launch whose batch branch has an open PR finishes that PR instead of starting over.

Run: uv run pytest scripts/dev/tests/fanout/test_fanout_continue_pr.py
"""

import pytest

from dev.fanout_lib.branch_pr import OpenPr
from dev.fanout_lib.launch import worktree_add_command, worktree_path
from dev.fanout_lib.place import main
from dev.fanout_lib.target import Target
from lib.git_testing import commit, git_out, init_repo, scrub_process_git_env
from lib.proc_testing import run as proc_run
from _fanout_fakes import HOST_KEY, fake_tools, labelled_issue, ok

HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"
CLAIMED = [labelled_issue(1, "one")]


def _launch(tools, tmp_path, *args):
    return main(["launch", *args, "--manifest-root", str(tmp_path)], tools)


@pytest.mark.parametrize("adopt", [True, False])
def test_an_adopted_worktree_checks_out_the_open_prs_head_and_tracks_it(
    tmp_path, monkeypatch, adopt
):
    scrub_process_git_env(monkeypatch)
    origin = init_repo(tmp_path / "origin", initial_commit="base")
    base = git_out(origin, "rev-parse", "HEAD")
    proc_run(["git", "-C", str(origin), "checkout", "-q", "-b", "worktree-fanout-7"])
    head = commit(origin, "the abandoned batch's work", work="x")
    proc_run(["git", "-C", str(origin), "checkout", "-q", "master"])
    checkout = tmp_path / "checkout"
    proc_run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    target = Target("DanielH2018/dotfiles", str(checkout), "origin/master")
    proc_run(["bash", "-c", worktree_add_command("7", target, adopt)], check=True)
    tree = worktree_path("7", target)
    assert git_out(tree, "rev-parse", "HEAD") == (head if adopt else base)
    upstream = git_out(tree, "rev-parse", "--abbrev-ref", "@{u}", check=False)
    assert (upstream == "origin/worktree-fanout-7") is adopt


ABANDONED_PR = "https://github.com/DanielH2018/server/pull/9736"


def test_a_batch_whose_branch_has_an_open_pr_continues_it(tmp_path):
    pr = OpenPr(ABANDONED_PR, draft=True, failing=("pytest https://ci/1",))
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)},
        issues=CLAIMED,
        continued_prs={"worktree-fanout-1": pr},
    )
    assert _launch(tools, tmp_path, "--batch", "1", "--host", "daniel-box") == 0
    (_, command, brief) = run.host_calls[-1]
    assert brief is not None
    tree = worktree_path("1")
    assert (
        f"worktree add -b worktree-fanout-1 --track {tree} origin/worktree-fanout-1"
        in command
    )
    assert "fanout-step: remote branch" not in command
    assert ABANDONED_PR in brief and "pytest https://ci/1" in brief


def test_an_unreadable_open_pr_launches_nothing_and_releases_the_claim(
    tmp_path, capsys
):
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)},
        issues=CLAIMED,
        continued_prs={"worktree-fanout-1": "`gh pr list` timed out"},
    )
    assert _launch(tools, tmp_path, "--batch", "1", "--host", "daniel-box") == 1
    assert len(run.host_calls) == 2  # headroom and health reads; no launch call
    assert any(c[1].startswith("release 1") for c in run.calls if c[0] == "findings")
    assert "could not read the open PR of worktree-fanout-1" in capsys.readouterr().err
