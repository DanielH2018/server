#!/usr/bin/env python3
"""Tests for block-protected-bash's third arm: a read-only agent's writes are denied (#4617).

Each rule is a `..._is_flagged` / `..._is_clean` pair, as in `test_block_protected_bash.py`,
so a rule that stopped matching fails its own test. The flagged inputs are the writes an
agent reading injected text would be steered into: a redirect into the repo, a commit, a
push, a PR comment.

Run: uv run pytest .claude/hooks/tests/test_block_protected_bash_read_only.py
"""

import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)  # block-protected-bash.py imports _hook_common

_spec = importlib.util.spec_from_file_location(
    "block_protected_bash", os.path.join(_HERE, "block-protected-bash.py")
)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from hooklib.read_only_agents import read_only_agents  # noqa: E402  (after the path insert)

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
    return payload


FLAGGED = [
    "echo x > ansible/roles/k8s/foo/defaults/main.yml",
    "printf x >> CLAUDE.md",
    "sed -i s/a/b/ README.md",
    "git log -1 && tee notes.md",
    "rm -rf ansible/roles/k8s/docs",
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
    'python3 -c \'open("x", "w")\'',
    "uv run pytest ansible/tests/repo -q",
    "cp ansible/vars/secrets.yml /tmp/copy.yml",
    # A write inside a substitution or an ssh remote command.
    "cat $(rm -rf x)",
    "ssh daniel-server 'rm -rf /srv/x'",
]

CLEAN = [
    "git log --oneline -5 -- ansible/roles/k8s/docs",
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
    "grep -rn foo ansible/ &>/dev/null",
    "echo '{}' > /tmp/payload.json && cat /tmp/payload.json",
    "timeout 5 git log -1",
    "uv run python scripts/diagnostics/probe.py health docs",
    "uv run python scripts/dev/fact_status.py lint",
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


@pytest.mark.parametrize("command", CLEAN)
def test_a_read_only_agents_read_is_clean(command):
    assert _mod.decision(_payload(command)) is None, f"should not act on: {command}"


@pytest.mark.parametrize("agent", [None, "home-assistant-engineer", "general-purpose"])
def test_the_same_write_from_a_writing_agent_is_clean(agent):
    """The arm keys on the agent, so the main session and a read+write agent keep writing."""
    assert _mod.decision(_payload("git commit -am x", agent=agent)) is None


def test_a_read_only_agents_write_to_a_protected_file_is_denied_not_asked():
    """Arm 1 would ask for this write; the read-only arm runs first and denies it."""
    assert _mod.decision(_payload("echo x >> ansible/vars/secrets.yml"))[0] == "deny"


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


def test_a_scratch_symlink_into_the_repo_is_not_scratch(tmp_path):
    """A redirect into a /tmp link that points at the tree writes the tree."""
    link = tmp_path / "link"
    link.symlink_to(os.path.join(_REPO, "CLAUDE.md"))
    verdict = _mod.decision(_payload(f"echo x > {link}"))
    assert verdict and verdict[0] == "deny"
