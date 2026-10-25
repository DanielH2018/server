"""The engine-upgrade play holds the host plane and moves the `homelab` project (H5.3).

The play and homelab-reconcile both write the `homelab` Compose project. Without the hold, an
updater run that waits on the play's dpkg lock installs a package once apt releases it, and
reconcile restarts `homelab-compose` during the play's recreate. Five things must stay true:

1. a task before the apt probe masks `homelab-updater.timer`, and a later task, still before
   any `state: stopped`, waits on the updater and reconcile services;
2. that wait's limit, the role default, exceeds the `TimeoutStartSec` of the rendered
   `homelab-updater.service`;
3. a task before the apt upgrade stops the `homelab` project;
4. a task after the daemon start recreates it with `recreate: always` and `become: true`;
5. the last task unmasks and starts the timer, guarded by the recorded mask state.

Run: uv run pytest ansible/tests/setup/test_engine_upgrade_holds_the_updater_timer.py
"""

import copy
import re

from lib import yaml_fast
from lib.repo_paths import REPO
from _helpers import SETUP_ROLES

ROLE = SETUP_ROLES / "docker_install"
PLAY = ROLE / "tasks" / "engine-upgrade.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"
UPDATER_UNIT = REPO / "hosts/packages/homelab-updater/units/homelab-updater.service"
LIMIT_VAR = "docker_install_updater_wait_limit_s"
TIMER = "homelab-updater.timer"
COMPOSE = "community.docker.docker_compose_v2"
SYSTEMD = "ansible.builtin.systemd_service"
APT = "ansible.builtin.apt"
START = "ansible.builtin.systemd_service"


def _flatten(tasks: list[dict]) -> list[dict]:
    """The play's tasks in run order, a block's body in place of the block."""
    out: list[dict] = []
    for task in tasks:
        if "block" in task:
            out.extend(_flatten(task["block"]))
        else:
            out.append(task)
    return out


def _module(task: dict, name: str) -> dict | None:
    args = task.get(name)
    return args if isinstance(args, dict) else None


def _first(tasks: list[dict], match) -> int | None:
    return next((i for i, t in enumerate(tasks) if match(t)), None)


def _is_mask(task: dict) -> bool:
    args = _module(task, SYSTEMD)
    return bool(args and args.get("name") == TIMER and args.get("masked") is True)


def _waits(task: dict) -> bool:
    return bool(task.get("until")) and "homelab-reconcile.service" in str(
        task.get("loop")
    )


def _stops_homelab(task: dict) -> bool:
    args = _module(task, COMPOSE)
    return bool(
        args
        and args.get("state") == "stopped"
        and "docker_install_homelab_compose_dir" in str(args.get("project_src"))
    )


def _recreates_homelab(task: dict) -> bool:
    args = _module(task, COMPOSE)
    return bool(
        args
        and args.get("state") == "present"
        and args.get("recreate") == "always"
        and "docker_install_homelab_compose_dir" in str(args.get("project_src"))
        and task.get("become") is True
    )


def _releases(task: dict) -> bool:
    args = _module(task, START)
    return bool(
        args
        and args.get("name") == TIMER
        and args.get("masked") is False
        and args.get("state") == "started"
        and "docker_install_updater_was_masked" in str(task.get("when"))
    )


def _seconds(text: str) -> int:
    match = re.search(r"^TimeoutStartSec=(\d+)(min|s)?$", text, re.MULTILINE)
    assert match, "homelab-updater.service carries no TimeoutStartSec"
    return int(match[1]) * (60 if match[2] == "min" else 1)


