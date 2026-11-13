"""Every host counts its failed system units in a textfile verdict its rules read (#8219).

node-exporter runs without --collector.systemd, so a unit with no OnFailure= hook and no
textfile verdict of its own failed with nothing watching it. `systemd-units-check.sh` in the
host-maintenance component covers every unit at once, and three properties hold:

THE VERDICT IS THE COUNT. No failed unit writes success 1; any failed unit, or a systemctl that
cannot list them, writes 0. Only the second fails the check's own unit, so the checker never
lists itself beside the unit it found.

THE RULES' LITERALS EQUAL THE SCRIPT'S DEADLINE_S, on one Failed and NotReporting pair per host.

EVERY HOST SHIPS AND ARMS THE TIMER, at a period that fits three runs inside the deadline.

Run: uv run pytest hosts/tests/test_systemd_units_check.py
"""

import os
import re

import pytest
from _aide_check_run import LOGGER_STUB
from _helpers import K8S_ROLES, REPO
from _host_packages import HOSTS, package, shipped_text
from lib import yaml_fast
from lib.proc_testing import fake_bin, run, write_exec

COMPONENT = REPO / "hosts" / "components" / "host-maintenance"
SCRIPT = COMPONENT / "files" / "systemd-units-check.sh"
CHECK_LIB = (
    REPO / "hosts" / "components" / "check-lib" / "files" / "homelab-check-lib.sh"
)
RULES = K8S_ROLES / "observability" / "files" / "rules" / "host-checks.yaml"
TIMER = "/usr/lib/systemd/system/homelab-systemd-units.timer"
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")

TWO_FAILED = """\
homelab-claude-pipeline-timeline.service loaded failed failed Claude pipeline timeline
nut-monitor.service                      loaded failed failed Network UPS Tools monitor
"""


def _run(tmp_path, listed: str, rc: int = 0):
    """Run the check with `systemctl list-units` printing `listed` and exiting `rc`.

    Returns (exit code, stdout, every `homelab_*` series of check systemd-units by name).
    """
    script = write_exec(tmp_path / "systemd-units-check.sh", SCRIPT.read_text())
    textfile = tmp_path / "textfile"
    textfile.mkdir()
    (tmp_path / "listed").write_text(listed)
    stubs = fake_bin(
        tmp_path / "bin",
        logger=LOGGER_STUB,
        systemctl=f'cat "{tmp_path / "listed"}"\nexit {rc}\n',
    )
    result = run(
        [str(script)],
        env={
            **os.environ,
            "HOMELAB_LIB_DIR": str(CHECK_LIB.parent),
            "TEXTFILE_DIR": str(textfile),
            "LOGGER_OUT": str(tmp_path / "logged"),
        },
        stub_bin=stubs,
    )
    prom = (textfile / "systemd-units.prom").read_text()
    series = dict(
        re.findall(r'^(homelab_\w+)\{check="systemd-units"\} (\d+)$', prom, re.M)
    )
    return result.returncode, result.stdout, series


def deadline() -> int:
    [found] = re.findall(r"^DEADLINE_S=(\d+)$", SCRIPT.read_text(), re.M)
    return int(found)


def test_no_failed_unit_writes_success_1(tmp_path):
    code, _, series = _run(tmp_path, "")
    assert (code, series["homelab_check_success"]) == (0, "1")
    assert series["homelab_systemd_failed_units"] == "0"
    assert series["homelab_check_deadline_seconds"] == str(deadline())


def test_a_failed_unit_writes_success_0_with_the_count(tmp_path):
    code, out, series = _run(tmp_path, TWO_FAILED)
    assert (code, series["homelab_check_success"]) == (0, "0")
    assert series["homelab_systemd_failed_units"] == "2"
    assert "homelab-claude-pipeline-timeline.service nut-monitor.service" in out


def test_a_systemctl_that_cannot_list_writes_success_0(tmp_path):
    code, _, series = _run(tmp_path, "Failed to connect to bus", rc=1)
    assert (code, series["homelab_check_success"]) == (1, "0")


def rules(text: str) -> dict[tuple[str, str], str]:
    """(alert, origin) -> expr for every SystemdUnits* rule."""
    [group] = yaml_fast.safe_load(text)["groups"]
    found = {}
    for rule in group["rules"]:
        if rule["alert"].startswith("SystemdUnits"):
            [origin] = re.findall(r'origin="([^"]+)"', rule["expr"])[:1]
            found[(rule["alert"], origin)] = rule["expr"]
    return found


def literal_problems(found: dict[tuple[str, str], str], deadline_s: int) -> list[str]:
    """Why the rule set or a NotReporting rule's literals differ from the script, or nothing."""
    expected = {
        (alert, host)
        for alert in ("SystemdUnitsFailed", "SystemdUnitsNotReporting")
        for host in HOSTS
    }
    problems = [f"no rule {key}" for key in sorted(expected - set(found))]
    problems += [f"an unexpected rule {key}" for key in sorted(set(found) - expected)]
    for (alert, origin), expr in sorted(found.items()):
        if alert != "SystemdUnitsNotReporting":
            continue
        for name, values in (
            ("threshold", STALE_ARM.findall(expr)),
            ("window", WINDOW.findall(expr)),
        ):
            if values != [str(deadline_s)]:
                problems.append(
                    f"{origin}: {name} reads {values}, DEADLINE_S is {deadline_s}"
                )
    return problems


def test_literal_problems_is_clean_on_the_shipped_rules():
    assert literal_problems(rules(RULES.read_text()), deadline()) == []


def test_literal_problems_is_flagged_on_a_window_that_drifted():
    text = RULES.read_text().replace(
        'check="systemd-units",origin="daniel-pi"}[900s]',
        'check="systemd-units",origin="daniel-pi"}[600s]',
    )
    assert literal_problems(rules(text), deadline()) == [
        "daniel-pi: window reads ['600'], DEADLINE_S is 900"
    ]


@pytest.mark.parametrize("host", HOSTS)
def test_every_host_arms_a_timer_that_runs_three_times_per_deadline(host):
    assert any(
        path.endswith("/homelab-systemd-units.timer") for path in package(host).links
    )
    [minutes] = re.findall(r"^OnCalendar=\*:0/(\d+)$", shipped_text(host, TIMER), re.M)
    assert int(minutes) * 60 * 3 == deadline()
