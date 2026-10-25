"""The shared kuma-check timer pair reruns a host check while its last verdict was DOWN.

A cron-fed push monitor that pushes `down` stays red until the producer's next slot, however
soon the fault is fixed. `roles/setup/common/tasks/kuma_check_timer.yml` replaces such a cron
with a systemd timer and oneshot service whose contract is the exit code: the script exits 1
after it pushes `down`, and `Restart=on-failure` reruns it every `RestartSec` until it exits 0.

Five properties, each invisible at deploy time because Ansible reports the units as applied
either way:

1. The service restarts on failure and only on failure. `Restart=always` is refused for a
   oneshot, and `on-success` would loop a green check forever.
2. The timer is `Persistent=true`: a daily slot missed inside an outage
   runs at boot instead of leaving the tile red for a day, and the restart bounds the
   boot-time false `down` that costs.
3. No `OnFailure=` alert unit. A `down` verdict is a unit failure by contract, and the
   Discord alert units page on a unit that broke.
4. Both directions are wired: `kuma_check_state: absent` stops and disables the timer and
   removes the units and the legacy cron. A one-way door is the reverse-state bug this repo
   keeps paying for.
5. A reboot does not end a red check's reruns (#4495). The restart loop lives in memory, and
   `Persistent=true` catches up only a slot that never fired. The service keeps a down marker,
   and a boot unit conditioned on that marker starts the service once at boot.

Run: uv run pytest ansible/tests/setup/test_kuma_check_timer.py
"""

import re
from pathlib import Path

import jinja2
import pytest
from lib.ansible_jinja_env import make_ansible_env
from _helpers import SETUP_ROLES, leaf_tasks, load_tasks
from _role_census import task_files
from _shell_render import rendered_shell_text

COMMON = SETUP_ROLES / "common"
SERVICE = COMMON / "templates" / "kuma-check.service.j2"
TIMER = COMMON / "templates" / "kuma-check.timer.j2"
BOOT = COMMON / "templates" / "kuma-check-boot.service.j2"
TASKS = COMMON / "tasks" / "kuma_check_timer.yml"

# Every check wired through the task file, by `kuma_check_name`. A census read by glob returns
# an empty set the moment the include moves, so the members are named here and the test says
# which one went missing.
KNOWN_CHECKS = frozenset(
    {
        "loki-read-route",
        "manifest-prune",
        "live-drift",
        "setup-drift",
        "github-ruleset-drift",
        "github-interaction-limit",
        "render-records",
    }
)

# The line a shell producer ends on. The oracle for the textual guard below: the Loki witness
# test drives its script and proves the line does what it says; this pins that every other
# shell producer wired through the task file carries the same line, so a producer converted
# without it cannot read as done.
EXIT_CONTRACT = '[[ "$STATUS" == up ]] || exit 1'

BASE_VARS = {
    "kuma_check_name": "widget",
    "kuma_check_description": "Widget check",
    "kuma_check_exec": "/usr/local/bin/widget.sh",
    "kuma_check_user": "ubuntu",
    "kuma_check_on_calendar": "*-*-* *:23:00",
    "kuma_check_restart_sec": "15min",
}


def directive(unit_text: str, key: str) -> list[str]:
    folded = re.sub(r"\\\n\s*", " ", unit_text)
    return [
        line.split("=", 1)[1].strip()
        for line in folded.splitlines()
        if line.strip().startswith(f"{key}=")
    ]


def _render(template, **overrides) -> str:
    env = make_ansible_env(undefined_cls=jinja2.StrictUndefined)
    return env.from_string(template.read_text()).render({**BASE_VARS, **overrides})


@pytest.fixture(scope="module")
def service() -> str:
    return _render(SERVICE)


@pytest.fixture(scope="module")
def timer() -> str:
    return _render(TIMER)


def test_service_restarts_on_failure_only(service: str) -> None:
    assert directive(service, "Type") == ["oneshot"]
    assert directive(service, "Restart") == ["on-failure"], (
        "Restart=on-failure is the whole mechanism: a check that exits 1 after pushing down "
        "reruns until it exits 0. always/on-success are refused for a oneshot"
    )
    assert directive(service, "RestartSec") == ["15min"]
    assert directive(service, "User") == ["ubuntu"]
    assert directive(service, "ExecStart") == ["/usr/local/bin/widget.sh"]


