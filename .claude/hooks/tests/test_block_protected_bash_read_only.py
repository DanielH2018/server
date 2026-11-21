#!/usr/bin/env python3
"""Tests for block-protected-bash's third arm: a read-only agent's writes are denied.

Each rule is a `..._is_flagged` / `..._is_clean` pair, as in `test_block_protected_bash.py`,
so a rule that stopped matching fails its own test. The flagged inputs are the writes an
agent reading injected text would be steered into: a redirect into the repo, a commit, a
push, a PR comment.

Run: uv run pytest .claude/hooks/tests/test_block_protected_bash_read_only.py
"""

import importlib.util
import os
import re
import sys

import pytest
from _hook_common import parse_payload
from _row_table import Census, Subject, check, proof_problems, tracked

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)  # block-protected-bash.py imports _hook_common

_spec = importlib.util.spec_from_file_location(
    "block_protected_bash", os.path.join(_HERE, "block-protected-bash.py")
)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from hooklib import read_only_commands  # noqa: E402  (after the path insert)
from hooklib.read_only_agents import read_only_agents, read_only_reason  # noqa: E402

# The agents the census must find. The issue named these six; security-review carries the key
# too, so the key and the "Read-only" description stay one-to-one.
MUST_BE_READ_ONLY = {
    "homelab-backup-observability-reviewer",
    "homelab-cicd-reviewer",
    "homelab-container-reviewer",
    "homelab-docs-freshness-reviewer",
    "homelab-network-diagnostician",
    "skeptic",
    "security-review",
}


def _payload(command, agent="skeptic"):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": _REPO}
    if agent:
        payload["agent_type"] = agent
    return parse_payload(payload).bash()


FLAGGED = [
    "echo x > k8s/roles/foo/defaults/main.yml",
    "printf x >> CLAUDE.md",
    "sed -i s/a/b/ README.md",
    "git log -1 && tee notes.md",
    "rm -rf k8s/roles/docs",
    "mv README.md /tmp/README.md",
    "cp /tmp/payload.py .claude/hooks/block-protected-bash.py",
    "git commit -am 'apply the fix'",
    "git -C . push origin HEAD",
    "git add -A",
    "git fetch origin +master:refs/heads/master",
    "git branch -D master",
    "git tag -a v1 -m release",
    "git -c core.pager=sh log",
    "git diff --output=CLAUDE.md",
    "env GIT_DIR=.git git reset --hard",
    "gh pr comment 12 --body hi",
    "gh pr merge --squash 12",
    "gh --repo o/r issue close 4",
    "gh api repos/o/r/issues/4/comments -f body=hi",
    "gh api -X DELETE repos/o/r/git/refs/heads/x",
    "echo 'unbalanced",
    "echo x > $PWD/CLAUDE.md",
    'echo x >> "$HOME/notes.md"',
    # A nested shell, `eval` or `source` runs text this hook does not judge.
    "bash -c 'rm -rf ansible'",
    "sh -c 'git push'",
    "eval 'git push'",
    "builtin eval 'git push'",
    "source /tmp/x.sh",
    # A wrapper is read past to the command it runs.
    "timeout 5 git push",
    "timeout 5 rm CLAUDE.md",
    "nice -n 5 rm CLAUDE.md",
    "uv run git commit -am x",
    "uv run gh pr comment 1 --body x",
    "echo CLAUDE.md | xargs rm",
    # The noclobber redirect, and a link the same command creates.
    "echo x >| CLAUDE.md",
    "ln -s $PWD/CLAUDE.md /tmp/zz9 && echo x > /tmp/zz9",
    "cd /tmp && echo x > CLAUDE.md",
    # Variables and options that make a read run a command.
    "GIT_PAGER='rm -rf x' git log",
    "GIT_EXTERNAL_DIFF=./evil git diff",
    "git ls-remote --upload-pack='touch /x' .",
    "ssh -o ProxyCommand='rm x' daniel-server true",
    # Writers no denylist named.
    "sort -o CLAUDE.md CLAUDE.md",
    "awk -i inplace 1 CLAUDE.md",
    "awk '{system(\"rm x\")}' CLAUDE.md",
    "sed 's/a/b/w CLAUDE.md' README.md",
    "gzip CLAUDE.md",
    "uv run ruff check --fix .",
    "prek run --all-files",
    "./scripts/deploy.sh --tags docs",
    "uv run python scripts/dev/findings.py open --title x",
    "uv run python scripts/secrets_mgmt/secret_rotation.py sync",
    "uv run python scripts/secrets_mgmt/secret_rotation.py record x 2026-01-01",
    'python3 -c \'open("x", "w")\'',
    "uv run pytest tests/repo -q",
    "cp secrets/secrets.yml /tmp/copy.yml",
    # A write inside a substitution or an ssh remote command.
    "cat $(rm -rf x)",
    "ssh daniel-server 'rm -rf /srv/x'",
    # The redirect check runs before the command allowlist, so an allowed ghread cannot bypass it.
    "ghread pr list --state open > CLAUDE.md",
]

