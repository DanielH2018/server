"""The remote-ips allowlist cron's decisions: which requests count as authenticated, which
addresses may enter, and what one run writes (`files/remote_allowlist.py`, issue #2123).

Run: uv run pytest ansible/roles/k8s/crowdsec/tests/test_remote_allowlist.py
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files")
)

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
    # Observed in Traefik's access log on 2026-09-21; the oracle for the sha256 derivation.
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
    lines = [_line(AUTH_ROUTER, 200, "173.249.254.219")]
    assert ra.authenticated_clients(lines, {AUTH_ROUTER}) == {"173.249.254.219"}


@pytest.mark.parametrize(
    "line",
    [
        _line(
            ra.router_name("homelab", "karakeep-public-api-trpc", TRPC_MATCH),
            200,
            "173.249.254.219",
        ),
        _line(AUTH_ROUTER, 302, "173.249.254.219"),  # Authelia's own redirect
        _line(AUTH_ROUTER, 403, "173.249.254.219"),  # the bouncer
        _line(
            AUTH_ROUTER, 200, "10.0.0.50"
        ),  # LAN — an Authelia bypass rule could pass this
        _line(AUTH_ROUTER, 200, "100.64.3.9"),  # CGNAT
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
    decided = ra.plan({"1.1.1.1", "2.2.2.2"}, [_item("2.2.2.2", 150)], NOW)
    assert decided.add == ["1.1.1.1"]
    assert decided.refresh == []
    assert decided.live == ["1.1.1.1", "2.2.2.2"]


def test_an_address_under_half_ttl_is_refreshed_and_an_expired_one_pruned():
    items = [_item("2.2.2.2", 10), _item("3.3.3.3", -1)]
    decided = ra.plan({"2.2.2.2"}, items, NOW)
    assert decided.refresh == ["2.2.2.2"]
    assert decided.prune == ["3.3.3.3"]
    assert decided.live == ["2.2.2.2"]


def test_an_unreadable_expiry_counts_as_due_for_refresh():
    decided = ra.plan({"2.2.2.2"}, [{"value": "2.2.2.2", "expiration": "soon"}], NOW)
    assert decided.refresh == ["2.2.2.2"]


def test_the_cap_stops_additions_and_names_what_it_left_out():
    items = [_item(f"10{i}.0.0.1", 100) for i in range(ra.CAP)]
    decided = ra.plan({"9.9.9.9"}, items, NOW)
    assert decided.add == []
    assert decided.over_cap == ["9.9.9.9"]


def test_main_writes_exactly_the_plan_and_refuses_with_no_authelia_router():
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[1:3] == ["kubectl", "get"]:
            return json.dumps(IRS)
        if argv[1:3] == ["kubectl", "-n"] and "logs" in argv:
            return (
                _line(AUTH_ROUTER, 200, "173.249.254.219")
                + "\n"
                + _line(AUTH_ROUTER, 200, "10.0.0.5")
            )
        if "inspect" in argv:
            return json.dumps({"name": ra.ALLOWLIST, "items": [_item("8.8.8.8", -2)]})
        return ""

    assert ra.main(runner=runner, now=NOW) == 0
    writes = [
        argv[argv.index("cscli") + 1 :]
        for argv in calls
        if "cscli" in argv and "inspect" not in argv
    ]
    assert writes == [
        ["allowlists", "remove", ra.ALLOWLIST, "8.8.8.8"],
        [
            "allowlists",
            "add",
            ra.ALLOWLIST,
            "173.249.254.219",
            "-e",
            "168h",
            "-d",
            "authenticated session seen 2026-09-21",
        ],
    ]

    def no_authelia(argv):
        return json.dumps({"items": []}) if argv[1:3] == ["kubectl", "get"] else ""

    assert ra.main(runner=no_authelia, now=NOW) == 1
