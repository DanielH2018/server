"""The numbers in hosts.yaml equal the bridge limits they ported.

M6's batch A1 ported `disk`, `memory`, `host_temp`, `ups` and the Prometheus half of
`pi_pressure` into `hosts.yaml`. A rule file is never Jinja, so each threshold is a literal.
Cutover-a2 deleted the first four rows and their env keys, and B8's Cutover-a2 deleted the
`pi_pressure` row and its `PI_*` keys, so `RETIRED_ENV` holds their values as literals: an edit to
a rule limit fails here until this table agrees, which makes the change a reviewed decision.

Run: uv run pytest ansible/tests/services/test_host_check_rule_literals.py
"""

import re

from _helpers import K8S_ROLES
from _k8s_render import render_pod_env
from lib import yaml_fast
from lib.repo_paths import HOST_VARS, ROLES

RULES = K8S_ROLES / "observability" / "files" / "rules" / "hosts.yaml"
SECONDS_PER_CYCLE = 300

# DECIDED: HostTemperatureHot holds for 1h (HWMON_TEMP_CONSECUTIVE 12 x 300s), not the 15 minutes
# (3 cycles) the bridge shipped with. daniel-box's k10temp/Tctl boosts into the low 90s as ordinary
# workload. Measured 2026-09-06 over a true 7 days (a [30d] query here truncates to about 11.4,
# #1314) at the 5-minute loop cadence: 12.0% of samples above the 90C limit, p95 93.1C, max
# 93.75C against AMD's rated 100C, in 115 excursions. Run lengths in cycles were 66x1, 23x2, 12x3,
# 5x4, 2x5, 3x6, 3x7, 1x8, then ONE of 18 (90 minutes, 2026-09-03). Nothing sits between 9 and 17
# cycles, so 12 is 1.5x the largest ordinary excursion and half the outlier. Pages per week by
# threshold were 3 -> 26, 5 -> 10, 8 -> 2, 12 -> 1, 19 -> 0. Twelve keeps the one 90-minute
# excursion paging on purpose: 90 minutes pinned above 90C is the shape of a cooling fault, and a
# threshold fitted to silence it would leave no margin to detect one (issue #1186).
#
# DECIDED: a declared hwmon max or crit is sanity-bounded (20..150C), not trusted. Three of the
# ten max-declaring sensors declare 65261.85, a 0xFFFF sentinel encoded as a number, and a ratio
# of it is unreachable, so those sensors would read green through a fire. Such a value is read as
# undeclared and falls through to crit, then the flat 85C fallback.
#
# DECIDED: daniel-box's k10temp/Tctl takes its limit from AMD's published Tjmax of 100C (the
# Ryzen 7 8845HS page, `Max. Operating Temperature (Tjmax)`), so it lands on 90C, the same
# effective limit daniel-server's coretemp carries from its own declared max. k10temp subtracts
# no Tctl offset on this CPU (the driver's `tctl_offset_table` holds six family-0x17 SKUs), so
# Tctl is the junction temperature (#1003, #1152). amd.com answers a plain fetch only with a
# browser User-Agent.
#
# DECIDED: every UPS arm reads nut-exporter alone. The Home Assistant re-export of the same UPS
# read the same upsd, so it covered only nut-exporter dying while upsd lived, which
# `ScrapeTargetDown{job="nut"}` already pages (#3105). Units are nut's own: charge in percent,
# runtime in seconds.
#
# DECIDED: a Claude Code process running as a retired uid never pages (operator, 2026-10-17,
# #5125). As an arm it held the Memory tile DOWN near-continuously from 2026-10-09, because
# background jobs and fan-out still started as the operator's uid, and it masked any real memory
# page. The `hosts` rules have no rule for it. `claude_uid_processes` is still written, and
# `test_claude_cgroup_metrics.py` holds the writer.
#
# The values of the env keys that the two Cutover-a2 PRs (A1's and B8's) deleted from
# monitor-bridge's templates/config/env.yaml.j2, as they stood. The bridge's streak counts times 300s are the
# rules' `for:`, and the floors are the census rules' counts.
RETIRED_ENV = {
    "DISK_MOUNTPOINTS": "/,/boot,/boot/efi",
    "DISK_MAX_PCT": "90",
    "MEM_MAX_PCT": "90",
    "HWMON_TEMP_RATIO": "0.90",
    "HWMON_TEMP_FALLBACK_C": "85",
    "HWMON_TEMP_RATED_MAX_C": "daniel-box/pci0000:00_0000:00:18_3/temp1=100",
    "HWMON_TEMP_MIN_PLAUSIBLE_C": "20",
    "HWMON_TEMP_MAX_PLAUSIBLE_C": "150",
    "HWMON_TEMP_CONSECUTIVE": "12",
    "HWMON_TEMP_ORIGINS_MIN": "3",
    "HWMON_TEMP_ORIGINS_CONSECUTIVE": "5",
    "THERMAL_THROTTLE_ORIGINS_MIN": "2",
    "THERMAL_THROTTLE_CONSECUTIVE": "3",
    "UPS_CHARGE_MIN_PCT": "50",
    "UPS_RUNTIME_MIN_S": "300",
    "UPS_CONSECUTIVE": "2",
    "HOST_ORIGINS_MIN": "2",
    "CLAUDE_CGROUP_EXPECTED": "daniel-box=claude-rc+fleet,daniel-server=fleet",
    # B8's `pi_pressure` row: load5 per core, MemAvailable in MB and a device's fill percent.
    "PI_LOAD_MAX": "1.5",
    "PI_MEM_MIN_MB": "50",
    "PI_DISK_MAX_PCT": "90",
}


