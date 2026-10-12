"""Every setup-role Discord failure alert renders from the one shared template.

`roles/setup/common/tasks/alert_unit.yml` installs `<name>-alert.service` and its webhook file
from `common/templates/`. Five role-local copies preceded it, and they drifted on a security
property: one embedded the webhook in unit text after the others had moved off that (#3694).
Three properties keep the single source single and correct:

1. Every `OnFailure=<name>-alert.service` in a setup unit template is installed by an import
   of the task file with that `alert_unit_name`, and no role carries its own alert template.
2. Each caller's message renders into ExecStart's single-quoted JSON as valid JSON. A quote in
   the message would break the payload, and curl would post a body Discord rejects.
3. The webhook file is root 0600 under `no_log`, and the task file carries no tags.
4. A caller that sets `alert_unit_quiet_minutes` pages once per window, not once per exit of
   its parent's restart loop (#4676), and a caller that does not renders no gate.

Run: uv run pytest ansible/tests/setup/test_alert_unit.py
"""

import json
import os
import re
from pathlib import Path

import jinja2
from lib.ansible_jinja_env import make_ansible_env
from lib.proc_testing import run
from _helpers import SETUP_ROLES, leaf_tasks, load_tasks, load_yaml
from _role_census import task_files_by_role

COMMON = SETUP_ROLES / "common"
UNIT = COMMON / "templates" / "unit-failure-alert.service.j2"
TASKS = COMMON / "tasks" / "alert_unit.yml"

# Named members, so a census that stops matching fails by name rather than agreeing with an
# empty set.
KNOWN_ALERTS = frozenset(
    {
        # One per agent user, named after it.
        "{{ claude_code_agent_user }}-clone-sync",
        "claude-memory-sync",
        "claude-rc",
        "gitops-deploy",
        "renovate-agent",
        "renovate-notify",
    }
)
CALLER_VARS = (
    "alert_unit_name",
    "alert_unit_description",
    "alert_unit_message",
    "alert_unit_env_dir",
)


def _wired_alerts_by_role() -> dict[str, tuple[Path, dict]]:
    """`alert_unit_name` -> (calling role directory, the import task's vars)."""
    found: dict[str, tuple[Path, dict]] = {}
    for role, tasks_file in task_files_by_role(SETUP_ROLES):
        for task in leaf_tasks(load_tasks(tasks_file)):
            target = task.get("ansible.builtin.import_tasks")
            if isinstance(target, str) and target.endswith(
                "common/tasks/alert_unit.yml"
            ):
                variables = task.get("vars") or {}
                found[str(variables.get("alert_unit_name"))] = (role, variables)
    return found


def _wired_alerts() -> dict[str, dict]:
    """`alert_unit_name` -> the import task's vars, for every setup role importing the file."""
    return {name: variables for name, (_, variables) in _wired_alerts_by_role().items()}


def onfailure_alerts(unit_texts: list[str]) -> set[str]:
    """Every `<name>` the given unit texts name as `OnFailure=<name>-alert.service`."""
    names: set[str] = set()
    for text in unit_texts:
        names |= set(re.findall(r"^OnFailure=(.+)-alert\.service$", text, re.MULTILINE))
    return names


def unwired_alerts(unit_texts: list[str], wired: set[str]) -> set[str]:
    """The OnFailure= alert names no import of the shared task file installs."""
    return onfailure_alerts(unit_texts) - wired


def local_alert_templates(paths: list[Path]) -> list[Path]:
    """The `*-alert.service.j2` paths that are not the shared template."""
    return [p for p in paths if p.name.endswith("-alert.service.j2") and p != UNIT]


def _render(text: str, **context: object) -> str:
    env = make_ansible_env(undefined_cls=jinja2.StrictUndefined)
    return env.from_string(text).render(context)


def test_every_onfailure_alert_renders_from_the_shared_template() -> None:
    wired = _wired_alerts()
    missing = KNOWN_ALERTS - wired.keys()
    assert not missing, (
        f"alerts no longer wired through alert_unit.yml: {sorted(missing)}"
    )
    texts = [p.read_text() for p in SETUP_ROLES.glob("*/templates/*.j2")]
    assert KNOWN_ALERTS <= onfailure_alerts(texts), "the OnFailure= scan found too few"
    unwired = unwired_alerts(texts, set(wired))
    assert not unwired, (
        f"OnFailure= names {sorted(unwired)}-alert.service, but no import of "
        f"common/tasks/alert_unit.yml installs it under that alert_unit_name"
    )
    for name, variables in wired.items():
        for key in CALLER_VARS:
            assert key in variables, f"{name}: import passes no {key}"


