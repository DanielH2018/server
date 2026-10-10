#!/usr/bin/env python3
"""The agent user takes the operator's dotfiles through chezmoi, and only their agent variant.

claude-dotfiles-sync.sh pulls the agent's chezmoi source and applies it as the agent. Two
properties carry the safety. A source that does not render the dotfiles' `is-agent` template
as true would hand the agent the operator's .gitconfig, so the script refuses to apply it. And
a plain apply chmods ~/.claude to 0700, which cuts the operator off from the agent's memory
store and artifacts, so the script applies every directory but ~/.claude first and everything
else without directories second. The script runs here against real scratch repositories and a
chezmoi stand-in that records each call.

Run: uv run pytest ansible/tests/setup/test_agent_dotfiles.py
"""

import subprocess
from pathlib import Path

import pytest
from _helpers import ANSIBLE, HOST_VARS, load_yaml
from _setup_render import render_setup_text
from lib import yaml_fast
from lib.git_testing import commit, git, init_repo, scrubbed_env
from lib.proc_testing import run, write_exec

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
SCRIPT = ROLE / "files" / "claude-dotfiles-sync.sh"
TASKS = ROLE / "tasks"

# Answers the subcommands the script uses. `managed` lists ~/.claude itself, which the first
# pass must leave out, and two directories it must keep.
CHEZMOI_STUB = """\
log="$STUB_CALLS"
case "$1" in
  source-path) echo "$STUB_SOURCE/home" ;;
  init) echo "init agent=$(grep -c 'agent = true' "$HOME/.config/chezmoi/chezmoi.toml")" >> "$log" ;;
  execute-template) printf '%s' "$STUB_IS_AGENT" ;;
  managed) printf '%s\\n' "$HOME/.claude" "$HOME/.claude/skills" "$HOME/.config/nvim" ;;
  apply) echo "$*" >> "$log" ;;
  *) echo "unexpected: $*" >> "$log"; exit 2 ;;
esac
"""