def env() -> dict[str, str]:
    return {**render_pod_env("monitor-bridge"), **RETIRED_ENV}


def rules_named(alert: str) -> list[dict]:
    [group] = yaml_fast.safe_load(RULES.read_text())["groups"]
    found = [r for r in group["rules"] if r["alert"] == alert]
    assert found, alert
    return found


def numbers(alert: str, pattern: str, replace: tuple[str, str] | None = None):
    """Every capture of `pattern` across the exprs of `alert`'s rules, in rule order."""
    out = []
    for rule in rules_named(alert):
        expr = rule["expr"]
        if replace:
            expr = expr.replace(*replace)
        out.extend(re.findall(pattern, expr))
    return out


def for_seconds(alert: str) -> set[int]:
    units = {"s": 1, "m": 60, "h": 3600}
    return {int(r["for"][:-1]) * units[r["for"][-1]] for r in rules_named(alert)}


def expected_limits() -> dict[str, list]:
    """What each rule family must read, derived from the bridge's rendered env."""
    e = env()
    mounts = "|".join(e["DISK_MOUNTPOINTS"].split(","))
    ratio = float(e["HWMON_TEMP_RATIO"])
    [rated] = [
        float(x.rsplit("=", 1)[1]) for x in e["HWMON_TEMP_RATED_MAX_C"].split(",")
    ]
    return {
        "disk": [e["DISK_MAX_PCT"]] * 2,
        "mounts": [mounts] * 2,
        "mem": [e["MEM_MAX_PCT"]] * 2,
        "ratio": [f"{ratio:g}"] * 4,
        "fallback": [e["HWMON_TEMP_FALLBACK_C"]] * 3,
        "rated": [f"{rated * ratio:g}"],
        "plausible": [
            (e["HWMON_TEMP_MIN_PLAUSIBLE_C"], e["HWMON_TEMP_MAX_PLAUSIBLE_C"])
        ]
        * 6,
        "charge": [e["UPS_CHARGE_MIN_PCT"]],
        "runtime": [e["UPS_RUNTIME_MIN_S"]],
        "pi_load": [e["PI_LOAD_MAX"]],
        "pi_mem": [e["PI_MEM_MIN_MB"]],
        "pi_disk": [e["PI_DISK_MAX_PCT"]],
    }


def found_limits(replace: tuple[str, str] | None = None) -> dict[str, list]:
    return {
        "disk": numbers("HostDiskFull", r"\) > (\d+)\n", replace),
        "mounts": numbers(
            "HostDiskFull", r'avail_bytes\{[^}]*mountpoint=~"([^"]+)"', replace
        ),
        "mem": numbers("HostMemoryHigh", r"\) > (\d+)\n", replace),
        "ratio": numbers("HostTemperatureHot", r"([\d.]+) \* \(\(", replace),
        "fallback": numbers("HostTemperatureHot", r"\* 0 \+ (\d+)\)\n\s+\)", replace),
        "rated": numbers(
            "HostTemperatureHot", r'sensor="temp1"\} \* 0 \+ (\d+)\)', replace
        ),
        "plausible": numbers("HostTemperatureHot", r"> (\d+)\) <= (\d+)\)", replace),
        "charge": numbers("UPSBatteryLow", r"\) < (\d+)\n", replace),
        "runtime": numbers("UPSRuntimeLow", r"\) < (\d+)\n", replace),
        "pi_load": numbers("PiLoadHigh", r"\n> ([\d.]+)\n", replace),
        "pi_mem": numbers("PiMemoryLow", r"< (\d+) \* 1048576", replace),
        "pi_disk": numbers("PiDiskFull", r"\) > (\d+)\n", replace),
    }


def test_every_threshold_equals_the_bridge_env():
    found = found_limits()
    want = expected_limits()
    # A set, so the comparison does not depend on how many hosts carry a copy of the rule.
    for key in want:
        assert set(found[key]) == set(want[key]), key


