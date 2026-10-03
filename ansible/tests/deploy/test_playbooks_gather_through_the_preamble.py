"""Every play that imports the shared preamble gathers facts through it, and only through it.

The preamble's last task gathers facts after the SOPS become password is set (#3285). A play
that also set `gather_facts: true` would escalate at fact-gathering before the password loads,
and a play carrying its own `setup:` task gathers twice. Both shapes are what the preamble
replaced in initial_setup.yml, preflight.yml, k3s-bringup.yml and deploy.yml.

Run: uv run pytest ansible/tests/deploy/test_playbooks_gather_through_the_preamble.py
"""

from _helpers import ANSIBLE, walk_tasks
from lib import yaml_fast

PREAMBLE = ANSIBLE / "pre_tasks" / "load_secrets.yml"
SETUP_MODULES = ("ansible.builtin.setup", "setup")


def imports_preamble(play: dict) -> bool:
    return any(
        str(task.get("ansible.builtin.import_tasks", "")).endswith(PREAMBLE.name)
        for task in walk_tasks(play.get("pre_tasks"))
    )


def play_problems(play: dict) -> list[str]:
    """What is wrong with how this preamble-importing play gathers facts."""
    problems = []
    if play.get("gather_facts", True):
        problems.append("gather_facts is not false")
    for section in ("pre_tasks", "tasks", "post_tasks"):
        for task in walk_tasks(play.get(section)):
            if any(module in task for module in SETUP_MODULES):
                problems.append(f"its own setup task {task.get('name')!r}")
    return problems


def preamble_plays() -> dict[str, list[dict]]:
    """Playbook file name -> the plays in it that import the preamble."""
    found: dict[str, list[dict]] = {}
    for playbook in sorted(ANSIBLE.glob("*.yml")):
        plays = yaml_fast.safe_load(playbook.read_text())
        if not isinstance(plays, list):
            continue
        hits = [p for p in plays if isinstance(p, dict) and imports_preamble(p)]
        if hits:
            found[playbook.name] = hits
    return found


def test_no_preamble_play_gathers_on_its_own() -> None:
    offenders = {
        f"{name}: {play['name']}": play_problems(play)
        for name, plays in preamble_plays().items()
        for play in plays
        if play_problems(play)
    }
    assert offenders == {}


def test_the_census_finds_every_playbook_that_gathered_on_its_own() -> None:
    assert {
        "initial_setup.yml",
        "preflight.yml",
        "k3s-bringup.yml",
        "k3s-storage-smoke.yml",
        "deploy.yml",
    } <= set(preamble_plays())


def test_the_preamble_gathers_last_under_the_play_become_with_an_opt_out() -> None:
    last = yaml_fast.safe_load(PREAMBLE.read_text())[-1]
    assert "ansible.builtin.setup" in last
    assert "become" not in last, "a become: false would gather unescalated"
    assert "load_secrets_gather_facts" in last["when"]
    assert last["tags"] == "always"


def test_a_play_with_its_own_setup_task_is_flagged() -> None:
    play = {
        "gather_facts": False,
        "pre_tasks": [{"name": "Gather facts", "ansible.builtin.setup": None}],
    }
    assert play_problems(play) == ["its own setup task 'Gather facts'"]


def test_a_play_that_gathers_implicitly_is_flagged() -> None:
    assert play_problems({"pre_tasks": []}) == ["gather_facts is not false"]
