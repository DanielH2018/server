"""Every `notify:` in initial_setup.yml's roles reaches a handler one of those roles defines.

The handlers moved out of the playbook into each role's `handlers/main.yml` (#3288). A notify
naming no handler fails only on the run where its task changes something, which no syntax check
sees, so this resolves every notify statically. Ansible resolves a notify across the whole play,
so the lookup covers the play's role list, not one role. The reverse also holds: a handler no task
notifies never runs, and its comments describe a caller that is gone (#7184).

That same play-wide lookup is why no two roles may define one handler name. Ansible resolves a
duplicate to the last one loaded into the play, and a role skipped on the host skips that
handler: six roles' `Reload systemd` left claude_code's login cap unreloaded on daniel-server
(#5483).

Run: uv run pytest ansible/tests/setup/test_setup_handlers_resolve.py
"""

from _helpers import ANSIBLE, SETUP_ROLES, load_tasks, walk_tasks
from _role_census import role_task_files
from lib import yaml_fast

PLAYBOOK = ANSIBLE / "initial_setup.yml"


def play_roles() -> list[str]:
    """The role names the initial_setup.yml play lists, in order."""
    (play,) = yaml_fast.safe_load(PLAYBOOK.read_text())
    return [r["role"] if isinstance(r, dict) else r for r in play["roles"]]


def notified(roles: list[str]) -> dict[str, set[str]]:
    """Handler name -> the roles whose tasks notify it."""
    found: dict[str, set[str]] = {}
    for role in roles:
        for tasks_file in role_task_files(SETUP_ROLES / role):
            for task in walk_tasks(load_tasks(tasks_file)):
                names = task.get("notify") or []
                for name in [names] if isinstance(names, str) else names:
                    found.setdefault(name, set()).add(role)
    return found


def defined(roles: list[str]) -> list[str]:
    """Every name a notify can reach in these roles: handler names and `listen:` topics."""
    names: list[str] = []
    for role in roles:
        path = SETUP_ROLES / role / "handlers" / "main.yml"
        if not path.exists():
            continue
        for handler in yaml_fast.safe_load(path.read_text()) or []:
            names.append(handler["name"])
            listen = handler.get("listen") or []
            names.extend([listen] if isinstance(listen, str) else listen)
    return names


def handler_owners(roles: list[str]) -> dict[str, list[str]]:
    """Handler name -> the roles defining it. `listen:` topics are left out: a shared topic fans
    out to every listener, where a shared name reaches only one handler."""
    owners: dict[str, list[str]] = {}
    for role in roles:
        path = SETUP_ROLES / role / "handlers" / "main.yml"
        if path.exists():
            for handler in yaml_fast.safe_load(path.read_text()) or []:
                owners.setdefault(handler["name"], []).append(role)
    return owners


def duplicated(owners: dict[str, list[str]]) -> dict[str, list[str]]:
    return {name: roles for name, roles in owners.items() if len(roles) > 1}


def unresolved(notifies: dict[str, set[str]], handlers: list[str]) -> list[str]:
    return sorted(name for name in notifies if name not in handlers)


def unnotified(
    owners: dict[str, list[str]], notifies: dict[str, set[str]]
) -> list[str]:
    return sorted(name for name in owners if name not in notifies)


def test_every_notify_in_initial_setup_resolves_to_a_handler() -> None:
    # fact: ansible/roles/setup/initial_setup/CLAUDE.md#Rules the task files do not state
    roles = play_roles()
    assert unresolved(notified(roles), defined(roles)) == []


def test_every_handler_in_the_play_has_a_notifier() -> None:
    # fact: ansible/roles/setup/initial_setup/CLAUDE.md#Rules the task files do not state
    roles = play_roles()
    assert unnotified(handler_owners(roles), notified(roles)) == []


def test_a_handler_with_no_notifier_is_flagged() -> None:
    owners = {"Restart x": ["x"], "Reload y": ["y"]}
    assert unnotified(owners, {"Reload y": {"y"}}) == ["Restart x"]


def test_the_census_finds_a_roles_notify_of_its_own_handler() -> None:
    """Non-vacuity: gitops_deploy's notify of its own reload handler is in the census."""
    notifies = notified(play_roles())
    assert "gitops_deploy" in notifies["Reload systemd for gitops_deploy"]


def test_a_notify_with_no_handler_is_flagged() -> None:
    assert unresolved({"Restart nothing": {"x"}}, defined(play_roles())) == [
        "Restart nothing"
    ]


def test_no_two_roles_in_the_play_define_one_handler_name() -> None:
    owners = handler_owners(play_roles())
    assert owners.get("Reload systemd for gitops_deploy") == ["gitops_deploy"], (
        "the census no longer finds gitops_deploy's own reload handler"
    )
    assert duplicated(owners) == {}


def test_a_handler_name_two_roles_define_is_flagged() -> None:
    owners = {"Reload systemd": ["claude_code", "renovate_agent"], "Restart x": ["x"]}
    assert duplicated(owners) == {"Reload systemd": ["claude_code", "renovate_agent"]}


def test_the_playbook_defines_no_handlers() -> None:
    (play,) = yaml_fast.safe_load(PLAYBOOK.read_text())
    assert "handlers" not in play, "initial_setup.yml's handlers belong in its roles"