class Agent:
    """A dotfiles origin, the agent's chezmoi source cloned from it, and the agent's home."""

    def __init__(self, root: Path) -> None:
        self.origin = init_repo(root / "origin.git", bare=True)
        self.pusher = init_repo(root / "pusher")
        commit(self.pusher, "seed", **{"home/dot_bashrc": "1\n"})
        git(self.pusher, "remote", "add", "origin", str(self.origin))
        git(self.pusher, "push", "-q", "origin", "master")
        self.source = root / "source"
        git(root, "clone", "-q", str(self.origin), str(self.source))
        self.home = root / "home"
        (self.home / ".config" / "chezmoi").mkdir(parents=True)
        self.config = self.home / ".config" / "chezmoi" / "chezmoi.toml"
        self.config.write_text("[data]\n    work = false\n")
        self.seed = self.home / ".config" / "chezmoi" / "seed.toml"
        self.seed.write_text("[data]\n    work = false\n    agent = true\n")
        self.chezmoi = root / "chezmoi"
        write_exec(self.chezmoi, CHEZMOI_STUB)
        self.calls = root / "calls.log"

    def push(self) -> None:
        commit(self.pusher, "upstream", **{"home/dot_bashrc": "2\n"})
        git(self.pusher, "push", "-q", "origin", "master")

    def sync(self, is_agent: str = "true") -> subprocess.CompletedProcess[str]:
        return run(
            ["bash", str(SCRIPT), str(self.chezmoi), str(self.seed)],
            env=scrubbed_env(
                HOME=str(self.home),
                USER="claude",
                STUB_CALLS=str(self.calls),
                STUB_SOURCE=str(self.source),
                STUB_IS_AGENT=is_agent,
            ),
        )

    def log(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def agent(tmp_path: Path) -> Agent:
    return Agent(tmp_path)


def test_the_agent_variant_is_applied_in_two_passes_that_spare_the_claude_dir(
    agent: Agent,
) -> None:
    result = agent.sync()
    assert result.returncode == 0, result.stderr
    home = agent.home
    assert agent.log() == [
        "init agent=1",
        f"apply --force --no-tty --include=dirs {home}/.claude/skills {home}/.config/nvim",
        "apply --force --no-tty --exclude=dirs",
    ]
    assert result.stdout.startswith("applied at ")


def test_a_source_without_the_agent_variant_is_refused(agent: Agent) -> None:
    """Applied anyway, it would write the operator's .gitconfig over the agent's identity."""
    result = agent.sync(is_agent="")
    assert result.returncode == 1
    assert "does not render the agent variant" in result.stderr
    assert not any(line.startswith("apply") for line in agent.log())


def test_every_run_renders_from_the_seed_so_the_agent_answer_survives(
    agent: Agent,
) -> None:
    """A render from a template that predates the prompt drops `agent` from chezmoi.toml."""
    agent.sync()
    assert agent.log()[0] == "init agent=1"
    assert agent.config.read_text() == agent.seed.read_text()


def test_a_moved_source_reports_the_range(agent: Agent) -> None:
    agent.push()
    result = agent.sync()
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("applied ") and ".." in result.stdout


def tasks(name: str) -> list[dict]:
    return yaml_fast.safe_load((TASKS / name).read_text())


def named(task_list: list[dict], name: str) -> dict:
    found = [t for t in task_list if t.get("name") == name]
    assert len(found) == 1, f"expected one task named {name!r}, found {len(found)}"
    return found[0]


def test_the_apply_runs_as_the_agent_and_never_fails_the_play() -> None:
    """A red task holds the GitOps deployer over the agent's own config."""
    task = named(
        tasks("agent_dotfiles.yml"), "Apply the operator's dotfiles to the agent user"
    )
    argv = task["ansible.builtin.command"]["argv"]
    assert argv[:4] == ["runuser", "-u", "{{ claude_code_agent_user }}", "--"]
    assert argv[4] == "/usr/local/bin/claude-dotfiles-sync.sh"
    assert task["failed_when"] is False


def test_only_a_root_owned_copy_is_removed_before_chezmoi_takes_over() -> None:
    """Once chezmoi has written the trees they are the agent's, and an apply keeps them."""
    task = named(
        tasks("agent_dotfiles.yml"),
        "Remove the operator config copy's root-owned trees",
    )
    assert "item.stat.pw_name == 'root'" in task["when"]


def test_switching_the_dotfiles_off_removes_every_unit() -> None:
    task = named(
        tasks("agent_dotfiles.yml"),
        "Remove the agent user's dotfiles sync units when its dotfiles are switched off",
    )
    assert task["when"] == "not claude_code_agent_dotfiles"
    assert task["loop"] == [
        "dotfiles-sync.service",
        "dotfiles-sync.timer",
        "dotfiles-sync-alert.service",
    ]


def test_chezmoi_owns_cc_wait_and_jsonq_when_the_dotfiles_are_on() -> None:
    """Both come from the same dotfiles source; two writers would flip them each apply."""
    agent = tasks("agent.yml")
    for name in (
        "Give the agent user the operator's cc-wait",
        "Give the agent user the operator's jsonq",
    ):
        assert "not claude_code_agent_dotfiles" in named(agent, name)["when"]


def test_the_unit_runs_the_script_as_the_agent_with_its_seed() -> None:
    unit = render_setup_text(
        "claude_code",
        "claude-dotfiles-sync.service.j2",
        {"claude_code_agent_dotfiles": True},
    )
    assert "User=claude\n" in unit
    assert (
        "ExecStart=/usr/local/bin/claude-dotfiles-sync.sh /var/lib/claude/.local/bin/chezmoi "
        "/var/lib/claude/.config/chezmoi/seed.toml\n"
    ) in unit
    assert "OnFailure=claude-dotfiles-sync-alert.service\n" in unit


def test_the_seed_answers_agent_true() -> None:
    seed = render_setup_text("claude_code", "agent-chezmoi-seed.toml.j2", {})
    assert "    agent = true\n" in seed
    assert '    profile = "server"\n' in seed


def test_daniel_box_switches_the_dotfiles_on() -> None:
    assert load_yaml(HOST_VARS / "daniel-box.yml")["claude_code_agent_dotfiles"] is True
