#!/usr/bin/env python3
"""The agent user signs and pushes as its own GitHub account, and only with its own token.

`roles/setup/claude_code/tasks/agent_github.yml` gives `claude` a signing key, a git config and
gh's token for the DanielClaudeBot machine account. Each test pins one leg: a commit that does
not carry the account's noreply address is not attributed to it, an unsigned one never reads
Verified, and a token that reaches anywhere but the agent's 0600 file is one an operator session
or a log could read.

Run: uv run pytest ansible/tests/setup/test_agent_github_identity.py
"""

import re

from _helpers import ANSIBLE
from _setup_render import render_setup_text
from fanout_lib.target import WORKTREE_PREFIX_ENV, branch_name
from lib import yaml_fast
from lib.git_testing import git

TASKS = ANSIBLE / "roles" / "setup" / "claude_code" / "tasks"
TOKEN = "sentinel-agent-gh-token"


def tasks(name: str) -> list[dict]:
    return yaml_fast.safe_load((TASKS / name).read_text())


def named(task_list: list[dict], name: str) -> dict:
    found = [t for t in task_list if t.get("name") == name]
    assert len(found) == 1, f"expected one task named {name!r}, found {len(found)}"
    return found[0]


def test_the_identity_is_given_only_to_an_enabled_agent_user() -> None:
    task = named(tasks("main.yml"), "Give the agent user its GitHub identity")
    assert task["ansible.builtin.import_tasks"] == "agent_github.yml"
    assert task["when"] == "claude_code_agent_user_enabled"


def test_gh_reads_the_token_for_the_agents_own_account() -> None:
    rendered = render_setup_text(
        "claude_code",
        "agent-gh-hosts.yml.j2",
        {"claude_code_agent_gh_token": TOKEN},
    )
    host = yaml_fast.safe_load(rendered)["github.com"]
    assert host["users"] == {"DanielClaudeBot": {"oauth_token": TOKEN}}
    assert host["oauth_token"] == TOKEN
    assert host["user"] == "DanielClaudeBot"
    assert host["git_protocol"] == "https"


def test_the_token_file_is_the_agents_alone_and_never_printed() -> None:
    task = named(
        tasks("agent_github.yml"),
        "Give the agent user gh's token for its GitHub account",
    )
    assert task["ansible.builtin.template"]["mode"] == "0600"
    assert task["diff"] is False and task["no_log"] is True
    gh_dir = named(
        tasks("agent_github.yml"),
        "Create the agent user's ssh and git config directories",
    )
    modes = {i["path"].rsplit("/", 1)[-1]: i["mode"] for i in gh_dir["loop"]}
    assert modes["gh"] == "0700"


def test_the_private_key_is_the_agents_alone_and_the_public_half_readable() -> None:
    """The first apply's chown runs before ssh-keygen, so this task sets both itself."""
    task = named(
        tasks("agent_github.yml"),
        "Give the signing key pair the home's group and its modes",
    )
    f = task["ansible.builtin.file"]
    assert (f["owner"], f["group"]) == (
        "{{ claude_code_agent_user }}",
        "{{ sys_user }}",
    )
    modes = {
        i["path"].removeprefix("{{ claude_code_agent_signing_key }}"): i["mode"]
        for i in task["loop"]
    }
    assert modes == {"": "0600", ".pub": "0644"}


def test_an_enabled_agent_user_without_its_token_fails_the_apply() -> None:
    task = named(
        tasks("agent_github.yml"),
        "Refuse to give the agent user a GitHub identity without its token",
    )
    assert task["ansible.builtin.assert"]["that"] == [
        "claude_code_agent_gh_token | default('') | length > 0"
    ]
    assert task["no_log"] is True


def git_config(rendered: str, tmp_path, key: str) -> list[str]:
    path = tmp_path / "gitconfig"
    path.write_text(rendered)
    # check=False: --get-all exits 1 on a missing key, and the comparison names which one.
    out = git(tmp_path, "config", "--file", str(path), "--get-all", key, check=False)
    return out.stdout.splitlines()


