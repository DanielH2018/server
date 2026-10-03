"""Every play that loads secrets.yml does it through the shared preamble, never inline.

The preamble's first task refuses a local-connection play run from a different machine
(test_local_connection_target.py evaluates it). A play that calls `community.sops.load_vars`
itself skips that assert. prune_backups.yml and seed_volume_backup.yml did, so either one run
from daniel-server executed every task there under a daniel-box recap (#3353).
test_playbooks_gather_through_the_preamble.py could not see them either, because it checks
only plays that already import the preamble.

Run: uv run pytest ansible/tests/deploy/test_plays_load_secrets_through_the_preamble.py
"""

from _helpers import ANSIBLE, walk_tasks
from lib import yaml_fast

PREAMBLE = ANSIBLE / "pre_tasks" / "load_secrets.yml"
LOAD_VARS_MODULES = ("community.sops.load_vars", "load_vars")


def inline_secret_loads(play: dict) -> list[str]:
    """Names of the tasks in this play that load SOPS vars without the preamble."""
    return [
        str(task.get("name"))
        for section in ("pre_tasks", "tasks", "post_tasks", "handlers")
        for task in walk_tasks(play.get(section))
        if any(module in task for module in LOAD_VARS_MODULES)
    ]


def imports_preamble(play: dict) -> bool:
    return any(
        str(task.get("ansible.builtin.import_tasks", "")).endswith(PREAMBLE.name)
        for task in walk_tasks(play.get("pre_tasks"))
    )


def plays_by_playbook() -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for playbook in sorted(ANSIBLE.glob("*.yml")):
        plays = yaml_fast.safe_load(playbook.read_text())
        if isinstance(plays, list):
            found[playbook.name] = [p for p in plays if isinstance(p, dict)]
    return found


def test_no_play_loads_secrets_inline() -> None:
    offenders = {
        f"{name}: {play.get('name')}": inline_secret_loads(play)
        for name, plays in plays_by_playbook().items()
        for play in plays
        if inline_secret_loads(play)
    }
    assert offenders == {}


def test_the_backup_playbooks_load_secrets_through_the_preamble() -> None:
    importers = {
        name
        for name, plays in plays_by_playbook().items()
        if any(map(imports_preamble, plays))
    }
    assert {"prune_backups.yml", "seed_volume_backup.yml"} <= importers


def test_an_inline_load_is_flagged() -> None:
    play = {
        "pre_tasks": [
            {
                "name": "Load secrets",
                "community.sops.load_vars": {"file": "vars/secrets.yml"},
            }
        ]
    }
    assert inline_secret_loads(play) == ["Load secrets"]


def test_an_inline_load_inside_a_block_is_flagged() -> None:
    play = {
        "tasks": [
            {
                "name": "Wrap",
                "block": [{"name": "Load", "load_vars": {"file": "secrets.yml"}}],
            }
        ]
    }
    assert inline_secret_loads(play) == ["Load"]
