"""Every task that stops the Pi's watchdog notifies a handler that starts it again.

initial_setup stops the watchdog daemon before its heavy apt work on the Pi. Only optimize_pi's
own task started it again, so a run that reached the stop without the `watchdog` tag left the
daemon dead (#3241). The handler is what makes the restart independent of tag selection.

Run: uv run pytest ansible/tests/setup/test_pi_watchdog_restarts_after_provisioning.py
"""

from _helpers import ANSIBLE
from _helpers import load_tasks
from _helpers import walk_tasks
from _role_census import task_files
from lib import yaml_fast

SETUP = ANSIBLE / "roles" / "setup"
SERVICE_MODULES = (
    "ansible.builtin.service",
    "ansible.builtin.systemd",
    "service",
    "systemd",
)


def _service_action(task: dict) -> dict | None:
    for module in SERVICE_MODULES:
        if isinstance(task.get(module), dict):
            return task[module]
    return None


def watchdog_stops(tasks: list[dict]) -> list[dict]:
    """The tasks that stop the watchdog service."""
    found = []
    for task in tasks:
        action = _service_action(task)
        if (
            action
            and action.get("name") == "watchdog"
            and action.get("state") == "stopped"
        ):
            found.append(task)
    return found


def stops_left_stopped(tasks: list[dict], handlers: list[dict]) -> list[str]:
    """Names of watchdog-stopping tasks whose notify reaches no handler that starts it."""
    starters = set()
    for handler in handlers:
        action = _service_action(handler)
        if (
            action
            and action.get("name") == "watchdog"
            and action.get("state") in ("started", "restarted")
        ):
            starters.add(handler["name"])
    offenders = []
    for task in watchdog_stops(tasks):
        notify = task.get("notify") or []
        notify = [notify] if isinstance(notify, str) else notify
        if not starters.intersection(notify):
            offenders.append(task["name"])
    return offenders


def _real_tasks() -> list[dict]:
    tasks: list[dict] = []
    for tasks_file in task_files(SETUP):
        tasks.extend(walk_tasks(load_tasks(tasks_file)))
    return tasks


def _real_handlers() -> list[dict]:
    handlers: list[dict] = []
    for handlers_file in sorted(SETUP.glob("*/handlers/main.yml")):
        handlers.extend(yaml_fast.safe_load(handlers_file.read_text()) or [])
    return handlers


def test_the_census_finds_the_provisioning_stop() -> None:
    names = [t["name"] for t in watchdog_stops(_real_tasks())]
    assert "Stop the hardware watchdog during provisioning (Pi)" in names


def test_every_real_watchdog_stop_is_paired_with_a_start() -> None:
    assert stops_left_stopped(_real_tasks(), _real_handlers()) == []


def test_a_stop_that_notifies_nothing_is_flagged() -> None:
    stop = {
        "name": "stop it",
        "ansible.builtin.systemd": {"name": "watchdog", "state": "stopped"},
    }
    assert stops_left_stopped([stop], _real_handlers()) == ["stop it"]


def test_a_stop_whose_handler_only_reloads_is_flagged() -> None:
    stop = {
        "name": "stop it",
        "ansible.builtin.systemd": {"name": "watchdog", "state": "stopped"},
        "notify": "Reload watchdog",
    }
    reload = {
        "name": "Reload watchdog",
        "ansible.builtin.service": {"name": "watchdog", "state": "reloaded"},
    }
    assert stops_left_stopped([stop], [reload]) == ["stop it"]