def test_an_onfailure_alert_with_no_import_is_flagged() -> None:
    unit = "[Unit]\nOnFailure=widget-alert.service\n"
    assert unwired_alerts([unit], {"widget"}) == set()
    assert unwired_alerts([unit], {"gitops-deploy"}) == {"widget"}


def test_no_role_carries_its_own_alert_template() -> None:
    local = local_alert_templates(sorted(SETUP_ROLES.glob("*/templates/*.j2")))
    assert not local, (
        f"{local} copy the alert unit; import common/tasks/alert_unit.yml instead"
    )


def test_a_role_local_alert_template_is_flagged() -> None:
    stray = SETUP_ROLES / "widget" / "templates" / "widget-alert.service.j2"
    assert local_alert_templates([UNIT]) == []
    assert local_alert_templates([UNIT, stray]) == [stray]


# What a caller's vars may reference, so each one renders as the play would render it.
CALLER_CONTEXT = {
    "inventory_hostname": "daniel-box",
    "claude_code_memory_sync_target": "daniel-server",
    "claude_code_agent_user": "claude",
    **load_yaml(SETUP_ROLES / "claude_code" / "defaults" / "main.yml"),
}


def _caller_vars(variables: dict) -> dict:
    """A caller's import vars with the Jinja in each string value resolved."""
    return {
        k: _render(v, **CALLER_CONTEXT) if isinstance(v, str) else v
        for k, v in variables.items()
    }


def test_each_message_renders_a_valid_json_payload() -> None:
    template = UNIT.read_text()
    for name, variables in _wired_alerts().items():
        resolved = _caller_vars(variables)
        message = resolved["alert_unit_message"]
        unit = _render(template, **resolved)
        assert "{{" not in message, f"{name}: curl's --expand-data would expand {{{{"
        payload = expanded_payload(unit, "2026-10-08 11:04 UTC")
        assert payload is not None, f"{name}: no single-quoted payload in ExecStart"
        assert json.loads(payload) == {
            "content": f"{message} Failed at 2026-10-08 11:04 UTC."
        }, f"{name}: the message does not survive as the JSON payload's content"
        assert (
            f"EnvironmentFile={variables['alert_unit_env_dir']}/alert-webhook.env"
            in unit
        )


def expanded_payload(unit: str, failed_at: str) -> str | None:
    """The `--expand-data` body as curl sends it, with the recorded failure time filled in."""
    found = re.search(r"--expand-data '([^']*)'", unit)
    if not found:
        return None
    return found.group(1).replace(
        "{{failed_at:trim:json}}", json.dumps(failed_at)[1:-1]
    )


def service_settings(unit: str) -> dict[str, str]:
    """The `[Service]` section's `Key=value` lines, comments and continuations skipped."""
    section = unit.split("[Service]", 1)[1]
    return dict(
        line.split("=", 1)
        for line in section.splitlines()
        if "=" in line and not line.startswith(("#", " "))
    )


def retries_until_delivered(settings: dict[str, str]) -> bool:
    """True when systemd re-runs a failed delivery and stops only on a malformed URL.

    Exit 22 must retry: curl -f gives it for a transient 5xx or 429 as well as a 4xx.
    """
    return (
        settings.get("Restart") == "on-failure"
        and bool(settings.get("RestartSec"))
        and settings.get("RestartPreventExitStatus", "").split() == ["3"]
    )


def test_a_failed_delivery_retries_until_the_network_returns() -> None:
    # curl's own retries span about two minutes. daniel-box reached nothing off the host from
    # 2026-10-05 23:55 to about 2026-10-08 21:55 UTC, and every page fired inside that window
    # exited 6 with nothing to try again (#3902).
    unit = _render(
        UNIT.read_text(),
        alert_unit_name="widget",
        alert_unit_description="Widget",
        alert_unit_message="widget failed",
        alert_unit_env_dir="/etc/widget",
    )
    settings = service_settings(unit)
    assert settings.get("Type") == "oneshot", "the [Service] parse found no Type="
    assert retries_until_delivered(settings), (
        "a failed page must retry: Restart=on-failure, a RestartSec, and "
        "RestartPreventExitStatus=3 so only a malformed URL is final"
    )


