"""A batch launched in another repo's register — `launch --repo`, through `fanout_lib.target`.

The dotfiles repo is the one such register. Its tree, its unit, its system prompt and its
claim each differ from this repo's, and the claim has to fall between the tree and the agent.

Run: uv run pytest scripts/dev/tests/test_fanout_target.py
"""

from pathlib import Path

import pytest

from fanout_lib.launch import (
    SYSTEM_PROMPT_FILE,
    LaunchError,
    create_worktree_command,
    exclude_fanout_command,
    launch,
    systemd_run_command,
    unit_name,
)
from fanout_lib.target import SERVER_TARGET, Target, resolve
from lib.git_testing import git, init_repo, scrub_process_git_env
from lib.proc_testing import run as proc_run
from lib.repo_paths import REPO as REPO_ROOT
from _fanout_fakes import fake_tools, ok

DOTFILES = Target(
    "DanielH2018/dotfiles", "/home/ubuntu/.local/share/chezmoi", "origin/main"
)
DOT_WT = "/home/ubuntu/.local/share/chezmoi/.claude/worktrees/fanout-763"


def test_a_register_findings_can_judge_resolves_to_its_checkout_and_default_branch():
    target = resolve("DanielH2018/dotfiles", lambda checkout: "origin/main")
    assert target.checkout == str(Path.home() / ".local/share/chezmoi")
    assert target.base == "origin/main" and target.base_branch == "main"
    assert resolve("DanielH2018/server") is SERVER_TARGET


def test_a_repo_with_no_register_or_no_default_branch_is_refused():
    with pytest.raises(ValueError, match="not a register"):
        resolve("DanielH2018/elsewhere", lambda checkout: "origin/main")
    with pytest.raises(ValueError, match="no remote default branch"):
        resolve("DanielH2018/dotfiles", lambda checkout: None)


def test_a_dotfiles_tree_comes_from_its_own_checkout_and_ignores_fanout():
    """The flagged half is the dotfiles tree; this repo's command keeps neither change."""
    cmd = create_worktree_command("763", "daniel-server", DOTFILES)
    assert (
        "git -C /home/ubuntu/.local/share/chezmoi worktree add -b worktree-fanout-763 "
        f"{DOT_WT} origin/main" in cmd
    )
    assert "/home/ubuntu/server" not in cmd
    # No primary fast-forward even on a host with no tick: `bin/land-sync` owns that `main`.
    assert "merge --ff-only" not in cmd
    assert cmd.index("fanout-step: exclude") < cmd.index("worktree add")
    server = create_worktree_command("763", "daniel-server")
    assert "info/exclude" not in server and "merge --ff-only origin/master" in server


def test_the_exclude_step_hides_fanout_in_a_linked_worktree_and_appends_once(
    tmp_path, monkeypatch
):
    scrub_process_git_env(monkeypatch)
    repo = tmp_path / "repo"
    init_repo(repo, initial_commit="init")
    target = Target("DanielH2018/dotfiles", str(repo), "origin/main")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "wt", str(wt))
    (wt / ".fanout").mkdir()
    (wt / ".fanout" / "brief.md").write_text("BRIEF")
    assert (
        ".fanout/" in git(wt, "status", "--porcelain").stdout
    )  # the unexcluded control
    for _ in range(2):
        proc_run(["bash", "-c", exclude_fanout_command(target)], check=True)
    assert git(wt, "status", "--porcelain").stdout == ""
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(".fanout/") == 1


def test_one_batch_id_gets_a_distinct_unit_in_each_repo():
    assert unit_name("763") == "fanout-763"
    assert unit_name("763", DOTFILES) == "fanout-dotfiles-763"


def test_a_dotfiles_unit_reads_the_system_prompt_from_this_repos_checkout():
    cmd = systemd_run_command("763", DOTFILES)
    prompt = f"/home/ubuntu/server/{SYSTEM_PROMPT_FILE}"
    assert f"--append-system-prompt-file {prompt}" in cmd
    assert f"-p WorkingDirectory={DOT_WT} " in cmd
    assert (REPO_ROOT / SYSTEM_PROMPT_FILE).is_file()


def test_a_dotfiles_launch_claims_between_the_tree_and_the_agent():
    tools, run = fake_tools({"daniel-box": ok("")})
    batch = launch(tools, "daniel-box", "763", "BRIEF", [763], DOTFILES)
    hosts = [host for host, _, _ in run.calls]
    assert hosts == ["daniel-box", "findings", "daniel-box"]
    prepare, claim, start = (cmd for _, cmd, _ in run.calls)
    assert "worktree lock" in prepare and "systemd-run" not in prepare
    assert run.calls[0][2] == "BRIEF"
    assert claim == (
        "claim 763 --worktree worktree-fanout-763 --repo DanielH2018/dotfiles"
    )
    assert start.startswith("systemd-run --user --unit fanout-dotfiles-763 ")
    assert batch.repo == "DanielH2018/dotfiles" and batch.worktree == DOT_WT


def test_a_refused_dotfiles_claim_releases_removes_the_tree_and_starts_nothing():
    tools, run = fake_tools({"daniel-box": ok("")}, findings_exit=3)
    with pytest.raises(LaunchError, match=r"claim refused \(3\)"):
        launch(tools, "daniel-box", "763", "BRIEF", [763], DOTFILES)
    commands = [cmd for _, cmd, _ in run.calls]
    assert not any("systemd-run" in c for c in commands)
    assert commands[2].startswith("release 763 --worktree worktree-fanout-763 ")
    assert f"worktree remove --force {DOT_WT}" in commands[3]