def problems(tasks: list[dict], wait_limit_s: int, unit_limit_s: int) -> list[str]:
    """What is wrong with the play `tasks`; empty when the hold and the recreate are intact."""
    flat = _flatten(tasks)
    found: list[str] = []
    probe = _first(flat, lambda t: _module(t, APT) is not None and t.get("check_mode"))
    stop = _first(
        flat,
        lambda t: (_module(t, COMPOSE) or {}).get("state") == "stopped",
    )
    upgrade = _first(
        flat,
        lambda t: (
            _module(t, APT) is not None
            and not t.get("check_mode")
            and "package_specs" in str(t)
        ),
    )
    mask = _first(flat, _is_mask)
    wait = _first(flat, _waits)
    if mask is None or probe is None or mask > probe:
        found.append(f"no task masks {TIMER} before the apt probe")
    if wait is None or stop is None or wait > stop:
        found.append("no task waits on the updater and reconcile before the first stop")
    if wait_limit_s <= unit_limit_s:
        found.append(
            f"the wait limit {wait_limit_s}s does not exceed the updater unit's {unit_limit_s}s"
        )
    home_stop = _first(flat, _stops_homelab)
    if home_stop is None or upgrade is None or home_stop > upgrade:
        found.append("no task stops the homelab project before the apt upgrade")
    daemon = _first(flat, lambda t: "docker.socket" in str(t.get("loop")))
    recreate = _first(flat, _recreates_homelab)
    if recreate is None or daemon is None or recreate < daemon:
        found.append("no task recreates the homelab project after the daemon start")
    if not flat or not _releases(flat[-1]):
        found.append(
            "the last task does not unmask and start the timer behind the recorded state"
        )
    return found


def _real() -> list[dict]:
    return yaml_fast.safe_load(PLAY.read_text())


def _limits() -> tuple[int, int]:
    wait = yaml_fast.safe_load(DEFAULTS.read_text())[LIMIT_VAR]
    return wait, _seconds(UPDATER_UNIT.read_text())


def test_the_real_play_holds_the_timer_and_recreates_homelab():
    assert problems(_real(), *_limits()) == []


def test_the_census_reads_the_real_plays_members():
    flat = _flatten(_real())
    assert _first(flat, _is_mask) is not None
    assert _first(flat, _waits) is not None
    assert _first(flat, _stops_homelab) is not None
    assert _first(flat, _recreates_homelab) is not None
    assert _releases(flat[-1])


def test_a_play_without_the_mask_task_is_flagged():
    tasks = [t for t in _real() if not _is_mask(t)]
    assert any("masks" in p for p in problems(tasks, *_limits()))


def test_a_play_that_only_stops_and_starts_the_unit_is_flagged():
    plain = [
        {"name": "probe", APT: {"name": "x"}, "check_mode": True},
        {
            "name": "stop",
            "ansible.builtin.systemd_service": {
                "name": "homelab-compose.service",
                "state": "stopped",
            },
        },
        {"name": "upgrade", APT: {"name": "{{ docker_install_package_specs }}"}},
        {
            "name": "start",
            "ansible.builtin.systemd_service": {
                "name": "homelab-compose.service",
                "state": "started",
            },
        },
    ]
    assert len(problems(plain, 3060, 3000)) >= 4


def test_a_wait_limit_at_reconciles_thirty_minutes_is_flagged():
    _, unit = _limits()
    assert any("wait limit" in p for p in problems(_real(), 30 * 60, unit))
    assert not any("wait limit" in p for p in problems(_real(), unit + 60, unit))


def test_a_recreate_without_recreate_always_is_flagged():
    tasks = copy.deepcopy(_real())
    for task in tasks:
        args = _module(task, COMPOSE)
        if args and args.get("recreate") == "always" and "homelab" in str(args):
            args["recreate"] = "auto"
    assert any("recreates" in p for p in problems(tasks, *_limits()))


def test_a_release_that_ignores_the_recorded_mask_is_flagged():
    tasks = copy.deepcopy(_real())
    tasks[-1].pop("when")
    assert any("last task" in p for p in problems(tasks, *_limits()))


def test_the_unit_limit_parser_reads_minutes():
    assert _seconds("[Service]\nTimeoutStartSec=50min\n") == 3000