CLEAN = [
    "git log --oneline -5 -- k8s/roles/docs",
    "git blame -L 1,20 CLAUDE.md 2>/dev/null",
    "git -C /home/ubuntu/server show HEAD:CLAUDE.md | head",
    "git branch -a",
    "git tag -l 'v*'",
    "git branch --contains abc123",
    "git worktree list",
    "git config --get remote.origin.url",
    "gh pr list --state open",
    "gh pr diff 12 | grep -n foo",
    "gh --repo o/r issue view 4",
    "gh api repos/o/r/pulls/12",
    "ghread pr list --state open",
    "ghread search prs --owner o -- networkpolicy | head",
    "ghread run logs 4567 --failed --out /tmp/run.log",
    "grep -rn foo ansible/ &>/dev/null",
    "echo '{}' > /tmp/payload.json && cat /tmp/payload.json",
    "timeout 5 git log -1",
    "uv run python scripts/diagnostics/probe.py health docs",
    "uv run python scripts/dev/fact_status.py lint",
    "uv run python scripts/dev/fact_status.py notice --since origin/master",
    "uv run python scripts/dev/fact_status.py report --json --no-replay",
    "uv run python scripts/secrets_mgmt/secret_rotation.py audit",
    "uv run python scripts/secrets_mgmt/secret_rotation.py consumers cloudflare_api_token",
    "rootq ufw",
    "rootq-socket nft",
    "ssh daniel-server rootq nft",
    "ssh daniel-server 'journalctl -u k3s -n 50 | grep -i error'",
    "for f in a b; do cat $f; done",
    "cat $(git rev-parse --show-toplevel)/CLAUDE.md",
    "kubectl get pods -n homelab -o wide",
    "sed -n 1,20p CLAUDE.md",
    "awk '{print $1}' CLAUDE.md | sort | uniq -c",
    "find ansible -name '*.yml' | xargs grep -l image",
    "command -v jq",
    "LC_ALL=C sort CLAUDE.md",
    "echo $HOME 2>&1 | head",
]


@pytest.mark.parametrize("command", FLAGGED)
def test_a_read_only_agents_write_is_flagged(command):
    verdict = _mod.decision(_payload(command))
    assert verdict and verdict[0] == "deny", f"should deny: {command}"
    assert "`skeptic` is read-only" in verdict[1]


@pytest.mark.parametrize(
    "command",
    [
        "ghread run logs 4567 --failed --out CLAUDE.md",
        "ghread pr diff 12 --out=/home/ubuntu/server/CLAUDE.md",
    ],
)
def test_a_ghread_out_outside_scratch_is_flagged(command):
    verdict = _mod.decision(_payload(command))
    assert verdict and verdict[0] == "deny", f"should deny: {command}"
    assert "writes `ghread --out`" in verdict[1]


@pytest.mark.parametrize("command", CLEAN)
def test_a_read_only_agents_read_is_clean(command):
    assert _mod.decision(_payload(command)) is None, f"should not act on: {command}"


@pytest.mark.parametrize("agent", [None, "home-assistant-engineer", "general-purpose"])
def test_the_same_write_from_a_writing_agent_is_clean(agent):
    """The arm keys on the agent, so the main session and a read+write agent keep writing."""
    assert _mod.decision(_payload("git commit -am x", agent=agent)) is None


def test_a_read_only_agents_write_to_a_protected_file_is_denied_not_asked():
    """Arm 1 would ask for this write; the read-only arm runs first and denies it."""
    assert _mod.decision(_payload("echo x >> secrets/secrets.yml"))[0] == "deny"


def test_the_set_is_derived_from_the_frontmatter_key(tmp_path):
    """`name:` decides, not the filename, and an agent without the key is left out."""
    (tmp_path / "a.md").write_text("---\nname: reader\nread_only: true\n---\nbody\n")
    (tmp_path / "b.md").write_text(
        "---\nname: writer\ntools: Bash\n---\nread_only: true\n"
    )
    assert read_only_agents(str(tmp_path)) == {"reader"}


def test_a_missing_agents_directory_gives_the_empty_set(tmp_path):
    assert read_only_agents(str(tmp_path / "absent")) == set()