def test_git_signs_every_commit_as_the_machine_account(tmp_path) -> None:
    rendered = render_setup_text("claude_code", "agent-gitconfig.j2")
    expect = {
        "user.name": ["DanielClaudeBot"],
        # The noreply address carries the account's numeric id, which is what GitHub matches.
        "user.email": ["338220904+DanielClaudeBot@users.noreply.github.com"],
        "user.signingkey": ["/var/lib/claude/.ssh/git_signing_ed25519"],
        "gpg.format": ["ssh"],
        "gpg.ssh.allowedsignersfile": ["/var/lib/claude/.config/git/allowed_signers"],
        "commit.gpgsign": ["true"],
        "tag.gpgsign": ["true"],
        "credential.https://github.com.helper": [
            "",
            "!/usr/bin/gh auth git-credential",
        ],
    }
    assert {key: git_config(rendered, tmp_path, key) for key in expect} == expect


def test_switching_the_agent_user_off_removes_its_token() -> None:
    task = named(
        tasks("main.yml"),
        "Remove the agent user's GitHub token when it is switched off",
    )
    assert task["ansible.builtin.file"] == {
        "path": "{{ claude_code_agent_user_home }}/.config/gh/hosts.yml",
        "state": "absent",
    }
    assert task["when"] == "not claude_code_agent_user_enabled"


def enter_worktree_branch(name: str) -> str:
    """The branch EnterWorktree gives a worktree: `/` becomes `+` (observed 2026-10-05)."""
    return "worktree-" + name.replace("/", "+")


def fence_lets_push(branch: str) -> bool:
    """Whether a branch falls in one of the agent branch fence's exclusions."""
    defaults = yaml_fast.safe_load(
        (ANSIBLE / "roles/setup/gitops_deploy/defaults/main.yml").read_text()
    )
    globs = defaults["gitops_deploy_fence_ruleset_exclude"]
    assert globs, "the fence declares no exclusions"
    return any(
        re.fullmatch(re.escape(g).replace(r"\*\*", ".*"), f"refs/heads/{branch}")
        for g in globs
    )


def instructed_worktree_name(rendered: str) -> str:
    """The worktree name the agent's CLAUDE.md tells a session to pass to EnterWorktree."""
    found = re.findall(r"call `EnterWorktree` with the name\s+`([^`]+)`", rendered)
    assert len(found) == 1, f"no single EnterWorktree example in:\n{rendered}"
    return found[0]


def test_the_agents_claude_md_names_worktrees_whose_branches_the_fence_lets_it_push() -> (
    None
):
    task = named(
        tasks("agent_github.yml"),
        "Tell the agent user's sessions the branch names its account may push",
    )
    assert task["ansible.builtin.template"]["dest"] == (
        "{{ claude_code_agent_user_home }}/.claude/CLAUDE.md"
    )
    name = instructed_worktree_name(
        render_setup_text("claude_code", "agent-user-claude-md.j2")
    )
    assert name == "claude/containers-role-cleanup"
    assert fence_lets_push(enter_worktree_branch(name))


def test_a_prefix_outside_the_fence_is_flagged() -> None:
    rendered = render_setup_text(
        "claude_code",
        "agent-user-claude-md.j2",
        {"claude_code_agent_worktree_prefix": "agent"},
    )
    assert not fence_lets_push(
        enter_worktree_branch(instructed_worktree_name(rendered))
    )
    # The bare slug the repo CLAUDE.md asks of the operator's sessions.
    assert not fence_lets_push(enter_worktree_branch("containers-role-cleanup"))


def test_a_fanout_the_agent_launches_names_batch_branches_the_fence_lets_it_push(
    monkeypatch,
) -> None:
    """fanout_place.py names its branches itself, so the profile's prefix must reach it."""
    profile = render_setup_text("claude_code", "agent-user-profile.j2")
    found = re.findall(rf"^export {WORKTREE_PREFIX_ENV}=(\S+)$", profile, re.M)
    assert found == ["claude"], f"no single {WORKTREE_PREFIX_ENV} export in:\n{profile}"
    monkeypatch.setenv(WORKTREE_PREFIX_ENV, found[0])
    assert branch_name("3618") == "worktree-claude+fanout-3618"
    assert fence_lets_push(branch_name("3618"))


def test_a_fanout_without_the_prefix_keeps_the_operators_branch_outside_the_fence(
    monkeypatch,
) -> None:
    monkeypatch.delenv(WORKTREE_PREFIX_ENV, raising=False)
    assert branch_name("3618") == "worktree-fanout-3618"
    assert not fence_lets_push(branch_name("3618"))
