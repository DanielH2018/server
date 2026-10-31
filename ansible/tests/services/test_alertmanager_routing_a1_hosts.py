"""M6 batch A1's host rules reach Alertmanager's receivers: they page since Cutover-b, bar B8's.

`test_alertmanager_routing.py` builds `PAGING` and `SHADOW` from the rule files. This module
holds the non-vacuity tests for the `hosts.yaml` share of them: every rule pages except the five
Pi pressure rules B8's tile still reads, `HostDiskFull` and the `UPS*` rules mail as well as
post, and a rule that regains `shadow` walks to the blackhole instead.

Run: uv run pytest ansible/tests/services/test_alertmanager_routing_a1_hosts.py
"""

from test_alertmanager_routing import (
    BLACKHOLE,
    HOSTS_RULES,
    PAGING,
    SHADOW,
    literal_sets,
    not_blackholed,
    reached,
    route_tree,
)

# The Pi pressure rules B8's `Pi Pressure` tile still reads. They keep `shadow` until B8.
A1_SHADOW_UNTIL_B8 = {
    "PiLoadHigh/daniel-pi",
    "PiMemoryLow/daniel-pi",
    "PiDiskFull/daniel-pi",
    "PiFilesystemReadOnly/daniel-pi",
    "PiMetricsMissing/daniel-pi",
}


def test_every_a1_host_rule_pages_but_the_pi_pressure_arms():
    """Non-vacuity for the A1 sets: every `hosts.yaml` rule pages since Cutover-b except the five
    Pi pressure rules, which stay in `SHADOW` until B8. `HostDiskFull` and the `UPS*` rules are
    critical, so they mail as well as post; the rest post to Discord alone."""
    assert set(literal_sets(HOSTS_RULES, shadow=True)) == A1_SHADOW_UNTIL_B8
    assert A1_SHADOW_UNTIL_B8 <= set(SHADOW)
    sets = literal_sets(HOSTS_RULES, shadow=False)
    names = {key.split("/")[0] for key in sets}
    assert names == {
        "HostDiskFull",
        "HostMemoryHigh",
        "ClaudeCgroupMemoryEvent",
        "ClaudeCgroupMemoryStalled",
        "ClaudeCgroupNotReporting",
        "HostTemperatureHot",
        "HostTemperatureNotReporting",
        "CPUThermalThrottled",
        "HostMetricsMissing",
        "HostCoverageShortfall",
        "HostTemperatureCoverageShortfall",
        "PiUndervoltage",
        "PiUndervoltageMissing",
        "UPSOnBattery",
        "UPSBatteryLow",
        "UPSRuntimeLow",
        "UPSReplaceBattery",
        "UPSSensorAbsent",
    }
    assert all(key in PAGING and key not in SHADOW for key in sets)
    for key, labels in sets.items():
        critical = labels["alertname"] == "HostDiskFull" or labels[
            "alertname"
        ].startswith("UPS")
        assert labels["severity"] == ("critical" if critical else "warning"), key
        assert PAGING[key][1] == (["discord", "email"] if critical else ["discord"]), (
            key
        )


def test_an_a1_host_rule_that_regains_shadow_is_flagged():
    """The rejecting half: with `shadow` back, the walk ends on the blackhole, so the receivers
    `PAGING` expects for the rule no longer match."""
    labels, receivers = PAGING["HostDiskFull/daniel-box"]
    assert receivers == ["discord", "email"]
    shadowed = {**labels, "shadow": "true"}
    assert [r["receiver"] for r in reached(route_tree(), shadowed)] == [BLACKHOLE]
    assert not_blackholed(shadowed) == []