def test_an_alert_that_gives_up_or_retries_a_rejection_is_flagged() -> None:
    retrying = {
        "Restart": "on-failure",
        "RestartSec": "5min",
        "RestartPreventExitStatus": "3",
    }
    assert retries_until_delivered(retrying)
    assert not retries_until_delivered(
        {k: v for k, v in retrying.items() if k != "Restart"}
    )
    assert not retries_until_delivered({**retrying, "RestartPreventExitStatus": "3 6"})
    assert not retries_until_delivered({**retrying, "RestartPreventExitStatus": "3 22"})
    assert not retries_until_delivered({**retrying, "RestartPreventExitStatus": ""})


def records_the_first_failure_time(unit: str, name: str) -> bool:
    """True when the first attempt writes the time, retries keep it, and curl posts it.

    The file must outlive a Restart= re-run (RuntimeDirectoryPreserve=restart) and must not be
    overwritten by one (`test -s ... ||`), or a late page names the retry's time.
    """
    settings = service_settings(unit)
    stamp = f"%t/{name}-alert/failed-at"
    return (
        settings.get("RuntimeDirectory") == f"{name}-alert"
        and settings.get("RuntimeDirectoryPreserve") == "restart"
        and settings.get("ExecStartPre", "").startswith(
            f"/bin/sh -c 'test -s {stamp} || "
        )
        and settings.get("ExecStartPre", "").endswith(f"> {stamp}'")
        and f"--variable 'failed_at@{stamp}'" in unit
    )


def test_a_late_page_names_the_time_its_parent_failed() -> None:
    # The alert retries for as long as the outage lasts, so a page can arrive days after the
    # failure and read as a fresh one (#3906).
    unit = _render(
        UNIT.read_text(),
        alert_unit_name="widget",
        alert_unit_description="Widget",
        alert_unit_message="widget failed.",
        alert_unit_env_dir="/etc/widget",
    )
    assert records_the_first_failure_time(unit, "widget")


def test_a_stamp_a_retry_drops_or_overwrites_is_flagged() -> None:
    unit = _render(
        UNIT.read_text(),
        alert_unit_name="widget",
        alert_unit_description="Widget",
        alert_unit_message="widget failed.",
        alert_unit_env_dir="/etc/widget",
    )
    assert not records_the_first_failure_time(
        unit.replace("RuntimeDirectoryPreserve=restart\n", ""), "widget"
    )
    assert not records_the_first_failure_time(
        unit.replace("test -s %t/widget-alert/failed-at || ", ""), "widget"
    )


def quiet_gate(unit: str, name: str) -> str | None:
    """The ExecCondition shell command as the shell runs it, or None when the gate is broken.

    The gate must read a stamp in the StateDirectory, because systemd removes the
    RuntimeDirectory after each delivery, and only ExecStartPost may write it, so an attempt
    that failed leaves the page eligible.
    """
    settings = service_settings(unit)
    stamp = f"%S/{name}-alert/delivered"
    condition = settings.get("ExecCondition", "")
    if not (
        settings.get("StateDirectory") == f"{name}-alert"
        and f"ExecStartPost=/usr/bin/touch {stamp}\n" in unit
        and stamp in condition
        and stamp not in settings.get("ExecStartPre", "")
    ):
        return None
    found = re.fullmatch(r"/bin/sh -c '(.*)'", condition)
    return found.group(1).replace("$$", "$") if found else None


def gate_exit(command: str, state_dir: Path) -> int:
    """Run the gate with `%S` pointed at `state_dir`; 0 pages, 1-254 skips."""
    return run(["/bin/sh", "-c", command.replace("%S", str(state_dir))]).returncode


def _quiet_unit(minutes: int | None) -> str:
    extra = {} if minutes is None else {"alert_unit_quiet_minutes": minutes}
    return _render(
        UNIT.read_text(),
        alert_unit_name="widget",
        alert_unit_description="Widget",
        alert_unit_message="widget failed.",
        alert_unit_env_dir="/etc/widget",
        **extra,
    )