def test_service_carries_no_alert_unit(service: str) -> None:
    assert not directive(service, "OnFailure"), (
        "a down verdict is a unit failure by contract; an OnFailure= alert would page Discord "
        "on every red verdict the tile already carries"
    )


def test_environment_file_is_emitted_only_when_given(service: str) -> None:
    assert not directive(service, "EnvironmentFile")
    with_env = _render(SERVICE, kuma_check_env_file="/etc/homelab/widget.env")
    assert directive(with_env, "EnvironmentFile") == ["/etc/homelab/widget.env"]


def test_environment_lines_are_emitted_one_per_assignment(service: str) -> None:
    assert not directive(service, "Environment")
    with_env = _render(
        SERVICE, kuma_check_environment=["KUBECONFIG=/x/config", "BOOT_GRACE_S=420"]
    )
    assert directive(with_env, "Environment") == [
        "KUBECONFIG=/x/config",
        "BOOT_GRACE_S=420",
    ]


def test_timer_is_persistent_and_names_its_service(timer: str) -> None:
    assert directive(timer, "OnCalendar") == ["*-*-* *:23:00"]
    assert directive(timer, "Persistent") == ["true"], (
        "decided 2026-09-19: a slot missed inside an outage runs at boot rather than leaving "
        "the tile red until the next slot; the restart bounds the boot-time false down"
    )
    assert directive(timer, "Unit") == ["kuma-check-widget.service"]
    assert directive(timer, "WantedBy") == ["timers.target"]


@pytest.fixture(scope="module")
def boot() -> str:
    return _render(BOOT)


def _marker_from_stop_post(service: str) -> tuple[str, str]:
    """The (removed, created) marker paths of the service's ExecStopPost= shell line."""
    [stop_post] = directive(service, "ExecStopPost")
    removed = re.search(r"then rm -f (\S+);", stop_post)
    created = re.search(r"touch (\S+); fi", stop_post)
    assert removed and created, f"unexpected ExecStopPost= shape: {stop_post}"
    return removed.group(1), created.group(1)


def test_service_keeps_a_down_marker_that_survives_a_reboot(service: str) -> None:
    [stop_post] = directive(service, "ExecStopPost")
    assert stop_post.startswith("+"), (
        "the marker lives under /var/lib, so the line runs as root whatever User= is"
    )
    assert '"$$SERVICE_RESULT" = success' in stop_post, (
        "a single `$` is systemd's own substitution, which leaves the shell an empty test"
    )
    removed, created = _marker_from_stop_post(service)
    assert removed == created == "/var/lib/kuma-check/widget.down", (
        "the marker must survive the reboot, so not /run, and be the one path both arms touch"
    )


def test_boot_unit_reruns_the_service_only_while_the_marker_exists(
    service: str, boot: str
) -> None:
    _, created = _marker_from_stop_post(service)
    assert directive(boot, "ConditionPathExists") == [created], (
        "the boot unit gates on a different path than the service writes, so it never reruns"
    )
    assert directive(boot, "Type") == ["oneshot"]
    assert directive(boot, "ExecStart") == [
        "/usr/bin/systemctl start --no-block kuma-check-widget.service"
    ]
    for key in ("Wants", "Requires", "BindsTo"):
        assert not directive(boot, key), (
            f"{key}= pulls the service in even when the condition fails, so every check "
            "would run at every boot"
        )
    assert directive(boot, "WantedBy") == ["multi-user.target"]


def _systemd_tasks() -> list[dict]:
    return [
        t
        for t in leaf_tasks(load_tasks(TASKS))
        if "ansible.builtin.systemd" in t or "ansible.builtin.systemd_service" in t
    ]


def _systemd_spec(task: dict) -> dict:
    return (
        task.get("ansible.builtin.systemd") or task["ansible.builtin.systemd_service"]
    )


