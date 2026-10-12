"""The operator's chezmoi seed answers every prompt the dotfiles' config template asks.

The dotfiles repo is not in this tree, so the agent user's seed (`claude_code`) stands in for
the prompt list: its sync refuses a source without the agent variant, so it is the seed that
moves first when the dotfiles add a prompt.
"""

import tomllib

import pytest
from _helpers import ANSIBLE, load_yaml, walk_tasks
from _setup_render import render_setup_text
from jinja2 import StrictUndefined
from lib.ansible_jinja_env import make_ansible_env

AGENT_ANSWER_TASK = (
    "Answer the agent prompt in a config seeded before the dotfiles asked it"
)


def _seed_data(role: str, template: str) -> dict:
    return tomllib.loads(render_setup_text(role, template, {}))["data"]


def test_the_operator_seed_answers_every_prompt_the_agent_seed_answers() -> None:
    # fact: ansible/roles/setup/chezmoi_setup/CLAUDE.md#At a glance
    operator = _seed_data("chezmoi_setup", "chezmoi-seed.toml.j2")
    agent = _seed_data("claude_code", "agent-chezmoi-seed.toml.j2")
    assert operator.keys() == agent.keys()


def test_the_operator_seed_answers_agent_false() -> None:
    assert _seed_data("chezmoi_setup", "chezmoi-seed.toml.j2")["agent"] is False


def _agent_answer_runs(check_mode: bool, seed_changed: bool) -> bool:
    tasks = load_yaml(ANSIBLE / "roles/setup/chezmoi_setup/tasks/main.yml")
    (task,) = [t for t in walk_tasks(tasks) if t.get("name") == AGENT_ANSWER_TASK]
    env = make_ansible_env(undefined_cls=StrictUndefined)

    def changed(value: dict) -> bool:
        return bool(value.get("changed"))

    env.tests["changed"] = changed
    clauses = task.get("when") or []
    clauses = clauses if isinstance(clauses, list) else [clauses]
    context = {
        "ansible_check_mode": check_mode,
        "chezmoi_setup_seed": {"changed": seed_changed},
    }
    return all(
        env.from_string("{% if " + c + " %}1{% endif %}").render(context) == "1"
        for c in clauses
    )


def test_agent_answer_skips_a_check_run_whose_seed_was_never_written() -> None:
    # --check reports the force: false seed changed on a host with no config but writes
    # nothing, and lineinfile then fails on the missing destination (#4631).
    assert not _agent_answer_runs(check_mode=True, seed_changed=True)


@pytest.mark.parametrize(
    ("check_mode", "seed_changed"), [(True, False), (False, True), (False, False)]
)
def test_agent_answer_runs_wherever_the_config_exists(
    check_mode: bool, seed_changed: bool
) -> None:
    assert _agent_answer_runs(check_mode, seed_changed)
