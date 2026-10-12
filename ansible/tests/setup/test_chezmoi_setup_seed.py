"""The operator's chezmoi seed answers every prompt the dotfiles' config template asks.

The dotfiles repo is not in this tree, so the agent user's seed (`claude_code`) stands in for
the prompt list: its sync refuses a source without the agent variant, so it is the seed that
moves first when the dotfiles add a prompt.
"""

import tomllib

from _setup_render import render_setup_text


def _seed_data(role: str, template: str) -> dict:
    return tomllib.loads(render_setup_text(role, template, {}))["data"]


def test_the_operator_seed_answers_every_prompt_the_agent_seed_answers() -> None:
    # fact: ansible/roles/setup/chezmoi_setup/CLAUDE.md#At a glance
    operator = _seed_data("chezmoi_setup", "chezmoi-seed.toml.j2")
    agent = _seed_data("claude_code", "agent-chezmoi-seed.toml.j2")
    assert operator.keys() == agent.keys()


def test_the_operator_seed_answers_agent_false() -> None:
    assert _seed_data("chezmoi_setup", "chezmoi-seed.toml.j2")["agent"] is False