def test_a_delivered_page_silences_the_alert_for_the_quiet_period(
    tmp_path: Path,
) -> None:
    # A parent with Restart=always fires OnFailure= on every exit of its restart loop: 1,540
    # to 1,686 claude-rc alert runs a day from 2026-10-05 to 2026-10-08 (#4676).
    command = quiet_gate(_quiet_unit(360), "widget")
    assert command is not None, "the quiet-period gate did not render"
    stamp = tmp_path / "widget-alert" / "delivered"
    assert gate_exit(command, tmp_path) == 0, (
        "no page yet, so the first one must go out"
    )
    stamp.parent.mkdir()
    stamp.touch()
    assert gate_exit(command, tmp_path) == 1, "a page inside the window must be skipped"
    # A fixed instant long before any run, so the test reads no clock of its own.
    os.utime(stamp, (1_759_276_800, 1_759_276_800))  # 2025-10-01 00:00 UTC
    assert gate_exit(command, tmp_path) == 0, "a page after the window must go out"


def test_a_gate_that_stamps_attempts_or_loses_its_stamp_is_flagged() -> None:
    unit = _quiet_unit(360)
    assert quiet_gate(unit, "widget") is not None
    assert (
        quiet_gate(unit.replace("StateDirectory=widget-alert\n", ""), "widget") is None
    )
    assert (
        quiet_gate(
            unit.replace("ExecStartPost=/usr/bin/touch", "ExecStartPre=/usr/bin/touch"),
            "widget",
        )
        is None
    )


def test_only_a_positive_quiet_period_renders_the_gate() -> None:
    # The variable is the switch: the five callers that leave it unset keep a page per
    # failure, and the one that sets it gets the gate.
    for minutes in (None, 0):
        unit = _quiet_unit(minutes)
        assert quiet_gate(unit, "widget") is None, f"{minutes!r} rendered a gate"
        assert "ExecCondition" not in service_settings(unit)
    assert quiet_gate(_quiet_unit(1), "widget") is not None, "1 rendered no gate"


def test_the_claude_rc_alert_pages_once_per_quiet_period() -> None:
    # claude-rc.service is the Restart=always parent the gate exists for.
    resolved = _caller_vars(_wired_alerts()["claude-rc"])
    minutes = int(resolved.get("alert_unit_quiet_minutes", 0))
    assert minutes > 0, "the claude-rc alert has no quiet period"
    unit = _render(UNIT.read_text(), **resolved)
    assert quiet_gate(unit, "claude-rc") is not None
    assert f"-mmin -{minutes} " in unit


def test_every_notified_handler_exists_in_the_calling_role() -> None:
    # A notify naming no handler fails the play only when the task changes, which is the
    # deploy that edits the alert, not the one that adds the caller.
    for name, (role, variables) in _wired_alerts_by_role().items():
        handlers = {h["name"] for h in load_tasks(role / "handlers" / "main.yml")}
        for handler in ["Reload systemd", *variables.get("alert_unit_notify", [])]:
            assert handler in handlers, (
                f"{name}: {role.name} defines no handler {handler!r}"
            )


def test_a_quote_in_the_message_breaks_the_payload() -> None:
    # The red half of the check above: a `"` ends the JSON string early.
    unit = _render(
        UNIT.read_text(),
        alert_unit_name="widget",
        alert_unit_description="Widget",
        alert_unit_message='widget "failed"',
        alert_unit_env_dir="/etc/widget",
    )
    payload = expanded_payload(unit, "2026-10-08 11:04 UTC")
    assert payload
    try:
        json.loads(payload)
    except json.JSONDecodeError:
        return
    raise AssertionError(
        "a quoted message parsed as JSON; the check above proves nothing"
    )


def test_webhook_file_is_root_only_and_task_file_has_no_tags() -> None:
    tasks = leaf_tasks(load_tasks(TASKS))
    for task in tasks:
        assert "tags" not in task, f"{task['name']}: tags here union with the caller's"
    webhook = next(
        t
        for t in tasks
        if t["ansible.builtin.template"]["dest"].endswith("/alert-webhook.env")
    )
    spec = webhook["ansible.builtin.template"]
    assert (spec["owner"], spec["group"], spec["mode"]) == ("root", "root", "0600")
    assert webhook.get("no_log") is True
