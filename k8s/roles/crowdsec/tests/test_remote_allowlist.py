"""The remote-ips allowlist cron's decisions: which requests count as authenticated, which
addresses may enter, and what one run writes (`files/remote_allowlist.py`).

Run: uv run pytest k8s/roles/crowdsec/tests/test_remote_allowlist.py
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

import remote_allowlist as ra

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
TRPC_MATCH = "Host(`karakeep.daniel-hunter.com`) && PathPrefix(`/api/trpc/`)"
PUBLIC_MATCH = "Host(`karakeep.daniel-hunter.com`)"


def _ingressroute(name, match, middlewares):
    return {
        "metadata": {"name": name, "namespace": "homelab"},
        "spec": {
            "routes": [
                {"match": match, "middlewares": [{"name": m} for m in middlewares]}
            ]
        },
    }


IRS = {
    "items": [
        _ingressroute(
            "karakeep-public",
            PUBLIC_MATCH,
            ["rate-limit-proxied", "authelia", "csp-karakeep"],
        ),
        _ingressroute("karakeep-public-api-trpc", TRPC_MATCH, ["rate-limit-proxied"]),
    ]
}
AUTH_ROUTER = ra.router_name("homelab", "karakeep-public", PUBLIC_MATCH)


def _line(router, status, host):
    return json.dumps(
        {"RouterName": router, "DownstreamStatus": status, "ClientHost": host}
    )


def test_router_name_matches_the_live_traefik_router():
    # Observed in Traefik's access log; the oracle for the sha256 derivation.
    assert ra.router_name("homelab", "karakeep-public-api-trpc", TRPC_MATCH) == (
        "homelab-karakeep-public-api-trpc-121339d25e3ce6f2e046@kubernetescrd"
    )


def test_only_routes_carrying_the_authelia_middleware_are_authenticated_routers():
    routers = ra.authenticated_routers(IRS)
    assert routers == {AUTH_ROUTER}
    assert (
        ra.router_name("homelab", "karakeep-public-api-trpc", TRPC_MATCH) not in routers
    )


def test_a_2xx_on_an_authelia_router_from_a_public_address_is_clean():
    lines = [_line(AUTH_ROUTER, 200, "1.1.1.1")]
    assert ra.authenticated_clients(lines, {AUTH_ROUTER}) == {"1.1.1.1"}


def test_a_cloudflare_chain_counts_its_rightmost_entry_and_never_the_client_sent_one():
    # The measured shape: the client sent XFF, Cloudflare appended the address it saw, and
    # Traefik logged the whole header.
    lines = [_line(AUTH_ROUTER, 200, "8.8.8.8, 9.9.9.9,1.1.1.1")]
    assert ra.authenticated_clients(lines, {AUTH_ROUTER}) == {"1.1.1.1"}


@pytest.mark.parametrize(
    "line",
    [
        _line(
            ra.router_name("homelab", "karakeep-public-api-trpc", TRPC_MATCH),
            200,
            "1.1.1.1",
        ),
        _line(AUTH_ROUTER, 302, "1.1.1.1"),  # Authelia's own redirect
        _line(AUTH_ROUTER, 403, "1.1.1.1"),  # the bouncer
        _line(
            AUTH_ROUTER, 200, "10.0.0.50"
        ),  # LAN — an Authelia bypass rule could pass this
        _line(AUTH_ROUTER, 200, "100.64.3.9"),  # CGNAT
        _line(AUTH_ROUTER, 200, "1.1.1.1,10.0.0.50"),  # chain ending in the LAN
        _line(AUTH_ROUTER, 200, "not-an-ip"),
        "not json at all",
    ],
)
def test_anything_else_is_flagged_out(line):
    assert ra.authenticated_clients([line], {AUTH_ROUTER}) == frozenset()


def _item(value, hours_left):
    return {
        "value": value,
        "expiration": (NOW + timedelta(hours=hours_left))
        .isoformat()
        .replace("+00:00", "Z"),
    }


def test_a_new_address_is_added_and_a_fresh_one_left_alone():
    decided = ra.plan({"1.1.1.1", "198.51.100.2"}, [_item("198.51.100.2", 150)], NOW)
    assert decided.add == ["1.1.1.1"]
    assert decided.refresh == []
    assert decided.live == ["1.1.1.1", "198.51.100.2"]


def test_an_address_under_half_ttl_is_refreshed_and_an_expired_one_pruned():
    items = [_item("198.51.100.2", 10), _item("198.51.100.3", -1)]
    decided = ra.plan({"198.51.100.2"}, items, NOW)
    assert decided.refresh == ["198.51.100.2"]
    assert decided.prune == ["198.51.100.3"]
    assert decided.live == ["198.51.100.2"]


def test_an_unreadable_expiry_counts_as_due_for_refresh():
    decided = ra.plan(
        {"198.51.100.2"}, [{"value": "198.51.100.2", "expiration": "soon"}], NOW
    )
    assert decided.refresh == ["198.51.100.2"]


def test_the_cap_stops_additions_and_names_what_it_left_out():
    items = [_item(f"203.0.113.{i + 1}", 100) for i in range(ra.CAP)]
    decided = ra.plan({"9.9.9.9"}, items, NOW)
    assert decided.add == []
    assert decided.over_cap == ["9.9.9.9"]


def _main_with_live_entries(count, sighting):
    """Run `main` over `count` unexpired entries and one sighting; return its exit and writes."""
    writes = []

    def runner(argv):
        if argv[:2] == [ra.KUBECTL, "get"]:
            return json.dumps(IRS)
        if argv[0] == ra.KUBECTL:
            return _line(AUTH_ROUTER, 200, sighting)
        if "inspect" in argv and ra.ALLOWLIST in argv:
            items = [_item(f"203.0.113.{i + 1}", 100) for i in range(count)]
            return json.dumps({"name": ra.ALLOWLIST, "items": items})
        if "inspect" in argv:
            return ""
        writes.append(argv[1:4])
        return ""

    return ra.main(runner=runner, now=NOW), writes


def test_a_sixteenth_address_is_added():
    code, writes = _main_with_live_entries(15, "9.9.9.9")
    assert code == 0
    assert writes == [["allowlists", "add", ra.ALLOWLIST]]


def test_a_seventeenth_address_is_refused_with_a_line_naming_the_entries(capsys):
    code, writes = _main_with_live_entries(16, "9.9.9.9")
    assert code == 1
    assert writes == []
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line.startswith("remote-ips at cap (16): live 203.0.113.1,")
    assert line.endswith("; not added 9.9.9.9")


HOME = ra.home_networks(
    [{"value": "198.51.100.7"}, {"value": "2001:db8:1:2::/64"}, {"value": "junk"}]
)


def test_a_sighting_inside_home_ips_takes_no_slot():
    items = [_item(f"203.0.113.{i + 1}", 100) for i in range(ra.CAP - 1)]
    seen = {"198.51.100.7", "2001:db8:1:2:aaaa::1", "9.9.9.9"}
    decided = ra.plan(seen, items, NOW, home=HOME)
    assert decided.add == ["9.9.9.9"]
    assert decided.over_cap == []


def test_a_live_entry_inside_home_ips_is_pruned_to_free_its_slot():
    items = [_item("2001:db8:1:2:bbbb::1", 150), _item("198.51.100.7", 150)]
    items += [_item(f"203.0.113.{i + 1}", 100) for i in range(ra.CAP - 2)]
    decided = ra.plan({"9.9.9.9", "2001:db8:1:3::1"}, items, NOW, home=HOME)
    assert decided.prune == ["2001:db8:1:2:bbbb::1", "198.51.100.7"]
    assert decided.add == ["2001:db8:1:3::1", "9.9.9.9"]
    assert decided.over_cap == []


def test_main_writes_exactly_the_plan_and_refuses_with_no_authelia_router():
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[:2] == [ra.KUBECTL, "get"]:
            return json.dumps(IRS)
        if argv[0] == ra.KUBECTL and "logs" in argv:
            return (
                _line(AUTH_ROUTER, 200, "1.1.1.1")
                + "\n"
                + _line(AUTH_ROUTER, 200, "10.0.0.5")
                + "\n"
                + _line(AUTH_ROUTER, 200, "198.51.100.7")
            )
        if "inspect" in argv and ra.HOME_ALLOWLIST in argv:
            return json.dumps(
                {"name": ra.HOME_ALLOWLIST, "items": [{"value": "198.51.100.7"}]}
            )
        if "inspect" in argv:
            return json.dumps({"name": ra.ALLOWLIST, "items": [_item("8.8.8.8", -2)]})
        return ""

    assert ra.main(runner=runner, now=NOW) == 0
    # The sidecar's own binaries only: no `k3s kubectl exec` into the engine container.
    assert {argv[0] for argv in calls} == {ra.KUBECTL, ra.CSCLI}
    assert not any("exec" in argv for argv in calls)
    writes = [
        argv[1:] for argv in calls if argv[0] == ra.CSCLI and "inspect" not in argv
    ]
    assert writes == [
        ["allowlists", "remove", ra.ALLOWLIST, "8.8.8.8"],
        [
            "allowlists",
            "add",
            ra.ALLOWLIST,
            "1.1.1.1",
            "-e",
            "168h",
            "-d",
            "authenticated session seen 2026-09-21",
        ],
    ]

    def no_authelia(argv):
        return json.dumps({"items": []}) if argv[:2] == [ra.KUBECTL, "get"] else ""

    assert ra.main(runner=no_authelia, now=NOW) == 1
