#!/usr/bin/env python3
"""The agent user signs and pushes as its own GitHub account, and only with its own token.

`roles/setup/claude_code/tasks/agent_github.yml` gives `claude` a signing key, a git config and
gh's token for the DanielClaudeBot machine account. Each test pins one leg: a commit that does
not carry the account's noreply address is not attributed to it, an unsigned one never reads
Verified, and a token that reaches anywhere but the agent's 0600 file is one an operator session
or a log could read.

Run: uv run pytest ansible/tests/setup/test_agent_github_identity.py
"""

import subprocess

from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast

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
    out = subprocess.run(
        ["git", "config", "--file", str(path), "--get-all", key],
        capture_output=True,
        text=True,
    )
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
