"""A batch launched in another repo's register — `launch --repo`, through `fanout_lib.target`.

The dotfiles repo is the one such register. Its tree, its unit, its system prompt and its
claim each differ from this repo's, and the claim has to fall between the tree and the agent.

Run: uv run pytest scripts/dev/tests/test_fanout_target.py
"""

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from fanout_lib.launch import (
    SYSTEM_PROMPT_FILE,
    LaunchError,
    create_worktree_command,
    exclude_fanout_command,
    launch,
    snapshot_command,
    systemd_run_command,
    unit_name,
)
from fanout_lib.target import SERVER_TARGET, Target, resolve, server_checkout
from lib.git_testing import commit, git, init_repo, scrub_process_git_env
from lib.proc_testing import run as proc_run
from lib.repo_paths import REPO as REPO_ROOT
from _fanout_fakes import fake_tools, ok

DOTFILES = Target(
    "DanielH2018/dotfiles", "/home/ubuntu/.local/share/chezmoi", "origin/main"
)
DOT_WT = "/home/ubuntu/.local/share/chezmoi/.claude/worktrees/fanout-763"
SNAPSHOT = f"{DOT_WT}/.fanout/server"


def test_a_register_findings_can_judge_resolves_to_its_checkout_and_default_branch():
    target = resolve("DanielH2018/dotfiles", lambda checkout: "origin/main")
    assert target.checkout == str(Path.home() / ".local/share/chezmoi")
    assert target.base == "origin/main" and target.base_branch == "main"
    assert resolve("DanielH2018/server") is SERVER_TARGET


def test_the_server_checkout_is_the_agent_users_clone_when_its_profile_names_one():
    """The agent user cannot read /home/ubuntu; its profile exports RUN_HOOK_PROJECT_DIR."""
    clone = {"RUN_HOOK_PROJECT_DIR": "/var/lib/claude/server"}
    assert server_checkout(clone) == "/var/lib/claude/server"
    assert server_checkout({}) == "/home/ubuntu/server"
    assert server_checkout({"RUN_HOOK_PROJECT_DIR": ""}) == "/home/ubuntu/server"


def test_a_repo_with_no_register_or_no_default_branch_is_refused():
    with pytest.raises(ValueError, match="not a register"):
        resolve("DanielH2018/elsewhere", lambda checkout: "origin/main")
    with pytest.raises(ValueError, match="no remote default branch"):
        resolve("DanielH2018/dotfiles", lambda checkout: None)


def test_a_dotfiles_tree_comes_from_its_own_checkout_and_ignores_fanout():
    """The flagged half is the dotfiles tree; this repo's command keeps the exclude out."""
    cmd = create_worktree_command("763", DOTFILES)
    assert (
        "git -C /home/ubuntu/.local/share/chezmoi worktree add -b worktree-fanout-763 "
        f"{DOT_WT} origin/main" in cmd
    )
    assert "/home/ubuntu/server" not in cmd
    assert cmd.index("fanout-step: exclude") < cmd.index("worktree add")
    server = create_worktree_command("763")
    assert "info/exclude" not in server


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


def test_a_dotfiles_unit_reads_the_system_prompt_from_its_snapshot_of_this_repo():
    cmd = systemd_run_command("763", DOTFILES)
    prompt = f"{SNAPSHOT}/{SYSTEM_PROMPT_FILE}"
    assert f"--append-system-prompt-file {prompt}" in cmd
    assert f"-p WorkingDirectory={DOT_WT} " in cmd
    assert (REPO_ROOT / SYSTEM_PROMPT_FILE).is_file()


def _settings_arg(cmd: str) -> dict | None:
    """The JSON `--settings` value in a systemd-run line, as the shell would hand it over."""
    argv = shlex.split(cmd)
    if "--settings" not in argv:
        return None
    return json.loads(argv[argv.index("--settings") + 1])


def test_a_dotfiles_unit_registers_the_fanout_stop_hook_through_settings():
    """The dotfiles worktree has no `.claude/settings.json`, so the hook must ride the argv."""
    settings = _settings_arg(systemd_run_command("763", DOTFILES))
    assert settings is not None
    [stop] = settings["hooks"]["Stop"]
    [hook] = stop["hooks"]
    assert hook["command"] == f"{SNAPSHOT}/.claude/hooks/run-hook.sh fanout-stop"
    assert (REPO_ROOT / ".claude" / "hooks" / "fanout-stop.py").is_file()


def test_a_server_unit_takes_the_hook_from_its_project_settings_not_the_argv():
    """A second registration would fire the hook twice per stop and halve its block cap."""
    assert _settings_arg(systemd_run_command("763", SERVER_TARGET)) is None
    project = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    commands = [h["command"] for s in project["hooks"]["Stop"] for h in s["hooks"]]
    assert any(c.endswith("run-hook.sh fanout-stop") for c in commands)


