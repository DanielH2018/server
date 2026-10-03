"""Every `notify:` in initial_setup.yml's roles reaches a handler one of those roles defines.

The handlers moved out of the playbook into each role's `handlers/main.yml` (#3288). A notify
naming no handler fails only on the run where its task changes something, which no syntax check
sees, so this resolves every notify statically. optimize_pi notifies two handlers that
initial_setup defines, so the lookup is across the whole play's role list, not per role.

Run: uv run pytest ansible/tests/setup/test_setup_handlers_resolve.py
"""

from _helpers import ANSIBLE, SETUP_ROLES, load_tasks, walk_tasks
from lib import yaml_fast

PLAYBOOK = ANSIBLE / "initial_setup.yml"
INITIAL_SETUP_HANDLERS = SETUP_ROLES / "initial_setup" / "handlers" / "main.yml"


def play_roles() -> list[str]:
    """The role names the initial_setup.yml play lists, in order."""
    (play,) = yaml_fast.safe_load(PLAYBOOK.read_text())
    return [r["role"] if isinstance(r, dict) else r for r in play["roles"]]


def notified(roles: list[str]) -> dict[str, set[str]]:
    """Handler name -> the roles whose tasks notify it."""
    found: dict[str, set[str]] = {}
    for role in roles:
        for tasks_file in sorted((SETUP_ROLES / role / "tasks").rglob("*.yml")):
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


def unresolved(notifies: dict[str, set[str]], handlers: list[str]) -> list[str]:
    return sorted(name for name in notifies if name not in handlers)


def test_every_notify_in_initial_setup_resolves_to_a_handler() -> None:
    # fact: ansible/roles/setup/initial_setup/CLAUDE.md#Rules the task files do not state
    roles = play_roles()
    assert unresolved(notified(roles), defined(roles)) == []


def test_the_census_finds_the_cross_role_notifies() -> None:
    """Non-vacuity: optimize_pi's notifies of initial_setup's handlers are in the census."""
    notifies = notified(play_roles())
    assert "optimize_pi" in notifies["Restart Watchdog"]
    assert "optimize_pi" in notifies["Restart systemd-journald"]
    assert "initial_setup" in notifies["Start Watchdog after provisioning"]


def test_a_notify_with_no_handler_is_flagged() -> None:
    assert unresolved({"Restart nothing": {"x"}}, defined(play_roles())) == [
        "Restart nothing"
    ]


def test_the_playbook_defines_no_handlers() -> None:
    (play,) = yaml_fast.safe_load(PLAYBOOK.read_text())
    assert "handlers" not in play, "initial_setup.yml's handlers belong in its roles"


def test_watchdog_restart_is_defined_before_the_provisioning_start() -> None:
    """Handlers fire in definition order, and a restart already starts the daemon."""
    names = [h["name"] for h in yaml_fast.safe_load(INITIAL_SETUP_HANDLERS.read_text())]
    assert names.index("Restart Watchdog") < names.index(
        "Start Watchdog after provisioning"
    )
