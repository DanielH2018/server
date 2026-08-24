"""Guards for the `remember` plugin log-rotation heartbeat (k3s role, tag `remember-logs`).

The script itself is straightforward; what is easy to break is the pair of numeric couplings it
depends on, because each looks like an isolated tunable and neither fails loudly when moved.

- **The Kuma deadline must exceed the cron period.** The heartbeat is hourly, unlike every
  sibling in health-crons.yml, which are `*/10` against a 1200s interval. Copying that 1200 onto
  this monitor makes it fire DOWN every hour with nothing wrong — the exact flap the Cloudflare
  DDNS entries in static-monitors.yaml.j2 already record having paid for once.
- **The prune horizon must be strictly beyond the alarm horizon.** Pruning at the alarm horizon
  deletes the evidence that raised the alarm, so the next run finds a clean tree and reports UP.
  A permanent stall then shows as one DOWN run per day instead of staying DOWN, which is a blip
  no one is watching for. The gap is what keeps the monitor latched while rotation is broken.

Both are stated as comments at their definitions; these are the executable half, because a
comment does not fail CI when someone rounds 5400 down to 1200.
"""

from __future__ import annotations

import json
import re

import yaml
from _helpers import ANSIBLE

K3S_DEFAULTS = yaml.safe_load(
    (ANSIBLE / "roles/setup/k3s/defaults/main.yml").read_text()
)
MONITORS = (
    ANSIBLE / "roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2"
).read_text()
SCRIPT = (ANSIBLE / "roles/setup/k3s/templates/remember-logs-health.sh.j2").read_text()
HEALTH_CRONS = (ANSIBLE / "roles/setup/k3s/tasks/health-crons.yml").read_text()


def _monitor_entity() -> dict:
    """The rendered Remember Log Rotation entity, read out of the template by filename key.

    Parsed from the raw template rather than a Jinja render: every field this file asserts on is
    a literal, and rendering would need the whole stub set test_kuma_static_monitors.py carries.
    """
    m = re.search(r"^  remember-logs-health\.json: \|\n\s+(\{.*\})$", MONITORS, re.M)
    assert m, "remember-logs-health.json entity missing from static-monitors.yaml.j2"
    # Blank the one Jinja expression in the line so the rest parses as JSON.
    line = re.sub(r"\{\{[^}]*\}\}", "0", m.group(1))
    return json.loads(line)


def _cron_period_seconds(minute: str) -> int:
    """Seconds between two runs of a cron whose `minute` field is `minute`, hour unrestricted.

    Only the two forms this repo's heartbeats use are understood — a bare minute (hourly) and
    `*/N`. Anything else raises rather than guessing, so a future cadence cannot silently
    resolve to a period that makes the assertion below vacuous.
    """
    if minute.startswith("*/"):
        return int(minute[2:]) * 60
    if minute.isdigit():
        return 3600
    raise AssertionError(
        f"unhandled cron minute spec {minute!r} — teach this helper the form"
    )


def test_kuma_deadline_exceeds_the_cron_period():
    period = _cron_period_seconds(str(K3S_DEFAULTS["k3s_remember_logs_cron_minute"]))
    interval = _monitor_entity()["interval"]
    assert interval > period, (
        f"Kuma interval {interval}s does not exceed the {period}s cron period — the monitor "
        f"will fire DOWN every cycle with nothing wrong. Move the interval and "
        f"k3s_remember_logs_cron_minute together."
    )


def test_prune_horizon_is_strictly_beyond_the_alarm_horizon():
    alarm = K3S_DEFAULTS["k3s_remember_log_max_age_days"]
    prune = K3S_DEFAULTS["k3s_remember_log_prune_age_days"]
    assert prune > alarm, (
        f"prune horizon {prune}d must be strictly beyond the alarm horizon {alarm}d — pruning at "
        f"the alarm horizon erases the evidence that raised the alarm, so a permanent stall "
        f"reports UP on the run after every DOWN."
    )


def test_alarm_horizon_clears_the_plugins_own_rotation_window():
    """rotate_logs() archives at -mtime +7, so an alarm at or below 7d races it.

    A file that ages out minutes before consolidation's next hourly run is not a fault, and a
    monitor that calls it one is a monitor people learn to ignore.
    """
    alarm = K3S_DEFAULTS["k3s_remember_log_max_age_days"]
    assert alarm > 7, (
        f"alarm horizon {alarm}d is inside the plugin's own 7-day rotation window — a file "
        f"mid-rotation would read as a stall."
    )


def test_push_monitor_does_not_retry():
    """A push monitor with retries > 0 flaps on a single missed beat.

    Enforced fleet-wide by test_kuma_static_monitors.py; restated here because this entity's
    hourly producer makes a missed beat likelier than for its */10 siblings.
    """
    assert _monitor_entity()["max_retries"] == 0


def test_script_measures_state_not_the_plugins_breadcrumb():
    """The whole reason this heartbeat exists.

    `.rotate-failed` is written only inside rotate_logs(), which is called only from
    run-consolidation.sh — so a stalled consolidation writes no breadcrumb and a breadcrumb
    check reads green through the failure it exists to catch. If someone ever 'simplifies' this
    script to read the breadcrumb, that is the regression.
    """
    # Comments stripped first: the script's header EXPLAINS why it avoids the breadcrumb, so a
    # substring search over the whole file matches the rationale and fails on the thing it is
    # asserting.
    code = "\n".join(
        ln for ln in SCRIPT.splitlines() if not ln.lstrip().startswith("#")
    )
    assert ".rotate-failed" not in code, (
        "script reads the plugin's .rotate-failed breadcrumb — that file is never written when "
        "consolidation stalls, which is the failure this monitor exists to catch."
    )
    assert "-mtime" in code, (
        "script no longer measures file age — it cannot see a stall"
    )


def test_cron_runs_as_the_store_owner_not_root():
    """The prune must not run as root.

    The stores are {{ sys_user }}-owned under /home/{{ sys_user }}; a root prune would be free to
    delete outside them if the search root were ever wrong.
    """
    block = HEALTH_CRONS.split("Schedule the remember log-rotation heartbeat", 1)
    assert len(block) == 2, (
        "remember log-rotation cron task not found in health-crons.yml"
    )
    task = block[1].split("- name:", 1)[0]
    assert 'user: "{{ sys_user }}"' in task, "prune cron must run as sys_user, not root"
