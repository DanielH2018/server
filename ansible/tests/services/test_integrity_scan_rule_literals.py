"""The AIDE and rkhunter NotReporting literals equal the DEADLINE_S each scan renders (#6168).

`files/rules/host-checks.yaml` is plain YAML, so each NotReporting rule carries its deadline as a
literal twice: the `>` of the stale-file arm and the `[<n>s]` window of the absent_over_time arm.
Both scans run on every host, so one DEADLINE_S pins three rules each. The DEADLINE_S also equals
the interval of the Kuma tile the rules shadow, so a cron moved without its tile or its rules
fails here rather than paging on every healthy week (M1 convention 1).

Run: uv run pytest ansible/tests/services/test_integrity_scan_rule_literals.py
"""

import re

import pytest
from _helpers import K8S_ROLES
from _shell_render import render_shell_script
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "host-checks.yaml"
TILES = K8S_ROLES / "uptime-kuma" / "templates" / "static-monitors.yaml.j2"
HOSTS = ["daniel-box", "daniel-server", "daniel-pi"]
# A bare `>` comparison: `>=` and `==` are not the stale-file arm.
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")
# Alert prefix -> (template, the `check` label its textfile carries, the Kuma tile's name prefix).
PRODUCERS = {
    "AideCheck": ("aide-check.sh.j2", "aide-check", "AIDE Integrity"),
    "RkhunterScan": ("rkhunter-check.sh.j2", "rkhunter-check", "rkhunter Scan"),
}


def rendered_deadline(template: str) -> int:
    """DEADLINE_S as the scan script renders it."""
    body = render_shell_script("setup", "initial_setup", template)
    [deadline] = re.findall(r"^\s*DEADLINE_S=(\d+)$", body, re.M)
    return int(deadline)


def tile_intervals(text: str, tile: str) -> list[int]:
    """The `interval` of each host's tile whose name starts with `tile`."""
    pattern = rf'"name": "{re.escape(tile)} \([^)]*\)".*?"interval": (\d+)'
    return [int(n) for n in re.findall(pattern, text)]


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
    for prefix, (_, check, _) in PRODUCERS.items():
        for host in HOSTS:
            expr = rules.get((prefix, host))
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
    return {prefix: rendered_deadline(spec[0]) for prefix, spec in PRODUCERS.items()}


def test_literal_problems_is_clean_on_the_shipped_rules():
    rules = not_reporting_rules(RULES.read_text())
    # Non-vacuity: one rule per scan per host.
    assert len(rules) == len(PRODUCERS) * len(HOSTS)
    assert literal_problems(rules, shipped_deadlines()) == []


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
    assert len(problems) == 6  # two literals on each of the three hosts
    assert all("DEADLINE_S renders 604800" in p for p in problems)


@pytest.mark.parametrize("prefix", sorted(PRODUCERS))
def test_each_scan_writes_its_check_and_its_deadline_equals_its_tiles_interval(prefix):
    template, check, tile = PRODUCERS[prefix]
    body = render_shell_script("setup", "initial_setup", template)
    assert re.search(
        rf'write_check_textfile {re.escape(check)} ".*" "\$DEADLINE_S"', body
    ), f"{template} does not write the {check} textfile its rules read"
    assert tile_intervals(TILES.read_text(), tile) == [
        rendered_deadline(template)
    ] * len(HOSTS)
