"""The numbers in hosts.yaml equal the monitor-bridge env keys they port.

M6's batch A1 ports `disk`, `memory`, `host_temp`, `ups` and the Prometheus half of
`pi_pressure` into `hosts.yaml`. A rule file is never Jinja, so each threshold is a literal. Until
each cutover both pagers run, and a limit edited on one side makes them disagree about the same
host.

Run: uv run pytest ansible/tests/services/test_host_check_rule_literals.py
"""

import re

from _helpers import K8S_ROLES
from _k8s_render import render_pod_env
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "hosts.yaml"
SECONDS_PER_CYCLE = 300


def env() -> dict[str, str]:
    return render_pod_env("monitor-bridge")


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
