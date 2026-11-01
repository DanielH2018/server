#!/usr/bin/env python3
"""The agent user signs and pushes as its own GitHub account, and only with its own token.

The claude-code host component gives `claude` a signing key, a git config and
gh's token for the DanielClaudeBot machine account. Each test pins one leg: a commit that does
not carry the account's noreply address is not attributed to it, an unsigned one never reads
Verified, and a token that reaches anywhere but the agent's 0600 file is one an operator session
or a log could read.

Run: uv run pytest ansible/tests/setup/test_agent_github_identity.py
"""

import re

from _agent_package import agent_setup_script, render_daniel_server_with
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from fanout_lib.target import branch_name
from lib.worktree_owner import WORKTREE_PREFIX_ENV
from lib import yaml_fast
from lib.git_testing import git

COMPONENT = ANSIBLE.parent / "hosts" / "components" / "claude-code" / "component.yml"
TOKEN = "sentinel-agent-gh-token"


def test_gh_reads_the_token_for_the_agents_own_account() -> None:
    rendered = render_setup_text(
        "claude_code",
        "agent-gh-hosts.yml.j2",
        {"claude_code_agent_gh_token_value": TOKEN},
    )
    host = yaml_fast.safe_load(rendered)["github.com"]
    assert host["users"] == {"DanielClaudeBot": {"oauth_token": TOKEN}}
    assert host["oauth_token"] == TOKEN
    assert host["user"] == "DanielClaudeBot"
    assert host["git_protocol"] == "https"


def test_the_token_file_is_the_agents_alone_and_the_operator_only_traverses_its_directory(
    tmp_path,
) -> None:
    component = yaml_fast.safe_load(COMPONENT.read_text())
    (output,) = [o for o in component["outputs"] if o["path"].endswith("gh/hosts.yml")]
    assert (output["owner"], output["group"], output["mode"]) == (
        "claude",
        "ubuntu",
        "0600",
    )
    assert output["keys"] == ["claude_code_agent_gh_token"]
    # The operator's group traverses to stat the token file (#6487); it never lists or reads.
    for flag, mode in ((True, "0710"), (False, "0700")):
        setup = agent_setup_script(
            tmp_path / str(flag), claude_code_agent_operator_read=flag
        )
        assert f'dir {mode} "$home/.config/gh"' in setup


def test_the_private_key_is_the_agents_alone_and_the_public_half_readable(
    tmp_path,
) -> None:
    """The key pair is made as the agent, then given its modes in the same script."""
    package = render_daniel_server_with(tmp_path)
    home = package.files["/usr/lib/homelab/claude-agent-home"][0].decode()
    assert '  as_agent chmod 0600 "$1"\n  as_agent chmod 0644 "$1.pub"\n' in home


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


def test_the_agents_claude_md_names_worktrees_whose_branches_the_fence_lets_it_push(
    tmp_path,
) -> None:
    setup = agent_setup_script(tmp_path)
    assert "put 0644 CLAUDE.md .claude/CLAUDE.md" in setup
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
    """`fanout.py place` names its branches itself, so the profile's prefix must reach it."""
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