def test_task_file_wires_both_directions() -> None:
    """The way in enables and starts the timer; the way out stops, disables and removes it."""
    specs = [_systemd_spec(t) for t in _systemd_tasks()]
    assert any(
        s.get("enabled") is True and s.get("state") == "started" for s in specs
    ), "no task enables and starts the timer"
    assert any(
        s.get("enabled") is False and s.get("state") == "stopped" for s in specs
    ), (
        "no task stops and disables the timer: kuma_check_state: absent is a one-way door"
    )
    files_absent = [
        t
        for t in leaf_tasks(load_tasks(TASKS))
        if (t.get("ansible.builtin.file") or {}).get("state") == "absent"
    ]
    assert files_absent, "the absent arm leaves the unit files on the host"
    removed = {p for t in files_absent for p in t.get("loop") or []}
    for path in (
        "/etc/systemd/system/kuma-check-{{ kuma_check_name }}-boot.service",
        "/var/lib/kuma-check/{{ kuma_check_name }}.down",
    ):
        assert path in removed, f"the absent arm leaves {path} on the host"
    assert any(
        s.get("name") == "kuma-check-{{ kuma_check_name }}-boot.service"
        and s.get("enabled") is False
        for s in specs
    ), "the absent arm never disables the boot-rerun unit"
    assert any(
        s.get("name") == "kuma-check-{{ kuma_check_name }}-boot.service"
        and s.get("enabled") is True
        and "state" not in s
        for s in specs
    ), (
        "the present arm must enable the boot-rerun unit without starting it, or a deploy "
        "reruns every red check"
    )
    crons_absent = [
        t
        for t in leaf_tasks(load_tasks(TASKS))
        if (t.get("ansible.builtin.cron") or {}).get("state") == "absent"
    ]
    assert crons_absent, "the legacy cron is never removed, so a host runs both"


def test_task_file_carries_no_tags() -> None:
    """Tags union: a tag here would make the pair selectable by a tag the caller never chose."""
    for task in leaf_tasks(load_tasks(TASKS)):
        assert "tags" not in task, f"{task.get('name')!r} carries tags"


def _wired_checks() -> dict[str, dict]:
    """`kuma_check_name` -> the import task's vars, for every setup role that imports the file."""
    found: dict[str, dict] = {}
    for tasks_file in task_files(SETUP_ROLES):
        for task in leaf_tasks(load_tasks(tasks_file)):
            target = task.get("ansible.builtin.import_tasks") or task.get(
                "ansible.builtin.include_tasks"
            )
            if not isinstance(target, str) or not target.endswith(
                "common/tasks/kuma_check_timer.yml"
            ):
                continue
            variables = task.get("vars") or {}
            if variables.get("kuma_check_state") == "absent":
                continue  # a teardown arm, not a wiring
            found[str(variables.get("kuma_check_name"))] = variables
    return found


def test_every_known_check_is_wired_with_the_full_contract() -> None:
    wired = _wired_checks()
    missing = KNOWN_CHECKS - wired.keys()
    assert not missing, (
        f"checks no longer wired through kuma_check_timer.yml: {sorted(missing)}"
    )
    for name, variables in wired.items():
        for key in BASE_VARS:
            assert key in variables, f"{name}: import passes no {key}"
        assert "kuma_check_state" in variables, (
            f"{name}: no kuma_check_state, so the host that leaves the list keeps the timer"
        )
        if variables.get("kuma_check_cron_name"):
            assert "kuma_check_cron_user" in variables or "kuma_check_user" in variables


def _shell_template_for(exec_path: str) -> Path | None:
    """The `.sh.j2` under any setup role that renders to `exec_path`, or None for a non-shell exec."""
    name = Path(exec_path.split()[0]).name
    if not name.endswith(".sh"):
        return None
    hits = list(SETUP_ROLES.glob(f"*/templates/{name}.j2"))
    assert len(hits) == 1, (
        f"{name}.j2: expected one template under setup roles, found {hits}"
    )
    return hits[0]


# Shell producers that exit explicitly per branch instead of ending on EXIT_CONTRACT, and the
# test that drives each one and asserts `rc == 1` on a down verdict. A member here without
# that assertion is exactly the inert shape the contract line exists to rule out.
EXPLICIT_EXIT_PRODUCERS = {
    "github-ruleset-drift": "test_github_ruleset_drift.py",
    "github-interaction-limit": "test_github_interaction_limit.py",
}
# The audit script runs the Python CLI, whose `--push` exit is tested in
# scripts/secrets_mgmt/tests; its own arms must never exit 0 after a down push. Since H7.36 it
# is a component's script (COMPONENT_CHECKS), not a wired import, so none is left here.
EXEC_PRODUCERS: frozenset[str] = frozenset()

