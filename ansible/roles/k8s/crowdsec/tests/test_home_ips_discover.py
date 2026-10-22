"""The crowdsec-home-ips CronJob's lookup and its ConfigMap write.

`files/home_ips_discover.py` runs with stubbed `/proc/net/if_inet6` bodies, a stub opener for
ipify and the WAN probes, and a `Kube` subclass that records the merge patch. The patch is the
module's whole output, so each test reads what it would have written. The CronJob's RBAC
against these calls is in ansible/tests/k8s/test_rbac_against_rest_script.py.

Run: uv run pytest ansible/roles/k8s/crowdsec/tests/test_home_ips_discover.py
"""

import io
import ipaddress
import urllib.error
from datetime import UTC, datetime

import home_ips_discover as hid

NOW = datetime(2026, 10, 18, 13, 5, tzinfo=UTC)
ENV = {"HOME_IPS_NAMESPACE": "homelab", "HOME_IPS_CONFIGMAP": "crowdsec-home-ips"}
PREFIX = "2601:600:a:b::/64"


def _row(address: str, plen: int = 0x40, scope: int = 0, flags: int = 0, dev="eno1"):
    """One `/proc/net/if_inet6` line for `address`, as the kernel writes it."""
    raw = f"{int(ipaddress.IPv6Address(address)):032x}"
    return f"{raw} 02 {plen:02x} {scope:02x} {flags:02x} {dev:>8}"


# The shape measured on daniel-server on 2026-10-18: a /64 SLAAC address and a /128 DHCPv6
# address in one prefix, link-local rows on every veth, and loopback.
TABLE = "\n".join(
    [
        _row("2601:600:a:b:1c2d:3e4f:5a6b:7c8d"),
        _row("2601:600:a:b::1f", plen=0x80),
        _row("fe80::1", scope=0x20, flags=0x80, dev="veth01"),
        _row("::1", plen=0x80, scope=0x10, flags=0x80, dev="lo"),
    ]
)


def test_the_measured_table_yields_its_one_home_prefix():
    assert hid.global_prefixes(TABLE) == [PREFIX]


def test_ula_link_local_and_unusable_addresses_are_skipped():
    table = "\n".join(
        [
            # The kernel marks a ULA as scope global, so only `is_global` drops it.
            _row("fd00:1:2:3::1"),
            _row("fe80::2", scope=0x20),
            _row("2601:600:c:d::1", flags=0x20),  # deprecated
            _row("2601:600:e:f::1", flags=0x40),  # tentative
            _row("2601:600:1:2::1", flags=0x08),  # DAD failed
        ]
    )
    assert hid.global_prefixes(table) == []


def test_two_distinct_prefixes_fail_the_lookup_rather_than_pick_one():
    table = TABLE + "\n" + _row("2a02:1:2:3::1")
    assert hid.lookup_ipv6(lambda: table) == (
        "",
        "2 distinct global /64s, so none is the home prefix",
    )


def test_a_host_with_no_global_address_has_no_prefix_and_no_error():
    assert hid.lookup_ipv6(lambda: _row("fe80::1", scope=0x20)) == ("", "")


class _Reply(io.BytesIO):
    status = 200


