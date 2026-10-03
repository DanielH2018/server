"""The maintenance window is derived from the reboot cron, and the sync never runs inside it.

Two schedules have to stay in step: `Weekly system restart` in
roles/setup/initial_setup/tasks/crons.yml, and the Kuma window this role declares to keep that
restart from paging Discord. Both read `weekly_reboot_*` from group_vars/all.yml, and these
hold that single source — a window that said one thing while the reboot did another would
suppress an ordinary hour and page through the restart.
"""

import json
import sys as _sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path as _Path

import yaml
from validate.k8s_manifests import make_env, make_lookup, register_ansible_filters

from lib.repo_paths import REPO

ROLE = _Path(__file__).resolve().parents[1]
_sys.path.insert(0, str(ROLE / "files"))

from render_maintenance import window_cron  # noqa: E402

SHARED_TEMPLATES = REPO / "ansible" / "templates"
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
GROUP_VARS = yaml.safe_load(
    (REPO / "ansible" / "inventory" / "group_vars" / "all.yml").read_text()
)
CRONS = (
    REPO / "ansible" / "roles" / "setup" / "initial_setup" / "tasks" / "crons.yml"
).read_text()

MINUTES_PER_WEEK = 7 * 24 * 60


def render(template, **overrides):
    context = {
        "k8s_namespace": "homelab",
        "playbook_dir": str(REPO / "ansible"),
        "tz": "America/Chicago",
        "puid": 1000,
        "pgid": 1000,
        **GROUP_VARS,
        **DEFAULTS,
        **overrides,
    }
    env = make_env([ROLE / "templates", SHARED_TEMPLATES])
    env.globals["lookup"] = make_lookup(context)
    register_ansible_filters(env)
    return env.get_template(template).render(context)


def window(**overrides):
    """The window the pod would compute, from the ConfigMap the deploy would render."""
    configmap = yaml.safe_load(
        render("maintenance-sync-configmap.yaml.j2", **overrides)
    )
    return window_cron(json.loads(configmap["data"]["window.json"]))


def window_timezone(**overrides):
    configmap = yaml.safe_load(
        render("maintenance-sync-configmap.yaml.j2", **overrides)
    )
    return json.loads(configmap["data"]["window.json"])["timezone"]


def week_minutes(cron, duration=1):
    """Every minute-of-week a five-field `cron` fires, spread over `duration` minutes.

    Only the shapes this role's two schedules use are understood — hourly `M * * * *`, daily
    `M H * * *` and weekly `M H * * D`. Anything else raises rather than reading as "no
    overlap": a schedule this cannot expand is one the collision check cannot vouch for.
    """
    minute, hour, day, month, weekday = cron.split()
    if (day, month) != ("*", "*"):
        raise AssertionError(f"unsupported schedule for this check: {cron}")
    hours = range(24) if hour == "*" else [int(hour)]
    weekdays = range(7) if weekday == "*" else [int(weekday)]
    if minute == "*":
        raise AssertionError(f"unsupported schedule for this check: {cron}")

    starts = [
        (day_of_week * 24 * 60) + (one_hour * 60) + int(minute)
        for day_of_week in weekdays
        for one_hour in hours
    ]
    return {
        (start + offset) % MINUTES_PER_WEEK
        for start in starts
        for offset in range(duration)
    }


def test_the_window_opens_before_the_reboot_and_closes_after_the_recovery():
    """07:25 to 08:15 at the committed schedule — the verify-by window."""
    assert window() == ("25 7 * * 0", 50)


def test_moving_the_reboot_moves_the_window():
    """The rejecting half: a window written as a literal would not follow the cron."""
    assert window(weekly_reboot_hour=3, weekly_reboot_minute=10) == ("5 3 * * 0", 50)


def test_a_shorter_shutdown_delay_shortens_the_window():
    """The `+5` is load-bearing, not decoration: the window spans it."""
    assert window(weekly_reboot_shutdown_delay_minutes=2)[1] == 47


def test_the_reboot_cron_reads_the_same_variables_rather_than_repeating_them():
    for variable in (
        "weekly_reboot_minute",
        "weekly_reboot_hour",
        "weekly_reboot_weekday",
        "weekly_reboot_shutdown_delay_minutes",
    ):
        assert variable in GROUP_VARS, variable
        assert "{{ " + variable + " }}" in CRONS, variable
    assert "shutdown -r +{{ weekly_reboot_shutdown_delay_minutes }}" in CRONS


def test_the_sync_never_runs_inside_the_window_it_declares():
    # fact: ansible/roles/k8s/uptime-kuma/CLAUDE.md#The weekly reboot's maintenance window is reconciled over the API
    """An `edit` calls Kuma's `bean.run(true)`, which restarts the window's own cron job — a
    run landing inside the open window could lift the suppression it exists to provide."""
    cron, duration = window()
    open_minutes = week_minutes(cron, duration)
    runs = week_minutes(DEFAULTS["uptime_kuma_k8s_maintenance_sync_schedule"])
    assert not (runs & open_minutes), sorted(runs & open_minutes)


def test_a_sync_schedule_inside_the_window_is_caught():
    """The rejecting half of the check above."""
    cron, duration = window()
    assert week_minutes("30 7 * * *") & week_minutes(cron, duration)


def test_the_configmap_ships_the_script_beside_the_window():
    data = yaml.safe_load(render("maintenance-sync-configmap.yaml.j2"))["data"]
    compile(data["render_maintenance.py"], "render_maintenance.py", "exec")
    assert (
        json.loads(data["window.json"])["title"]
        == (DEFAULTS["uptime_kuma_k8s_maintenance_window_title"])
    )


def test_the_job_applies_only_after_every_read_and_decision_succeeded():
    """`apply` is the pod's one non-init container, so a failed read never writes."""
    spec = yaml.safe_load(render("maintenance-sync-cronjob.yaml.j2"))["spec"][
        "jobTemplate"
    ]["spec"]["template"]["spec"]
    assert [c["name"] for c in spec["initContainers"]] == [
        "dump",
        "select",
        "detail",
        "render",
    ]
    assert [c["name"] for c in spec["containers"]] == ["apply"]
    assert spec["containers"][0]["args"][0].count("/work/desired.json") == 2


def test_the_window_is_declared_on_the_reboot_crons_clock():
    """A host crontab fires on the host's clock, which is UTC here; `tz` is the containers'
    America/Chicago and would put the window five hours off the reboot. Kuma's own identifier
    list has no `UTC` entry, so the window names a zone that is +00:00 all year instead."""
    assert GROUP_VARS["weekly_reboot_timezone"] == "UTC"
    declared = ZoneInfo(window_timezone())
    for month in (1, 7):
        offset = datetime(2026, month, 15, tzinfo=declared).utcoffset()
        assert offset.total_seconds() == 0, (month, offset)


def test_the_containers_display_timezone_would_be_caught():
    """The rejecting half: `tz` is what this was written as first, and it is five hours out."""
    chicago = datetime(2026, 7, 15, tzinfo=ZoneInfo("America/Chicago")).utcoffset()
    assert chicago.total_seconds() != 0


def test_the_sync_and_the_window_share_one_clock():
    """The collision check above compares two schedules as numbers; two clocks make it lie."""
    doc = yaml.safe_load(render("maintenance-sync-cronjob.yaml.j2"))
    assert doc["spec"]["timeZone"] == window_timezone()
