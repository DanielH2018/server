"""disk-health.sh's DEADLINE_S is derived from its cron's schedule, not typed beside it.

`homelab_check_deadline_seconds{check="disk-health"}` is the longest legitimate gap between two
textfile writes. docs/target-architecture/monitoring/m3-textfile-timer.md (*The deadline for
disk-health*) sets it at two cron periods plus 300s and bounds it by three relations, which this
module checks against the RENDERED script:

1. it equals 2 x the cron's period + 300;
2. it exceeds one period plus the boot grace, so a skipped post-boot slot cannot age the file
   past it;
3. it is at most the Healthchecks.io slug's timeout plus grace, so Prometheus pages no later
   than the dead-man does.

The rule that reads the deadline carries it as a literal, and a later PR pins that literal to
this same rendered value.
"""

import re

from jinja2 import StrictUndefined

from lib.ansible_jinja_env import template_env
from lib.render_guard import render_or_error
from lib.repo_paths import K3S_ROLE, ROLES
from _shell_render import render_shell_script, shell_template_path
from lib.estate import role_defaults
from validate.shell_templates import template_context

TEMPLATE = ("setup", "k3s", "disk-health.sh.j2")
K3S = role_defaults(K3S_ROLE)
BRIDGE = role_defaults(ROLES / "k8s" / "monitor-bridge")
SLUG = "daniel-box-disk-health"
MINUTE_VAR = "k3s_disk_health_cron_minute"


def _rendered_deadline(minute: str | None = None) -> int:
    """DEADLINE_S as the script renders it, at the role default or at `minute`."""
    overrides = None if minute is None else {MINUTE_VAR: minute}
    body = render_shell_script(*TEMPLATE, overrides=overrides)
    match = re.search(r"^DEADLINE_S=(\d+)$", body, re.M)
    assert match, f"{'/'.join(TEMPLATE)} no longer renders a DEADLINE_S line"
    assert (
        'homelab_check_deadline_seconds{check=\\"disk-health\\"} ${DEADLINE_S}' in body
    ), "the deadline series no longer writes DEADLINE_S"
    return int(match.group(1))


def _hc_budget_s() -> int:
    """The slug's timeout plus grace in monitor_bridge_healthchecks_expected."""
    rows = [
        r for r in BRIDGE["monitor_bridge_healthchecks_expected"] if r["slug"] == SLUG
    ]
    assert len(rows) == 1, (
        f"monitor_bridge_healthchecks_expected has {len(rows)} {SLUG} rows"
    )
    return rows[0]["timeout"] + rows[0]["grace"]


def _violations(minute: str, deadline: int) -> list[str]:
    """Every relation `deadline` breaks for a cron at `minute`; empty when all three hold."""
    every = re.fullmatch(r"\*/(\d+)", minute)
    if not every:
        return [
            f"{MINUTE_VAR}={minute!r} is not a `*/N` cadence, so it has no single period"
        ]
    period = int(every.group(1)) * 60
    grace = K3S["k3s_health_cron_boot_grace_s"]
    budget = _hc_budget_s()
    found = []
    if deadline != 2 * period + 300:
        found.append(f"DEADLINE_S={deadline} is not 2 x {period}s + 300s")
    if deadline <= period + grace:
        found.append(
            f"DEADLINE_S={deadline} does not exceed one period plus the {grace}s boot grace"
        )
    if deadline > budget:
        found.append(
            f"DEADLINE_S={deadline} exceeds {SLUG}'s timeout + grace ({budget}s)"
        )
    return found


def _strict_render_error(minute: str) -> str | None:
    """The render error at `minute` when an undefined name raises, as it does under Ansible.

    The shared harness renders an undefined name as `STUB`, so the template's refusal of a
    minute that is not `*/N` only shows under `StrictUndefined`. Ansible raises there too, with
    the message the template hands `mandatory`.
    """
    path = shell_template_path(*TEMPLATE)
    env = template_env(path.parent, undefined_cls=StrictUndefined)
    _, err = render_or_error(
        env, path.name, template_context(path, {MINUTE_VAR: minute})
    )
    return err


def test_the_rendered_deadline_meets_all_three_relations_is_clean():
    # The minute the render context resolves: the role default with the inventory laid over
    # it, so a group_vars or host_vars override is what gets checked.
    minute = template_context(shell_template_path(*TEMPLATE))[MINUTE_VAR]
    assert _violations(minute, _rendered_deadline()) == []


def test_an_every_n_minute_renders_under_strict_undefined_is_clean():
    assert _strict_render_error("*/10") is None


def test_a_minute_with_no_single_period_fails_the_render_is_flagged():
    # "0,30" would render 300 and an hourly "5" 900, both shorter than the real gap.
    for minute in ("0,30", "5"):
        err = _strict_render_error(minute)
        assert err and "disk_health_cron_minute_not_every_n" in err, (minute, err)
        assert "DEADLINE_S=" not in render_shell_script(
            *TEMPLATE, overrides={MINUTE_VAR: minute}
        )


def test_a_cadence_the_dead_man_would_beat_is_flagged():
    # */30 renders 3900s, past the slug's 2100s: Healthchecks.io would page first. Rendering at
    # the override also proves DEADLINE_S follows the variable rather than a typed number.
    assert _rendered_deadline("*/30") == 3900
    assert _violations("*/30", 3900) == [
        f"DEADLINE_S=3900 exceeds {SLUG}'s timeout + grace ({_hc_budget_s()}s)"
    ]


def test_a_deadline_that_drifted_from_the_schedule_is_flagged():
    assert _violations("*/10", 1200)[0] == "DEADLINE_S=1200 is not 2 x 600s + 300s"


def test_a_minute_that_is_not_every_n_is_flagged():
    assert _violations("0,30", 1500) == [
        f"{MINUTE_VAR}='0,30' is not a `*/N` cadence, so it has no single period"
    ]
