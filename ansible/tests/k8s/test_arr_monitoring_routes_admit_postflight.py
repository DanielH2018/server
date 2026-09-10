"""The three *arr `-monitoring` routes must admit what postflight.py actually asks for.

postflight runs on daniel-box and nowhere else. `get_via_service` tries the workload's
ClusterIP first, which answers only a caller on the pod's own node, then falls back to the
Traefik route — so whenever an *arr pod sits on daniel-server the route is the ONLY path the
§9.3 API-key check has. Two things have to line up for that path to reach the backend, and
until 2026-09-10 neither did (#1642):

  1. The route's ClientIP set has to include daniel-box (`k8s_node_client_ip`). It admitted
     daniel-server alone, so the request fell through to the app's own Authelia'd route.
  2. The route's PathPrefix has to cover the path postflight requests. It asked for
     `/api/<ver>/system/status`, which is on no monitoring route's prefix.

Both were 302s, which postflight reports as SKIP — a check that never runs rather than a false
alarm. The two sides live in different trees (`ansible/roles/k8s/<app>/templates/` and
`scripts/diagnostics/postflight.py`), so nothing but this guard notices one moving alone.

Run: uv run pytest ansible/tests/k8s/test_arr_monitoring_routes_admit_postflight.py
"""

import re

import pytest

from lib import yaml_fast
from _manifest_guards import ALL_VARS, K8S, _k8s_entries, _render, _role_defaults

# `scripts` is on pythonpath and `scripts/diagnostics` deliberately is not, so postflight is
# reached as a module of the `diagnostics` namespace package rather than by a sys.path insert.
from diagnostics import postflight

# The census this guard is about. Named rather than derived: a glob over the *arr roles would
# return an empty set the moment one is renamed, and `all(...)` over nothing passes.
ARR_APPS = frozenset({"sonarr", "radarr", "prowlarr"})


def _monitoring_matches() -> dict[str, str]:
    """{app: the app's `-monitoring` route match rule}, for the three *arr roles."""
    matches = {}
    for entry in _k8s_entries():
        if entry["name"] not in ARR_APPS:
            continue
        rendered = _render(
            K8S / entry["name"] / "templates" / "ingressroute-monitoring.yaml.j2",
            container_item=entry,
            domain="example.com",
            **_role_defaults(entry["name"]),
        )
        for doc in (d for d in yaml_fast.safe_load_all(rendered) if d):
            if not doc["metadata"]["name"].endswith("-monitoring"):
                continue
            matches[entry["name"]] = doc["spec"]["routes"][0]["match"]
    return matches


def _path_mismatches(monitored_paths: dict[str, str]) -> list[str]:
    """Apps whose monitoring route does not admit `monitored_paths[app]`."""
    problems = []
    for app, match in _monitoring_matches().items():
        prefixes = re.findall(r"PathPrefix\(`([^`]+)`\)", match)
        wanted = monitored_paths[app]
        if not any(wanted.startswith(prefix) for prefix in prefixes):
            problems.append(f"{app}: asks for {wanted}, route admits {prefixes}")
    return problems


def _cidr_mismatches(cidr: str) -> list[str]:
    """Apps whose monitoring route does not admit `cidr` as a client."""
    return [
        app
        for app, match in _monitoring_matches().items()
        if f"ClientIP(`{cidr}`)" not in match
    ]


def test_the_census_reaches_all_three_arr_monitoring_routes():
    # Without this every assertion below passes vacuously if a role is renamed or its
    # monitoring template moves — which is the failure mode this whole file exists to catch.
    missing = ARR_APPS - set(_monitoring_matches())
    assert not missing, (
        f"no rendered `-monitoring` route found for {sorted(missing)}; the guard below would "
        "have passed while checking nothing"
    )


def test_every_arr_route_admits_the_path_postflight_asks_for_is_clean():
    problems = _path_mismatches(postflight.ARR_MONITORED_PATH)
    assert not problems, (
        "postflight §9.3 requests a path its monitoring route does not admit, so the request "
        f"falls through to the app's Authelia'd route and the check reports SKIP forever "
        f"(#1642): {problems}"
    )


def test_a_path_the_route_does_not_admit_is_flagged():
    """The rejecting half: this is the exact map postflight held before #1642."""
    before = {
        "sonarr": "/api/v3/system/status",
        "radarr": "/api/v3/system/status",
        "prowlarr": "/api/v1/system/status",
    }
    problems = _path_mismatches(before)
    assert len(problems) == len(ARR_APPS), problems
    assert all("system/status" in problem for problem in problems)


def test_every_arr_route_admits_daniel_box_is_clean():
    box = ALL_VARS["k8s_node_client_ip"]
    assert not _cidr_mismatches(f"{box}/32"), (
        f"monitoring route(s) do not admit daniel-box ({box}), the only host postflight runs "
        f"on: {_cidr_mismatches(f'{box}/32')}. §9.3 is then SKIP whenever the pod is on "
        "daniel-server (#1642)."
    )


def test_a_cidr_no_route_admits_is_flagged():
    assert sorted(_cidr_mismatches("192.0.2.7/32")) == sorted(ARR_APPS)


@pytest.mark.parametrize("app", sorted(ARR_APPS))
def test_the_app_route_itself_is_not_widened(app):
    """The widening is per monitoring route. The app's own route keeps its Authelia gate.

    #1642's remediation is `extra_client_cidrs` on the narrow route, not a bypass on the wide
    one — a rule added to the app's route would hand daniel-box every write endpoint the *arr
    API has behind no credential but the shared X-Api-Key.
    """
    entry = next(c for c in _k8s_entries() if c["name"] == app)
    rendered = _render(
        K8S / app / "templates" / "ingressroute.yaml.j2",
        container_item=entry,
        domain="example.com",
        **_role_defaults(app),
    )
    assert "ClientIP" not in rendered