def test_every_for_equals_the_bridge_streak_times_the_cycle():
    e = env()
    cycles = {
        "HostTemperatureHot": int(e["HWMON_TEMP_CONSECUTIVE"]),
        "HostTemperatureNotReporting": int(e["HWMON_TEMP_ORIGINS_CONSECUTIVE"]),
        "CPUThermalThrottled": int(e["THERMAL_THROTTLE_CONSECUTIVE"]),
        "UPSOnBattery": int(e["UPS_CONSECUTIVE"]),
        "UPSBatteryLow": int(e["UPS_CONSECUTIVE"]),
        "UPSRuntimeLow": int(e["UPS_CONSECUTIVE"]),
        "UPSReplaceBattery": int(e["UPS_CONSECUTIVE"]),
    }
    for alert, n in cycles.items():
        assert for_seconds(alert) == {n * SECONDS_PER_CYCLE}, alert


def census_floors(alert: str, replace: tuple[str, str] | None = None) -> list[int]:
    return [int(n) for n in numbers(alert, r"or vector\(0\)\) < (\d+),?\n", replace)]


def test_every_coverage_floor_equals_the_bridge_floor():
    e = env()
    # Three disk arms and one memory arm share HOST_ORIGINS_MIN; the cooling arm has its own.
    host = int(e["HOST_ORIGINS_MIN"])
    throttle = int(e["THERMAL_THROTTLE_ORIGINS_MIN"])
    assert census_floors("HostCoverageShortfall") == [host] * 4 + [throttle]
    assert census_floors("HostTemperatureCoverageShortfall") == [
        int(e["HWMON_TEMP_ORIGINS_MIN"])
    ]


def test_a_coverage_floor_edited_on_one_side_is_flagged():
    floors = census_floors(
        "HostCoverageShortfall", ("vector(0)) < 2,", "vector(0)) < 1,")
    )
    assert floors != [int(env()["HOST_ORIGINS_MIN"])] * 4 + [
        int(env()["THERMAL_THROTTLE_ORIGINS_MIN"])
    ]


def test_the_cgroup_arm_names_the_cgroups_each_host_must_report():
    """CLAUDE_CGROUP_EXPECTED is `host=cgroup+cgroup,...`; the rules hold the same pairs."""
    want = {
        (host, cg)
        for item in env()["CLAUDE_CGROUP_EXPECTED"].split(",")
        for host, cgs in [item.split("=")]
        for cg in cgs.split("+")
    }
    got = {
        (m.group(1), m.group(2))
        for rule in rules_named("ClaudeCgroupNotReporting")
        for m in re.finditer(
            r'origin="([^"]+)", kind="full", cgroup="([^"]+)"', rule["expr"]
        )
    }
    assert got == want


def test_a_limit_edited_on_one_side_is_flagged():
    want = expected_limits()
    assert set(found_limits(("> 20) <= 150)", "> 20) <= 999)"))["plausible"]) != set(
        want["plausible"]
    )
    assert set(found_limits(("* 0 + 85", "* 0 + 80"))["fallback"]) != set(
        want["fallback"]
    )
    assert set(found_limits(("< 300\n", "< 301\n"))["runtime"]) != set(want["runtime"])


def test_the_retired_env_keys_are_gone_from_the_bridge():
    """The rendered env no longer carries a key that `RETIRED_ENV` now owns."""
    assert not set(RETIRED_ENV) & set(render_pod_env("monitor-bridge"))


def test_the_cgroup_pairs_follow_the_inventory():
    """Every `has_claude_code` host requires fleet, and claude-rc only where its RC unit runs.

    The pairs in `ClaudeCgroupNotReporting` are literals, so this is what makes them follow the
    inventory: a host that gains Claude Code without a rule is a writer nothing checks, and a host
    whose RC unit is switched off would page for a cgroup it legitimately lacks.
    """
    # fact: ansible/roles/k8s/node-exporter/CLAUDE.md#The textfile collector directory
    rc_default = yaml_fast.safe_load(
        (ROLES / "setup" / "claude_code" / "defaults" / "main.yml").read_text()
    )["claude_code_rc_enabled"]
    derived = set()
    for path in HOST_VARS.glob("*.yml"):
        host = yaml_fast.safe_load(path.read_text()) or {}
        if host.get("has_claude_code") is True:
            rc = host.get("claude_code_rc_enabled", rc_default)
            derived.add((path.stem, "fleet"))
            if rc:
                derived.add((path.stem, "claude-rc"))
    assert derived == {
        ("daniel-box", "claude-rc"),
        ("daniel-box", "fleet"),
        ("daniel-server", "fleet"),
    }
    got = {
        (m.group(1), m.group(2))
        for rule in rules_named("ClaudeCgroupNotReporting")
        for m in re.finditer(
            r'origin="([^"]+)", kind="full", cgroup="([^"]+)"', rule["expr"]
        )
    }
    assert got == derived


def test_a_cgroup_pair_missing_from_the_rules_is_flagged():
    """The rejecting half: dropping a host's pair from the expr no longer equals the inventory."""
    got = {
        (m.group(1), m.group(2))
        for rule in rules_named("ClaudeCgroupNotReporting")
        for m in re.finditer(
            r'origin="([^"]+)", kind="full", cgroup="([^"]+)"',
            rule["expr"].replace('cgroup="claude-rc"', 'cgroup="renamed"'),
        )
    }
    assert ("daniel-box", "claude-rc") not in got
