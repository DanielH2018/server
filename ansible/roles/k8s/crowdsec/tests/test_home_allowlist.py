"""The crowdsec-jobs sidecar's home allowlist: the ConfigMap's addresses into `home-ips`.

`files/home_allowlist.py` runs with a recording runner that answers `kubectl get configmap`
from a dict and `cscli allowlists inspect` from a list of stored entries, so each test reads
the cscli writes the run made and its verdict. The rules it holds are the host script's,
ported: an empty or failed IPv6 value never removes a stored /64 (#1248), an IPv6 fault is
reported even when the IPv4 half synced, and an unreachable WAN is a skip (#2793).

Run: uv run pytest ansible/roles/k8s/crowdsec/tests/test_home_allowlist.py
"""

import json
from datetime import UTC, datetime, timedelta

import home_allowlist as ha
import home_ips_discover

NOW = datetime(2026, 10, 18, 13, 6, tzinfo=UTC)
ENV = {"HOME_IPS_CONFIGMAP": "crowdsec-home-ips", "HOME_IPS_STALE_AFTER_S": "900"}
OLD_V6, NEW_V6 = "2601:600:a:b::/64", "2601:600:c:d::/64"


def _data(**fields):
    """A good lookup a minute ago, with `fields` replacing its values."""
    base = {
        "looked_up_at": (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ipv4": "98.42.7.9",
        "ipv4_error": "",
        "ipv6_prefix": OLD_V6,
        "ipv6_error": "",
    }
    return {**base, **fields}


def _run(capsys, data, stored):
    """Runs main against `data` and `stored`; returns (exit code, cscli writes, printed line)."""
    writes = []

    def runner(argv):
        if argv[0] == ha.KUBECTL:
            assert argv[1:] == [
                "-n",
                "homelab",
                "get",
                "configmap",
                "crowdsec-home-ips",
                "-o",
                "json",
            ]
            return json.dumps({"data": data})
        if argv[1:3] == ["allowlists", "inspect"]:
            return json.dumps({"items": [{"value": v} for v in stored]})
        writes.append(argv[1:])
        return ""

    code = ha.main(runner=runner, env=ENV, now=NOW)
    return code, writes, capsys.readouterr().out.splitlines()[-1]


def test_an_unchanged_allowlist_writes_nothing_and_is_up(capsys):
    assert _run(capsys, _data(), ["98.42.7.9", OLD_V6]) == (0, [], "home IPs unchanged")


def test_a_new_prefix_replaces_the_stored_one(capsys):
    code, writes, line = _run(capsys, _data(ipv6_prefix=NEW_V6), ["98.42.7.9", OLD_V6])
    assert code == 0
    # The new entry goes in before the old one comes out.
    assert writes == [
        ["allowlists", "add", "home-ips", NEW_V6, "-d", "home"],
        ["allowlists", "remove", "home-ips", OLD_V6],
    ]
    assert line == f"home allowlist updated: IPv6 {OLD_V6} -> {NEW_V6}"


def test_a_failed_ipv6_lookup_keeps_the_stored_prefix_and_is_down(capsys):
    """The red half of the pair above: the discovery wrote an error and no prefix value."""
    data = _data(ipv4="98.42.7.10", ipv6_error="2 distinct global /64s")
    del data["ipv6_prefix"]
    code, writes, line = _run(capsys, data, ["98.42.7.9", OLD_V6])
    # The IPv4 half still syncs, and nothing touches the /64.
    assert writes == [
        ["allowlists", "add", "home-ips", "98.42.7.10", "-d", "home"],
        ["allowlists", "remove", "home-ips", "98.42.7.9"],
    ]
    assert code == 1
    assert line == "IPv6: 2 distinct global /64s; IPv4 98.42.7.9 -> 98.42.7.10"


def test_an_empty_prefix_value_removes_nothing(capsys):
    code, writes, _ = _run(capsys, _data(ipv6_prefix=""), ["98.42.7.9", OLD_V6])
    assert (code, writes) == (0, [])


def test_an_unreachable_wan_is_a_skip_not_a_fault(capsys):
    error = f"{home_ips_discover.WAN_UNREACHABLE} — failed to resolve public IPv4"
    code, writes, line = _run(capsys, _data(ipv4_error=error), ["98.42.7.9", OLD_V6])
    assert (code, writes) == (0, [])
    assert line == f"skipped: {error}; home IPs unchanged"


def test_a_plain_ipv4_lookup_failure_is_down(capsys):
    code, _, line = _run(
        capsys, _data(ipv4_error="invalid IPv4 from ipify"), ["98.42.7.9"]
    )
    assert code == 1
    assert line.startswith("IPv4: invalid IPv4 from ipify;")


def test_a_lookup_older_than_three_periods_is_down_and_writes_nothing(capsys):
    stamp = (NOW - timedelta(seconds=901)).strftime("%Y-%m-%dT%H:%M:%SZ")
    code, writes, line = _run(capsys, _data(looked_up_at=stamp), [])
    assert (code, writes) == (1, [])
    assert line.startswith(
        "home-ips discovery is stale: last lookup 901s ago, past 900s"
    )


def test_a_configmap_never_written_is_down(capsys):
    code, writes, line = _run(capsys, {}, [])
    assert (code, writes) == (1, [])
    assert "last lookup never" in line


def test_an_empty_allowlist_gets_both_entries(capsys):
    code, writes, _ = _run(capsys, _data(), [])
    assert code == 0
    assert writes == [
        ["allowlists", "add", "home-ips", "98.42.7.9", "-d", "home"],
        ["allowlists", "add", "home-ips", OLD_V6, "-d", "home"],
    ]


def test_an_entry_of_neither_family_is_never_touched():
    assert (
        ha.plan({4: "98.42.7.9", 6: OLD_V6}, ["98.42.7.9", OLD_V6, "junk"]).remove == []
    )


def test_the_skip_marker_matches_what_the_discovery_writes():
    assert ha.WAN_UNREACHABLE == home_ips_discover.WAN_UNREACHABLE