# The checks H7.36 moved from this task file into host components. Each keeps the contract the
# task file renders, written out as plain units: the service reruns on failure, keeps the down
# marker, carries no alert unit, and has a boot unit gated on the marker and a persistent timer.
# Named, so a component that drops one fails here by name.
COMPONENTS = SETUP_ROLES.parents[2] / "hosts" / "components"
COMPONENT_CHECKS = {
    "homelab-secret-rotation-audit": (
        "secret-rotation",
        "secret-rotation-audit.sh.j2",
    ),
    "homelab-loki-route-witness": (
        "loki-route-witness",
        "loki-read-route-health.sh.j2",
    ),
}


def test_every_wired_shell_producer_exits_nonzero_after_a_down_push() -> None:
    """A producer converted without the exit contract reruns nothing and reads as done."""
    checked = 0
    for name, variables in _wired_checks().items():
        template = _shell_template_for(str(variables["kuma_check_exec"]))
        if template is None:
            continue
        text = template.read_text()
        if name in EXPLICIT_EXIT_PRODUCERS:
            harness = (
                Path(__file__).parent / EXPLICIT_EXIT_PRODUCERS[name]
            ).read_text()
            assert "assert rc == 1" in harness and "assert rc == 0" in harness, (
                f"{name}: its harness no longer asserts both exit codes"
            )
        elif name in EXEC_PRODUCERS:
            assert not re.search(r"^\s*exit 0\b", text, re.M), (
                f"{name}: an `exit 0` after a down push would end the reruns"
            )
            assert "audit --push" in text
        else:
            assert text.rstrip().endswith(EXIT_CONTRACT), (
                f"{name}: {template.name} must end on the exit contract, after the final push"
            )
        checked += 1
    # Lower it only for a deliberate retirement. H7.36 moved the secret-rotation audit to a
    # component (COMPONENT_CHECKS), which took the floor from 6 to 5.
    assert checked >= 5, (
        f"only {checked} shell producers checked; the census has shrunk"
    )


def _component_unit(component: str, unit: str, suffix: str) -> str:
    units = COMPONENTS / component / "units"
    hit = next(iter(units.glob(f"{unit}{suffix}*")), None)
    assert hit, f"{component}: no {unit}{suffix} under {units}"
    return hit.read_text()


@pytest.mark.parametrize("unit", COMPONENT_CHECKS)
def test_a_packaged_check_keeps_the_rerun_marker_boot_and_timer_contract(unit):
    component, _ = COMPONENT_CHECKS[unit]
    service = _component_unit(component, unit, ".service")
    assert directive(service, "Type") == ["oneshot"]
    assert directive(service, "Restart") == ["on-failure"]
    assert not directive(service, "OnFailure")
    [stop_post] = directive(service, "ExecStopPost")
    assert stop_post.startswith("+") and '"$$SERVICE_RESULT" = success' in stop_post
    touched = re.search(r"touch (\S+); fi", stop_post)
    assert touched, stop_post
    marker = touched.group(1)
    boot = _component_unit(component, f"{unit}-boot", ".service")
    assert directive(boot, "ConditionPathExists") == [marker]
    assert directive(boot, "ExecStart") == [
        f"/usr/bin/systemctl start --no-block {unit}.service"
    ]
    for key in ("Wants", "Requires", "BindsTo"):
        assert not directive(boot, key)
    timer = _component_unit(component, unit, ".timer")
    assert directive(timer, "Persistent") == ["true"]


@pytest.mark.parametrize("unit", COMPONENT_CHECKS)
def test_a_packaged_check_script_never_exits_zero_after_a_down_push(unit):
    component, template = COMPONENT_CHECKS[unit]
    text = rendered_shell_text("components", component, template)
    if template.startswith("secret-rotation-audit"):
        assert not re.search(r"^\s*exit 0\b", text, re.M)
        assert "audit --push" in text
    else:
        assert text.rstrip().endswith(EXIT_CONTRACT)


def test_the_packaged_check_contract_is_flagged_when_the_rerun_is_dropped():
    """Rejecting half: a service without Restart=on-failure reruns nothing."""
    service = _component_unit(
        "secret-rotation", "homelab-secret-rotation-audit", ".service"
    )
    broken = service.replace("Restart=on-failure\n", "")
    assert directive(service, "Restart") == ["on-failure"]
    assert directive(broken, "Restart") == []