def test_every_read_only_agent_in_the_tree_carries_the_key():
    """Named members first, then every agent whose description claims read-only."""
    derived = read_only_agents()
    assert MUST_BE_READ_ONLY <= derived, MUST_BE_READ_ONLY - derived
    agents_dir = os.path.join(_REPO, ".claude", "agents")
    claimed = set()
    for entry in os.listdir(agents_dir):
        if not entry.endswith(".md"):
            continue
        with open(os.path.join(agents_dir, entry)) as agent:
            head = agent.read().split("\n---", 1)[0]
        if "read-only" in head.lower():
            claimed.add(entry.removesuffix(".md"))
    assert claimed, "no agent describes itself as read-only: the census found nothing"
    assert claimed <= derived, (
        f"read-only in prose but not in frontmatter: {claimed - derived}"
    )


_SPAN = re.compile(r"`([^`\n]+)`(\s*\(refused)?")
_PLACEHOLDER = re.compile(r"<[^<>\s]+>")
_NAME = re.compile(r"^name:\s*(\S+)", re.M)
# Commands the `checks` table does not hold that an agent file might still tell its agent to run.
_OTHER_COMMANDS = {"git", "gh", "ssh", "uv", "python", "python3", "sudo", "doas"}
_OTHER_COMMANDS |= {"pytest", "prek", "ansible-playbook"}
_COMMAND_WORDS = _OTHER_COMMANDS | set(read_only_commands.checks(lambda _path: False))


def _is_command(span):
    words = span.split()
    if len(words) < 2:
        return False
    first = words[0]
    return (
        first in _COMMAND_WORDS
        or first.startswith("./")
        or (first.startswith("scripts/") and first.endswith(".py"))
    )


def _unrunnable_commands(subject):
    """Each backticked command the hook refuses unmarked, or that a `(refused` mark misnames."""
    hits = []
    for n, line in enumerate(subject.text.splitlines(), 1):
        for span, marked in _SPAN.findall(line):
            if not _is_command(span):
                continue
            command = _PLACEHOLDER.sub("x", span)
            refusal = read_only_reason(command, _REPO, _mod.written_paths)
            if refusal and not marked:
                hits.append(f"line {n}: `{span}` {refusal}")
            elif marked and not refusal:
                hits.append(
                    f"line {n}: `{span}` is marked refused, but the hook allows it"
                )
    return hits


def _read_only_agent_files():
    names = read_only_agents()
    return [
        rel
        for rel in tracked(".claude/agents/*.md")
        if (found := _NAME.search(Subject(rel).text)) and found.group(1) in names
    ]


READ_ONLY_AGENTS_RUN_ONLY_WHAT_THE_HOOK_ALLOWS = Census(
    name="read-only-agents-run-only-what-the-hook-allows",
    reason=(
        "A read-only agent's Bash goes through `read_only_reason`, so a command its file "
        "tells it to run but the allowlist refuses costs the agent that step of its review. "
        "Add the command to `read_only_commands.py` when it only reads, or cut it from the "
        "agent file. A command named only to say it is off-limits carries `(refused` after "
        "its span."
    ),
    files=_read_only_agent_files,
    offence=_unrunnable_commands,
    red=(
        Subject("a.md", "- `sudo ufw status verbose` — firewall path.\n"),
        Subject("a.md", "- `uv run pytest <suite>` (read-only test run)\n"),
        Subject("a.md", "- `rootq ufw` (refused: no)\n"),
    ),
    green=(
        Subject("a.md", "- `rootq ufw` / `kubectl get pods -n <ns>`\n"),
        Subject("a.md", "CI runs `prek run --all-files` (refused to you).\n"),
        Subject(
            "a.md", "`scripts/validate/render_templates.py` and `read_only: true`\n"
        ),
    ),
    must_find=frozenset(
        {
            ".claude/agents/homelab-cicd-reviewer.md",
            ".claude/agents/homelab-network-diagnostician.md",
            ".claude/agents/homelab-docs-freshness-reviewer.md",
        }
    ),
)


def test_read_only_agents_run_only_what_the_hook_allows():
    assert check(READ_ONLY_AGENTS_RUN_ONLY_WHAT_THE_HOOK_ALLOWS) == []


def test_the_read_only_agent_command_row_can_go_red():
    assert proof_problems(READ_ONLY_AGENTS_RUN_ONLY_WHAT_THE_HOOK_ALLOWS) == []


def test_a_scratch_symlink_into_the_repo_is_not_scratch(tmp_path):
    """A redirect into a /tmp link that points at the tree writes the tree."""
    link = tmp_path / "link"
    link.symlink_to(os.path.join(_REPO, "CLAUDE.md"))
    verdict = _mod.decision(_payload(f"echo x > {link}"))
    assert verdict and verdict[0] == "deny"
