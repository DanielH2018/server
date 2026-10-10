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

Run: uv run pytest ansible/tests/setup/test_alert_unit.py
"""

import json
import re
from pathlib import Path

import jinja2
from lib.ansible_jinja_env import make_ansible_env
from _helpers import SETUP_ROLES, leaf_tasks, load_tasks
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


def test_each_message_renders_a_valid_json_payload() -> None:
    template = UNIT.read_text()
    for name, variables in _wired_alerts().items():
        message = _render(
            variables["alert_unit_message"],
            inventory_hostname="daniel-box",
            claude_code_memory_sync_target="daniel-server",
            claude_code_agent_user="claude",
        )
        unit = _render(template, **{**variables, "alert_unit_message": message})
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