class _Opener:
    """Answers each URL from `answers`: bytes for a 200, an exception to raise."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls = []

    def __call__(self, request, timeout, **kwargs):
        url = request if isinstance(request, str) else request.full_url
        self.calls.append((url, kwargs.get("retry_seconds")))
        answer = self.answers[url]
        if isinstance(answer, BaseException):
            raise answer
        return _Reply(answer)


_DOWN = urllib.error.URLError(OSError(101, "Network is unreachable"))
PROBES = ["https://probe-a", "https://probe-b"]


def test_ipify_answering_a_public_address_is_the_ipv4_value():
    opener = _Opener({hid.IPIFY4: b"98.42.7.9\n"})
    assert hid.lookup_ipv4(opener, PROBES) == ("98.42.7.9", "")
    # The probes run only on a failed lookup.
    assert [url for url, _ in opener.calls] == [hid.IPIFY4]


def test_ipify_answering_junk_or_a_private_address_is_a_failed_lookup():
    assert hid.lookup_ipv4(_Opener({hid.IPIFY4: b"<html>"}), PROBES)[1].startswith(
        "invalid IPv4 from ipify"
    )
    assert hid.lookup_ipv4(_Opener({hid.IPIFY4: b"10.0.0.4"}), PROBES) == (
        "",
        "ipify returned a non-public IPv4 address: 10.0.0.4",
    )


def test_ipify_failing_with_the_wan_down_is_marked_wan_unreachable():
    opener = _Opener({hid.IPIFY4: _DOWN, PROBES[0]: _DOWN, PROBES[1]: _DOWN})
    value, error = hid.lookup_ipv4(opener, PROBES)
    assert value == ""
    assert error.startswith(f"{hid.WAN_UNREACHABLE} — failed to resolve public IPv4")
    # Each probe runs once, with no connect retry: the path has already failed.
    assert opener.calls[1:] == [(PROBES[0], 0), (PROBES[1], 0)]


def test_ipify_failing_with_the_wan_up_is_a_plain_failure():
    opener = _Opener({hid.IPIFY4: _DOWN, PROBES[0]: _DOWN, PROBES[1]: b""})
    _, error = hid.lookup_ipv4(opener, PROBES)
    assert error.startswith("failed to resolve public IPv4 from ipify")


class _Recorder(hid.Kube):
    def __init__(self, status: int = 200):
        super().__init__("https://api", "token")
        self.status = status
        self.calls = []

    def call(self, method, path, body):
        self.calls.append((method, path, body))
        return self.status, {"message": "forbidden"} if self.status != 200 else {}


def _main(kube, ipify, table=TABLE):
    return hid.main(
        [],
        ENV,
        kube,
        opener=_Opener({hid.IPIFY4: ipify, **dict.fromkeys(PROBES, b"")}),
        read_if_inet6=lambda: table,
        now=lambda: NOW,
    )


def test_a_good_run_writes_both_values_and_clears_both_errors(capsys):
    kube = _Recorder()
    assert _main(kube, b"98.42.7.9") == 0
    assert kube.calls == [
        (
            "PATCH",
            "/api/v1/namespaces/homelab/configmaps/crowdsec-home-ips",
            {
                "data": {
                    "looked_up_at": "2026-10-18T13:05:00Z",
                    "ipv4": "98.42.7.9",
                    "ipv4_error": "",
                    "ipv6_prefix": PREFIX,
                    "ipv6_error": "",
                }
            },
        )
    ]
    # The log line names what was written, never the addresses.
    out = capsys.readouterr().out
    assert "98.42" not in out and "2601" not in out
    assert out.endswith("IPv4 written; IPv6 written\n")


def test_a_second_run_patches_the_same_values():
    """The gate Job runs beside the schedule, so only `looked_up_at` may differ between runs."""
    bodies = []
    for _ in range(2):
        kube = _Recorder()
        assert _main(kube, b"98.42.7.9") == 0
        [(_, _, body)] = kube.calls
        bodies.append({k: v for k, v in body["data"].items() if k != "looked_up_at"})
    assert bodies[0] == bodies[1] != {}


def test_a_failed_ipv6_lookup_writes_its_error_and_leaves_the_stored_prefix():
    """A merge patch that omits `ipv6_prefix` leaves the stored /64 as it is (#1248)."""
    kube = _Recorder()
    table = TABLE + "\n" + _row("2a02:1:2:3::1")
    assert _main(kube, b"98.42.7.9", table) == 0
    [(_, _, body)] = kube.calls
    assert "ipv6_prefix" not in body["data"]
    assert body["data"]["ipv6_error"].startswith("2 distinct global /64s")
    # The IPv4 half is written whatever happened to IPv6.
    assert body["data"]["ipv4"] == "98.42.7.9"


def test_a_failed_ipv4_lookup_writes_its_error_and_leaves_the_stored_address():
    kube = _Recorder()
    assert _main(kube, _DOWN) == 0
    [(_, _, body)] = kube.calls
    assert "ipv4" not in body["data"]
    assert body["data"]["ipv4_error"].startswith("failed to resolve public IPv4")
    assert body["data"]["ipv6_prefix"] == PREFIX


def test_a_refused_patch_exits_1_naming_the_answer(capsys):
    assert _main(_Recorder(status=403), b"98.42.7.9") == 1
    assert "answered HTTP 403: forbidden" in capsys.readouterr().err