def test_a_dotfiles_launch_claims_between_the_tree_and_the_agent():
    tools, run = fake_tools({"daniel-box": ok("")})
    batch = launch(tools, "daniel-box", "763", "BRIEF", [763], DOTFILES)
    hosts = [host for host, _, _ in run.calls]
    assert hosts == ["daniel-box", "findings", "daniel-box"]
    prepare, claim, start = (cmd for _, cmd, _ in run.calls)
    assert "worktree lock" in prepare and "systemd-run" not in prepare
    assert prepare.index("worktree lock") < prepare.index(
        "fanout-step: server snapshot"
    )
    assert prepare.index("server snapshot") < prepare.index("brief.md")
    assert run.calls[0][2] == "BRIEF"
    assert claim == (
        "claim 763 --worktree worktree-fanout-763 --repo DanielH2018/dotfiles"
    )
    assert start.startswith("systemd-run --user --unit fanout-dotfiles-763 ")
    assert batch.repo == "DanielH2018/dotfiles" and batch.worktree == DOT_WT


def test_a_failed_systemd_run_after_the_claim_gives_the_claim_back():
    """The flagged half of `test_a_dotfiles_launch_claims_between_the_tree_and_the_agent`."""
    failed = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="Failed to start\nfanout-step: systemd-run\n"
    )
    tools, run = fake_tools({"daniel-box": ok("")})
    run.answers_by_call = [ok(""), ok(""), failed]
    with pytest.raises(LaunchError, match="systemd-run failed"):
        launch(tools, "daniel-box", "763", "BRIEF", [763], DOTFILES)
    assert [host for host, _, _ in run.calls] == [
        "daniel-box",
        "findings",
        "daniel-box",
        "findings",
    ]
    assert run.calls[3][1].startswith("release 763 --worktree worktree-fanout-763 ")


def test_a_refused_dotfiles_claim_releases_removes_the_tree_and_starts_nothing():
    tools, run = fake_tools({"daniel-box": ok("")}, findings_exit=3)
    with pytest.raises(LaunchError, match=r"claim refused \(3\)"):
        launch(tools, "daniel-box", "763", "BRIEF", [763], DOTFILES)
    commands = [cmd for _, cmd, _ in run.calls]
    assert not any("systemd-run" in c for c in commands)
    assert commands[2].startswith("release 763 --worktree worktree-fanout-763 ")
    assert f"worktree remove --force {DOT_WT}" in commands[3]


def test_a_dotfiles_review_unit_runs_its_snapshots_script():
    """The dotfiles worktree carries no `fanout_review.py`; the server batch's own test is
    `test_a_server_review_unit_runs_the_worktrees_script_not_the_primary_checkouts`."""
    cmd = systemd_run_command("763", DOTFILES, review=True)
    assert f" {SNAPSHOT}/scripts/dev/fanout_review.py --batch 763 " in cmd
    assert "/home/ubuntu/server/" not in cmd


def test_the_snapshot_holds_origin_master_while_the_primary_checkout_lags(
    tmp_path, monkeypatch
):
    """#3762: a dotfiles batch ran a lagging checkout's review pipeline and reported nothing."""
    scrub_process_git_env(monkeypatch)
    origin = init_repo(tmp_path / "origin.git", bare=True)
    server = tmp_path / "server"
    git(tmp_path, "clone", "-q", str(origin), str(server))
    script = "scripts/dev/fanout_review.py"
    files = {script: "old\n", ".claude/hooks/fanout-stop.py": "hook\n", "README": "x"}
    commit(server, "old", **files)
    git(server, "push", "-q", "origin", "master")
    upstream = tmp_path / "upstream"
    git(tmp_path, "clone", "-q", str(origin), str(upstream))
    commit(upstream, "new", **{script: "new\n"})
    git(upstream, "push", "-q", "origin", "master")
    target = Target("DanielH2018/dotfiles", str(tmp_path / "dot"), "origin/main")
    wt = Path(target.checkout) / ".claude/worktrees/fanout-763"
    wt.mkdir(parents=True)

    proc_run(["bash", "-c", snapshot_command("763", target, str(server))], check=True)

    assert (server / script).read_text() == "old\n"  # the lagging checkout, untouched
    snap = wt / ".fanout" / "server"
    assert (snap / script).read_text() == "new\n"
    assert (snap / ".claude/hooks/fanout-stop.py").read_text() == "hook\n"
    assert not (snap / "README").exists()
    assert sorted(p.name for p in (wt / ".fanout").iterdir()) == ["server"]


def test_a_failed_snapshot_removes_the_tree_before_any_claim():
    failed = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="fatal: bad object\nfanout-step: server snapshot\n"
    )
    tools, run = fake_tools({"daniel-box": ok("")})
    run.answers_by_call = [failed, ok("")]
    with pytest.raises(LaunchError, match="server snapshot failed"):
        launch(tools, "daniel-box", "763", "BRIEF", [763], DOTFILES, review=True)
    commands = [cmd for _, cmd, _ in run.calls]
    assert len(commands) == 2 and not any(c.startswith("claim") for c in commands)
    assert f"worktree remove --force {DOT_WT}" in commands[1]


def test_a_server_launch_takes_no_snapshot():
    """Its worktree is already a checkout of origin/master."""
    tools, run = fake_tools({"daniel-box": ok("")})
    launch(tools, "daniel-box", "763", "BRIEF", [763])
    assert "server snapshot" not in run.calls[0][1]
