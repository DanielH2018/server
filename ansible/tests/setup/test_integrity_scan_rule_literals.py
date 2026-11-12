"""The AIDE and rkhunter NotReporting literals equal the DEADLINE_S each scan renders (#6168).

`files/rules/host-checks.yaml` is plain YAML, so each NotReporting rule carries its deadline as a
literal twice: the `>` of the stale-file arm and the `[<n>s]` window of the absent_over_time arm.
One DEADLINE_S pins the rule of every host whose package ships that scan's timer; daniel-pi's
lists no aide-scan, and a rule paging for a host with no timer is flagged. A timer moved without its
rules fails here rather than paging on every healthy week (M1 convention 1). The DEADLINE_S
equalled the interval of the Kuma tile the rules shadowed until M6 batch B9's Cutover-1a deleted
the tiles.

Run: uv run pytest ansible/tests/setup/test_integrity_scan_rule_literals.py
"""

import re

import pytest
from _helpers import K8S_ROLES
from _host_packages import package
from _shell_render import render_shell_script
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "host-checks.yaml"
HOSTS = ["daniel-box", "daniel-server", "daniel-pi"]
# A bare `>` comparison: `>=` and `==` are not the stale-file arm.
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")
# Alert prefix -> (component, template, the `check` label its textfile carries, its timer).
PRODUCERS = {
    "AideCheck": (
        "aide-scan",
        "aide-check.sh.j2",
        "aide-check",
        "homelab-aide-check.timer",
    ),
    "RkhunterScan": (
        "integrity-scans",
        "rkhunter-check.sh.j2",
        "rkhunter-check",
        "homelab-rkhunter-check.timer",
    ),
}


def scheduled_hosts() -> dict[str, list[str]]:
    """Alert prefix -> the hosts whose package ships that scan's timer, so carry its rules."""
    return {
        prefix: [
            h for h in HOSTS if f"/usr/lib/systemd/system/{spec[3]}" in package(h).files
        ]
        for prefix, spec in PRODUCERS.items()
    }


def rendered_deadline(prefix: str) -> int:
    """DEADLINE_S as the scan's script renders it."""
    component, template = PRODUCERS[prefix][:2]
    body = render_shell_script("components", component, template)
    [deadline] = re.findall(r"^\s*DEADLINE_S=(\d+)$", body, re.M)
    return int(deadline)


def not_reporting_rules(text: str) -> dict[tuple[str, str], str]:
    """(alert prefix, origin) -> expr for every NotReporting rule of the two scans."""
    [group] = yaml_fast.safe_load(text)["groups"]
    found = {}
    for rule in group["rules"]:
        prefix = rule["alert"].removesuffix("NotReporting")
        if prefix not in PRODUCERS or prefix == rule["alert"]:
            continue
        [origin] = re.findall(r'origin="([^"]+)"', rule["expr"].split("unless")[0])[:1]
        found[(prefix, origin)] = rule["expr"]
    return found


def literal_problems(rules: dict[tuple[str, str], str], deadlines: dict[str, int]):
    """Why a rule's deadline literals differ from its scan's DEADLINE_S, or nothing."""
    problems = []
    scheduled = scheduled_hosts()
    for prefix, (_, _, check, _) in PRODUCERS.items():
        for host in HOSTS:
            expr = rules.get((prefix, host))
            if host not in scheduled[prefix]:
                if expr is not None:
                    problems.append(
                        f"{prefix}NotReporting pages for {host}, which runs no scan"
                    )
                continue
            if expr is None:
                problems.append(f"{prefix}NotReporting has no rule for {host}")
                continue
            if f'check="{check}"' not in expr:
                problems.append(f"{prefix}NotReporting on {host} does not read {check}")
            for name, found in (
                ("the `>` threshold", STALE_ARM.findall(expr)),
                ("the absent_over_time window", WINDOW.findall(expr)),
            ):
                if len(found) != 1:
                    problems.append(
                        f"{prefix}NotReporting on {host} has {len(found)} of {name}, not one"
                    )
                elif int(found[0]) != deadlines[prefix]:
                    problems.append(
                        f"{prefix}NotReporting on {host}: {name} reads {found[0]}s, "
                        f"and DEADLINE_S renders {deadlines[prefix]}"
                    )
    return problems


def shipped_deadlines() -> dict[str, int]:
    return {prefix: rendered_deadline(prefix) for prefix in PRODUCERS}


def test_literal_problems_is_clean_on_the_shipped_rules():
    rules = not_reporting_rules(RULES.read_text())
    # Non-vacuity: one rule per scan per host that schedules it, and daniel-pi schedules no AIDE.
    assert scheduled_hosts()["AideCheck"] == ["daniel-box", "daniel-server"]
    assert len(rules) == sum(len(hosts) for hosts in scheduled_hosts().values())
    assert literal_problems(rules, shipped_deadlines()) == []


def test_literal_problems_is_flagged_on_a_rule_for_an_unscheduled_host():
    rules = not_reporting_rules(RULES.read_text()) | {
        ("AideCheck", "daniel-pi"): "expr"
    }
    assert literal_problems(rules, shipped_deadlines()) == [
        "AideCheckNotReporting pages for daniel-pi, which runs no scan"
    ]


def test_literal_problems_is_flagged_on_a_window_that_drifted():
    text = RULES.read_text().replace(
        'origin="daniel-pi"}[612000s]', 'origin="daniel-pi"}[600000s]'
    )
    assert literal_problems(not_reporting_rules(text), shipped_deadlines()) == [
        "RkhunterScanNotReporting on daniel-pi: the absent_over_time window reads 600000s, "
        "and DEADLINE_S renders 612000"
    ]


def test_literal_problems_is_flagged_when_a_deadline_moves_without_its_rules():
    deadlines = shipped_deadlines() | {"AideCheck": 604800}
    problems = literal_problems(not_reporting_rules(RULES.read_text()), deadlines)
    assert len(problems) == 4  # two literals on each of the two scheduled hosts
    assert all("DEADLINE_S renders 604800" in p for p in problems)


@pytest.mark.parametrize("prefix", sorted(PRODUCERS))
def test_each_scan_writes_the_check_its_rules_read_with_its_deadline(prefix):
    component, template, check, _ = PRODUCERS[prefix]
    body = render_shell_script("components", component, template)
    assert re.search(
        rf'write_check_textfile {re.escape(check)} ".*" "\$DEADLINE_S"', body
    ), f"{template} does not write the {check} textfile its rules read"
