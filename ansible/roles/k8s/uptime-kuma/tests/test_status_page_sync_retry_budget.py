"""The dump stage's retry window has to outlast a Kuma restart, and fit the job deadline.

Kuma's Service has no endpoint at all for ~100s of a deploy rollout and for ~16 minutes of the
Sunday "Weekly system restart" cron, so a `dump` that gives up early turns a routine restart
into an Init:Error job and a missed status-page-sync heartbeat. The attempt cap is derived in
the template from two budget variables; these pin the derivation, the arithmetic that makes it
survive a restart, and the fact that it still fits inside one cron period.
"""

import re
import sys as _sys
from pathlib import Path as _Path

import yaml

ROLE = _Path(__file__).resolve().parents[1]
REPO = ROLE.parents[3]
_sys.path.insert(0, str(REPO / "scripts"))

from validate.k8s_manifests import make_env, make_lookup, register_ansible_filters  # noqa: E402

SHARED_TEMPLATES = REPO / "ansible" / "templates"
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())

# What the job actually has to absorb, which is NOT the length of the outage. The reboot cron
# and the schedule are both fixed, so the ticks either side of the outage are known: the 07:30
# run finishes before `shutdown -r +5` fires at 07:35, and the 07:45 run is the one that has to
# wait. Measured 2026-09-06: daniel-server booted at 07:48:25 and Kuma was Ready at ~07:51, so
# the 07:45 run needed ~6 minutes. Boot time varies week to week (07:38 and 07:41 on the two
# preceding Sundays), hence the margin asserted below rather than a bare comparison.
SUNDAY_REBOOT_WAIT_SECONDS = 6 * 60
# A deploy rollout removes the endpoint for longer than Kuma itself is down: `strategy: Recreate`
# leaves no old pod, and autokuma's startupProbe holds the new pod NotReady past the point where
# uptime-kuma is serving. Measured 2026-09-06: SIGTERM 11:03:35, pod Ready 11:05:18.
ROLLOUT_ENDPOINT_GAP_SECONDS = 103
# `*/15 * * * *`. A deadline past this would let `concurrencyPolicy: Forbid` skip more than the
# single following tick.
CRON_PERIOD_SECONDS = 15 * 60


def render(**overrides):
    context = {
        "k8s_namespace": "homelab",
        "playbook_dir": str(REPO / "ansible"),
        "tz": "America/Chicago",
        "puid": 1000,
        "pgid": 1000,
        **DEFAULTS,
        **overrides,
    }
    env = make_env([ROLE / "templates", SHARED_TEMPLATES])
    env.globals["lookup"] = make_lookup(context)
    register_ansible_filters(env)
    return env.get_template("status-page-sync-cronjob.yaml.j2").render(context)


def dump_attempt_cap(text):
    """The `attempt -le N` bound out of the rendered dump stage."""
    doc = yaml.safe_load(text)
    dump = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["initContainers"][0]
    assert dump["name"] == "dump", dump["name"]
    match = re.search(r'attempt"?\s*\]?\s*-le\s+"?(\d+)"?', dump["args"][0])
    assert match, dump["args"][0]
    return int(match.group(1))


def test_the_dump_attempt_cap_comes_out_of_the_budget_variables():
    expected = (
        DEFAULTS["kuma_status_page_sync_dump_budget_seconds"]
        // DEFAULTS["kuma_status_page_sync_dump_attempt_seconds"]
    )
    assert expected > 3, (
        "a cap this small is the pre-2026-09-06 value that failed every restart"
    )
    assert dump_attempt_cap(render()) == expected


def test_a_bigger_budget_moves_the_cap():
    """The rejecting half: a cap hardcoded back to a literal would not move with the budget."""
    doubled = render(
        kuma_status_page_sync_dump_budget_seconds=(
            DEFAULTS["kuma_status_page_sync_dump_budget_seconds"] * 2
        )
    )
    assert dump_attempt_cap(doubled) == dump_attempt_cap(render()) * 2


def test_the_retry_window_outlasts_the_sunday_reboot_with_margin():
    """`backoffLimit: 1` runs two pods, so the job's total wait is two budgets."""
    doc = yaml.safe_load(render())
    assert doc["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 1
    total = DEFAULTS["kuma_status_page_sync_dump_budget_seconds"] * 2
    assert total > SUNDAY_REBOOT_WAIT_SECONDS * 1.5


def test_the_first_pod_alone_covers_a_deploy_rollout():
    """A rollout can start at any moment, so its whole endpoint gap falls on one pod."""
    assert (
        DEFAULTS["kuma_status_page_sync_dump_budget_seconds"]
        > ROLLOUT_ENDPOINT_GAP_SECONDS
    )


def test_both_retry_windows_fit_inside_the_job_deadline():
    """Otherwise the pod is killed mid-attempt and reports DeadlineExceeded, not the diagnostic."""
    doc = yaml.safe_load(render())
    deadline = doc["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"]
    assert deadline == DEFAULTS["kuma_status_page_sync_deadline_seconds"]
    assert DEFAULTS["kuma_status_page_sync_dump_budget_seconds"] * 2 < deadline


def test_the_deadline_stays_inside_one_cron_period():
    assert DEFAULTS["kuma_status_page_sync_schedule"] == "*/15 * * * *"
    assert DEFAULTS["kuma_status_page_sync_deadline_seconds"] < CRON_PERIOD_SECONDS
