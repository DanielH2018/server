"""The tag the deployer applies a `requirements.yml` bump with names a task `initial_setup.yml` runs.

`deploy_changes.setup_tags_for` maps `ansible/requirements.yml` to `initial_setup.yml --tags
collections`. A `--tags` value that matches no task makes Ansible exit 0 having changed nothing,
so a renamed tag or a task moved out of the play would turn every collections bump into a silent
no-op behind a green apply.
"""

from pathlib import Path

from _role_census import role_task_files
from deployer_lib.deploy_changes import setup_tags_for

from lib import yaml_fast
from lib.repo_paths import REPO

PLAYBOOK = REPO / "ansible" / "initial_setup.yml"
ROLES = REPO / "ansible" / "roles" / "setup"


def _tags_in(tasks: object) -> set[str]:
    """Every tag a task list carries, blocks included."""
    found: set[str] = set()
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict):
            continue
        tags = task.get("tags", [])
        found |= {tags} if isinstance(tags, str) else set(tags)
        for key in ("block", "rescue", "always"):
            found |= _tags_in(task.get(key))
    return found


def playbook_tags(playbook: Path = PLAYBOOK, roles: Path = ROLES) -> set[str]:
    """Each tag a task of a role `playbook` lists carries, and each role entry's own tags."""
    found: set[str] = set()
    for play in yaml_fast.safe_load(playbook.read_text()):
        for entry in play.get("roles", []):
            name = entry["role"] if isinstance(entry, dict) else entry
            if isinstance(entry, dict):
                found |= set(entry.get("tags", []))
            for path in role_task_files(roles / name):
                found |= _tags_in(yaml_fast.safe_load(path.read_text()))
    return found


def test_the_requirements_tag_matches_a_task_initial_setup_runs():
    tags = setup_tags_for(["ansible/requirements.yml"])
    assert tags == {"collections"}
    assert tags <= playbook_tags()


def test_a_play_whose_tasks_lost_the_tag_is_flagged(tmp_path):
    tasks = tmp_path / "roles" / "demo" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        "- name: Install collections\n  tags: [galaxy]\n  ansible.builtin.command: x\n"
    )
    playbook = tmp_path / "initial_setup.yml"
    playbook.write_text("- hosts: all\n  roles:\n    - {role: demo, tags: [demo]}\n")
    found = playbook_tags(playbook, tmp_path / "roles")
    assert found == {"demo", "galaxy"}
    assert not setup_tags_for(["ansible/requirements.yml"]) <= found
